from pathlib import Path

from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import ScanService
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore


STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "autoflix_cli" / "app" / "static"


class StubVideoProviders(ProviderService):
    def list_providers(self):
        return []

    def search(self, query, provider_id=None, content_type=None):
        del query, provider_id, content_type
        return []


def test_static_ui_text_is_utf8_not_mojibake():
    text = "\n".join(
        (STATIC_DIR / filename).read_text(encoding="utf-8")
        for filename in ("index.html", "app.js")
    )

    assert "Téléchargements" in text
    assert "épisodes" in text
    assert "Qualité" in text

    mojibake_markers = ["Ã", "Â", "â€", "âœ", "�"]
    found = [marker for marker in mojibake_markers if marker in text]
    assert found == []


def test_static_assets_are_served_with_utf8_charset(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=ScanService(store=store, providers={}),
    )
    client = app.test_client()

    try:
        index = client.get("/")
        script = client.get("/assets/app.js")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert index.status_code == 200
    assert "charset=utf-8" in index.headers["Content-Type"]
    assert "Téléchargements".encode("utf-8") in index.data
    assert script.status_code == 200
    assert "charset=utf-8" in script.headers["Content-Type"]
    assert "épisodes".encode("utf-8") in script.data


def test_player_subtitles_are_lazy_loaded():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "resetSubtitleState(playback.subtitles || [])" in script
    assert "applySubtitleTrack(mode, options = {})" in script
    assert 'state.hls.subtitleDisplay = false' in script
    assert "for (const sub of playback.subtitles" not in script
    assert "ST ✓" in script
    source_badges = script.split("function sourceBadges(source)", 1)[1].split("async function loadSources", 1)[0]
    assert "subtitle_languages" not in source_badges


def test_player_subtitle_calibration_is_isolated_and_logged():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "subtitle-calibration-start" in index
    assert "offset_ms" in script
    assert "subtitle_cb" in script
    assert "subtitle_tracks_available" in script
    assert "subtitle_calibration_preview" in script
    assert "subtitle_calibration_validated" in script
    assert "subtitle_calibration_rejected" in script
    assert "subtitleUrlWithOffset(trackMeta.url, state.subtitleOffsetMs, state.subtitleTrackLoadNonce)" in script


def test_static_local_tracking_replaces_anilist_ui():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "/api/tracking/${detail.provider_id}/${detail.content_id}/settings" in script
    assert 'media_kind: isScan ? "scan" : "video"' in script
    assert 'Notifications nouveaux chapitres' in script
    assert "/api/anilist" not in script
    assert "AniList" not in script
    assert 'detail.provider_id === "anime_sama"' not in script


def test_static_manga_entry_uses_scan_tracking_flow():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'data-view="scans" type="button">Manga' in index
    manga_search_branch = script.split('if (type === "manga" || type === "scan") {', 1)[1].split("return;", 1)[0]
    assert "state.scanQuery = query" in manga_search_branch
    assert "await runScanSearch()" in manga_search_branch
    assert "runMangaSearch" not in manga_search_branch


def test_static_scan_reader_has_fullscreen_controls():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "data-reader-fullscreen" in script
    assert 'await enterAppFullscreen("scan")' in script
    assert "function scanFullscreenStage()" in script
    assert "function handleScanPageClick(event)" in script
    assert "state.currentView === \"scans\" && state.scanReader?.mode === \"single\"" in script
    assert "app-scan-fullscreen" in styles
    assert ".scan-reader-stage" in styles


def test_static_scan_chapters_with_unknown_page_count_are_clickable():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "const unavailable = !chapter.id;" in script
    assert 'return chapters.filter((chapter) => chapter.id);' in script
    assert "Pages a charger" in script
    assert "Number(chapter.pages || 0) > 0" not in script


def test_static_history_can_delete_entries():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "data-history-delete" in script
    assert 'id="clear-history"' in script
    assert 'api("/api/history", {' in script
    assert 'method: "DELETE"' in script
    assert "function deleteHistoryEntry(historyKey)" in script
    assert "function clearHistory()" in script
    assert ".history-main" in styles
    assert ".history-actions" in styles
