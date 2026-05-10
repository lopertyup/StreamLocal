from autoflix_cli.app import tracker_service as tracker_service_module
from autoflix_cli.app.models import ContentDetails, Episode, PlayableSource, Season
from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import (
    ScanChapter,
    ScanDetails,
    ScanPage,
    ScanProviderInfo,
    ScanService,
    ScanSummary,
)
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore


class FakeVideoProviders(ProviderService):
    def __init__(self, store, counts=None):
        super().__init__(store)
        self.counts = counts or {"vf": 1}

    def list_providers(self):
        return []

    def search(self, query, provider_id=None, content_type=None):
        del query, provider_id, content_type
        return []

    def get_details(self, provider_id, content_id):
        del provider_id
        return ContentDetails(
            provider_id="fake_video",
            provider_name="Fake Video",
            content_id=content_id,
            title="Fixture Anime",
            content_type="anime",
            image="https://img.test/anime.jpg",
            seasons=[Season(id="season-1", title="Saison 1")],
        )

    def get_season(self, provider_id, content_id, season_id):
        del provider_id, content_id
        episodes = []
        for language, count in self.counts.items():
            for number in range(1, count + 1):
                episodes.append(
                    Episode(
                        id=f"{season_id}-{language}-{number}",
                        title=f"Episode {number}",
                        number=number,
                        language=language,
                    )
                )
        return Season(id=season_id, title="Saison 1", languages=list(self.counts), episodes=episodes)

    def get_sources(self, provider_id, content_id, episode_id):
        del provider_id, content_id
        return [PlayableSource(id=f"source:{episode_id}", name="Source")]


class FakeScanProvider:
    info = ScanProviderInfo(
        id="fake",
        name="Fake Scan",
        label="Fake Scan",
        languages=["fr"],
    )

    def __init__(self, chapter_count=1):
        self.chapter_count = chapter_count

    def search(self, query, language="fr"):
        return [
            ScanSummary(
                provider_id="fake",
                provider_name="Fake Scan",
                content_id="manga-1",
                title=query,
                languages=[language],
            )
        ]

    def get_details(self, content_id, language="fr"):
        return ScanDetails(
            provider_id="fake",
            provider_name="Fake Scan",
            content_id=content_id,
            title="Fixture Manga",
            image="https://img.test/manga.jpg",
            languages=[language],
            chapters=[
                ScanChapter(
                    id=f"chapter-{number}",
                    title=f"Ch. {number}",
                    chapter=str(number),
                    language=language,
                    pages=2,
                )
                for number in range(1, self.chapter_count + 1)
            ],
        )

    def get_chapters(self, content_id, language="fr"):
        return self.get_details(content_id, language).chapters

    def get_pages(self, content_id, chapter_id, quality="data"):
        del content_id, chapter_id, quality
        return [ScanPage(index=0, url="https://img.test/1.jpg")]


def make_client(tmp_path, video_provider=None, scan_provider=None):
    store = DesktopStore(tmp_path / "desktop.json")
    video_service = video_provider or FakeVideoProviders(store)
    scan_service = ScanService(store=store, providers={"fake": scan_provider or FakeScanProvider()})
    app = create_app(
        store=store,
        provider_service=video_service,
        scan_service=scan_service,
    )
    return app, app.test_client(), store, video_service


def stop_app(app):
    app.config["AUTOFLIX_TRACKER"].stop()
    app.config["AUTOFLIX_DOWNLOADS"].stop()


def test_local_video_tracking_bootstraps_without_notification(tmp_path):
    app, client, store, _video = make_client(tmp_path)

    try:
        response = client.post(
            "/api/tracking/fake_video/anime-1/settings",
            json={
                "enabled": True,
                "media_kind": "video",
                "content_type": "anime",
                "provider_name": "Fake Video",
                "title": "Fixture Anime",
            },
        )
    finally:
        stop_app(app)

    assert response.status_code == 200
    tracking = store.get_tracking("fake_video", "anime-1")
    assert tracking["follow_enabled"] is True
    assert tracking["last_available_episode"] == 1
    assert tracking["episode_state"]["season-1|vf"]["last_episode"] == 1
    assert store.data["notifications"] == []


def test_local_video_refresh_detects_language_specific_episode(tmp_path, monkeypatch):
    native_notifications = []
    monkeypatch.setattr(
        tracker_service_module,
        "notify_native",
        lambda title, body="": native_notifications.append((title, body)) or True,
    )
    video = FakeVideoProviders(None, counts={"vf": 1, "vostfr": 1})
    app, client, store, video = make_client(tmp_path, video_provider=video)

    try:
        client.post(
            "/api/tracking/fake_video/anime-1/settings",
            json={
                "enabled": True,
                "media_kind": "video",
                "content_type": "anime",
                "provider_name": "Fake Video",
                "title": "Fixture Anime",
            },
        )
        video.counts = {"vf": 2, "vostfr": 1}
        response = client.post("/api/tracking/refresh")
    finally:
        stop_app(app)

    assert response.status_code == 200
    assert native_notifications == [("Fixture Anime", "Episode 2 disponible (VF)")]
    assert store.data["notifications"][-1]["language"] == "vf"
    assert store.data["notifications"][-1]["episode"] == 2


def test_local_video_auto_download_uses_new_episode_source(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_service_module, "notify_native", lambda title, body="": True)
    video = FakeVideoProviders(None, counts={"vf": 1})
    app, client, store, video = make_client(tmp_path, video_provider=video)
    submitted = []

    try:
        tracker = app.config["AUTOFLIX_TRACKER"]
        tracker._best_source_for_episode = lambda *args, **kwargs: "source:new"
        app.config["AUTOFLIX_DOWNLOADS"].submit = lambda source_id, auto=False: submitted.append((source_id, auto)) or {"id": "job-1"}
        client.post(
            "/api/tracking/fake_video/anime-1/settings",
            json={
                "enabled": True,
                "auto_download": True,
                "media_kind": "video",
                "content_type": "anime",
                "provider_name": "Fake Video",
                "title": "Fixture Anime",
            },
        )
        video.counts = {"vf": 2}
        response = client.post("/api/tracking/refresh")
    finally:
        stop_app(app)

    assert response.status_code == 200
    assert submitted == [("source:new", True)]
    assert store.data["notifications"][-1]["kind"] == "auto_download"


def test_tracking_refresh_detects_new_scan_chapter(tmp_path, monkeypatch):
    native_notifications = []
    monkeypatch.setattr(
        tracker_service_module,
        "notify_native",
        lambda title, body="": native_notifications.append((title, body)) or True,
    )
    app, client, store, _video = make_client(tmp_path, scan_provider=FakeScanProvider(chapter_count=1))

    try:
        response = client.post(
            "/api/tracking/fake/manga-1/settings",
            json={
                "enabled": True,
                "media_kind": "scan",
                "content_type": "manga",
                "provider_name": "Fake Scan",
                "title": "Fixture Manga",
            },
        )
        assert response.status_code == 200
        app.config["AUTOFLIX_SCANS"].providers["fake"].chapter_count = 2
        response = client.post("/api/tracking/refresh")
    finally:
        stop_app(app)

    assert response.status_code == 200
    tracking = store.get_tracking("fake", "manga-1")
    assert tracking["last_available_chapter"] == 2
    assert tracking["last_seen_chapter"] == 2
    assert native_notifications == [("Fixture Manga", "Chapitre 2 disponible")]
    assert store.data["notifications"][-1]["kind"] == "new_scan_chapter"
