from autoflix_cli.app.scans import LelScansProvider, ScanService
from autoflix_cli.scraping import lelscans


class FakeResponse:
    def __init__(self, text, url="https://lelscans.net/lecture-en-ligne-one-piece", status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


INDEX_HTML = """
<html>
  <body>
    <select>
      <option value="https://lelscans.net/scan-one-piece/1182">1182</option>
    </select>
    <select>
      <option value="https://lelscans.net/lecture-ligne-assassination-classroom.php">Assassination Classroom</option>
      <option value="https://lelscans.net/lecture-en-ligne-one-piece">One Piece</option>
    </select>
  </body>
</html>
"""


DETAIL_HTML = """
<html>
  <head>
    <title>One Piece lecture en ligne scan</title>
    <meta name="lelscan" content="One Piece">
    <meta property="og:image" content="/mangas/one-piece/thumb_cover.jpg">
  </head>
  <body>
    <div id="header-image">
      <a href="https://lelscans.net/lecture-en-ligne-one-piece">Lecture en ligne One Piece</a>
    </div>
    <select>
      <option value="https://lelscans.net/scan-one-piece/1182">1182</option>
      <option value="/scan-one-piece/1000.5">1000.5</option>
      <option value="/scan-one-piece/680">680</option>
    </select>
  </body>
</html>
"""


CHAPTER_HTML = """
<html>
  <body>
    <div id="image">
      <img src="/mangas/one-piece/1019/00.jpg?v=fr1626419567">
    </div>
    <div id="navigation">
      <a href="/scan-one-piece/1019/1">1</a>
      <a href="/scan-one-piece/1019/2">2</a>
      <a href="/scan-one-piece/1019/3">3</a>
    </div>
  </body>
</html>
"""


def test_lelscans_scraper_searches_manga_list(monkeypatch):
    monkeypatch.setattr(
        lelscans.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(INDEX_HTML, url=url),
    )

    results = lelscans.search_manga("one piece")

    assert len(results) == 1
    assert results[0].title == "One Piece"
    assert results[0].detail_url == "https://lelscans.net/lecture-en-ligne-one-piece"
    assert results[0].cover_url == "https://lelscans.net/mangas/one-piece/thumb_cover.jpg"


def test_lelscans_scraper_extracts_details_and_chapters(monkeypatch):
    monkeypatch.setattr(
        lelscans.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(DETAIL_HTML, url=url),
    )

    info = lelscans.get_manga_info("https://lelscans.net/scan-one-piece/1019")
    chapters = lelscans.get_chapters(info.detail_url)

    assert info.title == "One Piece"
    assert info.cover_url == "https://lelscans.net/mangas/one-piece/thumb_cover.jpg"
    assert info.detail_url == "https://lelscans.net/lecture-en-ligne-one-piece"
    assert [chapter.chapter for chapter in chapters] == ["680", "1000.5", "1182"]
    assert all(chapter.pages == 1 for chapter in chapters)


def test_lelscans_scraper_generates_page_urls_from_navigation(monkeypatch):
    monkeypatch.setattr(
        lelscans.scraper,
        "get",
        lambda url, **kwargs: FakeResponse(CHAPTER_HTML, url=url),
    )

    pages = lelscans.get_pages("https://lelscans.net/scan-one-piece/1019")

    assert pages == [
        "https://lelscans.net/mangas/one-piece/1019/00.jpg",
        "https://lelscans.net/mangas/one-piece/1019/01.jpg",
        "https://lelscans.net/mangas/one-piece/1019/02.jpg",
    ]


def test_lelscans_provider_contract(monkeypatch):
    monkeypatch.setattr(
        lelscans,
        "search_manga",
        lambda query: [
            lelscans.LelScansManga(
                title="One Piece",
                detail_url="https://lelscans.net/lecture-en-ligne-one-piece",
                cover_url="https://lelscans.net/mangas/one-piece/thumb_cover.jpg",
                latest_chapter="1019",
            )
        ],
    )
    monkeypatch.setattr(
        lelscans,
        "get_manga_info",
        lambda url: lelscans.LelScansManga(
            title="One Piece",
            detail_url="https://lelscans.net/lecture-en-ligne-one-piece",
            cover_url="https://lelscans.net/mangas/one-piece/thumb_cover.jpg",
        ),
    )
    monkeypatch.setattr(
        lelscans,
        "get_chapters",
        lambda url: [
            lelscans.LelScansChapter(
                title="Chapitre 1019",
                chapter="1019",
                url="https://lelscans.net/scan-one-piece/1019",
                pages=1,
            )
        ],
    )
    monkeypatch.setattr(
        lelscans,
        "get_pages",
        lambda url: ["https://lelscans.net/mangas/one-piece/1019/00.jpg"],
    )

    provider = LelScansProvider()
    summary = provider.search("one piece")[0]
    details = provider.get_details(summary.content_id)
    pages = provider.get_pages(summary.content_id, details.chapters[0].id)

    assert "lelscans" in ScanService(providers=None).providers
    assert summary.media_kind == "scan"
    assert summary.subtitle == "Dernier chapitre 1019"
    assert details.title == "One Piece"
    assert details.chapters[0].chapter == "1019"
    assert pages[0].url == "https://lelscans.net/mangas/one-piece/1019/00.jpg"
