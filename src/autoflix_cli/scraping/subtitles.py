import math
from typing import Any, Dict, List

from curl_cffi import requests


class SubtitleExtractor:
    """
    Subtitle extractor optimized to be used as a library.
    Results are sorted by order of confidence (OpenSubtitles > WYZIE > Subsense).
    """

    # Order of confidence of sources (lower = higher in the list)
    SOURCE_PRIORITY = {
        "OpenSubtitles (Stremio)": 1,
        "OpenSubtitles (AI)": 2,
        "WYZIE": 3,
        "Subsense": 4,
    }

    def _truthy(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    def _number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _download_count(self, subtitle: Dict[str, Any]) -> float:
        for key in ("download_count", "downloads", "g"):
            count = self._number(subtitle.get(key))
            if count:
                return count
        return 0.0

    def _metadata_score(self, subtitle: Dict[str, Any]) -> float:
        source = subtitle.get("source") or ""
        priority = self.SOURCE_PRIORITY.get(source, 99)
        score = max(0, 500 - priority * 60)

        if self._truthy(subtitle.get("from_trusted") or subtitle.get("trusted")):
            score += 80
        if self._truthy(subtitle.get("ai_translated")):
            score -= 140
        if self._truthy(subtitle.get("machine_translated")):
            score -= 140

        downloads = self._download_count(subtitle)
        if downloads:
            score += min(90, math.log1p(downloads) * 18)

        votes = self._number(subtitle.get("votes"))
        if votes:
            score += min(60, votes * 4)

        return score

    def _dedupe_subtitles(self, subtitles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        unique = []
        for sub in subtitles:
            url = sub.get("url")
            key = url or sub.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(sub)
        return unique

    def _fetch_stremio(self, base_url, imdb_id, season=None, episode=None):
        """Helper for Stremio-style subtitle APIs."""
        if season and episode:
            endpoint = f"{base_url}/subtitles/series/{imdb_id}:{season}:{episode}.json"
        else:
            endpoint = f"{base_url}/subtitles/movie/{imdb_id}.json"

        try:
            response = requests.get(endpoint, timeout=10, impersonate="chrome")
            response.raise_for_status()
            data = response.json()
            return data.get("subtitles", [])
        except Exception as e:
            # Uncomment for debugging:
            # print(f"Subtitles Error ({base_url}): {e}")
            # In library mode, we stay discreet about network errors
            return []

    def get_opensubtitles_stremio(self, imdb_id, season=None, episode=None):
        """OpenSubtitles via Stremio bridge (French support included)."""
        # Using OpenSubtitles v3
        base_url = "https://opensubtitles-v3.strem.io"
        subs = self._fetch_stremio(base_url, imdb_id, season, episode)
        for s in subs:
            s["source"] = "OpenSubtitles (Stremio)"
        return subs

    def get_opensubtitles_ai(self, imdb_id, season=None, episode=None):
        """OpenSubtitles via AI-translated Stremio bridge."""
        # Using Stremio Homes AI translated config (with URL encoded pipes and French added)
        base_url = "https://opensubtitles.stremio.homes/en%7Cfr%7Chi%7Cde%7Car%7Ctr%7Ces%7Cta%7Cte%7Cru%7Cko/ai-translated=true%7Cfrom=all%7Cauto-adjustment=true"
        subs = self._fetch_stremio(base_url, imdb_id, season, episode)
        for s in subs:
            s["source"] = "OpenSubtitles (AI)"
        return subs

    def get_subsense(self, imdb_id, season=None, episode=None):
        """Subsense via Stremio bridge (French support included)."""
        # Adding 'fr' to the config and URL encoding JSON
        import urllib.parse
        config_json = '{"languages":["en","fr","hi","ta","es","ar"],"maxSubtitles":10}'
        config = f"n0tcjfba-{urllib.parse.quote(config_json)}"
        base_url = f"https://subsense.nepiraw.com/{config}"
        subs = self._fetch_stremio(base_url, imdb_id, season, episode)
        for s in subs:
            s["source"] = "Subsense"
        return subs

    def get_wyzie(self, imdb_id, season=None, episode=None):
        """WYZIE Subtitles API."""
        base_url = "https://sub.wyzie.io"
        url = f"{base_url}/search?id={imdb_id}&source=all"
        if season and episode:
            url = f"{base_url}/search?id={imdb_id}&season={season}&episode={episode}&source=all"

        try:
            response = requests.get(url, timeout=10, impersonate="chrome")
            response.raise_for_status()
            data = response.json()
            normalized = []
            for item in data:
                normalized.append(
                    {
                        "lang": item.get("display") or item.get("language"),
                        "url": item.get("url"),
                        "source": "WYZIE",
                    }
                )
            return normalized
        except:
            return []

    def search(self, imdb_id, season=None, episode=None, lang_filter=None):
        """
        Search, filter and sort subtitles by order of confidence.
        :param lang_filter: Language code or name (e.g., 'French', 'fr').
        :return: Sorted list of dictionaries.
        """
        all_subs = []
        all_subs.extend(self.get_opensubtitles_stremio(imdb_id, season, episode))
        all_subs.extend(self.get_opensubtitles_ai(imdb_id, season, episode))
        all_subs.extend(self.get_wyzie(imdb_id, season, episode))
        all_subs.extend(self.get_subsense(imdb_id, season, episode))

        # 1. Filter by language (case insensitive)
        if lang_filter:
            f = lang_filter.lower()
            # Dynamic mapping from languages.py
            from ..languages import get_language_aliases

            aliases = get_language_aliases()
            target = aliases.get(f, f)

            filtered = []
            for sub in all_subs:
                l = (sub.get("lang") or sub.get("lang_code") or "").lower()
                if target in l or l in target or (len(f) == 2 and l.startswith(f)):
                    filtered.append(sub)
            all_subs = filtered

        all_subs = self._dedupe_subtitles(all_subs)
        all_subs.sort(
            key=lambda sub: (
                -self._metadata_score(sub),
                self.SOURCE_PRIORITY.get(sub.get("source") or "", 99),
                str(sub.get("id") or sub.get("url") or ""),
            )
        )
        return all_subs


# Instantiate a global instance for easy use
subtitle_extractor = SubtitleExtractor()
