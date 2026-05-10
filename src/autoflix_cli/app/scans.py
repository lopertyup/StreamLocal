from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, TypeVar

from ..scraping import lelscans as lelscans_scraper
from ..scraping import manga as manga_scraper
from . import diagnostics
from .encoding import decode_payload, encode_payload
from .models import ProviderError
from .store import DesktopStore


T = TypeVar("T")

SUPPORTED_SCAN_LANGUAGES = {"fr", "en"}


@dataclass
class ScanProviderInfo:
    id: str
    name: str
    label: str
    languages: List[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanSummary:
    provider_id: str
    provider_name: str
    content_id: str
    title: str
    content_type: str = "manga"
    media_kind: str = "scan"
    image: str = ""
    subtitle: str = ""
    genres: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    year: Optional[str] = None
    status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanChapter:
    id: str
    title: str
    chapter: str = ""
    volume: str = ""
    language: str = ""
    pages: int = 0
    readable_at: str = ""
    scanlation_groups: List[str] = field(default_factory=list)
    progress: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanPage:
    index: int
    url: str
    filename: str = ""
    quality: str = "data"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScanDetails:
    provider_id: str
    provider_name: str
    content_id: str
    title: str
    content_type: str = "manga"
    media_kind: str = "scan"
    image: str = ""
    description: str = ""
    genres: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    year: Optional[str] = None
    status: str = ""
    chapters: List[ScanChapter] = field(default_factory=list)
    external_ids: Dict[str, Any] = field(default_factory=dict)
    favorite: bool = False
    progress: Optional[Dict[str, Any]] = None

    def summary(self) -> ScanSummary:
        return ScanSummary(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            content_id=self.content_id,
            title=self.title,
            content_type=self.content_type,
            media_kind=self.media_kind,
            image=self.image,
            genres=list(self.genres),
            languages=list(self.languages),
            year=self.year,
            status=self.status,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["chapters"] = [chapter.to_dict() for chapter in self.chapters]
        data["summary"] = self.summary().to_dict()
        return data


class ScanProvider(Protocol):
    info: ScanProviderInfo

    def search(self, query: str, language: str = "fr") -> List[ScanSummary]:
        ...

    def get_details(self, content_id: str, language: str = "fr") -> ScanDetails:
        ...

    def get_chapters(self, content_id: str, language: str = "fr") -> List[ScanChapter]:
        ...

    def get_pages(
        self,
        content_id: str,
        chapter_id: str,
        quality: str = "data",
    ) -> List[ScanPage]:
        ...


class AnimeSamaMangaProvider:
    info = ScanProviderInfo(
        id="anime_sama",
        name="Anime-Sama",
        label="Anime-Sama Manga",
        languages=["fr"],
    )

    def search(self, query: str, language: str = "fr") -> List[ScanSummary]:
        del language
        summaries: List[ScanSummary] = []
        for item in manga_scraper.search_manga(query):
            content_id = encode_payload(
                {
                    "catalogue_url": item.catalogue_url,
                    "scan_url": item.scan_url,
                    "title": item.title,
                    "cover_url": item.cover_url,
                    "slug": item.slug,
                }
            )
            summaries.append(
                ScanSummary(
                    provider_id=self.info.id,
                    provider_name=self.info.name,
                    content_id=content_id,
                    title=item.title,
                    image=item.cover_url,
                    subtitle="Manga",
                    genres=list(item.genres),
                    languages=["fr"],
                    status="Scans",
                )
            )
        return summaries

    def get_details(self, content_id: str, language: str = "fr") -> ScanDetails:
        del language
        payload = decode_payload(content_id)
        info = manga_scraper.get_manga_info(payload.get("scan_url") or payload["catalogue_url"])
        chapter_pages = manga_scraper.fetch_chapters(info.scan_url)
        chapters = [
            ScanChapter(
                id=str(index),
                title=f"Chapitre {index + 1}",
                chapter=str(index + 1),
                language="fr",
                pages=len(pages),
            )
            for index, pages in enumerate(chapter_pages)
        ]
        return ScanDetails(
            provider_id=self.info.id,
            provider_name=self.info.name,
            content_id=content_id,
            title=info.title,
            image=info.cover_url,
            description="",
            genres=list(info.genres),
            languages=["fr"],
            status="Scans",
            chapters=chapters,
        )

    def get_chapters(self, content_id: str, language: str = "fr") -> List[ScanChapter]:
        return self.get_details(content_id, language).chapters

    def get_pages(
        self,
        content_id: str,
        chapter_id: str,
        quality: str = "data",
    ) -> List[ScanPage]:
        del quality
        payload = decode_payload(content_id)
        info = manga_scraper.get_manga_info(payload.get("scan_url") or payload["catalogue_url"])
        chapter_pages = manga_scraper.fetch_chapters(info.scan_url)
        try:
            chapter_index = int(chapter_id)
        except (TypeError, ValueError):
            raise ProviderError("chapter_not_found", "Chapitre manga introuvable.", 404)
        if chapter_index < 0 or chapter_index >= len(chapter_pages):
            raise ProviderError("chapter_not_found", "Chapitre manga introuvable.", 404)
        return [
            ScanPage(index=index, url=url, filename=f"{index + 1}.jpg", quality="image")
            for index, url in enumerate(chapter_pages[chapter_index])
        ]


class LelScansProvider:
    info = ScanProviderInfo(
        id="lelscans",
        name="Lelscans",
        label="Lelscans",
        languages=["fr"],
    )

    def search(self, query: str, language: str = "fr") -> List[ScanSummary]:
        del language
        summaries: List[ScanSummary] = []
        for item in lelscans_scraper.search_manga(query):
            content_id = encode_payload(
                {
                    "detail_url": item.detail_url,
                    "title": item.title,
                    "cover_url": item.cover_url,
                }
            )
            summaries.append(
                ScanSummary(
                    provider_id=self.info.id,
                    provider_name=self.info.name,
                    content_id=content_id,
                    title=item.title,
                    image=item.cover_url,
                    subtitle=item.latest_chapter and f"Dernier chapitre {item.latest_chapter}" or "Scans FR",
                    genres=list(item.genres),
                    languages=["fr"],
                    status=item.status,
                )
            )
        return summaries

    def get_details(self, content_id: str, language: str = "fr") -> ScanDetails:
        del language
        payload = decode_payload(content_id)
        info = lelscans_scraper.get_manga_info(payload["detail_url"])
        chapters = [
            ScanChapter(
                id=encode_payload(
                    {
                        "chapter_url": chapter.url,
                        "chapter": chapter.chapter,
                        "title": chapter.title,
                    }
                ),
                title=chapter.title,
                chapter=chapter.chapter,
                language="fr",
                pages=chapter.pages,
            )
            for chapter in lelscans_scraper.get_chapters(info.detail_url)
        ]
        return ScanDetails(
            provider_id=self.info.id,
            provider_name=self.info.name,
            content_id=content_id,
            title=info.title,
            image=info.cover_url or payload.get("cover_url", ""),
            description="",
            genres=list(info.genres),
            languages=["fr"],
            status=info.status,
            chapters=chapters,
        )

    def get_chapters(self, content_id: str, language: str = "fr") -> List[ScanChapter]:
        return self.get_details(content_id, language).chapters

    def get_pages(
        self,
        content_id: str,
        chapter_id: str,
        quality: str = "data",
    ) -> List[ScanPage]:
        del content_id, quality
        payload = decode_payload(chapter_id)
        pages = lelscans_scraper.get_pages(payload["chapter_url"])
        if not pages:
            raise ProviderError("chapter_pages_unavailable", "Pages du chapitre indisponibles.", 502)
        return [
            ScanPage(index=index, url=url, filename=f"{index + 1}.jpg", quality="image")
            for index, url in enumerate(pages)
        ]


def _clean_language(language: Optional[str]) -> str:
    language = (language or "fr").strip().lower()
    if language not in SUPPORTED_SCAN_LANGUAGES:
        raise ProviderError(
            "unsupported_scan_language",
            "Langue de scan non supportee.",
            422,
        )
    return language


class ScanService:
    """Scan registry used as the internal manga adapter layer."""

    def __init__(
        self,
        store: Optional[DesktopStore] = None,
        providers: Optional[Dict[str, ScanProvider]] = None,
    ):
        self.store = store or DesktopStore()
        self.providers: Dict[str, ScanProvider] = (
            providers
            if providers is not None
            else {
                "anime_sama": AnimeSamaMangaProvider(),
                "lelscans": LelScansProvider(),
            }
        )
        self.last_search_errors: List[str] = []

    def list_providers(self) -> List[Dict[str, Any]]:
        return [provider.info.to_dict() for provider in self.providers.values() if provider.info.enabled]

    def search(
        self,
        query: str,
        provider_id: Optional[str] = None,
        language: str = "fr",
    ) -> List[ScanSummary]:
        query = (query or "").strip()
        if not query:
            raise ProviderError("empty_query", "La recherche est vide.")
        _clean_language(language)
        if provider_id and provider_id != "all":
            provider = self._ensure_provider(provider_id)
            return self._provider_call(
                provider_id,
                "scan_search",
                lambda: provider.search(query, language),
                query=query,
                language=language,
            )
        results: List[ScanSummary] = []
        errors: List[str] = []
        self.last_search_errors = []
        for pid, provider in self.providers.items():
            if not provider.info.enabled:
                continue
            try:
                found = self._provider_call(
                    pid,
                    "scan_search",
                    lambda provider=provider: provider.search(query, language),
                    query=query,
                    language=language,
                )
                results.extend(found)
            except Exception as exc:
                errors.append(f"{provider.info.name}: {exc}")
                self.last_search_errors.append(provider.info.name)
        if not results and errors:
            raise ProviderError(
                "scan_providers_unavailable",
                "Aucun provider manga n'a repondu correctement. " + " | ".join(errors[:3]),
                502,
            )
        return results

    def get_details(self, provider_id: str, content_id: str, language: str = "fr") -> ScanDetails:
        language = _clean_language(language)
        provider = self._ensure_provider(provider_id)
        details = self._provider_call(
            provider_id,
            "scan_details",
            lambda: provider.get_details(content_id, language),
            language=language,
        )
        details.favorite = self.store.is_favorite(provider_id, content_id)
        details.progress = self.store.get_scan_progress(provider_id, content_id)
        mapped_id = self.store.get_anilist_mapping(
            details.provider_name,
            details.title,
            media_kind="scan",
            provider_id=provider_id,
            content_id=content_id,
            content_type=details.content_type,
            anilist_type="MANGA",
        )
        if mapped_id:
            details.external_ids["anilist_id"] = mapped_id
        for chapter in details.chapters:
            chapter.progress = self.store.get_scan_progress(provider_id, content_id, chapter.id)
        return details

    def get_pages(
        self,
        provider_id: str,
        content_id: str,
        chapter_id: str,
        quality: str = "data",
    ) -> List[ScanPage]:
        provider = self._ensure_provider(provider_id)
        return self._provider_call(
            provider_id,
            "scan_pages",
            lambda: provider.get_pages(content_id, chapter_id, quality),
            quality=quality,
        )

    def _ensure_provider(self, provider_id: Optional[str]) -> ScanProvider:
        if not provider_id or provider_id not in self.providers:
            raise ProviderError("unknown_scan_provider", f"Provider scan inconnu: {provider_id}", 404)
        return self.providers[provider_id]

    def _provider_call(
        self,
        provider_id: str,
        action: str,
        callback: Callable[[], T],
        **fields: Any,
    ) -> T:
        session = diagnostics.current()
        provider = self._ensure_provider(provider_id)
        if not session:
            return callback()

        started = time.perf_counter()
        session.log("INFO", "SCAN_PROVIDER", action, status="start", provider=provider.info.name, **fields)
        try:
            result = callback()
        except Exception as exc:
            session.log_exception(
                "SCAN_PROVIDER",
                action,
                exc,
                duration_ms=(time.perf_counter() - started) * 1000,
                provider=provider.info.name,
                **fields,
            )
            raise

        extra: Dict[str, Any] = {}
        if isinstance(result, list):
            extra["result_count"] = len(result)
        elif isinstance(result, ScanDetails):
            extra.update({"title": result.title, "chapter_count": len(result.chapters)})

        session.log(
            "INFO",
            "SCAN_PROVIDER",
            action,
            status="success",
            duration_ms=(time.perf_counter() - started) * 1000,
            provider=provider.info.name,
            **fields,
            **extra,
        )
        return result
