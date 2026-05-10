from autoflix_cli.scraping import manga


class FakeResponse:
    def __init__(self, text="", json_data=None, status_code=200, url="https://anime-sama.test/"):
        self.text = text
        self._json_data = json_data
        self.status_code = status_code
        self.url = url
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


def test_search_manga_filters_scan_results(monkeypatch):
    monkeypatch.setattr(manga, "website_origin", "https://anime-sama.test")
    html = """
    <div id="list_catalog">
      <div class="catalog-card">
        <a href="/catalogue/kingdom/"><img src="/covers/kingdom.jpg"></a>
        <div class="card-content">
          <h2 class="card-title">Kingdom</h2>
          <div class="info-row"><span class="info-label">Genres</span><p class="info-value">Action, Drame</p></div>
          <div class="info-row"><span class="info-label">Types</span><p class="info-value">Anime, Scans</p></div>
          <div class="info-row"><span class="info-label">Langues</span><p class="info-value">VOSTFR, VF</p></div>
        </div>
      </div>
      <div class="catalog-card">
        <a href="/catalogue/other/"><img src="/covers/other.jpg"></a>
        <div class="card-content">
          <h2 class="card-title">Other</h2>
          <div class="info-row"><span class="info-label">Types</span><p class="info-value">Anime</p></div>
        </div>
      </div>
    </div>
    """

    monkeypatch.setattr(manga.scraper, "get", lambda url, **kwargs: FakeResponse(html, url=url))

    results = manga.search_manga("kingdom")

    assert len(results) == 1
    assert results[0].title == "Kingdom"
    assert results[0].cover_url == "https://anime-sama.test/covers/kingdom.jpg"
    assert results[0].scan_url == "https://anime-sama.test/catalogue/kingdom/scan/vf/"


def test_search_manga_expands_scan_variants(monkeypatch):
    monkeypatch.setattr(manga, "website_origin", "https://anime-sama.test")
    search_html = """
    <div id="list_catalog">
      <div class="catalog-card">
        <a href="/catalogue/one-piece/"><img src="/covers/one-piece.jpg"></a>
        <h2 class="card-title">One Piece</h2>
        <div class="info-row"><span class="info-label">Types</span><p class="info-value">Anime, Scans</p></div>
        <div class="info-row"><span class="info-label">Langues</span><p class="info-value">VF</p></div>
      </div>
    </div>
    """
    details_html = """
    <script>
      panneauScan("Scans (couleur)", "scan/vf");
      panneauScan("Scans (noir et blanc)", "scan_noir-et-blanc/vf");
    </script>
    """

    def fake_get(url, **kwargs):
        del kwargs
        if "catalogue/?search=" in url:
            return FakeResponse(search_html, url=url)
        return FakeResponse(details_html, url=url)

    monkeypatch.setattr(manga.scraper, "get", fake_get)

    results = manga.search_manga("one piece")

    assert [item.title for item in results] == [
        "One Piece - Scans (couleur)",
        "One Piece - Scans (noir et blanc)",
    ]
    assert [item.scan_url for item in results] == [
        "https://anime-sama.test/catalogue/one-piece/scan/vf/",
        "https://anime-sama.test/catalogue/one-piece/scan_noir-et-blanc/vf/",
    ]


def test_get_manga_info_extracts_slug_title_and_cover(monkeypatch):
    monkeypatch.setattr(manga, "website_origin", "https://anime-sama.test")
    html = """
    <h4 id="titreOeuvre">Kingdom </h4>
    <img id="coverOeuvre" src="https://cdn.test/kingdom.jpg" alt="Kingdom ">
    """
    monkeypatch.setattr(manga.scraper, "get", lambda url, **kwargs: FakeResponse(html, url=url))

    info = manga.get_manga_info("https://anime-sama.test/catalogue/kingdom/")

    assert info.title == "Kingdom"
    assert info.exact_title == "Kingdom "
    assert info.slug == "kingdom"
    assert info.cover_url == "https://cdn.test/kingdom.jpg"
    assert info.scan_url == "https://anime-sama.test/catalogue/kingdom/scan/vf/"


def test_get_manga_info_preserves_custom_scan_url(monkeypatch):
    monkeypatch.setattr(manga, "website_origin", "https://anime-sama.test")
    html = """
    <h4 id="titreOeuvre">One Piece   </h4>
    <img id="coverOeuvre" src="https://cdn.test/one-piece.jpg" alt="One Piece">
    """
    monkeypatch.setattr(manga.scraper, "get", lambda url, **kwargs: FakeResponse(html, url=url))

    info = manga.get_manga_info("https://anime-sama.test/catalogue/one-piece/scan_noir-et-blanc/vf/")

    assert info.title == "One Piece"
    assert info.catalogue_url == "https://anime-sama.test/catalogue/one-piece/"
    assert info.scan_url == "https://anime-sama.test/catalogue/one-piece/scan_noir-et-blanc/vf/"
    assert info.slug == "one-piece"


def test_fetch_chapters_builds_image_urls_from_api(monkeypatch):
    monkeypatch.setattr(manga, "website_origin", "https://anime-sama.test")
    scan_html = """
    <h3 id="titreOeuvre">Kingdom </h3>
    <div id="scansPlacement"></div>
    """

    def fake_get(url, **kwargs):
        if url.endswith("/s2/scans/get_nb_chap_et_img.php"):
            assert kwargs["params"]["oeuvre"] == "Kingdom "
            return FakeResponse(json_data={"1": 2, "2": 1}, url=url)
        return FakeResponse(scan_html, url=url)

    monkeypatch.setattr(manga.scraper, "get", fake_get)

    chapters = manga.fetch_chapters("https://anime-sama.test/catalogue/kingdom/scan/vf/")

    assert chapters == [
        [
            "https://anime-sama.test/s2/scans/Kingdom%20/1/1.jpg",
            "https://anime-sama.test/s2/scans/Kingdom%20/1/2.jpg",
        ],
        ["https://anime-sama.test/s2/scans/Kingdom%20/2/1.jpg"],
    ]
