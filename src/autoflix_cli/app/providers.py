import re
import time
import unicodedata
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional, TypeVar

from curl_cffi import requests

from ..anilist import anilist_client
from ..scraping import anime_sama, coflix, french_stream, player as player_scraper
from ..scraping.coflix import CoflixMovie, CoflixSeries
from ..scraping.french_stream import FrenchStreamMovie, FrenchStreamSeason
from ..scraping.goldenanime import goldenanime
from ..scraping.goldenms import goldenms_extractor
from . import diagnostics
from .encoding import decode_payload, encode_payload, make_content_key
from .models import ContentDetails, ContentSummary, Episode, PlayableSource, ProviderError, Season
from .store import DesktopStore
from .subtitles import proxied_external_subtitles

T = TypeVar("T")

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "anime_sama": {
        "id": "anime_sama",
        "name": "Anime-Sama",
        "label": "Anime-Sama",
        "types": ["anime"],
        "languages": ["fr"],
    },
    "goldenanime": {
        "id": "goldenanime",
        "name": "GoldenAnime",
        "label": "GoldenAnime VO",
        "types": ["anime"],
        "languages": ["vo", "en", "ja"],
    },
    "goldenms": {
        "id": "goldenms",
        "name": "GoldenMS",
        "label": "GoldenMS",
        "types": ["movie", "series"],
        "languages": ["multi"],
    },
    "coflix": {
        "id": "coflix",
        "name": "Coflix",
        "label": "Coflix",
        "types": ["movie", "series"],
        "languages": ["fr"],
    },
    "french_stream": {
        "id": "french_stream",
        "name": "French-Stream",
        "label": "French-Stream",
        "types": ["movie", "series"],
        "languages": ["fr"],
    },
}


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


def _provider_name(provider_id: str) -> str:
    return PROVIDERS[provider_id]["name"]


def _infer_coflix_type(url: str) -> str:
    return "movie" if "/film/" in url else "series"


def _infer_french_stream_type(url: str) -> str:
    if "/films/" in url or "-film-" in url:
        return "movie"
    return "series"


def _episode_number(title: str, default: Optional[int] = None) -> Optional[int]:
    match = re.search(r"(\d+)", title or "")
    if not match:
        return default
    return int(match.group(1))


def _player_domain(url: str) -> str:
    try:
        return url.split("/")[2].split(".")[-2]
    except Exception:
        return "stream"


_QUALITY_RANK = {
    "2160p": 4, "4k": 4, "uhd": 4,
    "1440p": 3, "2k": 3, "qhd": 3,
    "1080p": 2, "fhd": 2,
    "720p": 1, "hd": 1,
}

_SUBTITLE_LANG_ALIASES = {
    "ar": "ar",
    "ara": "ar",
    "arabe": "ar",
    "arabic": "ar",
    "arabiar": "ar",
    "arab": "ar",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "allemand": "de",
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "spanish": "es",
    "spanies": "es",
    "espanol": "es",
    "espagnol": "es",
    "fr": "fr",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "francais": "fr",
    "vf": "fr",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "italiano": "it",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "japan": "ja",
    "japonais": "ja",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "korea": "ko",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "portugues": "pt",
    "portu": "pt",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "russian federation": "ru",
    "tr": "tr",
    "tur": "tr",
    "turkish": "tr",
    "turkce": "tr",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
    "zh-tw": "zh-TW",
    "zhtw": "zh-TW",
    "zh-hant": "zh-TW",
    "traditional chinese": "zh-TW",
    "chinese traditional": "zh-TW",
    "taiwan": "zh-TW",
    "taiwanese": "zh-TW",
    "zh-cn": "zh-CN",
    "zhcn": "zh-CN",
    "zh-hans": "zh-CN",
    "simplified chinese": "zh-CN",
    "chinese simplified": "zh-CN",
}


def _quality_rank(quality: str) -> int:
    if not quality:
        return 0
    q = str(quality).strip().lower()
    return _QUALITY_RANK.get(q, 0)


def _normalize_subtitle_lang(value: Any) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    text = raw.lower().replace("_", "-")
    if not text:
        return ""

    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))

    for candidate in (text, folded, folded.replace(" ", "-"), folded.replace("-", "")):
        if candidate in _SUBTITLE_LANG_ALIASES:
            return _SUBTITLE_LANG_ALIASES[candidate]

    if text.startswith("zh-tw") or "traditional" in folded or "taiwan" in folded:
        return "zh-TW"
    if text.startswith("zh-cn") or "simplified" in folded:
        return "zh-CN"

    prefix_map = (
        ("arabi", "ar"),
        ("japan", "ja"),
        ("portu", "pt"),
        ("spani", "es"),
        ("engli", "en"),
        ("frenc", "fr"),
        ("franc", "fr"),
        ("germa", "de"),
        ("ital", "it"),
        ("russi", "ru"),
        ("korea", "ko"),
        ("turk", "tr"),
        ("chine", "zh"),
    )
    for prefix, code in prefix_map:
        if folded.startswith(prefix):
            return code

    match = re.match(r"^([a-z]{2})(?:-[a-z]{2,4})?$", text)
    if match:
        code = match.group(1)
        return code if code != "zh" else text

    return raw[:24]


