import re
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import diagnostics
from .downloads import DownloadManager
from .store import DesktopStore


def notify_native(title: str, body: str = "") -> bool:
    """Show a native notification. Returns True on success."""
    if sys.platform == "win32":
        return _notify_windows(title, body)
    if sys.platform == "darwin":
        return _notify_macos(title, body)
    return _notify_linux(title, body)


def _ps_escape(value: str) -> str:
    return str(value).replace("'", "''")


def _notify_windows(title: str, body: str) -> bool:
    safe_title = _ps_escape(title)
    safe_body = _ps_escape(body)
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null;"
        "$tpl = [Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
        "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($tpl);"
        "$nodes = $xml.GetElementsByTagName('text');"
        f"$nodes.Item(0).AppendChild($xml.CreateTextNode('{safe_title}')) | Out-Null;"
        f"$nodes.Item(1).AppendChild($xml.CreateTextNode('{safe_body}')) | Out-Null;"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AutoFlix').Show($toast);"
    )
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return True
    except Exception as exc:
        diagnostics.warning("TRACKER", "notify_windows_failed", error=str(exc))
        return False


def _notify_macos(title: str, body: str) -> bool:
    try:
        script = f'display notification "{body}" with title "{title}"'
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _notify_linux(title: str, body: str) -> bool:
    try:
        subprocess.Popen(["notify-send", title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


class TrackerService:
    """Periodic local provider check + auto-download trigger."""

    def __init__(
        self,
        store: DesktopStore,
        provider_service: Any,
        download_manager: DownloadManager,
        scan_service: Any = None,
    ):
        self.store = store
        self.providers = provider_service
        self.downloads = download_manager
        self.scans = scan_service
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cycle_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger_now(self) -> None:
        """Wake the loop and run a check immediately."""
        self._wake.set()

    def _interval_seconds(self) -> int:
        prefs = self.store.get_preferences()
        minutes = prefs.get("check_interval_minutes") or 30
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 30
        return max(60, minutes * 60)

    def _loop(self) -> None:
        self._wake.wait(timeout=15)
        self._wake.clear()
        while not self._stop.is_set():
            try:
                self.run_check_cycle()
            except Exception as exc:
                diagnostics.exception("TRACKER", "loop_error", exc)
            self._wake.clear()
            self._wake.wait(timeout=self._interval_seconds())

    def run_check_cycle(self) -> List[Dict[str, Any]]:
        with self._cycle_lock:
            results: List[Dict[str, Any]] = []
            for entry in self.store.list_tracking():
                if not entry.get("follow_enabled") and not entry.get("auto_download"):
                    continue
                updated = self.refresh_entry(entry)
                if updated:
                    results.append(updated)
            return results

    def refresh_entry(self, entry: Dict[str, Any], notify: bool = True) -> Optional[Dict[str, Any]]:
        if entry.get("media_kind") == "scan":
            return self.refresh_scan_entry(entry, notify=notify)
        return self.refresh_video_entry(entry, notify=notify)

    def refresh_video_entry(self, entry: Dict[str, Any], notify: bool = True) -> Optional[Dict[str, Any]]:
        provider_id = entry.get("provider_id")
        content_id = entry.get("content_id")
        if not provider_id or not content_id:
            return None

        try:
            details, buckets = self._collect_video_episodes(provider_id, content_id)
        except Exception as exc:
            diagnostics.exception(
                "TRACKER",
                "video_refresh_failed",
                exc,
                provider=provider_id,
                content=str(content_id)[:12],
            )
            return None

        if not buckets:
            return None

        previous_state = entry.get("episode_state") if isinstance(entry.get("episode_state"), dict) else {}
        next_state: Dict[str, Dict[str, Any]] = {}
        releases: List[Dict[str, Any]] = []

        for key, bucket in buckets.items():
            language = bucket["language"]
            if not self._language_is_tracked(entry, language):
                continue
            episodes = bucket["episodes"]
            if not episodes:
                continue
            latest = max(item["number"] for item in episodes)
            previous_latest = self._int_value((previous_state.get(key) or {}).get("last_episode")) or 0
            next_state[key] = {
                "season_id": bucket["season_id"],
                "season_title": bucket["season_title"],
                "language": language,
                "last_episode": latest,
                "episode_count": len(episodes),
            }
            if notify and previous_latest and latest > previous_latest:
                for item in episodes:
                    if item["number"] > previous_latest:
                        releases.append({
                            **item,
                            "season_id": bucket["season_id"],
                            "season_title": bucket["season_title"],
                            "language": language,
                        })

        if not next_state:
            return None

        last_available = max(item["last_episode"] for item in next_state.values())
        updated = self.store.set_tracking_state(
            provider_id=provider_id,
            content_id=content_id,
            updates={
                "media_kind": "video",
                "content_type": getattr(details, "content_type", entry.get("content_type") or "anime"),
                "provider_name": getattr(details, "provider_name", provider_id),
                "title": getattr(details, "title", "") or entry.get("title"),
                "image": getattr(details, "image", "") or entry.get("image"),
                "last_available_episode": last_available,
                "episode_state": next_state,
            },
        )

        if notify and releases:
            self._handle_video_releases(updated, releases)
        return self.store.get_tracking(provider_id, content_id)

    def refresh_scan_entry(self, entry: Dict[str, Any], notify: bool = True) -> Optional[Dict[str, Any]]:
        if not self.scans:
            return None
        provider_id = entry.get("provider_id")
        content_id = entry.get("content_id")
        if not provider_id or not content_id:
            return None
        try:
            details = self.scans.get_details(provider_id, content_id, entry.get("language") or "fr")
        except Exception as exc:
            diagnostics.exception(
                "TRACKER",
                "scan_refresh_failed",
                exc,
                provider=provider_id,
                content=str(content_id)[:12],
            )
            return None

        last_chapter, chapter_count = self._scan_chapter_state(details)
        if not last_chapter and not chapter_count:
            return None

        previous_available = self._int_value(entry.get("last_available_chapter")) or 0
        previous_seen = self._int_value(entry.get("last_seen_chapter")) or 0
        previous_known = max(previous_available, previous_seen)
        available_chapter = max(previous_available, last_chapter or 0) or last_chapter
        updates = {
            "media_kind": "scan",
            "content_type": getattr(details, "content_type", "manga"),
            "provider_name": getattr(details, "provider_name", provider_id),
            "title": getattr(details, "title", "") or entry.get("title"),
            "image": getattr(details, "image", "") or entry.get("image"),
            "last_available_chapter": available_chapter,
            "chapter_count": chapter_count,
            "language": entry.get("language") or ((getattr(details, "languages", []) or ["fr"])[0]),
        }

        if last_chapter and (not notify or not previous_known):
            updates["last_seen_chapter"] = last_chapter
        elif last_chapter and previous_known and last_chapter > previous_known:
            updates["last_seen_chapter"] = last_chapter
            title = updates["title"] or "Manga"
            body = f"Chapitre {last_chapter} disponible"
            prefs = self.store.get_preferences()
            if prefs.get("notifications_enabled"):
                notify_native(title, body)
            self.store.push_notification(
                kind="new_scan_chapter",
                title=title,
                body=body,
                provider_id=provider_id,
                content_id=content_id,
                chapter=last_chapter,
            )

        self.store.set_tracking_state(
            provider_id=provider_id,
            content_id=content_id,
            updates=updates,
        )
        return self.store.get_tracking(provider_id, content_id)

    def _collect_video_episodes(self, provider_id: str, content_id: str) -> Tuple[Any, Dict[str, Dict[str, Any]]]:
        details = self.providers.get_details(provider_id, content_id)
        buckets: Dict[str, Dict[str, Any]] = {}
        counters: Dict[str, int] = {}
        for season in getattr(details, "seasons", []) or []:
            season_full = season
            episodes = list(getattr(season_full, "episodes", []) or [])
            if not episodes:
                try:
                    season_full = self.providers.get_season(provider_id, content_id, season.id)
                    episodes = list(getattr(season_full, "episodes", []) or [])
                except Exception:
                    continue
            for episode in episodes:
                language = self._normalize_language(
                    getattr(episode, "language", None)
                    or getattr(season_full, "language", None)
                    or "multi"
                )
                key = self._episode_state_key(getattr(season_full, "id", ""), language)
                counters[key] = counters.get(key, 0) + 1
                number = self._episode_number(
                    getattr(episode, "number", None),
                    getattr(episode, "title", ""),
                    counters[key],
                )
                bucket = buckets.setdefault(key, {
                    "season_id": getattr(season_full, "id", ""),
                    "season_title": getattr(season_full, "title", "") or "Saison",
                    "language": language,
                    "episodes": [],
                })
                bucket["episodes"].append({
                    "episode": episode,
                    "episode_id": getattr(episode, "id", ""),
                    "episode_title": getattr(episode, "title", "") or f"Episode {number}",
                    "number": number,
                })
        return details, buckets

    def _handle_video_releases(self, entry: Dict[str, Any], releases: List[Dict[str, Any]]) -> None:
        prefs = self.store.get_preferences()
        for release in releases:
            number = release["number"]
            language = release.get("language") or "multi"
            title = entry.get("title") or "Anime"
            body = f"Episode {number} disponible ({language.upper()})"
            kind = "new_episode"
            if entry.get("auto_download"):
                try:
                    source_id = self._best_source_for_episode(
                        entry["provider_id"],
                        entry["content_id"],
                        number,
                        season_id=release.get("season_id"),
                        language=language,
                    )
                    if source_id:
                        self.downloads.submit(source_id, auto=True)
                        body = f"Episode {number} disponible ({language.upper()}) - telechargement lance"
                        kind = "auto_download"
                except Exception as exc:
                    diagnostics.exception(
                        "TRACKER",
                        "auto_submit_failed",
                        exc,
                        provider=entry.get("provider_id"),
                        episode=number,
                        language=language,
                    )
            if prefs.get("notifications_enabled"):
                notify_native(title, body)
            self.store.push_notification(
                kind=kind,
                title=title,
                body=body,
                provider_id=entry.get("provider_id"),
                content_id=entry.get("content_id"),
                season_id=release.get("season_id"),
                season_title=release.get("season_title"),
                language=language,
                episode=number,
            )

    @staticmethod
    def _episode_state_key(season_id: str, language: str) -> str:
        return f"{season_id}|{language or 'multi'}"

    @staticmethod
    def _normalize_language(value: Any) -> str:
        language = str(value or "multi").strip().lower()
        return language or "multi"

    @classmethod
    def _episode_number(cls, value: Any, title: str, default: int) -> int:
        direct = cls._int_value(value)
        if direct:
            return direct
        parsed = cls._int_value(title)
        return parsed or default

    def _language_is_tracked(self, entry: Dict[str, Any], language: str) -> bool:
        tracked = [
            self._normalize_language(item)
            for item in (entry.get("tracked_languages") or [])
            if str(item or "").strip()
        ]
        return not tracked or "all" in tracked or self._normalize_language(language) in tracked

    @classmethod
    def _scan_chapter_state(cls, details: Any) -> Tuple[Optional[int], int]:
        chapters = list(getattr(details, "chapters", []) or [])
        chapter_count = len(chapters)
        numbers: List[int] = []
        for index, chapter in enumerate(chapters, start=1):
            number = cls._int_value(getattr(chapter, "chapter", None))
            if number is None:
                number = cls._chapter_number_from_text(getattr(chapter, "title", ""))
            numbers.append(number or index)
        return (max(numbers) if numbers else None, chapter_count)

    @staticmethod
    def _int_value(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            match = re.search(r"(\d+)", str(value))
            return int(match.group(1)) if match else None

    @staticmethod
    def _chapter_number_from_text(value: str) -> Optional[int]:
        match = re.search(r"(\d+)", value or "")
        return int(match.group(1)) if match else None

    def _best_source_for_episode(
        self,
        provider_id: str,
        content_id: str,
        episode_number: int,
        season_id: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Optional[str]:
        details = self.providers.get_details(provider_id, content_id)
        target_episode_id: Optional[str] = None

        for season in details.seasons:
            if season_id and season.id != season_id:
                continue
            season_full = season
            episodes = season_full.episodes
            if not episodes:
                try:
                    season_full = self.providers.get_season(provider_id, content_id, season.id)
                    episodes = season_full.episodes
                except Exception:
                    continue
            for episode in episodes:
                episode_language = self._normalize_language(
                    getattr(episode, "language", None)
                    or getattr(season_full, "language", None)
                    or "multi"
                )
                if language and episode_language != self._normalize_language(language):
                    continue
                number = self._episode_number(getattr(episode, "number", None), getattr(episode, "title", ""), 0)
                if number == episode_number:
                    target_episode_id = episode.id
                    break
            if target_episode_id:
                break

        if not target_episode_id:
            return None

        try:
            sources = self.providers.get_sources(provider_id, content_id, target_episode_id)
        except Exception as exc:
            diagnostics.exception(
                "TRACKER",
                "auto_sources_failed",
                exc,
                provider=provider_id,
                episode=episode_number,
            )
            return None
        if not sources:
            return None
        return sources[0].id
