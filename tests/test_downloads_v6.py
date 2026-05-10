import json
from pathlib import Path

from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import ScanService
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore
from autoflix_cli.app.downloads import build_output_path, make_dedup_key


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


def download_record(job_id, output_path, state="done"):
    return {
        "id": job_id,
        "title": f"Download {job_id}",
        "state": state,
        "output_path": str(output_path),
        "created_at": "2026-05-10T00:00:00",
    }


def test_default_download_path_is_local_downloads_folder(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    store = DesktopStore(tmp_path / "desktop.json")

    assert Path(store.get_preferences()["download_path"]) == tmp_path / "Téléchargements"


def test_legacy_default_download_path_is_migrated(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    data_file = tmp_path / "desktop.json"
    legacy_path = Path.home() / "Videos" / ("AutoFlix" + "Dev")
    data_file.write_text(json.dumps({"preferences": {"download_path": str(legacy_path)}}), encoding="utf-8")

    store = DesktopStore(data_file)

    assert Path(store.get_preferences()["download_path"]) == tmp_path / "Téléchargements"


def test_custom_download_path_is_preserved(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    data_file = tmp_path / "desktop.json"
    custom_path = tmp_path / "Custom"
    data_file.write_text(json.dumps({"preferences": {"download_path": str(custom_path)}}), encoding="utf-8")

    store = DesktopStore(data_file)

    assert Path(store.get_preferences()["download_path"]) == custom_path


def test_manual_dedup_key_distinguishes_sources():
    progress = {
        "provider_id": "anime_sama",
        "content_id": "anime-1",
        "season_title": "Saison 1",
        "episode_title": "Épisode 6",
        "episode_number": 6,
    }

    lecteur_1 = {
        "source_name": "Lecteur 1",
        "source_type": "player",
        "url": "https://lpayer.embed4me.com/#abc",
    }
    lecteur_4 = {
        "source_name": "Lecteur 4",
        "source_type": "player",
        "url": "https://minochinos.com/embed/abc",
    }

    assert make_dedup_key(progress, lecteur_1) != make_dedup_key(progress, lecteur_4)
    assert make_dedup_key(progress) == make_dedup_key(progress)


def test_output_path_can_include_source_name(tmp_path):
    path = build_output_path(
        tmp_path,
        {
            "provider": "Anime-Sama",
            "series_title": "L'Atelier des Sorciers",
            "season_title": "Saison 1",
            "episode_title": "Épisode 3",
            "episode_number": 3,
        },
        source_name="Lecteur 4",
    )

    assert path == (
        tmp_path
        / "Anime-Sama"
        / "LAtelier des Sorciers"
        / "Saison 1"
        / "E03 - Épisode 3 - Lecteur 4.mp4"
    )


def test_delete_download_removes_file_and_record(tmp_path):
    app, client, store = make_app(tmp_path)
    output = tmp_path / "video.mp4"
    output.write_bytes(b"video")
    store.save_download(download_record("job-1", output))

    try:
        response = client.delete("/api/downloads/job-1")
        body = response.get_json()
    finally:
        stop_app(app)

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["file_deleted"] is True
    assert not output.exists()
    assert store.get_download("job-1") is None


def test_delete_download_protects_active_jobs(tmp_path):
    app, client, store = make_app(tmp_path)
    output = tmp_path / "active.mp4"
    output.write_bytes(b"video")
    store.save_download(download_record("job-1", output, state="downloading"))

    try:
        response = client.delete("/api/downloads/job-1")
    finally:
        stop_app(app)

    assert response.status_code == 409
    assert output.exists()
    assert store.get_download("job-1") is not None


def test_clear_done_removes_finished_files_and_records(tmp_path):
    app, client, store = make_app(tmp_path)
    active_output = tmp_path / "active.mp4"
    active_output.write_bytes(b"video")
    for job_id, state in (("done", "done"), ("cancelled", "cancelled"), ("failed", "failed")):
        output = tmp_path / f"{job_id}.mp4"
        output.write_bytes(b"video")
        store.save_download(download_record(job_id, output, state=state))
    store.save_download(download_record("active", active_output, state="queued"))

    try:
        response = client.post("/api/downloads/clear-done")
        body = response.get_json()
    finally:
        stop_app(app)

    assert response.status_code == 200
    assert body["removed"] == 3
    assert body["files_deleted"] == 3
    assert not (tmp_path / "done.mp4").exists()
    assert not (tmp_path / "cancelled.mp4").exists()
    assert not (tmp_path / "failed.mp4").exists()
    assert active_output.exists()
    assert [job["id"] for job in store.list_downloads()] == ["active"]
