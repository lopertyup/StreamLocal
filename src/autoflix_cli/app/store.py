import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from platformdirs import user_data_dir

from ..tracker import tracker
from .encoding import make_content_key


def _default_download_dir() -> str:
    home = Path.home()
    candidates = [home / "Videos", home / "Movies", home / "Vidéos"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate / "AutoFlix")
    return str(home / "Videos" / "AutoFlix")


def _mapping_media_id(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("media_id") or value.get("anilist_id")
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_tracking_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _season_progress_percent(progress: Dict[str, Any]) -> Optional[float]:
    season_count = _clean_tracking_int(progress.get("season_episode_count"))
    if not season_count or season_count <= 0:
        return None

    season_index = _clean_tracking_int(progress.get("season_episode_index"))
    if season_index is not None and season_index >= 0:
        current_episode = season_index + 1
    else:
        current_episode = _clean_tracking_int(progress.get("episode_number")) or 0

    if current_episode <= 0:
        return None
    current_episode = min(current_episode, season_count)
    return min(100.0, max(0.0, (current_episode / season_count) * 100.0))


def _clean_anilist_type(value: Any, media_kind: Optional[str] = None, content_type: Optional[str] = None) -> str:
    anilist_type = str(value or "").upper()
    if anilist_type in {"ANIME", "MANGA"}:
        return anilist_type
    if media_kind == "scan" or content_type == "manga":
        return "MANGA"
    return "ANIME"


def _clean_string_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)]


