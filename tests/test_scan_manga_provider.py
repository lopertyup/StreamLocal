import pytest

from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.scans import ScanMangaProvider, ScanService
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore
from autoflix_cli.app.models import ProviderError
from autoflix_cli.scraping import scan_manga


class FakeResponse:
    def __init__(
        self,
        text="",
        url="https://www.scan-manga.com/?po",
        status_code=200,
        headers=None,
        data=b"jpgdata",
    ):
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=UTF-8"}
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        import json

        return json.loads(self.text)

    def iter_content(self, chunk_size=16384):
        del chunk_size
        yield self._data


class StubVideoProviders(ProviderService):
    def list_providers(self):
        return []

    def search(self, query, provider_id=None, content_type=None):
        del query, provider_id, content_type
        return []


SEARCH_HTML = """
<html>
  <body>
    <article>
      <img src="/covers/kingdom.jpg">
      <a href="/123/Kingdom.html">Kingdom</a>
    </article>
    <article class="premium">
      <a href="/lecture-en-ligne/Kingdom-Chapitre-848-FR_480999.html">Kingdom 848 Premium</a>
    </article>
  </body>
</html>
"""


SEARCH_JSON = """
{
  "genre": {"1": "Action", "3": "Historique"},
  "title": [
    {
      "nom": "",
      "nom_match": "The Third Prince of the Fallen Kingdom Has Regressed",
      "url": "/15415-04603/The-Third-Prince-of-the-Fallen-Kingdom-Has-Regressed.html",
      "demo": "Shonen",
      "type": "Manga",
      "genre": [1],
      "statut": 1,
      "type_lecture": 1,
      "image": "Third_Prince_1.jpg",
      "l_ch": 12,
      "v_l_ch": ""
    },
    {
      "nom": "",
      "nom_match": "Kingdom",
      "url": "/1805-54398/Kingdom.html",
      "demo": "Seinen",
      "type": "Manga",
      "genre": [1, 3],
      "statut": 1,
      "type_lecture": 1,
      "image": "Kingdom_1_1540.jpg",
      "logo": "Kingdom_2_4427.jpg",
      "l_ch": 874,
      "v_l_ch": "Prepublication"
    },
    {
      "nom": "",
      "nom_match": "KINGDOM OF Z",
      "url": "/6135/KINGDOM-OF-Z.html",
      "demo": "Seinen",
      "type": "Manga",
      "genre": [1],
      "statut": 2,
      "type_lecture": 1,
      "image": "Kingdom_Of_Z.jpg",
      "l_ch": 45,
      "v_l_ch": ""
    }
  ]
}
"""


DETAIL_HTML = """
<html>
  <head>
    <title>Kingdom - Scan-Manga</title>
    <meta property="og:image" content="/covers/kingdom.jpg">
    <meta name="description" content="Synopsis Kingdom">
  </head>
  <body>
    <div class="genres"><a href="/genre/action">Action</a></div>
    <a href="/lecture-en-ligne/Kingdom-Chapitre-846-FR_480111.html">Chapitre 846</a>
    <a href="/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html">Chapitre 847</a>
    <a class="premium" href="/lecture-en-ligne/Kingdom-Chapitre-848-FR_480999.html">Chapitre 848 Premium</a>
  </body>
</html>
"""


CHAPTER_HTML = """
<html>
  <body>
    <div id="readerarea">
      <img data-src="/uploads/kingdom/847/001.jpg?token=abc">
      <img src="https://img.scan-manga.com/uploads/kingdom/847/002.webp">
    </div>
  </body>
</html>
"""


DECORATIVE_CHAPTER_HTML = """
<html>
  <head>
    <meta property="og:image" content="https://static.scan-manga.com/img/manga/Kingdom_1_1540.jpg">
  </head>
  <body>
    <img src="https://static.scan-manga.com/img/logo_sm_color.png">
    <img src="https://static.scan-manga.com/img/favicon.png">
  </body>
</html>
"""


PAID_CHAPTER_HTML = """
<html>
  <body>
    <main>Chapitre premium payant disponible avec abonnement.</main>
  </body>
</html>
"""


