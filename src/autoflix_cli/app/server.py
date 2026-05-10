import os
import re
import socket
import subprocess
import sys
import threading
import uuid
import logging
import time
import hmac
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, stream_with_context
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from ..scraping import manga as manga_scraper
from ..scraping import scan_manga as scan_manga_scraper
from . import autostart, diagnostics
from .downloads import DownloadManager, find_ffmpeg
from .models import ProviderError
from .playback import prepare_playback
from .providers import ProviderService
from .scans import ScanService
from .store import ACTIVE_DOWNLOAD_STATES, DesktopStore
from .subtitles import fetch_subtitle_as_vtt, subtitle_request_from_token
from .tracker_service import TrackerService


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _json_error(message: str, code: str = "error", status: int = 500):
    return jsonify({"error": code, "message": message}), status


def _episode_number(progress: Dict[str, Any]) -> Optional[int]:
    if progress.get("episode_number"):
        try:
            return int(progress["episode_number"])
        except (TypeError, ValueError):
            pass
    match = re.search(r"(\d+)", progress.get("episode_title", ""))
    return int(match.group(1)) if match else None


def _chapter_number(progress: Dict[str, Any]) -> Optional[int]:
    for key in ("chapter_number", "chapter", "chapter_index"):
        value = progress.get(key)
        if value is None or value == "":
            continue
        try:
            number = int(float(value))
            if key == "chapter_index":
                number += 1
            return number
        except (TypeError, ValueError):
            match = re.search(r"(\d+)", str(value))
            if match:
                return int(match.group(1))
    match = re.search(r"(\d+)", progress.get("chapter_title", "") or progress.get("episode_title", ""))
    return int(match.group(1)) if match else None