class DesktopStore:
    """JSON-backed state used by the desktop UI."""

    def __init__(self, data_file: Optional[Path] = None):
        data_dir = Path(user_data_dir("AutoFlix", "PaulExplorer"))
        self.data_file = data_file or data_dir / "desktop.json"
        self._lock = threading.RLock()
        self.data = self._load()
        self._ensure_defaults()

    def _load(self) -> Dict[str, Any]:
        if not self.data_file.exists():
            return {}
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.data_file, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)

    def _ensure_defaults(self) -> None:
        self.data.setdefault("favorites", {})
        self.data.setdefault("history", {})
        self.data.setdefault("last_watched_global", None)
        self.data.setdefault("anilist_mappings", {})
        self.data.setdefault("downloads", {})
        self.data.setdefault("tracking", {})
        self.data.setdefault("notifications", [])
        self.data.setdefault("manga_progress", {})
        preferences = self.data.setdefault("preferences", {})
        preferences.setdefault("language", tracker.get_language() or "fr")
        preferences.setdefault("player", "integrated")
        preferences.setdefault("download_path", _default_download_dir())
        preferences.setdefault("download_quality", "max")
        preferences.setdefault("check_interval_minutes", 30)
        preferences.setdefault("notifications_enabled", True)
        preferences.setdefault("default_quality", "max")
        preferences.setdefault("start_with_windows", False)
        preferences.setdefault("minimize_to_tray", True)

    def get_preferences(self) -> Dict[str, Any]:
        self._ensure_defaults()
        return dict(self.data["preferences"])

    def update_preferences(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._ensure_defaults()
            preferences = self.data["preferences"]
            for key in ("language", "player", "download_path",
                        "download_quality", "default_quality"):
                if key in changes:
                    preferences[key] = changes.get(key) or ""
            if "check_interval_minutes" in changes:
                try:
                    minutes = int(changes["check_interval_minutes"])
                    preferences["check_interval_minutes"] = max(5, min(720, minutes))
                except (TypeError, ValueError):
                    pass
            if "notifications_enabled" in changes:
                preferences["notifications_enabled"] = bool(changes["notifications_enabled"])
            if "start_with_windows" in changes:
                preferences["start_with_windows"] = bool(changes["start_with_windows"])
            if "minimize_to_tray" in changes:
                preferences["minimize_to_tray"] = bool(changes["minimize_to_tray"])

            if "language" in changes and preferences["language"]:
                tracker.set_language(preferences["language"])
            if "player" in changes and preferences["player"]:
                tracker.set_player(
                    "browser" if preferences["player"] == "integrated" else preferences["player"]
                )
            self._save()
            return self.get_preferences()

    def get_anilist_token(self) -> str:
        return self.get_preferences().get("anilist_token") or tracker.get_anilist_token() or ""

    def list_favorites(self) -> List[Dict[str, Any]]:
        favorites = list(self.data.get("favorites", {}).values())
        favorites.sort(key=lambda item: item.get("title", "").lower())
        return favorites

    def is_favorite(self, provider_id: str, content_id: str) -> bool:
        return make_content_key(provider_id, content_id) in self.data.get("favorites", {})

    def add_favorite(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        provider_id = summary["provider_id"]
        content_id = summary["content_id"]
        key = make_content_key(provider_id, content_id)
        item = dict(summary)
        item["favorite_added_at"] = datetime.now().isoformat()
        self.data.setdefault("favorites", {})[key] = item
        self._save()
        return item

    def remove_favorite(self, provider_id: str, content_id: str) -> None:
        self.data.setdefault("favorites", {}).pop(make_content_key(provider_id, content_id), None)
        self._save()

    def _history_entry_with_key(self, key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(entry)
        item.setdefault("history_key", key)
        return item

    def list_history(self) -> List[Dict[str, Any]]:
        entries = [
            (index, self._history_entry_with_key(key, item))
            for index, (key, item) in enumerate(self.data.get("history", {}).items())
            if isinstance(item, dict)
        ]
        entries.sort(key=lambda pair: (pair[1].get("last_watched", ""), pair[0]), reverse=True)
        return [item for _, item in entries]

    def get_last_watched(self) -> Optional[Dict[str, Any]]:
        entry = self.data.get("last_watched_global")
        if not isinstance(entry, dict):
            return None
        key = entry.get("history_key")
        if key and key in self.data.get("history", {}):
            return self._history_entry_with_key(key, entry)
        for history_key, history_entry in self.data.get("history", {}).items():
            if history_entry == entry or (
                isinstance(history_entry, dict)
                and history_entry.get("last_watched") == entry.get("last_watched")
                and history_entry.get("provider_id") == entry.get("provider_id")
                and history_entry.get("content_id") == entry.get("content_id")
                and history_entry.get("episode_id") == entry.get("episode_id")
            ):
                return self._history_entry_with_key(history_key, history_entry)
        return dict(entry)

    def _refresh_last_watched_global_locked(self) -> None:
        entries = [
            (index, key, item)
            for index, (key, item) in enumerate(self.data.get("history", {}).items())
            if isinstance(item, dict)
        ]
        if not entries:
            self.data["last_watched_global"] = None
            return
        _, key, entry = max(entries, key=lambda item: (item[2].get("last_watched", ""), item[0]))
        self.data["last_watched_global"] = self._history_entry_with_key(key, entry)

    def delete_history_entry(self, history_key: str) -> bool:
        with self._lock:
            history = self.data.setdefault("history", {})
            if history_key not in history:
                return False
            history.pop(history_key, None)
            current = self.data.get("last_watched_global")
            if not isinstance(current, dict) or current.get("history_key") == history_key:
                self._refresh_last_watched_global_locked()
            else:
                self._refresh_last_watched_global_locked()
            self._save()
            return True

    def clear_history(self) -> int:
        with self._lock:
            history = self.data.setdefault("history", {})
            removed = len(history)
            history.clear()
            self.data["last_watched_global"] = None
            self._save()
            return removed

    def get_content_progress(self, provider_id: str, content_id: str) -> Optional[Dict[str, Any]]:
        key_prefix = make_content_key(provider_id, content_id) + ":"
        matches = [
            (index, item)
            for index, (key, item) in enumerate(self.data.get("history", {}).items())
            if key.startswith(key_prefix)
        ]
        if not matches:
            return None
        matches.sort(key=lambda pair: (pair[1].get("last_watched", ""), pair[0]), reverse=True)
        return matches[0][1]

    def get_progress_by_history_key(self, history_key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not history_key:
            return None
        entry = self.data.get("history", {}).get(history_key)
        return dict(entry) if isinstance(entry, dict) else None

    def get_scan_progress(
        self,
        provider_id: str,
        content_id: str,
        chapter_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        key_prefix = f"{make_content_key(provider_id, content_id)}:scan:"
        if chapter_id:
            return self.data.get("history", {}).get(f"{key_prefix}{chapter_id}")
        matches = [
            (index, item)
            for index, (key, item) in enumerate(self.data.get("history", {}).items())
            if key.startswith(key_prefix) and item.get("media_kind") == "scan"
        ]
        if not matches:
            return None
        matches.sort(key=lambda pair: (pair[1].get("last_watched", ""), pair[0]), reverse=True)
        return matches[0][1]

    def save_progress(
        self,
        progress: Dict[str, Any],
        current_time: float = 0.0,
        duration: Optional[float] = None,
        completed: bool = False,
    ) -> Dict[str, Any]:
        provider_id = progress.get("provider_id", progress.get("provider", "unknown"))
        content_id = progress.get("content_id", "")
        episode_id = progress.get("episode_id", progress.get("episode_title", ""))
        key = progress.get("history_key") or f"{make_content_key(provider_id, content_id)}:{episode_id}"

        position = max(0.0, float(current_time or 0.0))
        total = _clean_optional_float(duration)
        if total is not None and total <= 0:
            total = None
        percent = 0.0
        if total and total > 0:
            percent = min(100.0, max(0.0, (position / total) * 100.0))
        if completed:
            percent = 100.0

        series_percent = _season_progress_percent(progress)
        if series_percent is not None:
            percent = series_percent

        entry = {
            **progress,
            "history_key": key,
            "position": position,
            "duration": total,
            "percent": percent,
            "completed": bool(completed),
            "last_watched": datetime.now().isoformat(),
        }
        for key_name in ("episode_number", "season_episode_index", "season_episode_count"):
            cleaned = _clean_tracking_int(progress.get(key_name))
            if cleaned is not None:
                entry[key_name] = cleaned
        if series_percent is not None:
            entry["series_percent"] = series_percent
        self.data.setdefault("history", {})[key] = entry
        self.data["last_watched_global"] = entry
        self._save()

        # Keep the old CLI history useful for people switching between modes.
        if progress.get("provider"):
            tracker.save_progress(
                provider=progress.get("provider"),
                series_title=progress.get("series_title", progress.get("title", "")),
                season_title=progress.get("season_title", ""),
                episode_title=progress.get("episode_title", ""),
                series_url=progress.get("series_url", ""),
                season_url=progress.get("season_url", ""),
                episode_url=progress.get("episode_url", ""),
                logo_url=progress.get("logo_url"),
            )

        return entry

    def save_scan_progress(self, progress: Dict[str, Any]) -> Dict[str, Any]:
        provider_id = progress.get("provider_id", progress.get("provider", "unknown"))
        content_id = progress.get("content_id", "")
        chapter_id = progress.get("chapter_id") or progress.get("episode_id") or ""
        key = progress.get("history_key") or f"{make_content_key(provider_id, content_id)}:scan:{chapter_id}"

        try:
            page_count = max(0, int(progress.get("page_count") or 0))
        except (TypeError, ValueError):
            page_count = 0
        try:
            if "page_index" in progress:
                page_index = int(progress.get("page_index") or 0)
            else:
                page_index = int(progress.get("current_page") or 1) - 1
        except (TypeError, ValueError):
            page_index = 0
        page_index = max(0, page_index)
        if page_count:
            page_index = min(page_index, max(0, page_count - 1))

        completed = bool(progress.get("completed"))
        percent = 100.0 if completed else 0.0
        if page_count > 0 and not completed:
            percent = min(100.0, max(0.0, ((page_index + 1) / page_count) * 100.0))

        title = progress.get("title") or progress.get("series_title") or "Manga"
        chapter_title = (
            progress.get("chapter_title")
            or progress.get("episode_title")
            or f"Chapitre {progress.get('chapter_number') or ''}".strip()
        )
        entry = {
            **progress,
            "media_kind": "scan",
            "provider_id": provider_id,
            "provider": progress.get("provider") or progress.get("provider_name") or provider_id,
            "content_id": content_id,
            "chapter_id": chapter_id,
            "episode_id": chapter_id,
            "series_title": title,
            "season_title": progress.get("season_title") or "Manga",
            "episode_title": chapter_title,
            "logo_url": progress.get("logo_url") or progress.get("image") or "",
            "page_index": page_index,
            "current_page": page_index + 1,
            "page_count": page_count,
            "position": float(page_index + 1),
            "duration": float(page_count) if page_count else None,
            "percent": percent,
            "completed": completed,
            "history_key": key,
            "last_watched": datetime.now().isoformat(),
        }
        with self._lock:
            self.data.setdefault("history", {})[key] = entry
            self.data["last_watched_global"] = entry
            self._save()
        return entry

    def get_manga_progress(self, scan_url: Optional[str] = None):
        self._ensure_defaults()
        progress = self.data.get("manga_progress", {})
        if scan_url:
            entry = progress.get(scan_url)
            return dict(entry) if entry else None
        entries = [dict(item) for item in progress.values()]
        entries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return entries

    def set_manga_progress(
        self,
        progress: Optional[Dict[str, Any]] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        payload = {**(progress or {}), **fields}
        scan_url = str(payload.get("scan_url") or "").strip()
        if not scan_url:
            raise ValueError("scan_url manquant")

        try:
            chapter = max(0, int(payload.get("chapter") or 0))
        except (TypeError, ValueError):
            chapter = 0
        try:
            page = max(0, int(payload.get("page") or 0))
        except (TypeError, ValueError):
            page = 0

        entry = {
            "scan_url": scan_url,
            "title": payload.get("title") or "Manga",
            "cover_url": payload.get("cover_url") or "",
            "chapter": chapter,
            "page": page,
            "updated_at": datetime.now().isoformat(),
        }
        for key in ("chapter_count", "page_count"):
            if key in payload:
                try:
                    entry[key] = max(0, int(payload.get(key) or 0))
                except (TypeError, ValueError):
                    entry[key] = 0

        with self._lock:
            self.data.setdefault("manga_progress", {})[scan_url] = entry
            self._save()
        return dict(entry)

    def _content_mapping_key(self, media_kind: str, provider_id: str, content_id: str) -> str:
        return f"{media_kind or 'video'}|{provider_id}|{content_id}"

    def get_anilist_mapping(
        self,
        provider: str,
        series_title: str,
        season_title: Optional[str] = None,
        media_kind: Optional[str] = None,
        provider_id: Optional[str] = None,
        content_id: Optional[str] = None,
        content_type: Optional[str] = None,
        anilist_type: Optional[str] = None,
    ) -> Optional[int]:
        mappings = self.data.get("anilist_mappings", {})
        if provider_id and content_id:
            kinds = [media_kind] if media_kind else (["scan"] if content_type == "manga" else ["video", "scan"])
            for kind in [item for item in kinds if item]:
                media_id = _mapping_media_id(mappings.get(self._content_mapping_key(kind, provider_id, content_id)))
                if media_id:
                    return media_id

        if season_title:
            key = f"{provider}|{series_title}|{season_title}"
            media_id = _mapping_media_id(mappings.get(key))
            if media_id:
                return media_id
        key = f"{provider}|{series_title}"
        media_id = _mapping_media_id(mappings.get(key))
        if media_id:
            return media_id
        if media_kind == "scan" or content_type == "manga" or str(anilist_type or "").upper() == "MANGA":
            return None
        return tracker.get_anilist_mapping(provider, series_title, season_title)

    def set_anilist_mapping(
        self,
        provider: str,
        series_title: str,
        media_id: int,
        season_title: Optional[str] = None,
        media_kind: Optional[str] = None,
        provider_id: Optional[str] = None,
        content_id: Optional[str] = None,
        content_type: Optional[str] = None,
        anilist_type: Optional[str] = None,
    ) -> None:
        with self._lock:
            mappings = self.data.setdefault("anilist_mappings", {})
            if provider_id and content_id:
                kind = media_kind or ("scan" if content_type == "manga" else "video")
                mappings[self._content_mapping_key(kind, provider_id, content_id)] = {
                    "media_id": int(media_id),
                    "media_kind": kind,
                    "provider_id": provider_id,
                    "content_id": content_id,
                    "content_type": content_type or ("manga" if kind == "scan" else "anime"),
                    "anilist_type": _clean_anilist_type(anilist_type, kind, content_type),
                    "updated_at": datetime.now().isoformat(),
                }
            if provider and series_title:
                key = (
                    f"{provider}|{series_title}|{season_title}"
                    if season_title
                    else f"{provider}|{series_title}"
                )
                mappings[key] = int(media_id)
                tracker.set_anilist_mapping(provider, series_title, int(media_id), season_title)
            self._save()

    # ---------- Downloads ----------

    def list_downloads(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self.data.get("downloads", {}).values())
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs

    def get_download(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.data.get("downloads", {}).get(job_id)

    def find_download_by_dedup(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for job in self.data.get("downloads", {}).values():
                if job.get("dedup_key") == dedup_key:
                    return job
        return None

    def save_download(self, job: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.data.setdefault("downloads", {})[job["id"]] = job
            self._save()
            return job

    def update_download(self, job_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            jobs = self.data.setdefault("downloads", {})
            current = jobs.get(job_id)
            if not current:
                return None
            current.update(changes)
            current["updated_at"] = datetime.now().isoformat()
            self._save()
            return current

    def delete_download(self, job_id: str) -> bool:
        with self._lock:
            jobs = self.data.setdefault("downloads", {})
            if job_id in jobs:
                jobs.pop(job_id, None)
                self._save()
                return True
        return False

    def clear_finished_downloads(self) -> int:
        with self._lock:
            jobs = self.data.setdefault("downloads", {})
            removed = 0
            for key in list(jobs.keys()):
                if jobs[key].get("state") in ("done", "cancelled"):
                    jobs.pop(key, None)
                    removed += 1
            if removed:
                self._save()
        return removed

    # ---------- Tracking ----------

    def get_tracking(self, provider_id: str, content_id: str) -> Dict[str, Any]:
        key = make_content_key(provider_id, content_id)
        with self._lock:
            return dict(self.data.get("tracking", {}).get(key) or {})

    def set_auto_download(
        self,
        provider_id: str,
        content_id: str,
        enabled: bool,
        anilist_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        key = make_content_key(provider_id, content_id)
        with self._lock:
            tracking = self.data.setdefault("tracking", {})
            entry = tracking.setdefault(key, {})
            entry["provider_id"] = provider_id
            entry["content_id"] = content_id
            entry.setdefault("media_kind", "video")
            if entry.get("media_kind") == "scan":
                entry["auto_download"] = False
            else:
                entry["auto_download"] = bool(enabled)
                if enabled:
                    entry["follow_enabled"] = True
            if anilist_id:
                entry["anilist_id"] = int(anilist_id)
                entry.setdefault("anilist_type", "ANIME")
            entry["updated_at"] = datetime.now().isoformat()
            self._save()
            return dict(entry)

    def ensure_tracking(
        self,
        provider_id: str,
        content_id: str,
        media_kind: str = "video",
        content_type: str = "anime",
        title: str = "",
        image: str = "",
        provider_name: str = "",
        anilist_id: Optional[int] = None,
        anilist_type: Optional[str] = None,
        language: Optional[str] = None,
        follow_enabled: Optional[bool] = None,
        auto_download: Optional[bool] = None,
        tracked_languages: Optional[List[str]] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        key = make_content_key(provider_id, content_id)
        media_kind = media_kind or ("scan" if content_type == "manga" else "video")
        with self._lock:
            tracking = self.data.setdefault("tracking", {})
            entry = tracking.setdefault(key, {})
            entry["provider_id"] = provider_id
            entry["content_id"] = content_id
            entry["media_kind"] = media_kind
            entry["content_type"] = content_type or ("manga" if media_kind == "scan" else "anime")
            if provider_name:
                entry["provider_name"] = provider_name
            if title:
                entry["title"] = title
            if image:
                entry["image"] = image
            if language:
                entry["language"] = language
            if follow_enabled is not None:
                entry["follow_enabled"] = bool(follow_enabled)
            else:
                entry.setdefault("follow_enabled", True)
            if "auto_download" not in entry:
                entry["auto_download"] = False
            if auto_download is not None:
                entry["auto_download"] = bool(auto_download)
            if media_kind == "scan":
                entry["auto_download"] = False
                entry.setdefault("chapter_notifications", True)
            if tracked_languages is not None:
                entry["tracked_languages"] = _clean_string_list(tracked_languages)
            if anilist_id:
                entry["anilist_id"] = int(anilist_id)
                entry["anilist_type"] = _clean_anilist_type(anilist_type, media_kind, content_type)
            for name, value in fields.items():
                if value is None:
                    continue
                if name in {
                    "last_seen_episode",
                    "last_aired_episode",
                    "last_available_episode",
                    "last_completed_episode",
                    "last_seen_chapter",
                    "last_available_chapter",
                    "chapter_count",
                    "last_read_chapter",
                    "anilist_progress",
                }:
                    cleaned = _clean_tracking_int(value)
                    if cleaned is not None:
                        entry[name] = cleaned
                else:
                    entry[name] = value
            entry["updated_at"] = datetime.now().isoformat()
            self._save()
            return dict(entry)

    def set_tracking_settings(
        self,
        provider_id: str,
        content_id: str,
        enabled: bool,
        media_kind: str = "video",
        content_type: str = "anime",
        title: str = "",
        image: str = "",
        provider_name: str = "",
        auto_download: bool = False,
        language: Optional[str] = None,
        tracked_languages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.ensure_tracking(
            provider_id=provider_id,
            content_id=content_id,
            media_kind=media_kind,
            content_type=content_type,
            title=title,
            image=image,
            provider_name=provider_name,
            language=language,
            follow_enabled=enabled,
            auto_download=auto_download if media_kind != "scan" else False,
            tracked_languages=tracked_languages,
        )

    def set_tracking_state(
        self,
        provider_id: str,
        content_id: str,
        last_seen_episode: Optional[int] = None,
        last_aired_episode: Optional[int] = None,
        next_airing: Optional[Dict[str, Any]] = None,
        anilist_status: Optional[str] = None,
        updates: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = make_content_key(provider_id, content_id)
        with self._lock:
            tracking = self.data.setdefault("tracking", {})
            entry = tracking.setdefault(key, {
                "provider_id": provider_id,
                "content_id": content_id,
                "auto_download": False,
                "media_kind": "video",
            })
            if last_seen_episode is not None:
                entry["last_seen_episode"] = int(last_seen_episode)
            if last_aired_episode is not None:
                entry["last_aired_episode"] = int(last_aired_episode)
            if next_airing is not None:
                entry["next_airing"] = next_airing
            if anilist_status is not None:
                entry["anilist_status"] = anilist_status
            for name, value in (updates or {}).items():
                if value is None:
                    continue
                if name in {
                    "last_seen_episode",
                    "last_aired_episode",
                    "last_available_episode",
                    "last_completed_episode",
                    "last_seen_chapter",
                    "last_available_chapter",
                    "chapter_count",
                    "last_read_chapter",
                    "anilist_progress",
                }:
                    cleaned = _clean_tracking_int(value)
                    if cleaned is not None:
                        entry[name] = cleaned
                else:
                    entry[name] = value
            entry["last_check"] = datetime.now().isoformat()
            self._save()
            return dict(entry)

    def list_tracking(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.data.get("tracking", {}).values())

    def list_auto_download_tracking(self) -> List[Dict[str, Any]]:
        return [item for item in self.list_tracking() if item.get("auto_download")]

    # ---------- Notifications ----------

    def push_notification(self, kind: str, title: str, body: str = "", **extras: Any) -> Dict[str, Any]:
        notification = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "kind": kind,
            "title": title,
            "body": body,
            "created_at": datetime.now().isoformat(),
            "consumed": False,
            **extras,
        }
        with self._lock:
            queue = self.data.setdefault("notifications", [])
            queue.append(notification)
            if len(queue) > 50:
                self.data["notifications"] = queue[-50:]
            self._save()
        return notification

    def pending_notifications(self) -> List[Dict[str, Any]]:
        with self._lock:
            queue = self.data.setdefault("notifications", [])
            pending = [item for item in queue if not item.get("consumed")]
            for item in queue:
                item["consumed"] = True
            if pending:
                self._save()
        return pending
