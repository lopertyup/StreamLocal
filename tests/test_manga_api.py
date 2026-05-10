from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import ScanService
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore
from autoflix_cli.scraping import manga


class StubVideoProviders(ProviderService):
    def list_providers(self):
        return []

    def search(self, query, provider_id=None, content_type=None):
        del query, provider_id, content_type
        return []


class FakeImageResponse:
    status_code = 200
    headers = {"Content-Type": "image/jpeg", "Content-Length": "7"}

    def iter_content(self, chunk_size=16384):
        del chunk_size
        yield b"jpgdata"


def test_manga_api_routes_and_progress(tmp_path, monkeypatch):
    scan_url = "https://anime-sama.test/catalogue/kingdom/scan/vf/"
    page_url = "https://anime-sama.test/s2/scans/Kingdom%20/1/1.jpg"

    monkeypatch.setattr(
        manga,
        "search_manga",
        lambda query: [
            manga.MangaInfo(
                title=f"{query.title()}",
                catalogue_url="https://anime-sama.test/catalogue/kingdom/",
                scan_url=scan_url,
                cover_url="https://cdn.test/kingdom.jpg",
                slug="kingdom",
                genres=["Action"],
                languages=["VF"],
            )
        ],
    )
    monkeypatch.setattr(
        manga,
        "get_manga_info",
        lambda url: manga.MangaInfo(
            title="Kingdom",
            catalogue_url="https://anime-sama.test/catalogue/kingdom/",
            scan_url=scan_url,
            cover_url="https://cdn.test/kingdom.jpg",
            slug="kingdom",
        ),
    )
    monkeypatch.setattr(manga, "fetch_chapters", lambda url: [[page_url], [page_url.replace("/1/", "/2/")]])
    monkeypatch.setattr(manga, "fetch_image", lambda url: FakeImageResponse())

    store = DesktopStore(tmp_path / "desktop.json")
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=ScanService(store=store, providers={}),
    )
    client = app.test_client()

    try:
        search = client.get("/api/manga/search?q=kingdom")
        assert search.status_code == 200
        assert search.get_json()["results"][0]["scan_url"] == scan_url

        info = client.get(f"/api/manga/info?url={scan_url}")
        assert info.status_code == 200
        assert info.get_json()["title"] == "Kingdom"

        chapters = client.get(f"/api/manga/chapters?url={scan_url}")
        assert chapters.status_code == 200
        assert chapters.get_json()["chapters"][0][0] == page_url

        image = client.get(f"/api/manga/image?url={page_url}")
        assert image.status_code == 200
        assert image.content_type == "image/jpeg"
        assert image.data == b"jpgdata"

        saved = client.post(
            "/api/manga/progress",
            json={
                "scan_url": scan_url,
                "title": "Kingdom",
                "cover_url": "https://cdn.test/kingdom.jpg",
                "chapter": 1,
                "page": 3,
            },
        )
        assert saved.status_code == 200
        assert saved.get_json()["progress"]["chapter"] == 1

        progress = client.get(f"/api/manga/progress?url={scan_url}")
        assert progress.status_code == 200
        assert progress.get_json()["progress"]["page"] == 3
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()