def _clean_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_playback_episode_context(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    context: Dict[str, Any] = {}
    for key in (
        "season_id",
        "season_title",
        "episode_id",
        "episode_title",
        "content_type",
    ):
        if key in value and value.get(key) is not None:
            context[key] = value.get(key)

    for key in (
        "season_number",
        "episode_number",
        "season_episode_index",
        "season_episode_count",
    ):
        cleaned = _clean_int(value.get(key))
        if cleaned is not None:
            context[key] = cleaned

    return context


def _download_counts(downloads: list[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "all": len(downloads),
        "active": sum(1 for item in downloads if item.get("state") in ACTIVE_DOWNLOAD_STATES),
        "done": sum(1 for item in downloads if item.get("state") == "done"),
        "errors": sum(1 for item in downloads if item.get("state") == "failed"),
    }


def create_app(
    store: Optional[DesktopStore] = None,
    provider_service: Optional[ProviderService] = None,
    scan_service: Optional[ScanService] = None,
    shutdown_token: Optional[str] = None,
    shutdown_callback: Optional[Callable[[], None]] = None,
) -> Flask:
    static_dir = Path(__file__).parent / "static"
    app = Flask(__name__, static_folder=None)
    app.config["AUTOFLIX_STORE"] = store or DesktopStore()
    app.config["AUTOFLIX_PROVIDERS"] = provider_service or ProviderService(app.config["AUTOFLIX_STORE"])
    app.config["AUTOFLIX_SCANS"] = scan_service or ScanService(app.config["AUTOFLIX_STORE"])
    app.config["AUTOFLIX_SESSIONS"] = {}
    app.config["AUTOFLIX_SHUTDOWN_TOKEN"] = shutdown_token or ""
    app.config["AUTOFLIX_SHUTDOWN_CALLBACK"] = shutdown_callback
    app.config["AUTOFLIX_DOWNLOADS"] = DownloadManager(app.config["AUTOFLIX_STORE"])
    app.config["AUTOFLIX_TRACKER"] = TrackerService(
        app.config["AUTOFLIX_STORE"],
        app.config["AUTOFLIX_PROVIDERS"],
        app.config["AUTOFLIX_DOWNLOADS"],
        app.config["AUTOFLIX_SCANS"],
    )
    app.config["AUTOFLIX_TRACKER"].start()

    @app.before_request
    def diagnostics_before_request():
        g.autoflix_request_started = time.perf_counter()

    @app.after_request
    def diagnostics_after_request(response):
        if diagnostics.is_enabled() and request.path.startswith("/api/") and request.path != "/api/devlog":
            started = getattr(g, "autoflix_request_started", time.perf_counter())
            rule = request.url_rule.rule if request.url_rule else request.path
            status = "ok"
            if response.status_code >= 500:
                status = "error"
            elif response.status_code >= 400:
                status = "warning"
            session = diagnostics.current()
            if session:
                fields: Dict[str, Any] = {
                    "method": request.method,
                    "path": rule,
                    "http_status": response.status_code,
                }
                if request.args:
                    fields["params"] = dict(request.args.items())
                session.log(
                    "INFO",
                    "API",
                    "request",
                    status=status,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    **fields,
                )
        return response

    @app.errorhandler(ProviderError)
    def handle_provider_error(exc: ProviderError):
        diagnostics.error(
            "API",
            "provider_error",
            code=exc.code,
            message=exc.message,
            http_status=exc.status_code,
        )
        return jsonify(exc.to_dict()), exc.status_code

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        if isinstance(exc, HTTPException):
            diagnostics.error(
                "API",
                "http_error",
                error=exc.name,
                message=exc.description,
                http_status=exc.code,
            )
            return jsonify({"error": exc.name, "message": exc.description}), exc.code
        diagnostics.exception("API", "unexpected_error", exc, http_status=500)
        return _json_error(str(exc), "unexpected_error", 500)

    @app.route("/")
    def index():
        response = send_from_directory(static_dir, "index.html")
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/assets/<path:path>")
    def assets(path: str):
        response = send_from_directory(static_dir, path)
        lower_path = path.lower()
        if lower_path.endswith(".js"):
            response.headers["Content-Type"] = "text/javascript; charset=utf-8"
        elif lower_path.endswith(".css"):
            response.headers["Content-Type"] = "text/css; charset=utf-8"
        if lower_path in {"app.js", "styles.css"}:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.route("/api/internal/shutdown", methods=["POST"])
    def internal_shutdown():
        expected = str(app.config.get("AUTOFLIX_SHUTDOWN_TOKEN") or "")
        if not expected:
            return _json_error("Route interne indisponible.", "shutdown_unavailable", 404)

        payload = request.get_json(silent=True) or {}
        token = str(request.headers.get("X-AutoFlix-Token") or payload.get("token") or "")
        if not hmac.compare_digest(token, expected):
            return _json_error("Token invalide.", "invalid_shutdown_token", 403)

        callback = app.config.get("AUTOFLIX_SHUTDOWN_CALLBACK")
        if callback:
            threading.Thread(target=callback, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/api/devlog", methods=["POST"])
    def devlog():
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "event")
        status = str(payload.get("status") or "ok")
        details = payload.get("details") or {}
        if not isinstance(details, dict):
            details = {"value": details}
        diagnostics.info("UI", action, status=status, **details)
        return jsonify({"ok": True})

    @app.route("/api/providers")
    def providers():
        service: ProviderService = app.config["AUTOFLIX_PROVIDERS"]
        return jsonify({"providers": service.list_providers()})

    @app.route("/api/scans/providers")
    def scan_providers():
        service: ScanService = app.config["AUTOFLIX_SCANS"]
        return jsonify({"providers": service.list_providers()})

    @app.route("/api/scans/search")
    def scan_search():
        service: ScanService = app.config["AUTOFLIX_SCANS"]
        query = request.args.get("q", "")
        provider = request.args.get("provider") or None
        language = request.args.get("language") or "fr"
        if provider == "all":
            provider = None
        results = service.search(query, provider, language)
        diagnostics.info(
            "API",
            "scan_search",
            query=query,
            provider=provider or "all",
            language=language,
            result_count=len(results),
            providers_error=getattr(service, "last_search_errors", []),
        )
        return jsonify({"results": [result.to_dict() for result in results]})

    @app.route("/api/scans/<provider_id>/<content_id>")
    def scan_details(provider_id: str, content_id: str):
        service: ScanService = app.config["AUTOFLIX_SCANS"]
        language = request.args.get("language") or "fr"
        details = service.get_details(provider_id, content_id, language)
        diagnostics.info(
            "API",
            "scan_details",
            provider=provider_id,
            title=details.title,
            language=language,
            chapter_count=len(details.chapters),
        )
        return jsonify(details.to_dict())

    @app.route("/api/scans/<provider_id>/<content_id>/chapters/<chapter_id>/pages")
    def scan_pages(provider_id: str, content_id: str, chapter_id: str):
        service: ScanService = app.config["AUTOFLIX_SCANS"]
        quality = request.args.get("quality") or "data"
        pages = service.get_pages(provider_id, content_id, chapter_id, quality)
        diagnostics.info(
            "API",
            "scan_pages",
            provider=provider_id,
            chapter=chapter_id,
            page_count=len(pages),
            quality=quality,
        )
        return jsonify({"pages": [page.to_dict() for page in pages]})

    @app.route("/api/scans/scan_manga/images/proxy")
    def scan_manga_image():
        url = request.args.get("url")
        if not url:
            return _json_error("URL image Scan-Manga manquante.", "missing_scan_manga_image", 422)
        try:
            upstream = scan_manga_scraper.fetch_image(url)
        except ValueError as exc:
            return _json_error(str(exc), "invalid_scan_manga_image", 422)
        except scan_manga_scraper.ScanMangaCloudflareError:
            return _json_error(
                "Image Scan-Manga bloquee par Cloudflare.",
                "scan_manga_image_cloudflare",
                502,
            )
        if upstream.status_code >= 400:
            return _json_error("Image Scan-Manga indisponible.", "scan_manga_image_unavailable", upstream.status_code)

        content_type = upstream.headers.get("Content-Type") or upstream.headers.get("content-type") or "image/jpeg"
        response_headers = {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        }
        content_length = upstream.headers.get("Content-Length") or upstream.headers.get("content-length")
        if content_length:
            response_headers["Content-Length"] = content_length

        def generate():
            for chunk in upstream.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=response_headers,
        )

    @app.route("/api/scans/progress", methods=["POST"])
    def save_scan_progress():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        payload = request.get_json(silent=True) or {}
        if not payload.get("provider_id") or not payload.get("content_id") or not payload.get("chapter_id"):
            return _json_error("Progression scan invalide.", "invalid_scan_progress", 422)
        entry = current_store.save_scan_progress(payload)
        if payload.get("completed"):
            provider_id = entry.get("provider_id")
            content_id = entry.get("content_id")
            tracking = current_store.get_tracking(provider_id, content_id) if provider_id and content_id else {}
            if provider_id and content_id and tracking:
                current_store.ensure_tracking(
                    provider_id=provider_id,
                    content_id=content_id,
                    media_kind="scan",
                    content_type="manga",
                    title=entry.get("series_title") or entry.get("title", ""),
                    image=entry.get("logo_url") or entry.get("image") or "",
                    provider_name=entry.get("provider") or entry.get("provider_name") or provider_id,
                    language=entry.get("language"),
                    follow_enabled=tracking.get("follow_enabled", True),
                    last_read_chapter=_chapter_number(entry),
                )
        return jsonify({"progress": entry})

    @app.route("/api/manga/search")
    def manga_search():
        query = request.args.get("q", "")
        if not query.strip():
            return jsonify({"results": []})
        results = manga_scraper.search_manga(query)
        diagnostics.info(
            "API",
            "manga_search",
            query=query,
            result_count=len(results),
        )
        return jsonify({"results": [result.to_dict() for result in results]})

    @app.route("/api/manga/info")
    def manga_info():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        url = request.args.get("url") or request.args.get("catalogue_url") or request.args.get("scan_url")
        if not url:
            return _json_error("URL manga manquante.", "missing_manga_url", 422)
        info = manga_scraper.get_manga_info(url)
        payload = info.to_dict()
        payload["progress"] = current_store.get_manga_progress(info.scan_url)
        diagnostics.info("API", "manga_info", title=info.title, scan_url=info.scan_url)
        return jsonify(payload)

    @app.route("/api/manga/chapters")
    def manga_chapters():
        url = request.args.get("url") or request.args.get("scan_url")
        if not url:
            return _json_error("URL scan manga manquante.", "missing_manga_url", 422)
        chapters = manga_scraper.fetch_chapters(url)
        items = [
            {
                "index": index,
                "title": f"Chapitre {index + 1}",
                "page_count": len(pages),
                "pages": pages,
            }
            for index, pages in enumerate(chapters)
        ]
        diagnostics.info(
            "API",
            "manga_chapters",
            scan_url=url,
            chapter_count=len(chapters),
            page_count=sum(len(pages) for pages in chapters),
        )
        return jsonify({"chapters": chapters, "items": items})

    @app.route("/api/manga/image")
    def manga_image():
        url = request.args.get("url")
        if not url:
            return _json_error("URL image manga manquante.", "missing_manga_image", 422)
        try:
            upstream = manga_scraper.fetch_image(url)
        except ValueError as exc:
            return _json_error(str(exc), "invalid_manga_image", 422)
        if upstream.status_code >= 400:
            return _json_error("Image manga indisponible.", "manga_image_unavailable", upstream.status_code)

        content_type = upstream.headers.get("Content-Type") or upstream.headers.get("content-type") or "image/jpeg"
        response_headers = {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        }
        content_length = upstream.headers.get("Content-Length") or upstream.headers.get("content-length")
        if content_length:
            response_headers["Content-Length"] = content_length

        def generate():
            for chunk in upstream.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=response_headers,
        )

    @app.route("/api/manga/progress", methods=["GET", "POST"])
    def manga_progress():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        if request.method == "GET":
            scan_url = request.args.get("url") or request.args.get("scan_url")
            return jsonify({"progress": current_store.get_manga_progress(scan_url)})

        payload = request.get_json(silent=True) or {}
        if not payload.get("scan_url"):
            return _json_error("Progression manga invalide.", "invalid_manga_progress", 422)
        try:
            entry = current_store.set_manga_progress(payload)
        except ValueError as exc:
            return _json_error(str(exc), "invalid_manga_progress", 422)
        return jsonify({"progress": entry})

    @app.route("/api/home")
    def home():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        return jsonify(
            {
                "resume": current_store.get_last_watched(),
                "favorites": current_store.list_favorites(),
                "history": current_store.list_history()[:24],
                "preferences": current_store.get_preferences(),
            }
        )

    @app.route("/api/history", methods=["DELETE"])
    def delete_history():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        payload = request.get_json(silent=True) or {}
        history_key = str(payload.get("history_key") or "").strip()
        if history_key:
            removed = current_store.delete_history_entry(history_key)
            if not removed:
                return _json_error("Entree d'historique introuvable.", "unknown_history_entry", 404)
            return jsonify({
                "removed": 1,
                "resume": current_store.get_last_watched(),
                "history": current_store.list_history(),
            })
        if payload.get("all") is True:
            removed_count = current_store.clear_history()
            return jsonify({"removed": removed_count, "resume": None, "history": []})
        return _json_error("Suppression d'historique invalide.", "invalid_history_delete", 422)

    @app.route("/api/search")
    def search():
        service: ProviderService = app.config["AUTOFLIX_PROVIDERS"]
        query = request.args.get("q", "")
        provider = request.args.get("provider") or None
        content_type = request.args.get("type") or None
        if provider == "all":
            provider = None
        if content_type == "all":
            content_type = None
        results = service.search(query, provider, content_type)
        diagnostics.info(
            "API",
            "search",
            query=query,
            provider=provider or "all",
            type=content_type or "all",
            result_count=len(results),
            providers_error=getattr(service, "last_search_errors", []),
        )
        return jsonify({"results": [result.to_dict() for result in results]})

    @app.route("/api/content/<provider_id>/<content_id>")
    def content_details(provider_id: str, content_id: str):
        service: ProviderService = app.config["AUTOFLIX_PROVIDERS"]
        details = service.get_details(provider_id, content_id)
        diagnostics.info(
            "API",
            "details",
            provider=provider_id,
            title=details.title,
            type=details.content_type,
            season_count=len(details.seasons),
        )
        return jsonify(details.to_dict())

    @app.route("/api/content/<provider_id>/<content_id>/seasons/<season_id>")
    def content_season(provider_id: str, content_id: str, season_id: str):
        service: ProviderService = app.config["AUTOFLIX_PROVIDERS"]
        season = service.get_season(provider_id, content_id, season_id)
        diagnostics.info(
            "API",
            "season",
            provider=provider_id,
            title=season.title,
            episode_count=len(season.episodes),
            language=season.language or ",".join(season.languages),
        )
        return jsonify(season.to_dict())

    @app.route("/api/content/<provider_id>/<content_id>/episodes/<episode_id>/sources")
    def episode_sources(provider_id: str, content_id: str, episode_id: str):
        service: ProviderService = app.config["AUTOFLIX_PROVIDERS"]
        sources = service.get_sources(provider_id, content_id, episode_id)
        diagnostics.info(
            "API",
            "sources",
            provider=provider_id,
            source_count=len(sources),
            sources=[
                {
                    "name": source.name,
                    "quality": source.quality,
                    "type": source.source_type,
                    "subtitles": len(source.subtitles),
                }
                for source in sources
            ],
        )
        return jsonify({"sources": [source.to_dict() for source in sources]})

    @app.route("/api/favorites")
    def list_favorites():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        return jsonify({"favorites": current_store.list_favorites()})

    @app.route("/api/favorites", methods=["POST"])
    def add_favorite():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        payload = request.get_json(silent=True) or {}
        summary = payload.get("summary") or payload
        if not summary.get("provider_id") or not summary.get("content_id"):
            return _json_error("Favori invalide.", "invalid_favorite", 422)
        return jsonify({"favorite": current_store.add_favorite(summary)})

    @app.route("/api/favorites/<provider_id>/<content_id>", methods=["DELETE"])
    def remove_favorite(provider_id: str, content_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        current_store.remove_favorite(provider_id, content_id)
        return jsonify({"ok": True})

    @app.route("/api/playback/start", methods=["POST"])
    def playback_start():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        sessions: Dict[str, Dict[str, Any]] = app.config["AUTOFLIX_SESSIONS"]
        payload = request.get_json(silent=True) or {}
        source_id = payload.get("source_id")
        if not source_id:
            return _json_error("Source manquante.", "missing_source", 422)

        playback = prepare_playback(source_id)
        playback_id = uuid.uuid4().hex
        progress = playback.get("progress") or {}
        episode_context = _clean_playback_episode_context(payload.get("episode_context"))
        if episode_context:
            progress = {**progress, **episode_context}
            playback["progress"] = progress
        sessions[playback_id] = playback
        if progress:
            current_store.save_progress(
                progress,
                progress.get("position") or 0.0,
                progress.get("duration"),
                False,
            )

        diagnostics.info(
            "API",
            "playback_start",
            status="success",
            playback_id=playback_id[:8],
            media_kind=playback.get("media_kind"),
            title=playback.get("title"),
            source_name=playback.get("source_name"),
        )
        return jsonify({"playback_id": playback_id, **playback})

    @app.route("/api/subtitles/proxy/<token>.vtt")
    def subtitle_proxy(token: str):
        subtitle_request = subtitle_request_from_token(token)
        try:
            offset_ms = int(float(request.args.get("offset_ms") or 0))
        except (TypeError, ValueError):
            offset_ms = 0
        vtt_content = fetch_subtitle_as_vtt(
            subtitle_request["url"],
            headers=subtitle_request.get("headers"),
            context=subtitle_request.get("context", ""),
            offset_ms=offset_ms,
        )
        if offset_ms:
            diagnostics.info(
                "SUBTITLES",
                "offset_applied",
                status="ok",
                offset_ms=offset_ms,
                offset_s=round(offset_ms / 1000, 3),
                upstream_domain=diagnostics.domain_of(subtitle_request["url"]),
                context=subtitle_request.get("context", ""),
            )
        diagnostics.info(
            "SUBTITLES",
            "proxy_served",
            status="ok",
            offset_ms=offset_ms,
            upstream_domain=diagnostics.domain_of(subtitle_request["url"]),
            context=subtitle_request.get("context", ""),
        )
        return Response(
            vtt_content,
            mimetype="text/vtt",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.route("/api/progress", methods=["POST"])
    def save_progress():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        sessions: Dict[str, Dict[str, Any]] = app.config["AUTOFLIX_SESSIONS"]
        payload = request.get_json(silent=True) or {}
        playback_id = payload.get("playback_id")
        session = sessions.get(playback_id or "")
        if not session:
            return _json_error("Session de lecture inconnue.", "unknown_playback", 404)

        progress = session.get("progress") or {}
        current_time = payload.get("current_time")
        if current_time is None:
            current_time = payload.get("timestamp")
        entry = current_store.save_progress(
            progress,
            current_time=float(current_time or 0),
            duration=payload.get("duration"),
            completed=bool(payload.get("completed")),
        )
        if payload.get("completed"):
            episode = _episode_number(progress)
            provider_id = progress.get("provider_id")
            content_id = progress.get("content_id")
            tracking = current_store.get_tracking(provider_id, content_id) if provider_id and content_id else {}
            if episode and provider_id and content_id and tracking:
                current_store.ensure_tracking(
                    provider_id=provider_id,
                    content_id=content_id,
                    media_kind="video",
                    content_type=progress.get("content_type") or "anime",
                    title=progress.get("series_title", ""),
                    image=progress.get("logo_url") or "",
                    provider_name=progress.get("provider") or progress.get("provider_name") or provider_id,
                    follow_enabled=tracking.get("follow_enabled", True),
                    auto_download=tracking.get("auto_download", False),
                    last_completed_episode=episode,
                )
        return jsonify({"progress": entry})

    @app.route("/api/settings")
    def settings():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        return jsonify({"preferences": current_store.get_preferences()})

    @app.route("/api/settings", methods=["POST"])
    def update_settings():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        payload = request.get_json(silent=True) or {}
        preferences = current_store.update_preferences(payload)
        if "start_with_windows" in payload:
            autostart.set_start_with_windows(bool(preferences.get("start_with_windows")))
        return jsonify({"preferences": preferences})

    # ---------- Downloads ----------

    @app.route("/api/downloads")
    def list_downloads():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        ffmpeg = find_ffmpeg()
        downloads = current_store.list_downloads()
        preferences = current_store.get_preferences()
        return jsonify({
            "downloads": downloads,
            "counts": _download_counts(downloads),
            "download_path": preferences.get("download_path") or "",
            "ffmpeg_available": bool(ffmpeg),
            "ffmpeg_path": ffmpeg or "",
        })

    @app.route("/api/downloads", methods=["POST"])
    def create_download():
        manager: DownloadManager = app.config["AUTOFLIX_DOWNLOADS"]
        payload = request.get_json(silent=True) or {}
        source_id = payload.get("source_id")
        if not source_id:
            return _json_error("Source manquante.", "missing_source", 422)
        record = manager.submit(source_id)
        return jsonify({"download": record})

    @app.route("/api/downloads/<job_id>/cancel", methods=["POST"])
    def cancel_download(job_id: str):
        manager: DownloadManager = app.config["AUTOFLIX_DOWNLOADS"]
        ok = manager.cancel(job_id)
        if not ok:
            return _json_error("Job introuvable.", "unknown_job", 404)
        return jsonify({"ok": True})

    @app.route("/api/downloads/<job_id>/retry", methods=["POST"])
    def retry_download(job_id: str):
        manager: DownloadManager = app.config["AUTOFLIX_DOWNLOADS"]
        payload = request.get_json(silent=True) or {}
        record = manager.retry(job_id, source_id=payload.get("source_id"))
        if not record:
            return _json_error("Job introuvable.", "unknown_job", 404)
        return jsonify({"download": record})

    @app.route("/api/downloads/<job_id>", methods=["DELETE"])
    def delete_download(job_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        try:
            result = current_store.delete_download_with_file(job_id)
        except OSError as exc:
            return _json_error(f"Impossible de supprimer le fichier: {exc}", "delete_file_failed", 500)
        if result.get("missing"):
            return _json_error("Job introuvable.", "unknown_job", 404)
        if result.get("active"):
            return _json_error("Téléchargement actif. Annule-le avant de le supprimer.", "active_download", 409)
        return jsonify(result)

    @app.route("/api/downloads/<job_id>/file")
    def serve_download_file(job_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        record = current_store.get_download(job_id)
        if not record:
            return _json_error("Job introuvable.", "unknown_job", 404)
        path = Path(record.get("output_path") or "")
        if not path.is_file():
            return _json_error("Fichier introuvable.", "missing_file", 404)
        mimetype = "video/mp4"
        if path.suffix.lower() == ".mkv":
            mimetype = "video/x-matroska"
        elif path.suffix.lower() == ".webm":
            mimetype = "video/webm"
        return send_file(
            str(path),
            mimetype=mimetype,
            as_attachment=False,
            conditional=True,
        )

    @app.route("/api/downloads/<job_id>/open-folder", methods=["POST"])
    def open_download_folder(job_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        record = current_store.get_download(job_id)
        if not record:
            return _json_error("Job introuvable.", "unknown_job", 404)
        path = Path(record.get("output_path") or "")
        folder = path.parent if path else None
        if not folder or not folder.exists():
            return _json_error("Dossier introuvable.", "missing_folder", 404)
        try:
            if sys.platform == "win32":
                if path.exists():
                    subprocess.Popen(["explorer", "/select,", str(path)])
                else:
                    os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            return _json_error(f"Impossible d'ouvrir le dossier: {exc}", "open_failed", 500)
        return jsonify({"ok": True})

    @app.route("/api/downloads/clear-done", methods=["POST"])
    def clear_done_downloads():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        summary = current_store.clear_finished_downloads_with_files()
        if summary.get("file_errors"):
            return jsonify(summary), 500
        return jsonify(summary)

    # ---------- Tracking ----------

    @app.route("/api/tracking")
    def list_tracking():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        return jsonify({"tracking": current_store.list_tracking()})

    @app.route("/api/tracking/<provider_id>/<content_id>", methods=["GET"])
    def get_tracking(provider_id: str, content_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        return jsonify({"tracking": current_store.get_tracking(provider_id, content_id)})

    @app.route("/api/tracking/<provider_id>/<content_id>/settings", methods=["POST"])
    def set_tracking_settings(provider_id: str, content_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        tracker_service: TrackerService = app.config["AUTOFLIX_TRACKER"]
        payload = request.get_json(silent=True) or {}
        media_kind = str(payload.get("media_kind") or "video").lower()
        content_type = payload.get("content_type") or ("manga" if media_kind == "scan" else "anime")
        if media_kind not in {"video", "scan"}:
            return _json_error("Type de média invalide.", "invalid_media_kind", 422)
        enabled = bool(payload.get("enabled"))
        auto_download = bool(payload.get("auto_download")) if media_kind != "scan" else False
        entry = current_store.set_tracking_settings(
            provider_id=provider_id,
            content_id=content_id,
            enabled=enabled,
            media_kind=media_kind,
            content_type=content_type,
            title=payload.get("title") or "",
            image=payload.get("image") or "",
            provider_name=payload.get("provider_name") or payload.get("provider") or provider_id,
            auto_download=auto_download,
            language=payload.get("language"),
            tracked_languages=payload.get("tracked_languages"),
        )
        if enabled:
            entry = tracker_service.refresh_entry(entry, notify=False) or entry
            tracker_service.trigger_now()
        return jsonify({"tracking": entry})

    @app.route("/api/tracking/<provider_id>/<content_id>/auto-download", methods=["POST"])
    def set_auto_download(provider_id: str, content_id: str):
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        tracker_service: TrackerService = app.config["AUTOFLIX_TRACKER"]
        payload = request.get_json(silent=True) or {}
        enabled = bool(payload.get("enabled"))
        entry = current_store.set_auto_download(provider_id, content_id, enabled)
        if enabled:
            tracker_service.trigger_now()
        return jsonify({"tracking": entry})

    @app.route("/api/tracking/refresh", methods=["POST"])
    def refresh_tracking():
        tracker_service: TrackerService = app.config["AUTOFLIX_TRACKER"]
        results = tracker_service.run_check_cycle()
        return jsonify({"updated": results})

    # ---------- Notifications ----------

    @app.route("/api/notifications/pending")
    def pending_notifications():
        current_store: DesktopStore = app.config["AUTOFLIX_STORE"]
        return jsonify({"notifications": current_store.pending_notifications()})

    @app.route("/api/system/ffmpeg")
    def system_ffmpeg():
        path = find_ffmpeg()
        return jsonify({"available": bool(path), "path": path or ""})

    return app


class LocalAppServer:
    def __init__(self, app: Optional[Flask] = None, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port or find_free_port()
        self.app = app or create_app()
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        if self._server:
            return self.url
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._server = make_server(self.host, self.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        try:
            tracker_service = self.app.config.get("AUTOFLIX_TRACKER")
            if tracker_service:
                tracker_service.stop()
            downloads = self.app.config.get("AUTOFLIX_DOWNLOADS")
            if downloads:
                downloads.stop()
        except Exception:
            pass
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
