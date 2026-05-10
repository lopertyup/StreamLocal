from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import ScanService
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore


class StubVideoProviders(ProviderService):
    def list_providers(self):
        return []

    def search(self, query, provider_id=None, content_type=None):
        del query, provider_id, content_type
        return []


def make_app(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=ScanService(store=store, providers={}),
    )
    return app, app.test_client(), store


def stop_app(app):
    app.config["AUTOFLIX_TRACKER"].stop()
    app.config["AUTOFLIX_DOWNLOADS"].stop()


def progress_payload(key, episode_title):
    return {
        "history_key": key,
        "provider_id": "fake",
        "content_id": "show-1",
        "episode_id": episode_title.lower(),
        "series_title": "Fixture Show",
        "season_title": "Saison 1",
        "episode_title": episode_title,
        "logo_url": "https://img.test/show.jpg",
    }


def test_delete_single_history_entry_updates_home_resume(tmp_path):
    app, client, store = make_app(tmp_path)
    try:
        store.save_progress(progress_payload("fake:show-1:e1", "Episode 1"), current_time=10, duration=100)
        store.save_progress(progress_payload("fake:show-1:e2", "Episode 2"), current_time=20, duration=100)

        home = client.get("/api/home").get_json()
        assert [item["history_key"] for item in home["history"]] == ["fake:show-1:e2", "fake:show-1:e1"]
        assert home["resume"]["history_key"] == "fake:show-1:e2"

        response = client.delete("/api/history", json={"history_key": "fake:show-1:e2"})
        body = response.get_json()
    finally:
        stop_app(app)

    assert response.status_code == 200
    assert body["removed"] == 1
    assert [item["history_key"] for item in body["history"]] == ["fake:show-1:e1"]
    assert body["resume"]["history_key"] == "fake:show-1:e1"


def test_clear_history_requires_explicit_all_flag(tmp_path):
    app, client, store = make_app(tmp_path)
    try:
        store.save_progress(progress_payload("fake:show-1:e1", "Episode 1"), current_time=10, duration=100)

        invalid = client.delete("/api/history", json={})
        response = client.delete("/api/history", json={"all": True})
        body = response.get_json()
    finally:
        stop_app(app)

    assert invalid.status_code == 422
    assert response.status_code == 200
    assert body == {"history": [], "removed": 1, "resume": None}
    assert store.list_history() == []
    assert store.get_last_watched() is None


def test_video_history_percent_uses_current_season_episode(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")

    entry = store.save_progress(
        {
            **progress_payload("fake:show-1:e10", "Episode 10"),
            "episode_number": 10,
            "season_episode_index": 9,
            "season_episode_count": 20,
        },
        current_time=120,
        duration=240,
    )

    assert entry["position"] == 120.0
    assert entry["duration"] == 240.0
    assert entry["season_episode_index"] == 9
    assert entry["season_episode_count"] == 20
    assert entry["series_percent"] == 50.0
    assert entry["percent"] == 50.0


def test_video_history_without_season_count_keeps_playback_percent(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")

    entry = store.save_progress(
        progress_payload("fake:show-1:e1", "Episode 1"),
        current_time=25,
        duration=100,
    )

    assert entry["position"] == 25.0
    assert entry["duration"] == 100.0
    assert entry["percent"] == 25.0
    assert "series_percent" not in entry
