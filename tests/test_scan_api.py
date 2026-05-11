from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import (
    AnimeSamaMangaProvider,
    ScanChapter,
    ScanDetails,
    ScanPage,
    ScanProviderInfo,
    ScanService,
    ScanSummary,
)
from autoflix_cli.app.encoding import encode_payload
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore
from autoflix_cli.scraping import manga


class StubVideoProviders(ProviderService):
    def list_providers(self):
        return []

    def search(self, query, provider_id=None, content_type=None):
        del query, provider_id, content_type
        return []


class FakeScanProvider:
    info = ScanProviderInfo(
        id="fake",
        name="Fake Scan",
        label="Fake Scan",
        languages=["fr", "en"],
    )

    def search(self, query, language="fr"):
        return [
            ScanSummary(
                provider_id="fake",
                provider_name="Fake Scan",
                content_id="manga-1",
                title=f"{query} {language}",
                image="https://img.test/cover.jpg",
                languages=[language],
            )
        ]

    def get_details(self, content_id, language="fr"):
        return ScanDetails(
            provider_id="fake",
            provider_name="Fake Scan",
            content_id=content_id,
            title="Fixture Manga",
            image="https://img.test/cover.jpg",
            languages=[language],
            chapters=[
                ScanChapter(
                    id="chapter-1",
                    title="Ch. 1",
                    chapter="1",
                    language=language,
                    pages=2,
                )
            ],
        )

    def get_chapters(self, content_id, language="fr"):
        return self.get_details(content_id, language).chapters

    def get_pages(self, content_id, chapter_id, quality="data"):
        del content_id, chapter_id
        return [
            ScanPage(index=0, url=f"https://img.test/{quality}/1.jpg", filename="1.jpg", quality=quality),
            ScanPage(index=1, url=f"https://img.test/{quality}/2.jpg", filename="2.jpg", quality=quality),
        ]


def test_default_scan_registry_only_includes_supported_sources():
    assert list(ScanService(providers=None).providers) == ["anime_sama", "lelscans", "scan_manga"]


def test_scan_api_routes_and_progress(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")
    scan_service = ScanService(store=store, providers={"fake": FakeScanProvider()})
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=scan_service,
    )
    client = app.test_client()

    try:
        providers = client.get("/api/scans/providers")
        assert providers.status_code == 200
        assert providers.get_json()["providers"][0]["id"] == "fake"

        search = client.get("/api/scans/search?q=naruto&provider=fake&language=fr")
        assert search.status_code == 200
        result = search.get_json()["results"][0]
        assert result["media_kind"] == "scan"
        assert result["content_type"] == "manga"

        details = client.get("/api/scans/fake/manga-1?language=fr")
        assert details.status_code == 200
        body = details.get_json()
        assert body["title"] == "Fixture Manga"
        assert body["chapters"][0]["progress"] is None

        pages = client.get("/api/scans/fake/manga-1/chapters/chapter-1/pages?quality=data-saver")
        assert pages.status_code == 200
        assert pages.get_json()["pages"][0]["url"] == "https://img.test/data-saver/1.jpg"

        progress = client.post(
            "/api/scans/progress",
            json={
                "provider_id": "fake",
                "provider_name": "Fake Scan",
                "content_id": "manga-1",
                "title": "Fixture Manga",
                "chapter_id": "chapter-1",
                "chapter_title": "Ch. 1",
                "page_index": 1,
                "page_count": 2,
            },
        )
        assert progress.status_code == 200
        assert progress.get_json()["progress"]["percent"] == 100.0

        details = client.get("/api/scans/fake/manga-1?language=fr").get_json()
        assert details["chapters"][0]["progress"]["current_page"] == 2
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()


def test_anime_sama_scan_provider_preserves_variant_scan_url(monkeypatch):
    variant_url = "https://anime-sama.test/catalogue/one-piece/scan_noir-et-blanc/vf/"
    calls = []

    def fake_info(url):
        calls.append(url)
        return manga.MangaInfo(
            title="One Piece",
            catalogue_url="https://anime-sama.test/catalogue/one-piece/",
            scan_url=variant_url,
            cover_url="https://cdn.test/one-piece.jpg",
            slug="one-piece",
        )

    monkeypatch.setattr(manga, "get_manga_info", fake_info)
    monkeypatch.setattr(manga, "fetch_chapters", lambda url: [["https://img.test/1.jpg"]])

    provider = AnimeSamaMangaProvider()
    content_id = encode_payload(
        {
            "catalogue_url": "https://anime-sama.test/catalogue/one-piece/",
            "scan_url": variant_url,
            "title": "One Piece - Scans (noir et blanc)",
            "cover_url": "https://cdn.test/one-piece.jpg",
            "slug": "one-piece",
        }
    )

    details = provider.get_details(content_id)
    pages = provider.get_pages(content_id, "0")

    assert calls == [variant_url, variant_url]
    assert details.chapters[0].id == "0"
    assert pages[0].url == "https://img.test/1.jpg"