def test_scan_manga_scraper_searches_api_and_orders_exact_match(monkeypatch):
    monkeypatch.setattr(
        scan_manga.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(SEARCH_JSON, url=url, headers={"Content-Type": "application/json"}),
    )

    results = scan_manga.search_manga("kingdom")

    assert [item.title for item in results] == [
        "Kingdom",
        "KINGDOM OF Z",
        "The Third Prince of the Fallen Kingdom Has Regressed",
    ]
    assert results[0].title == "Kingdom"
    assert results[0].detail_url == "https://www.scan-manga.com/1805-54398/Kingdom.html"
    assert results[0].cover_url == "https://static.scan-manga.com/img/manga/Kingdom_1_1540.jpg"
    assert results[0].latest_chapter == "874"
    assert results[0].genres == ["Action", "Historique"]


def test_scan_manga_scraper_extracts_details_and_filters_paid_chapters(monkeypatch):
    monkeypatch.setattr(
        scan_manga.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(DETAIL_HTML, url=url),
    )

    info = scan_manga.get_manga_info("https://www.scan-manga.com/123/Kingdom.html")
    chapters = scan_manga.get_chapters(info.detail_url)

    assert info.title == "Kingdom"
    assert info.description == "Synopsis Kingdom"
    assert info.cover_url == "https://www.scan-manga.com/covers/kingdom.jpg"
    assert info.latest_chapter == "847"
    assert info.genres == ["Action"]
    assert [chapter.chapter for chapter in chapters] == ["846", "847"]


def test_scan_manga_scraper_supports_direct_chapter_url(monkeypatch):
    monkeypatch.setattr(
        scan_manga.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(CHAPTER_HTML, url=url),
    )

    url = "https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html"
    info = scan_manga.search_manga(url)[0]
    chapters = scan_manga.get_chapters(url)

    assert info.title == "Kingdom"
    assert info.latest_chapter == "847"
    assert chapters[0].chapter == "847"


def test_scan_manga_scraper_extracts_page_images(monkeypatch):
    monkeypatch.setattr(
        scan_manga.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(CHAPTER_HTML, url=url),
    )

    pages = scan_manga.get_pages("https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html")

    assert pages == [
        "https://www.scan-manga.com/uploads/kingdom/847/001.jpg?token=abc",
        "https://img.scan-manga.com/uploads/kingdom/847/002.webp",
    ]


def test_scan_manga_scraper_does_not_return_decorative_images(monkeypatch):
    monkeypatch.setattr(
        scan_manga.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(DECORATIVE_CHAPTER_HTML, url=url),
    )

    with pytest.raises(RuntimeError):
        scan_manga.get_pages("https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html")


def test_scan_manga_builds_page_urls_from_reader_data():
    pages = scan_manga._pages_from_chapter_data(
        {
            "dN": "data2.scan-manga.com",
            "s": "kingdom",
            "v": "31921",
            "c": "480774",
            "p": {
                "2": {"f": "2_hash", "e": "jpg"},
                "1": {"f": "1_hash", "e": "png"},
            },
        }
    )

    assert pages == [
        "https://data2.scan-manga.com/kingdom/31921/480774/1_hash.png",
        "https://data2.scan-manga.com/kingdom/31921/480774/2_hash.jpg",
    ]


def test_scan_manga_retries_chapter_reader_with_fresh_session(monkeypatch):
    attempts = []

    def fake_get_pages_once(url, session=None):
        attempts.append(session)
        if session is None:
            raise scan_manga.ScanMangaCloudflareError("blocked")
        return ["https://data2.scan-manga.com/kingdom/31921/480774/1_hash.png"]

    monkeypatch.setattr(scan_manga, "_get_pages_once", fake_get_pages_once)
    monkeypatch.setattr(scan_manga, "_new_session", lambda profile="chrome": f"session:{profile}")

    pages = scan_manga.get_pages("https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html")

    assert pages == ["https://data2.scan-manga.com/kingdom/31921/480774/1_hash.png"]
    assert attempts == [None, "session:chrome"]


def test_scan_manga_uses_cached_chapter_pages_after_cloudflare_retries(monkeypatch):
    monkeypatch.setattr(
        scan_manga,
        "_get_pages_once",
        lambda url, session=None: (_ for _ in ()).throw(scan_manga.ScanMangaCloudflareError("blocked")),
    )
    monkeypatch.setattr(scan_manga, "_new_session", lambda profile="chrome": f"session:{profile}")
    monkeypatch.setattr(
        scan_manga,
        "_cached_chapter_pages",
        lambda url: ["https://data2.scan-manga.com/kingdom/31921/480774/1_hash.png"],
    )

    pages = scan_manga.get_pages("https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html")

    assert pages == ["https://data2.scan-manga.com/kingdom/31921/480774/1_hash.png"]


