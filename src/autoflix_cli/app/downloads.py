import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any, Dict, Optional

import m3u8
from curl_cffi import requests as cffi_requests

from ..proxy import basename_from_uri, decode_headers_param, encode_headers_token
from . import diagnostics
from .encoding import decode_payload
from .models import ProviderError
from .playback import prepare_playback
from .store import DesktopStore


_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_PROGRESS_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
_SIZE_RE = re.compile(r"total_size=(\d+)")


def sanitize_filename(value: str, fallback: str = "AutoFlix") -> str:
    if not value:
        return fallback
    cleaned = str(value)
    for ch in ("'", "‘", "’", "“", "”", "`"):
        cleaned = cleaned.replace(ch, "")
    cleaned = _INVALID_CHARS.sub("_", cleaned).strip(" .")
    return cleaned[:120] or fallback


def make_dedup_key(progress: Dict[str, Any]) -> str:
    parts = [
        progress.get("provider_id") or progress.get("provider") or "",
        progress.get("content_id") or "",
        progress.get("season_title") or "",
        progress.get("episode_title") or "",
        str(progress.get("episode_number") or ""),
        str(progress.get("anilist_id") or ""),
    ]
    return "|".join(parts)


def find_ffmpeg() -> Optional[str]:
    path = shutil.which("ffmpeg")
    if path:
        return path
    if sys.platform == "win32":
        candidates = [
            r"C:\\ffmpeg\\bin\\ffmpeg.exe",
            r"C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe",
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
    return None


def build_output_path(base_dir: Path, progress: Dict[str, Any], extension: str = "mp4") -> Path:
    provider = sanitize_filename(progress.get("provider") or progress.get("provider_id") or "Unknown", "Unknown")
    series = sanitize_filename(progress.get("series_title") or "AutoFlix", "AutoFlix")
    season = sanitize_filename(progress.get("season_title") or "", "")
    episode_number = progress.get("episode_number")
    episode_title = progress.get("episode_title") or ""
    parts = []
    if episode_number:
        parts.append(f"E{int(episode_number):02d}")
    if episode_title:
        parts.append(sanitize_filename(episode_title, ""))
    if not parts:
        parts.append(f"AutoFlix_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    filename = " - ".join([part for part in parts if part]) + f".{extension}"
    folder = base_dir / provider / series
    if season:
        folder = folder / season
    return folder / filename


def resolve_best_hls_variant(proxy_stream_url: str) -> str:
    """If the HLS URL is a master playlist, rewrite the proxy URL to point to the best variant.

    Fetches the manifest directly from the upstream (not via the local proxy) to avoid
    nested proxy URLs being baked into segment URIs.
    Returns the original URL on any failure (safe fallback).
    """
    try:
        parsed = urllib.parse.urlparse(proxy_stream_url)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if "url" not in qs:
            return proxy_stream_url
        upstream_url = qs["url"][0]
        headers_token = qs.get("headers", [""])[0]
        headers = decode_headers_param(headers_token)

        response = cffi_requests.get(
            upstream_url,
            headers=headers or None,
            timeout=20,
            impersonate="chrome",
        )
        response.raise_for_status()
        content = response.text or ""
        if "#EXT-X-STREAM-INF" not in content:
            return proxy_stream_url

        playlist = m3u8.loads(content, uri=upstream_url)
        if not playlist.is_variant or not playlist.playlists:
            return proxy_stream_url

        best = max(
            playlist.playlists,
            key=lambda p: (
                getattr(p.stream_info, "bandwidth", 0) or 0,
                (getattr(p.stream_info, "resolution", None) or (0, 0))[1] or 0,
            ),
        )
        best_uri = best.absolute_uri or best.uri
        if not best_uri:
            return proxy_stream_url

        # Re-encode headers as base64url. Embed basename in path so ffmpeg sees
        # the .m3u8/.m4s extension directly in the URL path (not in query string).
        new_query = (
            "url=" + urllib.parse.quote(best_uri)
            + "&headers=" + encode_headers_token(headers)
        )
        basename = basename_from_uri(best_uri, "playlist.m3u8")
        path_parts = [p for p in parsed.path.split("/") if p]
        endpoint = path_parts[0] if path_parts else "stream"
        new_url = parsed._replace(
            path=f"/{endpoint}/{basename}",
            query=new_query,
        ).geturl()
        diagnostics.info(
            "DOWNLOAD",
            "hls_variant_selected",
            status="ok",
            bandwidth=getattr(best.stream_info, "bandwidth", None),
            resolution=str(getattr(best.stream_info, "resolution", "") or ""),
            variant_uri=best_uri[:80],
        )
        return new_url
    except Exception as exc:
        diagnostics.warning("DOWNLOAD", "hls_variant_failed", error=str(exc))
        return proxy_stream_url


def parse_ffmpeg_progress(line: str, total_seconds: Optional[float]) -> Optional[Dict[str, Any]]:
    """Parse a single ffmpeg stderr line. Returns dict with percent/seconds/size if found."""
    update: Dict[str, Any] = {}
    time_match = _PROGRESS_TIME_RE.search(line)
    if time_match:
        hours = int(time_match.group(1))
        minutes = int(time_match.group(2))
        seconds = float(time_match.group(3))
        elapsed = hours * 3600 + minutes * 60 + seconds
        update["seconds"] = elapsed
        if total_seconds and total_seconds > 0:
            update["percent"] = max(0.0, min(100.0, (elapsed / total_seconds) * 100.0))
    duration_match = _DURATION_RE.search(line)
    if duration_match and not total_seconds:
        hours = int(duration_match.group(1))
        minutes = int(duration_match.group(2))
        seconds = float(duration_match.group(3))
        update["duration"] = hours * 3600 + minutes * 60 + seconds
    return update or None


class DownloadJob:
    def __init__(
        self,
        job_id: str,
        source_id: str,
        progress: Dict[str, Any],
        output_path: Path,
        media_kind: str = "hls",
        title: str = "",
        provider: str = "",
        quality: str = "",
        source_name: str = "",
        dedup_key: str = "",
        anilist_id: Optional[int] = None,
        auto: bool = False,
    ):
        self.id = job_id
        self.source_id = source_id
        self.progress = progress
        self.output_path = output_path
        self.media_kind = media_kind
        self.title = title
        self.provider = provider
        self.quality = quality
        self.source_name = source_name
        self.dedup_key = dedup_key
        self.anilist_id = anilist_id
        self.auto = auto
        self.process: Optional[subprocess.Popen] = None
        self.cancel_requested = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "title": self.title,
            "provider": self.provider,
            "quality": self.quality,
            "source_name": self.source_name,
            "dedup_key": self.dedup_key,
            "anilist_id": self.anilist_id,
            "auto": self.auto,
            "media_kind": self.media_kind,
            "output_path": str(self.output_path),
            "progress": self.progress,
            "state": "queued",
            "percent": 0.0,
            "seconds": 0.0,
            "duration": None,
            "size": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "completed_at": None,
        }


class DownloadManager:
    """Background download queue using FFmpeg as the engine."""

    def __init__(self, store: DesktopStore, max_concurrent: int = 1):
        self.store = store
        self.max_concurrent = max_concurrent
        self._queue: "Queue[DownloadJob]" = Queue()
        self._jobs: Dict[str, DownloadJob] = {}
        self._jobs_lock = threading.RLock()
        self._workers = []
        self._stop_event = threading.Event()
        for _ in range(max_concurrent):
            worker = threading.Thread(target=self._worker_loop, daemon=True)
            worker.start()
            self._workers.append(worker)

    def stop(self) -> None:
        self._stop_event.set()
        with self._jobs_lock:
            for job in self._jobs.values():
                job.cancel_requested = True
                if job.process and job.process.poll() is None:
                    try:
                        job.process.terminate()
                    except Exception:
                        pass

    def submit(
        self,
        source_id: str,
        anilist_id: Optional[int] = None,
        auto: bool = False,
    ) -> Dict[str, Any]:
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            raise ProviderError(
                "ffmpeg_missing",
                "FFmpeg introuvable. Installe ffmpeg et ajoute-le au PATH.",
                400,
            )

        try:
            payload = decode_payload(source_id)
        except Exception as exc:
            raise ProviderError("invalid_source", f"Source invalide: {exc}", 422) from exc

        progress = payload.get("progress") or {}
        dedup_key = make_dedup_key(progress)
        existing = self.store.find_download_by_dedup(dedup_key)
        if existing and existing.get("state") in ("queued", "resolving", "downloading", "done"):
            diagnostics.info(
                "DOWNLOAD",
                "submit_dedup",
                status="skipped",
                dedup_key=dedup_key,
                state=existing.get("state"),
            )
            return existing

        prefs = self.store.get_preferences()
        base_dir = Path(prefs.get("download_path") or _default_dir())
        output_path = build_output_path(base_dir, progress)
        title_parts = [
            progress.get("series_title"),
            progress.get("season_title"),
            progress.get("episode_title"),
        ]
        title = " - ".join([part for part in title_parts if part]) or payload.get("source_name") or "AutoFlix"

        job = DownloadJob(
            job_id=uuid.uuid4().hex,
            source_id=source_id,
            progress=progress,
            output_path=output_path,
            title=title,
            provider=progress.get("provider", ""),
            quality=payload.get("quality") or "",
            source_name=payload.get("source_name") or "",
            dedup_key=dedup_key,
            anilist_id=anilist_id or progress.get("anilist_id"),
            auto=auto,
        )
        record = job.to_dict()
        self.store.save_download(record)
        with self._jobs_lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        diagnostics.info(
            "DOWNLOAD",
            "submit",
            status="queued",
            job_id=job.id[:8],
            title=title,
            output_path=str(output_path),
            auto=auto,
        )
        return record

    def cancel(self, job_id: str) -> bool:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                record = self.store.get_download(job_id)
                if record and record.get("state") in ("queued",):
                    self.store.update_download(job_id, {"state": "cancelled"})
                    return True
                return False
            job.cancel_requested = True
            if job.process and job.process.poll() is None:
                try:
                    job.process.terminate()
                except Exception:
                    pass
        self.store.update_download(job_id, {"state": "cancelled"})
        diagnostics.info("DOWNLOAD", "cancel", status="cancelled", job_id=job_id[:8])
        return True

    def retry(self, job_id: str, source_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        record = self.store.get_download(job_id)
        if not record:
            return None
        new_source = source_id or record.get("source_id")
        return self.submit(new_source, anilist_id=record.get("anilist_id"), auto=record.get("auto", False))

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=1)
            except Exception:
                continue
            if job.cancel_requested:
                self.store.update_download(job.id, {"state": "cancelled"})
                continue
            try:
                self._run_job(job)
            except Exception as exc:
                diagnostics.exception("DOWNLOAD", "worker_error", exc, job_id=job.id[:8])
                self.store.update_download(job.id, {"state": "failed", "error": str(exc)})
            finally:
                with self._jobs_lock:
                    self._jobs.pop(job.id, None)

    def _run_job(self, job: DownloadJob) -> None:
        self.store.update_download(job.id, {"state": "resolving"})
        diagnostics.info("DOWNLOAD", "resolve", status="start", job_id=job.id[:8])

        try:
            playback = prepare_playback(job.source_id)
        except Exception as exc:
            diagnostics.exception("DOWNLOAD", "resolve", exc, job_id=job.id[:8])
            self.store.update_download(job.id, {"state": "failed", "error": f"resolve: {exc}"})
            return

        if job.cancel_requested:
            self.store.update_download(job.id, {"state": "cancelled"})
            return

        stream_url = playback.get("stream_url")
        media_kind = playback.get("media_kind") or "hls"
        if not stream_url:
            self.store.update_download(job.id, {"state": "failed", "error": "stream_url manquant"})
            return

        if media_kind == "hls":
            stream_url = resolve_best_hls_variant(stream_url)

        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path:
            self.store.update_download(job.id, {"state": "failed", "error": "ffmpeg introuvable"})
            return

        cmd = [
            ffmpeg_path,
            "-y",
            "-loglevel", "info",
            "-stats",
        ]
        if media_kind == "hls":
            cmd += [
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-allowed_extensions", "ALL",
                "-allowed_segment_extensions", "ts,m4s,mp4,aac,m4a",
                "-i", stream_url,
                "-map", "0:v?",
                "-map", "0:a?",
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
            ]
        else:
            cmd += [
                "-i", stream_url,
                "-map", "0",
                "-c", "copy",
                "-movflags", "+faststart",
            ]
        cmd.append(str(job.output_path))
        diagnostics.info(
            "DOWNLOAD",
            "ffmpeg_start",
            status="start",
            job_id=job.id[:8],
            media_kind=media_kind,
            output_path=str(job.output_path),
            cmd=" ".join(shlex.quote(part) for part in cmd),
        )

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            job.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:
            diagnostics.exception("DOWNLOAD", "ffmpeg_spawn", exc, job_id=job.id[:8])
            self.store.update_download(job.id, {"state": "failed", "error": f"ffmpeg: {exc}"})
            return

        self.store.update_download(job.id, {"state": "downloading"})

        total_seconds: Optional[float] = None
        last_percent = -1.0
        stderr_tail: list[str] = []
        try:
            for line in job.process.stderr or []:
                if job.cancel_requested:
                    try:
                        job.process.terminate()
                    except Exception:
                        pass
                    break
                stripped = line.rstrip()
                if stripped:
                    stderr_tail.append(stripped)
                    if len(stderr_tail) > 30:
                        stderr_tail = stderr_tail[-30:]
                update = parse_ffmpeg_progress(line, total_seconds)
                if not update:
                    continue
                if "duration" in update and not total_seconds:
                    total_seconds = update["duration"]
                if "percent" in update and abs(update["percent"] - last_percent) >= 1.0:
                    last_percent = update["percent"]
                    self.store.update_download(
                        job.id,
                        {
                            "percent": round(update["percent"], 2),
                            "seconds": round(update.get("seconds") or 0.0, 2),
                            "duration": total_seconds,
                        },
                    )
        except Exception as exc:
            diagnostics.exception("DOWNLOAD", "ffmpeg_read", exc, job_id=job.id[:8])

        return_code = job.process.wait() if job.process else -1
        if job.cancel_requested:
            self.store.update_download(job.id, {"state": "cancelled"})
            try:
                if job.output_path.exists():
                    job.output_path.unlink()
            except OSError:
                pass
            diagnostics.info("DOWNLOAD", "ffmpeg_cancelled", status="cancelled", job_id=job.id[:8])
            return

        if return_code != 0:
            tail_text = "\n".join(stderr_tail[-12:]) if stderr_tail else ""
            error = f"ffmpeg exit code {return_code}"
            if tail_text:
                error = f"{error}\n{tail_text}"
            self.store.update_download(job.id, {"state": "failed", "error": error})
            diagnostics.error(
                "DOWNLOAD",
                "ffmpeg_failed",
                job_id=job.id[:8],
                code=return_code,
                stderr_tail=stderr_tail[-6:],
            )
            return

        size = None
        try:
            size = job.output_path.stat().st_size
        except OSError:
            pass

        self.store.update_download(
            job.id,
            {
                "state": "done",
                "percent": 100.0,
                "size": size,
                "completed_at": datetime.now().isoformat(),
            },
        )
        diagnostics.info(
            "DOWNLOAD",
            "ffmpeg_done",
            status="success",
            job_id=job.id[:8],
            size=size,
        )

        try:
            self.store.push_notification(
                kind="download_done",
                title="Téléchargement terminé",
                body=job.title,
                job_id=job.id,
                output_path=str(job.output_path),
            )
        except Exception:
            pass


def _default_dir() -> str:
    home = Path.home()
    return str(home / "Videos" / "AutoFlix")
