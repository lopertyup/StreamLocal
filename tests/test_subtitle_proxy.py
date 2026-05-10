from autoflix_cli import proxy
from autoflix_cli.app import subtitles
from autoflix_cli.app.encoding import encode_payload
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


class FakeSubtitleResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _app(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=ScanService(store=store, providers={}),
    )
    return app


def test_subtitle_proxy_converts_srt_to_webvtt(tmp_path, monkeypatch):
    fetched = []

    def fake_get(url, timeout=None, impersonate=None):
        fetched.append((url, timeout, impersonate))
        return FakeSubtitleResponse(
            b"1\r\n00:00:01,000 --> 00:00:02,500\r\nBonjour\r\n"
        )

    monkeypatch.setattr(subtitles.requests, "get", fake_get)
    app = _app(tmp_path)
    client = app.test_client()
    token = encode_payload({"url": "https://subs.test/movie.srt"})

    try:
        response = client.get(f"/api/subtitles/proxy/{token}.vtt")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert response.status_code == 200
    assert response.content_type.startswith("text/vtt")
    assert response.get_data(as_text=True) == (
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.500\nBonjour\n"
    )
    assert fetched == [("https://subs.test/movie.srt", 10, "chrome")]


def test_subtitle_proxy_disables_browser_cache(tmp_path, monkeypatch):
    def fake_get(url, timeout=None, impersonate=None):
        del url, timeout, impersonate
        return FakeSubtitleResponse(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nBonjour\n")

    monkeypatch.setattr(subtitles.requests, "get", fake_get)
    app = _app(tmp_path)
    client = app.test_client()
    token = encode_payload({"url": "https://subs.test/movie.vtt"})

    try:
        response = client.get(f"/api/subtitles/proxy/{token}.vtt")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"


def test_subtitle_proxy_applies_positive_offset_to_srt(tmp_path, monkeypatch):
    def fake_get(url, timeout=None, impersonate=None):
        del url, timeout, impersonate
        return FakeSubtitleResponse(
            b"1\n00:00:01,000 --> 00:00:02,500\nBonjour\n"
        )

    monkeypatch.setattr(subtitles.requests, "get", fake_get)
    app = _app(tmp_path)
    client = app.test_client()
    token = encode_payload({"url": "https://subs.test/movie.srt"})

    try:
        response = client.get(f"/api/subtitles/proxy/{token}.vtt?offset_ms=2000")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert response.status_code == 200
    assert response.get_data(as_text=True) == (
        "WEBVTT\n\n1\n00:00:03.000 --> 00:00:04.500\nBonjour\n"
    )


def test_subtitle_proxy_clamps_negative_offset_to_zero(tmp_path, monkeypatch):
    def fake_get(url, timeout=None, impersonate=None):
        del url, timeout, impersonate
        return FakeSubtitleResponse(
            b"WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nBonjour\n"
        )

    monkeypatch.setattr(subtitles.requests, "get", fake_get)
    app = _app(tmp_path)
    client = app.test_client()
    token = encode_payload({"url": "https://subs.test/movie.vtt"})

    try:
        response = client.get(f"/api/subtitles/proxy/{token}.vtt?offset_ms=-1500")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert response.status_code == 200
    assert response.get_data(as_text=True) == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nBonjour"
    )