def test_scan_manga_fetch_image_uses_cdn_headers(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        return FakeResponse("", url=url, headers={"Content-Type": "image/png"}, data=b"pngdata")

    monkeypatch.setattr(scan_manga.scraper, "get", fake_get)

    response = scan_manga.fetch_image(
        "https://data2.scan-manga.com/kingdom/31921/480774/1_hash.png"
    )

    assert response.status_code == 200
    assert captured["headers"]["Origin"] == "https://www.scan-manga.com"
    assert captured["headers"]["Sec-Fetch-Mode"] == "cors"
    assert captured["headers"]["Sec-Fetch-Dest"] == "empty"
    assert captured["headers"]["Accept"] == "*/*"


def test_scan_manga_scraper_rejects_paid_chapter(monkeypatch):
    monkeypatch.setattr(
        scan_manga.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(PAID_CHAPTER_HTML, url=url),
    )

    with pytest.raises(scan_manga.ScanMangaPaidContentError):
        scan_manga.get_pages("https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-848-FR_480999.html")


def test_scan_manga_provider_contract_and_proxy_urls(monkeypatch):
    monkeypatch.setattr(
        scan_manga,
        "search_manga",
        lambda query: [
            scan_manga.ScanMangaManga(
                title="Kingdom",
                detail_url="https://www.scan-manga.com/123/Kingdom.html",
                cover_url="https://www.scan-manga.com/covers/kingdom.jpg",
                latest_chapter="847",
            )
        ],
    )
    monkeypatch.setattr(
        scan_manga,
        "get_manga_info",
        lambda url: scan_manga.ScanMangaManga(
            title="Kingdom",
            detail_url="https://www.scan-manga.com/123/Kingdom.html",
            cover_url="https://www.scan-manga.com/covers/kingdom.jpg",
        ),
    )
    monkeypatch.setattr(
        scan_manga,
        "get_chapters",
        lambda url: [
            scan_manga.ScanMangaChapter(
                title="Chapitre 847",
                chapter="847",
                url="https://www.scan-manga.com/lecture-en-ligne/Kingdom-Chapitre-847-FR_480774.html",
                pages=1,
            )
        ],
    )
    monkeypatch.setattr(
        scan_manga,
        "get_pages",
        lambda url: ["https://img.scan-manga.com/uploads/kingdom/847/001.jpg"],
    )

    provider = ScanMangaProvider()
    summary = provider.search("kingdom")[0]
    details = provider.get_details(summary.content_id)
    pages = provider.get_pages(summary.content_id, details.chapters[0].id)

    assert "scan_manga" in ScanService(providers=None).providers
    assert summary.media_kind == "scan"
    assert summary.subtitle == "Dernier chapitre 847"
    assert details.chapters[0].chapter == "847"
    assert pages[0].url.startswith("/api/scans/scan_manga/images/proxy?url=")
    assert pages[0].filename == "1.jpg"


def test_scan_manga_provider_maps_cloudflare_error(monkeypatch):
    monkeypatch.setattr(
        scan_manga,
        "search_manga",
        lambda query: (_ for _ in ()).throw(scan_manga.ScanMangaCloudflareError("challenge")),
    )

    with pytest.raises(ProviderError) as exc:
        ScanMangaProvider().search("kingdom")

    assert exc.value.code == "scan_manga_cloudflare"
    assert exc.value.status_code == 502


def test_scan_manga_image_proxy_route(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan_manga,
        "fetch_image",
        lambda url: FakeResponse(
            "",
            url=url,
            headers={"Content-Type": "image/jpeg", "Content-Length": "7"},
            data=b"jpgdata",
        ),
    )

    store = DesktopStore(tmp_path / "desktop.json")
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=ScanService(store=store, providers={}),
    )
    client = app.test_client()

    try:
        image = client.get(
            "/api/scans/scan_manga/images/proxy?url=https%3A%2F%2Fimg.scan-manga.com%2Fuploads%2Fkingdom%2F847%2F001.jpg"
        )

        assert image.status_code == 200
        assert image.content_type == "image/jpeg"
        assert image.data == b"jpgdata"
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()