def _normalize_subtitle_tracks(subtitles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    for sub in subtitles or []:
        if not isinstance(sub, dict):
            continue
        item = dict(sub)
        lang = ""
        for key in ("lang", "lang_code", "language", "srclang", "locale", "label", "name"):
            lang = _normalize_subtitle_lang(item.get(key))
            if lang:
                break
        if lang:
            item["lang"] = lang
        if not item.get("label"):
            for key in ("name", "title", "language"):
                if item.get(key):
                    item["label"] = str(item.get(key))
                    break
        tracks.append(item)
    return tracks


def _subtitle_languages(subtitles: List[Dict[str, Any]]) -> List[str]:
    languages: List[str] = []
    for sub in subtitles or []:
        if not isinstance(sub, dict):
            continue
        for key in ("lang", "lang_code", "language", "label", "name"):
            value = sub.get(key)
            normalized = _normalize_subtitle_lang(value)
            if normalized and normalized not in languages:
                languages.append(normalized)
                break
    return languages


def _source_score(
    has_subtitles: bool,
    has_french_subs: bool,
    quality: str,
    is_direct: bool,
) -> int:
    score = 0
    if has_french_subs:
        score += 60
    elif has_subtitles:
        score += 30
    score += _quality_rank(quality) * 10
    if is_direct:
        score += 5
    return score


class ProviderService:
    def __init__(self, store: Optional[DesktopStore] = None):
        self.store = store or DesktopStore()
        self.last_search_errors: List[str] = []

    def list_providers(self) -> List[Dict[str, Any]]:
        return list(PROVIDERS.values())

    def search(
        self,
        query: str,
        provider_id: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> List[ContentSummary]:
        query = (query or "").strip()
        if not query:
            raise ProviderError("empty_query", "La recherche est vide.")

        provider_ids = [provider_id] if provider_id else list(PROVIDERS.keys())
        results: List[ContentSummary] = []
        errors: List[str] = []
        self.last_search_errors = []

        for pid in provider_ids:
            if pid not in PROVIDERS:
                raise ProviderError("unknown_provider", f"Provider inconnu: {pid}", 404)
            if content_type and content_type not in PROVIDERS[pid]["types"]:
                continue
            try:
                if pid == "anime_sama":
                    found = self._provider_call(
                        pid,
                        "search",
                        lambda: self._search_anime_sama(query),
                        query=query,
                        type=content_type or "all",
                    )
                elif pid == "goldenanime":
                    found = self._provider_call(
                        pid,
                        "search",
                        lambda: self._search_goldenanime(query),
                        query=query,
                        type=content_type or "all",
                    )
                elif pid == "goldenms":
                    found = self._provider_call(
                        pid,
                        "search",
                        lambda: self._search_goldenms(query, content_type),
                        query=query,
                        type=content_type or "all",
                    )
                elif pid == "coflix":
                    found = self._provider_call(
                        pid,
                        "search",
                        lambda: self._search_coflix(query, content_type),
                        query=query,
                        type=content_type or "all",
                    )
                elif pid == "french_stream":
                    found = self._provider_call(
                        pid,
                        "search",
                        lambda: self._search_french_stream(query, content_type),
                        query=query,
                        type=content_type or "all",
                    )
                else:
                    found = []
                results.extend(found)
            except Exception as exc:
                errors.append(f"{PROVIDERS[pid]['name']}: {exc}")
                self.last_search_errors.append(PROVIDERS[pid]["name"])

        if not results and errors:
            raise ProviderError(
                "providers_unavailable",
                "Aucun provider n'a répondu correctement. " + " | ".join(errors[:3]),
                502,
            )

        return results

    def get_details(self, provider_id: str, content_id: str) -> ContentDetails:
        self._ensure_provider(provider_id)
        if provider_id == "anime_sama":
            details = self._provider_call(provider_id, "details", lambda: self._details_anime_sama(content_id))
        elif provider_id == "goldenanime":
            details = self._provider_call(provider_id, "details", lambda: self._details_goldenanime(content_id))
        elif provider_id == "goldenms":
            details = self._provider_call(provider_id, "details", lambda: self._details_goldenms(content_id))
        elif provider_id == "coflix":
            details = self._provider_call(provider_id, "details", lambda: self._details_coflix(content_id))
        elif provider_id == "french_stream":
            details = self._provider_call(provider_id, "details", lambda: self._details_french_stream(content_id))
        else:
            raise ProviderError("unknown_provider", f"Provider inconnu: {provider_id}", 404)

        details.favorite = self.store.is_favorite(provider_id, content_id)
        details.progress = self.store.get_content_progress(provider_id, content_id)
        if not details.external_ids.get("anilist_id"):
            mapped_id = self.store.get_anilist_mapping(
                details.provider_name,
                details.title,
                media_kind="video",
                provider_id=provider_id,
                content_id=content_id,
                content_type=details.content_type,
            )
            if mapped_id:
                details.external_ids["anilist_id"] = mapped_id
        return details

    def get_season(self, provider_id: str, content_id: str, season_id: str) -> Season:
        self._ensure_provider(provider_id)
        if provider_id == "anime_sama":
            return self._provider_call(provider_id, "season", lambda: self._season_anime_sama(content_id, season_id))
        if provider_id == "coflix":
            return self._provider_call(provider_id, "season", lambda: self._season_coflix(content_id, season_id))
        if provider_id == "goldenms":
            return self._provider_call(provider_id, "season", lambda: self._season_goldenms(content_id, season_id))
        if provider_id == "french_stream":
            return self._provider_call(provider_id, "season", lambda: self._season_french_stream(content_id, season_id))
        if provider_id == "goldenanime":
            details = self._provider_call(provider_id, "details", lambda: self._details_goldenanime(content_id))
            for season in details.seasons:
                if season.id == season_id:
                    return season
        raise ProviderError("season_not_found", "Saison introuvable.", 404)

    def get_sources(
        self, provider_id: str, content_id: str, episode_id: str
    ) -> List[PlayableSource]:
        self._ensure_provider(provider_id)
        payload = decode_payload(episode_id)
        if provider_id == "goldenanime":
            return self._provider_call(
                provider_id,
                "sources",
                lambda: self._sources_goldenanime(content_id, episode_id, payload),
                title=payload.get("series_title", ""),
                episode=payload.get("episode_title", ""),
            )
        if provider_id == "goldenms":
            return self._provider_call(
                provider_id,
                "sources",
                lambda: self._sources_goldenms(content_id, episode_id, payload),
                title=payload.get("series_title", ""),
                episode=payload.get("episode_title", ""),
            )
        return self._provider_call(
            provider_id,
            "sources",
            lambda: self._sources_from_players(content_id, episode_id, payload),
            title=payload.get("series_title", ""),
            episode=payload.get("episode_title", ""),
        )

    def _ensure_provider(self, provider_id: str) -> None:
        if provider_id not in PROVIDERS:
            raise ProviderError("unknown_provider", f"Provider inconnu: {provider_id}", 404)

    def _provider_call(
        self,
        provider_id: str,
        action: str,
        callback: Callable[[], T],
        **fields: Any,
    ) -> T:
        session = diagnostics.current()
        provider_name = _provider_name(provider_id)
        if not session:
            return callback()

        started = time.perf_counter()
        session.log("INFO", "PROVIDER", action, status="start", provider=provider_name, **fields)
        try:
            result = callback()
        except Exception as exc:
            session.log_exception(
                "PROVIDER",
                action,
                exc,
                duration_ms=(time.perf_counter() - started) * 1000,
                provider=provider_name,
                **fields,
            )
            raise

        extra: Dict[str, Any] = {}
        if isinstance(result, list):
            extra["result_count"] = len(result)
        elif isinstance(result, ContentDetails):
            extra.update(
                {
                    "title": result.title,
                    "type": result.content_type,
                    "season_count": len(result.seasons),
                }
            )
        elif isinstance(result, Season):
            extra.update(
                {
                    "title": result.title,
                    "episode_count": len(result.episodes),
                    "language": result.language or ",".join(result.languages),
                }
            )

        session.log(
            "INFO",
            "PROVIDER",
            action,
            status="success",
            duration_ms=(time.perf_counter() - started) * 1000,
            provider=provider_name,
            **fields,
            **extra,
        )
        return result

    def _search_anime_sama(self, query: str) -> List[ContentSummary]:
        anime_sama.get_website_url()
        summaries = []
        for result in anime_sama.search(query):
            content_id = encode_payload(
                {
                    "url": result.url,
                    "title": result.title,
                    "image": result.img,
                    "genres": result.genres,
                    "type": "anime",
                }
            )
            summaries.append(
                ContentSummary(
                    provider_id="anime_sama",
                    provider_name=_provider_name("anime_sama"),
                    content_id=content_id,
                    title=result.title,
                    content_type="anime",
                    image=result.img,
                    genres=_as_list(result.genres),
                    languages=["fr"],
                )
            )
        return summaries

    def _details_anime_sama(self, content_id: str) -> ContentDetails:
        anime_sama.get_website_url()
        payload = decode_payload(content_id)
        series = anime_sama.get_series(payload["url"])
        seasons = [
            Season(
                id=encode_payload(
                    {
                        "provider_id": "anime_sama",
                        "url": season.url,
                        "title": season.title,
                        "series_title": series.title,
                        "series_url": series.url,
                        "logo_url": series.img,
                    }
                ),
                title=season.title,
            )
            for season in series.seasons
        ]
        return ContentDetails(
            provider_id="anime_sama",
            provider_name=_provider_name("anime_sama"),
            content_id=content_id,
            title=series.title,
            content_type="anime",
            image=series.img,
            genres=_as_list(series.genres),
            seasons=seasons,
        )

    def _season_anime_sama(self, content_id: str, season_id: str) -> Season:
        season_payload = decode_payload(season_id)
        season = anime_sama.get_season(season_payload["url"])
        episodes: List[Episode] = []
        for language, language_episodes in season.episodes.items():
            for index, episode in enumerate(language_episodes):
                episode_id = self._episode_token(
                    provider_id="anime_sama",
                    content_id=content_id,
                    episode_title=episode.title,
                    season_title=season.title,
                    series_title=season_payload["series_title"],
                    series_url=season_payload["series_url"],
                    season_url=season_payload["url"],
                    logo_url=season_payload.get("logo_url"),
                    episode_url="",
                    language=language,
                    episode_number=_episode_number(episode.title, index + 1),
                    players=self._serialize_players(episode.players),
                )
                episodes.append(
                    Episode(
                        id=episode_id,
                        title=episode.title,
                        number=_episode_number(episode.title, index + 1),
                        language=language,
                    )
                )
        return Season(
            id=season_id,
            title=season.title,
            languages=list(season.episodes.keys()),
            episodes=episodes,
        )

    def _search_goldenanime(self, query: str) -> List[ContentSummary]:
        if query.isdigit():
            media = anilist_client.get_media_with_relations(int(query))
            results = [media] if media else []
        else:
            results = anilist_client.search_media(query)

        summaries = []
        for media in results:
            if not media:
                continue
            title = (
                media.get("title", {}).get("english")
                or media.get("title", {}).get("romaji")
                or query
            )
            episodes = media.get("episodes")
            content_id = encode_payload(
                {
                    "anilist_id": media.get("id"),
                    "title": title,
                    "episodes": episodes,
                    "image": media.get("coverImage", {}).get("large")
                    or media.get("coverImage", {}).get("medium")
                    or "",
                    "year": media.get("seasonYear"),
                    "format": media.get("format"),
                }
            )
            subtitle = []
            if media.get("format"):
                subtitle.append(media["format"])
            if episodes:
                subtitle.append(f"{episodes} episodes")
            summaries.append(
                ContentSummary(
                    provider_id="goldenanime",
                    provider_name=_provider_name("goldenanime"),
                    content_id=content_id,
                    title=title,
                    content_type="anime",
                    image=media.get("coverImage", {}).get("medium", ""),
                    subtitle=" · ".join(subtitle),
                    languages=["vo"],
                    year=str(media.get("seasonYear") or "") or None,
                )
            )
        return summaries

    def _details_goldenanime(self, content_id: str) -> ContentDetails:
        payload = decode_payload(content_id)
        episode_count = payload.get("episodes") or 24
        episode_count = max(1, min(int(episode_count), 250))
        episodes = []
        for number in range(1, episode_count + 1):
            episode_id = self._episode_token(
                provider_id="goldenanime",
                content_id=content_id,
                episode_title=f"Episode {number}",
                season_title="VO",
                series_title=payload.get("title") or f"AniList {payload.get('anilist_id')}",
                series_url=f"anilist:{payload.get('anilist_id') or ''}",
                season_url="",
                episode_url="",
                logo_url=payload.get("image"),
                language="vo",
                episode_number=number,
                players=[],
                extra={"anilist_id": payload.get("anilist_id")},
            )
            episodes.append(Episode(id=episode_id, title=f"Episode {number}", number=number, language="vo"))

        return ContentDetails(
            provider_id="goldenanime",
            provider_name=_provider_name("goldenanime"),
            content_id=content_id,
            title=payload.get("title", "Anime"),
            content_type="anime",
            image=payload.get("image", ""),
            subtitle="VO et sous-titres",
            year=str(payload.get("year") or "") or None,
            seasons=[Season(id=encode_payload({"provider_id": "goldenanime", "title": "VO"}), title="VO", language="vo", episodes=episodes)],
            external_ids={"anilist_id": payload.get("anilist_id")},
        )

    def _search_goldenms(
        self, query: str, content_type: Optional[str]
    ) -> List[ContentSummary]:
        search_types = [content_type] if content_type in ("movie", "series") else ["movie", "series"]
        summaries: List[ContentSummary] = []
        for media_type in search_types:
            metas = self._search_cinemeta(query, media_type == "movie")
            for meta in metas[:8]:
                imdb_id = meta.get("id")
                if not imdb_id:
                    continue
                title = meta.get("name", query)
                content_id = encode_payload(
                    {
                        "imdb_id": imdb_id,
                        "title": title,
                        "type": media_type,
                        "image": meta.get("poster", ""),
                        "year": meta.get("releaseInfo", ""),
                    }
                )
                summaries.append(
                    ContentSummary(
                        provider_id="goldenms",
                        provider_name=_provider_name("goldenms"),
                        content_id=content_id,
                        title=title,
                        content_type=media_type,
                        image=meta.get("poster", ""),
                        subtitle=meta.get("releaseInfo", ""),
                        year=meta.get("releaseInfo", ""),
                        languages=["multi"],
                    )
                )
        return summaries

    def _details_goldenms(self, content_id: str) -> ContentDetails:
        payload = decode_payload(content_id)
        is_movie = payload.get("type") == "movie"
        full_meta = self._get_cinemeta_details(payload["imdb_id"], is_movie)
        title = full_meta.get("name") or payload.get("title", "")
        tmdb_id = full_meta.get("moviedb_id")
        poster = full_meta.get("poster") or payload.get("image", "")
        year = full_meta.get("year") or payload.get("year")
        genres = _as_list(full_meta.get("genres"))

        if is_movie:
            episode_id = self._episode_token(
                provider_id="goldenms",
                content_id=content_id,
                episode_title="Film",
                season_title="Film",
                series_title=title,
                series_url=f"tmdb:{tmdb_id or ''}|imdb:{payload.get('imdb_id') or ''}",
                season_url="",
                episode_url="",
                logo_url=poster,
                language="multi",
                episode_number=1,
                players=[],
                extra={
                    "is_movie": True,
                    "tmdb_id": tmdb_id,
                    "imdb_id": payload.get("imdb_id"),
                    "year": year,
                    "title": title,
                },
            )
            seasons = [Season(id=encode_payload({"provider_id": "goldenms", "movie": True}), title="Film", episodes=[Episode(id=episode_id, title="Film", number=1)])]
        else:
            seasons = self._goldenms_seasons_from_meta(content_id, payload, full_meta, title, poster, tmdb_id, year)

        return ContentDetails(
            provider_id="goldenms",
            provider_name=_provider_name("goldenms"),
            content_id=content_id,
            title=title,
            content_type="movie" if is_movie else "series",
            image=poster,
            genres=genres,
            year=str(year or "") or None,
            seasons=seasons,
            external_ids={"tmdb_id": tmdb_id, "imdb_id": payload.get("imdb_id")},
        )

    def _season_goldenms(self, content_id: str, season_id: str) -> Season:
        payload = decode_payload(content_id)
        details = self._details_goldenms(content_id)
        for season in details.seasons:
            if season.id == season_id:
                return season
        season_payload = decode_payload(season_id)
        if "season" in season_payload:
            full_meta = self._get_cinemeta_details(payload["imdb_id"], False)
            title = full_meta.get("name") or payload.get("title", "")
            poster = full_meta.get("poster") or payload.get("image", "")
            tmdb_id = full_meta.get("moviedb_id")
            year = full_meta.get("year") or payload.get("year")
            seasons = self._goldenms_seasons_from_meta(content_id, payload, full_meta, title, poster, tmdb_id, year)
            for season in seasons:
                if season.id == season_id:
                    return season
        raise ProviderError("season_not_found", "Saison introuvable.", 404)

    def _search_coflix(
        self, query: str, content_type: Optional[str]
    ) -> List[ContentSummary]:
        coflix.get_website_url()
        summaries = []
        for result in coflix.search(query):
            result_type = _infer_coflix_type(result.url)
            if content_type and content_type != result_type:
                continue
            content_id = encode_payload(
                {
                    "url": result.url,
                    "title": result.title,
                    "image": result.img,
                    "genres": result.genres,
                    "type": result_type,
                }
            )
            summaries.append(
                ContentSummary(
                    provider_id="coflix",
                    provider_name=_provider_name("coflix"),
                    content_id=content_id,
                    title=result.title,
                    content_type=result_type,
                    image=result.img,
                    genres=_as_list(result.genres),
                    languages=["fr"],
                )
            )
        return summaries

    def _details_coflix(self, content_id: str) -> ContentDetails:
        coflix.get_website_url()
        payload = decode_payload(content_id)
        content = coflix.get_content(payload["url"])
        if isinstance(content, CoflixMovie):
            episode_id = self._episode_token(
                provider_id="coflix",
                content_id=content_id,
                episode_title="Film",
                season_title="Film",
                series_title=content.title,
                series_url=content.url,
                season_url=content.url,
                episode_url=content.url,
                logo_url=content.img,
                language="fr",
                episode_number=1,
                players=self._serialize_players(content.players),
            )
            seasons = [Season(id=encode_payload({"provider_id": "coflix", "movie": True}), title="Film", episodes=[Episode(id=episode_id, title="Film", number=1, language="fr")])]
            return ContentDetails(
                provider_id="coflix",
                provider_name=_provider_name("coflix"),
                content_id=content_id,
                title=content.title,
                content_type="movie",
                image=content.img,
                genres=_as_list(content.genres),
                year=str(content.year or "") or None,
                seasons=seasons,
            )

        if isinstance(content, CoflixSeries):
            seasons = [
                Season(
                    id=encode_payload(
                        {
                            "provider_id": "coflix",
                            "url": season.url,
                            "title": season.title,
                            "series_title": content.title,
                            "series_url": content.url,
                            "logo_url": content.img,
                        }
                    ),
                    title=season.title,
                )
                for season in content.seasons
            ]
            return ContentDetails(
                provider_id="coflix",
                provider_name=_provider_name("coflix"),
                content_id=content_id,
                title=content.title,
                content_type="series",
                image=content.img,
                genres=_as_list(content.genres),
                seasons=seasons,
            )

        raise ProviderError("unsupported_content", "Contenu Coflix non supporté.", 422)

    def _season_coflix(self, content_id: str, season_id: str) -> Season:
        season_payload = decode_payload(season_id)
        if season_payload.get("movie"):
            return self._details_coflix(content_id).seasons[0]

        season = coflix.get_season(season_payload["url"])
        episodes = []
        for index, episode in enumerate(season.episodes):
            episode_id = self._episode_token(
                provider_id="coflix",
                content_id=content_id,
                episode_title=episode.title,
                season_title=season.title,
                series_title=season_payload["series_title"],
                series_url=season_payload["series_url"],
                season_url=season_payload["url"],
                episode_url=episode.url,
                logo_url=season_payload.get("logo_url"),
                language="fr",
                episode_number=_episode_number(episode.title, index + 1),
                players=[],
            )
            episodes.append(
                Episode(
                    id=episode_id,
                    title=episode.title,
                    number=_episode_number(episode.title, index + 1),
                    language="fr",
                )
            )
        return Season(id=season_id, title=season.title, episodes=episodes, language="fr")

    def _search_french_stream(
        self, query: str, content_type: Optional[str]
    ) -> List[ContentSummary]:
        summaries = []
        for result in french_stream.search(query):
            result_type = _infer_french_stream_type(result.url)
            if content_type and content_type != result_type:
                continue
            content_id = encode_payload(
                {
                    "url": result.url,
                    "title": result.title,
                    "image": result.img,
                    "genres": result.genres,
                    "type": result_type,
                }
            )
            summaries.append(
                ContentSummary(
                    provider_id="french_stream",
                    provider_name=_provider_name("french_stream"),
                    content_id=content_id,
                    title=result.title,
                    content_type=result_type,
                    image=result.img,
                    genres=_as_list(result.genres),
                    languages=["fr"],
                )
            )
        return summaries

    def _details_french_stream(self, content_id: str) -> ContentDetails:
        payload = decode_payload(content_id)
        content = french_stream.get_content(payload["url"])
        if isinstance(content, FrenchStreamMovie):
            episode_id = self._episode_token(
                provider_id="french_stream",
                content_id=content_id,
                episode_title="Film",
                season_title="Film",
                series_title=content.title,
                series_url=content.url,
                season_url=content.url,
                episode_url=content.url,
                logo_url=content.img,
                language="fr",
                episode_number=1,
                players=self._serialize_players(content.players),
            )
            seasons = [Season(id=encode_payload({"provider_id": "french_stream", "movie": True}), title="Film", episodes=[Episode(id=episode_id, title="Film", number=1, language="fr")])]
            return ContentDetails(
                provider_id="french_stream",
                provider_name=_provider_name("french_stream"),
                content_id=content_id,
                title=content.title,
                content_type="movie",
                image=content.img,
                genres=_as_list(content.genres),
                seasons=seasons,
            )

        if isinstance(content, FrenchStreamSeason):
            seasons = []
            for language, episodes_raw in content.episodes.items():
                episodes = []
                for index, episode in enumerate(episodes_raw):
                    episode_id = self._episode_token(
                        provider_id="french_stream",
                        content_id=content_id,
                        episode_title=episode.title,
                        season_title=content.title,
                        series_title=content.title,
                        series_url=content.url,
                        season_url=content.url,
                        episode_url="",
                        logo_url=payload.get("image"),
                        language=language,
                        episode_number=_episode_number(episode.title, index + 1),
                        players=self._serialize_players(episode.players),
                    )
                    episodes.append(
                        Episode(
                            id=episode_id,
                            title=episode.title,
                            number=_episode_number(episode.title, index + 1),
                            language=language,
                        )
                    )
                seasons.append(
                    Season(
                        id=encode_payload(
                            {
                                "provider_id": "french_stream",
                                "url": content.url,
                                "title": content.title,
                                "language": language,
                            }
                        ),
                        title=f"{content.title} - {language.upper()}",
                        language=language,
                        episodes=episodes,
                    )
                )
            return ContentDetails(
                provider_id="french_stream",
                provider_name=_provider_name("french_stream"),
                content_id=content_id,
                title=content.title,
                content_type="series",
                image=payload.get("image", ""),
                seasons=seasons,
            )

        raise ProviderError("unsupported_content", "Contenu French-Stream non supporté.", 422)

    def _season_french_stream(self, content_id: str, season_id: str) -> Season:
        details = self._details_french_stream(content_id)
        for season in details.seasons:
            if season.id == season_id:
                return season
        raise ProviderError("season_not_found", "Saison introuvable.", 404)

    def _sources_from_players(
        self, content_id: str, episode_id: str, episode_payload: Dict[str, Any]
    ) -> List[PlayableSource]:
        players = episode_payload.get("players") or []

        if not players and episode_payload.get("provider_id") == "coflix" and episode_payload.get("episode_url"):
            details = coflix.get_episode(episode_payload["episode_url"])
            players = self._serialize_players(details.players)
            episode_payload["episode_title"] = details.title or episode_payload.get("episode_title")

        supported = [item for item in players if player_scraper.is_supported(item.get("url", ""))]
        sources = []
        for index, item in enumerate(supported):
            source_name = item.get("name") or f"Lecteur {index + 1}"
            diagnostics.info(
                "PROVIDER",
                "source",
                provider=episode_payload.get("provider"),
                name=source_name,
                quality="",
                type="player",
                subtitles=0,
                direct=False,
                domain=diagnostics.domain_of(item.get("url", "")),
            )
            source_id = self._source_token(
                episode_payload=episode_payload,
                content_id=content_id,
                episode_id=episode_id,
                source_name=source_name,
                url=item["url"],
                headers=self._default_headers(episode_payload.get("provider_id")),
                is_direct=False,
                source_type="player",
                quality="",
            )
            sources.append(
                PlayableSource(
                    id=source_id,
                    name=f"{item.get('name') or 'Lecteur'} - {_player_domain(item['url'])}",
                    source_type="player",
                    quality="",
                    language=episode_payload.get("language"),
                    provider_name=episode_payload.get("provider"),
                )
            )

        if not sources:
            raise ProviderError(
                "no_supported_player",
                "Aucun lecteur supporté n'a été trouvé pour cet épisode.",
                404,
            )
        return sources

    def _sources_goldenanime(
        self, content_id: str, episode_id: str, episode_payload: Dict[str, Any]
    ) -> List[PlayableSource]:
        title = episode_payload.get("series_title")
        anilist_id = episode_payload.get("anilist_id")
        episode_number = episode_payload.get("episode_number") or 1
        raw_sources = goldenanime.extract_vo(title=title, anilist_id=anilist_id, episode=episode_number)
        valid = [item for item in raw_sources if self._is_valid_stream(item)]
        return self._sources_from_stream_dicts(content_id, episode_id, episode_payload, valid)

    def _sources_goldenms(
        self, content_id: str, episode_id: str, episode_payload: Dict[str, Any]
    ) -> List[PlayableSource]:
        raw_sources = goldenms_extractor.extract(
            title=episode_payload.get("title") or episode_payload.get("series_title"),
            tmdb_id=episode_payload.get("tmdb_id"),
            imdb_id=episode_payload.get("imdb_id"),
            year=episode_payload.get("year") if episode_payload.get("is_movie") else None,
            season=episode_payload.get("season_number"),
            episode=episode_payload.get("episode_number") if not episode_payload.get("is_movie") else None,
        )
        valid = [item for item in raw_sources if self._is_valid_stream(item)]
        valid = self._strip_subtitles_from_movie_series_sources(valid)
        valid.extend(self._goldenms_videasy_hls_sources(episode_payload))
        return self._sources_from_stream_dicts(content_id, episode_id, episode_payload, valid)

    def _goldenms_videasy_hls_sources(
        self,
        episode_payload: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        tmdb_id = str(episode_payload.get("tmdb_id") or "").strip()
        if not tmdb_id:
            return []

        is_movie = bool(episode_payload.get("is_movie"))
        season = None if is_movie else episode_payload.get("season_number")
        episode = None if is_movie else episode_payload.get("episode_number")
        title = episode_payload.get("title") or episode_payload.get("series_title")
        if not title:
            return []

        try:
            sources = goldenms_extractor.search_videasy(
                title,
                tmdb_id=tmdb_id,
                imdb_id=episode_payload.get("imdb_id"),
                year=episode_payload.get("year") if is_movie else None,
                season=season,
                episode=episode,
            )
        except Exception:
            return []

        results = []
        for source in sources:
            item = dict(source)
            source_name = str(item.get("source") or "")
            source_url = str(item.get("url") or "")
            if "1movies" in source_name.lower() or "floral.tylerfisher55.workers.dev" in source_url.lower():
                continue
            raw_subtitles = _normalize_subtitle_tracks(item.get("subtitles") or [])
            item["subtitles"] = proxied_external_subtitles(
                raw_subtitles,
                headers=item.get("headers") or goldenms_extractor._videasy_headers(),
                context="videasy",
            )
            if item["subtitles"]:
                item["subtitle_source"] = "videasy_api"
            else:
                item.pop("subtitle_source", None)
            # Keep Vidlink as the default HLS choice while exposing Videasy as
            # an additional local-player source.
            item.setdefault("score", 4)
            results.append(item)
        return results

    def _strip_subtitles_from_movie_series_sources(
        self,
        raw_sources: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        sources = []
        for item in raw_sources:
            source = dict(item)
            source["subtitles"] = []
            source.pop("subtitle_source", None)
            sources.append(source)
        return sources

    def _is_vidlink_source(self, item: Dict[str, Any]) -> bool:
        return (item.get("source") or "").strip().lower().startswith("vidlink")

    def _sources_from_stream_dicts(
        self,
        content_id: str,
        episode_id: str,
        episode_payload: Dict[str, Any],
        raw_sources: Iterable[Dict[str, Any]],
    ) -> List[PlayableSource]:
        sources = []
        for index, item in enumerate(raw_sources):
            url = item.get("url")
            if not url:
                continue
            source_type = item.get("type", "VIDEO")
            is_direct = self._is_direct_stream(item)
            headers = item.get("headers") or self._stream_headers(item, episode_payload)
            source_name = item.get("source") or f"Source {index + 1}"
            quality = item.get("quality", "")
            subtitles = item.get("subtitles") or []
            stream_context = item.get("stream_context") or item.get("context") or ""
            sub_languages = _subtitle_languages(subtitles)
            has_subs = len(subtitles) > 0
            has_french_subs = "fr" in sub_languages
            subtitle_source = item.get("subtitle_source") or (
                "provider" if has_subs else None
            )
            try:
                score = int(item["score"]) if item.get("score") is not None else _source_score(
                    has_subs,
                    has_french_subs,
                    quality,
                    is_direct,
                )
            except (TypeError, ValueError):
                score = _source_score(has_subs, has_french_subs, quality, is_direct)
            diagnostics.info(
                "PROVIDER",
                "source",
                provider=episode_payload.get("provider"),
                name=source_name,
                quality=quality,
                type=source_type,
                subtitles=len(subtitles),
                subtitle_languages=sub_languages,
                direct=is_direct,
                score=score,
                domain=diagnostics.domain_of(url),
            )
            source_id = self._source_token(
                episode_payload=episode_payload,
                content_id=content_id,
                episode_id=episode_id,
                source_name=source_name,
                url=url,
                headers=headers,
                is_direct=is_direct,
                source_type=source_type,
                quality=quality,
                subtitles=subtitles,
                stream_context=stream_context,
            )
            sources.append(
                PlayableSource(
                    id=source_id,
                    name=source_name,
                    source_type=source_type,
                    quality=quality,
                    language=episode_payload.get("language"),
                    subtitles=subtitles,
                    has_subtitles=has_subs,
                    subtitle_languages=sub_languages,
                    subtitle_source=subtitle_source,
                    score=score,
                    provider_name=episode_payload.get("provider"),
                )
            )
        if not sources:
            raise ProviderError("no_stream", "Aucun stream compatible n'a été trouvé.", 404)
        sources.sort(key=lambda s: s.score, reverse=True)
        return sources

    def _episode_token(
        self,
        provider_id: str,
        content_id: str,
        episode_title: str,
        season_title: str,
        series_title: str,
        series_url: str,
        season_url: str,
        episode_url: str,
        logo_url: Optional[str],
        language: Optional[str],
        episode_number: Optional[int],
        players: List[Dict[str, str]],
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            "provider_id": provider_id,
            "provider": _provider_name(provider_id),
            "content_id": content_id,
            "episode_title": episode_title,
            "season_title": season_title,
            "series_title": series_title,
            "series_url": series_url,
            "season_url": season_url,
            "episode_url": episode_url,
            "logo_url": logo_url,
            "language": language,
            "episode_number": episode_number,
            "players": players,
        }
        if extra:
            payload.update(extra)
        payload["history_key"] = (
            f"{make_content_key(provider_id, content_id)}:"
            f"{season_title}:{language or ''}:{episode_title}"
        )
        return encode_payload(payload)

    def _source_token(
        self,
        episode_payload: Dict[str, Any],
        content_id: str,
        episode_id: str,
        source_name: str,
        url: str,
        headers: Dict[str, str],
        is_direct: bool,
        source_type: str,
        quality: str,
        subtitles: Optional[List[Dict[str, Any]]] = None,
        stream_context: str = "",
    ) -> str:
        progress = {
            "provider_id": episode_payload["provider_id"],
            "provider": episode_payload["provider"],
            "content_id": content_id,
            "episode_id": episode_id,
            "series_title": episode_payload.get("series_title", ""),
            "season_title": episode_payload.get("season_title", ""),
            "episode_title": episode_payload.get("episode_title", ""),
            "series_url": episode_payload.get("series_url", ""),
            "season_url": episode_payload.get("season_url", ""),
            "episode_url": episode_payload.get("episode_url", ""),
            "logo_url": episode_payload.get("logo_url"),
            "language": episode_payload.get("language"),
            "episode_number": episode_payload.get("episode_number"),
            "history_key": episode_payload.get("history_key"),
            "anilist_id": episode_payload.get("anilist_id"),
        }
        saved_progress = self.store.get_progress_by_history_key(
            episode_payload.get("history_key")
        )
        if saved_progress:
            for key in ("position", "duration", "percent", "completed"):
                if key in saved_progress:
                    progress[key] = saved_progress[key]
        return encode_payload(
            {
                "source_name": source_name,
                "url": url,
                "headers": headers or {},
                "is_direct": bool(is_direct),
                "source_type": source_type or "VIDEO",
                "quality": quality or "",
                "subtitles": subtitles or [],
                "stream_context": stream_context or "",
                "progress": progress,
            }
        )

    def _serialize_players(self, players: Iterable[Any]) -> List[Dict[str, str]]:
        return [
            {"name": getattr(player, "name", "Lecteur"), "url": getattr(player, "url", "")}
            for player in players
            if getattr(player, "url", "")
        ]

    def _default_headers(self, provider_id: Optional[str]) -> Dict[str, str]:
        if provider_id == "anime_sama":
            return {"Referer": anime_sama.website_origin}
        if provider_id == "coflix":
            return {"Referer": "https://lecteurvideo.com/"}
        if provider_id == "french_stream":
            return {"Referer": french_stream.website_origin}
        return {}

    def _stream_headers(
        self, item: Dict[str, Any], episode_payload: Dict[str, Any]
    ) -> Dict[str, str]:
        source = (item.get("source") or "").lower()
        if episode_payload.get("provider_id") == "goldenanime":
            if "allanime" in source:
                return {"Referer": goldenanime.allanime_referer + "/"}
            if "animetsu" in source:
                return {
                    "Referer": goldenanime.animetsu_base + "/",
                    "Origin": goldenanime.animetsu_base,
                }
            return {"Referer": goldenanime.sudatchi_base + "/"}
        return {}

    def _is_valid_stream(self, item: Dict[str, Any]) -> bool:
        url = item.get("url", "")
        item_type = (item.get("type") or "").upper()
        return (
            item_type in {"M3U8", "MP4", "VIDEO", "PLAYER/M3U8", "IFRAME"}
            or ".m3u8" in url.lower()
            or "master" in url.lower()
            or player_scraper.is_supported(url)
        )

    def _is_direct_stream(self, item: Dict[str, Any]) -> bool:
        url = item.get("url", "")
        item_type = (item.get("type") or "").upper()
        if item.get("direct") is not None:
            return bool(item.get("direct"))
        if item_type == "IFRAME":
            return True
        if player_scraper.is_supported(url) and ".m3u8" not in url.lower():
            return False
        return (
            item_type in {"M3U8", "MP4", "VIDEO"}
            or ".m3u8" in url.lower()
            or "master" in url.lower()
        )

    def _search_cinemeta(self, title: str, is_movie: bool) -> List[Dict[str, Any]]:
        media_type = "movie" if is_movie else "series"
        url = (
            "https://v3-cinemeta.strem.io/catalog/"
            f"{media_type}/top/search={urllib.parse.quote(title)}.json"
        )
        response = requests.get(url, timeout=10, impersonate="chrome")
        response.raise_for_status()
        return response.json().get("metas", [])

    def _get_cinemeta_details(self, imdb_id: str, is_movie: bool) -> Dict[str, Any]:
        media_type = "movie" if is_movie else "series"
        url = f"https://v3-cinemeta.strem.io/meta/{media_type}/{imdb_id}.json"
        response = requests.get(url, timeout=10, impersonate="chrome")
        response.raise_for_status()
        return response.json().get("meta", {})

    def _goldenms_seasons_from_meta(
        self,
        content_id: str,
        payload: Dict[str, Any],
        full_meta: Dict[str, Any],
        title: str,
        poster: str,
        tmdb_id: Any,
        year: Any,
    ) -> List[Season]:
        season_map: Dict[int, List[Dict[str, Any]]] = {}
        for video in full_meta.get("videos", []) or []:
            season_number = int(video.get("season") or 1)
            season_map.setdefault(season_number, []).append(video)

        if not season_map:
            season_map = {1: [{"season": 1, "episode": 1, "name": "Episode 1"}]}

        seasons = []
        for season_number in sorted(season_map):
            episodes = []
            for video in sorted(season_map[season_number], key=lambda item: int(item.get("episode") or 0)):
                episode_number = int(video.get("episode") or 1)
                episode_title = video.get("name") or f"Episode {episode_number}"
                episode_id = self._episode_token(
                    provider_id="goldenms",
                    content_id=content_id,
                    episode_title=f"Episode {episode_number}",
                    season_title=f"Saison {season_number}",
                    series_title=title,
                    series_url=f"tmdb:{tmdb_id or ''}|imdb:{payload.get('imdb_id') or ''}",
                    season_url="",
                    episode_url="",
                    logo_url=poster,
                    language="multi",
                    episode_number=episode_number,
                    players=[],
                    extra={
                        "is_movie": False,
                        "tmdb_id": tmdb_id,
                        "imdb_id": payload.get("imdb_id"),
                        "year": year,
                        "title": title,
                        "season_number": season_number,
                    },
                )
                episodes.append(
                    Episode(
                        id=episode_id,
                        title=f"E{episode_number:02d} - {episode_title}",
                        number=episode_number,
                        language="multi",
                    )
                )
            seasons.append(
                Season(
                    id=encode_payload({"provider_id": "goldenms", "season": season_number}),
                    title=f"Saison {season_number}",
                    number=season_number,
                    episodes=episodes,
                )
            )
        return seasons