def test_subtitle_proxy_replays_videasy_headers(tmp_path, monkeypatch):
    fetched = []

    def fake_get(url, **kwargs):
        fetched.append((url, kwargs))
        return FakeSubtitleResponse(b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSalut\n")

    monkeypatch.setattr(subtitles.requests, "get", fake_get)
    app = _app(tmp_path)
    client = app.test_client()
    token = encode_payload(
        {
            "url": "https://cdn.videasy.test/subs/fr.vtt",
            "headers": {
                "Referer": "https://player.videasy.net/",
                "Origin": "https://player.videasy.net",
            },
            "context": "videasy",
        }
    )

    try:
        response = client.get(f"/api/subtitles/proxy/{token}.vtt")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert response.status_code == 200
    assert fetched[0][0] == "https://cdn.videasy.test/subs/fr.vtt"
    assert fetched[0][1]["headers"]["Referer"] == "https://player.videasy.net/"
    assert fetched[0][1]["headers"]["Origin"] == "https://player.videasy.net"
    assert fetched[0][1]["headers"]["User-Agent"]


def test_subtitle_proxy_injects_videasy_timestamp_map(tmp_path, monkeypatch):
    def fake_get(url, **kwargs):
        del url, kwargs
        return FakeSubtitleResponse(b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSalut\n")

    monkeypatch.setattr(subtitles.requests, "get", fake_get)
    monkeypatch.setattr(proxy, "get_videasy_pts_start", lambda default=900000: 1234567)
    app = _app(tmp_path)
    client = app.test_client()
    token = encode_payload(
        {
            "url": "https://cdn.videasy.test/subs/fr.vtt",
            "context": "videasy",
        }
    )

    try:
        response = client.get(f"/api/subtitles/proxy/{token}.vtt")
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    assert response.status_code == 200
    assert response.get_data(as_text=True).startswith(
        "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:1234567\n\n"
    )


def test_playback_start_returns_subtitle_tracks_for_open_player(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_URL", "http://127.0.0.1:45678")
    monkeypatch.setattr(proxy, "PROXY_PORT", 45678)
    app = _app(tmp_path)
    client = app.test_client()
    source_id = encode_payload(
        {
            "source_name": "Vidlink",
            "url": "https://stream.vidlink.test/path/master.m3u8",
            "headers": {
                "Referer": "https://vidlink.pro/",
                "Origin": "https://vidlink.pro",
            },
            "is_direct": True,
            "source_type": "M3U8",
            "quality": "Multi",
            "subtitles": [
                {
                    "lang": "fr",
                    "url": "/api/subtitles/proxy/abc.vtt",
                    "subtitle_source": "external",
                }
            ],
            "progress": {},
        }
    )

    try:
        response = client.post("/api/playback/start", json={"source_id": source_id})
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    body = response.get_json()
    assert response.status_code == 200
    assert body["source_name"] == "Vidlink"
    assert body["media_kind"] == "hls"
    assert body["subtitles"] == [
        {
            "lang": "fr",
            "url": "/api/subtitles/proxy/abc.vtt",
            "subtitle_source": "external",
        }
    ]


def test_playback_start_merges_episode_context_into_history(tmp_path):
    store = DesktopStore(tmp_path / "desktop.json")
    app = create_app(
        store=store,
        provider_service=StubVideoProviders(store),
        scan_service=ScanService(store=store, providers={}),
    )
    client = app.test_client()
    source_id = encode_payload(
        {
            "source_name": "Iframe",
            "url": "https://player.test/embed/episode-10",
            "headers": {},
            "is_direct": False,
            "source_type": "iframe",
            "quality": "",
            "subtitles": [],
            "progress": {
                "provider_id": "fake",
                "provider": "Fake",
                "content_id": "show-1",
                "episode_id": "episode-10",
                "series_title": "Fixture Show",
                "season_title": "Saison 1",
                "episode_title": "Episode 10",
                "history_key": "fake:show-1:e10",
            },
        }
    )

    try:
        response = client.post(
            "/api/playback/start",
            json={
                "source_id": source_id,
                "episode_context": {
                    "episode_number": 10,
                    "season_episode_index": 9,
                    "season_episode_count": 20,
                },
            },
        )
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    body = response.get_json()
    entry = store.get_progress_by_history_key("fake:show-1:e10")
    assert response.status_code == 200
    assert body["progress"]["season_episode_index"] == 9
    assert body["progress"]["season_episode_count"] == 20
    assert entry["percent"] == 50.0
    assert entry["series_percent"] == 50.0


def test_playback_start_does_not_rank_or_probe_subtitle_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_URL", "http://127.0.0.1:45678")
    monkeypatch.setattr(proxy, "PROXY_PORT", 45678)
    app = _app(tmp_path)
    client = app.test_client()
    wrong_track_url = subtitles.external_subtitle_track_url("https://subs.test/wrong.srt")
    match_track_url = subtitles.external_subtitle_track_url("https://subs.test/match.srt")
    alternate_track_url = subtitles.external_subtitle_track_url("https://subs.test/alternate.srt")
    source_id = encode_payload(
        {
            "source_name": "Vidlink",
            "url": "https://stream.vidlink.test/path/master.m3u8",
            "headers": {
                "Referer": "https://vidlink.pro/",
                "Origin": "https://vidlink.pro",
            },
            "is_direct": True,
            "source_type": "M3U8",
            "quality": "Multi",
            "subtitles": [
                {
                    "lang": "fr",
                    "url": wrong_track_url,
                    "source": "OpenSubtitles (Stremio)",
                    "g": "999",
                },
                {
                    "lang": "fr",
                    "url": match_track_url,
                    "source": "OpenSubtitles (Stremio)",
                    "g": "1",
                },
                {
                    "lang": "fr",
                    "url": alternate_track_url,
                    "source": "OpenSubtitles (Stremio)",
                    "g": "2",
                },
            ],
            "progress": {},
        }
    )

    try:
        response = client.post("/api/playback/start", json={"source_id": source_id})
    finally:
        app.config["AUTOFLIX_TRACKER"].stop()
        app.config["AUTOFLIX_DOWNLOADS"].stop()

    body = response.get_json()
    assert response.status_code == 200
    assert [track["url"] for track in body["subtitles"]] == [
        wrong_track_url,
        match_track_url,
        alternate_track_url,
    ]


def test_provider_subtitles_keep_original_urls_for_anime_path(tmp_path):
    service = ProviderService(DesktopStore(tmp_path / "desktop.json"))
    source = service._sources_from_stream_dicts(
        "content-token",
        "episode-token",
        {
            "provider_id": "goldenanime",
            "provider": "GoldenAnime",
            "series_title": "Anime",
            "season_title": "VO",
            "episode_title": "Episode 1",
            "language": "vo",
            "episode_number": 1,
        },
        [
            {
                "source": "Animetsu",
                "quality": "1080p",
                "url": "https://stream.anime.test/master.m3u8",
                "type": "M3U8",
                "subtitles": [
                    {"lang": "en", "url": "https://subs.anime.test/episode.vtt"}
                ],
            }
        ],
    )[0]

    assert source.subtitle_source == "provider"
    assert source.subtitles == [
        {"lang": "en", "url": "https://subs.anime.test/episode.vtt"}
    ]
