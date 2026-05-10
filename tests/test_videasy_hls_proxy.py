import re
import urllib.parse

from autoflix_cli import proxy as hls_proxy
from autoflix_cli.app import diagnostics
from autoflix_cli.scraping import goldenms


class FakeResponse:
    def __init__(self, text="", json_data=None, status_code=200, headers=None):
        self.text = text
        self._json_data = json_data
        self.status_code = status_code
        self.headers = dict(headers or {})

    def json(self):
        return self._json_data

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield b"segment"


def _proxy_urls(text, endpoint):
    pattern = (
        rf"http://{re.escape(hls_proxy.PROXY_HOST)}:"
        rf"{hls_proxy.PROXY_PORT}/{endpoint}/[^\"'\s]+"
    )
    return re.findall(pattern, text)


def _query_params(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def _pts_bytes(pts):
    return bytes(
        [
            0x20 | (((pts >> 30) & 0x07) << 1) | 0x01,
            (pts >> 22) & 0xFF,
            (((pts >> 15) & 0x7F) << 1) | 0x01,
            (pts >> 7) & 0xFF,
            ((pts & 0x7F) << 1) | 0x01,
        ]
    )


def _ts_packet_with_pts(pts):
    pes = (
        b"\x00\x00\x01"
        + b"\xe0"
        + b"\x00\x00"
        + b"\x80"
        + b"\x80"
        + b"\x05"
        + _pts_bytes(pts)
    )
    packet = bytes([0x47, 0x41, 0x00, 0x10]) + pes
    return packet + (b"\xff" * (188 - len(packet)))


def test_goldenms_videasy_source_marks_context_and_headers(monkeypatch):
    seen_headers = []
    requested_urls = []
    logs = []

    def fake_get(url, headers=None, timeout=None):
        del timeout
        requested_urls.append(url)
        seen_headers.append(dict(headers or {}))
        status = 200 if "/cdn/" in url else 404
        return FakeResponse(text="encrypted", status_code=status)

    def fake_post(url, json=None, timeout=None):
        del url, json, timeout
        return FakeResponse(
            json_data={
                "result": {
                    "sources": [
                        {
                            "quality": "1080p",
                            "url": "https://cdn.videasy.test/hls/master.m3u8?token=abc",
                        }
                    ],
                    "subtitles": [
                        {
                            "language": "fr",
                            "label": "Français",
                            "url": "https://cdn.videasy.test/subs/fr.vtt",
                        }
                    ],
                }
            }
        )

    monkeypatch.setattr(goldenms.scraper, "get", fake_get)
    monkeypatch.setattr(goldenms.scraper, "post", fake_post)
    monkeypatch.setattr(
        goldenms.diagnostics,
        "info",
        lambda component, action, status="ok", **fields: logs.append((component, action, status, fields)),
    )

    extractor = goldenms.MediaExtractor()
    extractor.videasy_api = "https://api.videasy.test"
    results = extractor.search_videasy("Terminator 2", tmdb_id="280", year="1991")

    assert len(results) == 1
    source = results[0]
    assert source["stream_context"] == "videasy"
    assert source["headers"]["Referer"] == "https://player.videasy.net/"
    assert source["headers"]["Origin"] == "https://player.videasy.net"
    assert source["headers"]["User-Agent"]
    assert source["subtitles"] == [
        {
            "lang": "fr",
            "label": "Français",
            "url": "https://cdn.videasy.test/subs/fr.vtt",
            "subtitle_source": "videasy_api",
        }
    ]
    assert seen_headers[-1]["Referer"] == "https://player.videasy.net/"
    assert "/cdn/" in requested_urls[0]
    assert not any("/1movies/" in url for url in requested_urls)

    api_log = [item for item in logs if item[1] == "videasy_api_response"][0]
    assert api_log[0] == "PROVIDER"
    assert api_log[2] == "ok"
    assert api_log[3]["provider"] == "GoldenMS"
    assert api_log[3]["server"] == "cdn"
    assert api_log[3]["has_subtitles"] is True
    assert api_log[3]["subtitle_sample"]["lang"] == "fr"

    normalized = hls_proxy._normalize_hls_headers(
        source["url"],
        source["headers"],
        route="stream",
        context=source["stream_context"],
    )
    assert normalized["Sec-Fetch-Dest"] == "empty"
    assert normalized["Sec-Fetch-Mode"] == "cors"
    assert normalized["Sec-Fetch-Site"] == "cross-site"


def test_videasy_segment_detects_pts_and_records_latest(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    with hls_proxy._hls_sessions_lock:
        hls_proxy._hls_sessions.clear()
    with hls_proxy._hls_pts_lock:
        hls_proxy._latest_videasy_pts_start = None

    segment = _ts_packet_with_pts(900000)
    logs = []

    class SegmentResponse:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size=8192):
            del chunk_size
            yield segment

    def fake_fetch(url, headers, method="GET", stream=False, max_retries=3, **kwargs):
        del url, headers, method, stream, max_retries, kwargs
        return SegmentResponse()

    def fake_info(component, action, status="ok", **fields):
        logs.append((component, action, status, fields))

    monkeypatch.setattr(hls_proxy, "fetch_with_retry", fake_fetch)
    monkeypatch.setattr(hls_proxy.diagnostics, "info", fake_info)

    response = hls_proxy.app.test_client().get(
        "/ts/seg-1.ts",
        query_string={
            "url": "https://yoru.midwesteagle.com/hls/seg-1.ts",
            "headers": hls_proxy.encode_headers_token(
                {
                    "Referer": "https://player.videasy.net/",
                    "Origin": "https://player.videasy.net",
                }
            ),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    assert response.data == segment
    pts_log = [item for item in logs if item[1] == "pts_detected"][0]
    assert pts_log[0] == "PROXY"
    assert pts_log[3]["upstream_domain"] == "yoru.midwesteagle.com"
    assert pts_log[3]["pts_start"] == 900000
    assert pts_log[3]["pts_offset_s"] == 10.0
    assert hls_proxy.get_videasy_pts_start() == 900000


def test_videasy_context_forces_player_headers_browser_ua_and_no_range():
    normalized = hls_proxy._normalize_hls_headers(
        "https://cdn.videasy.test/hls/master.m3u8",
        {
            "User-Agent": "python-httpx/0.28",
            "Referer": "https://cineby.gd/",
            "Origin": "https://cineby.gd",
            "Range": "bytes=0-99",
        },
        route="ts",
        context="videasy",
    )

    assert normalized["User-Agent"].startswith("Mozilla/5.0")
    assert normalized["Referer"] == "https://player.videasy.net/"
    assert normalized["Origin"] == "https://player.videasy.net"
    assert normalized["Sec-Fetch-Dest"] == "empty"
    assert "Range" not in normalized


def test_videasy_master_playlist_propagates_headers_to_variants(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    master_url = "https://cdn.videasy.test/path/master.m3u8?token=master"
    master = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=800000",
            "variant/low.m3u8?token=one.two&expires=9",
            "",
        ]
    )
    fetched = []

    def fake_fetch(url, headers, method="GET", stream=False, max_retries=3, **kwargs):
        del method, stream, max_retries, kwargs
        fetched.append((url, dict(headers)))
        return FakeResponse(text=master)

    monkeypatch.setattr(hls_proxy, "fetch_with_retry", fake_fetch)
    headers = {
        "User-Agent": "Test UA",
        "Accept": "*/*",
        "Referer": "https://player.videasy.net/",
        "Origin": "https://player.videasy.net",
    }
    response = hls_proxy.app.test_client().get(
        "/stream/master.m3u8",
        query_string={
            "url": master_url,
            "headers": hls_proxy.encode_headers_token(headers),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    assert fetched[0][1]["Sec-Fetch-Dest"] == "empty"

    variant_urls = _proxy_urls(response.get_data(as_text=True), "stream")
    assert len(variant_urls) == 1
    params = _query_params(variant_urls[0])
    assert params["context"] == ["videasy"]
    assert params["url"] == [
        "https://cdn.videasy.test/path/variant/low.m3u8?token=one.two&expires=9"
    ]
    decoded_headers = hls_proxy.decode_headers_param(params["headers"][0])
    assert decoded_headers == fetched[0][1]


def test_videasy_master_playlist_logs_and_rewrites_subtitle_media(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    master_url = "https://yoru.midwesteagle.com/path/master.m3u8?token=master"
    master = "\n".join(
        [
            "#EXTM3U",
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="subs/en.m3u8?token=sub"',
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Audio",URI="audio/main.m3u8"',
            '#EXT-X-STREAM-INF:BANDWIDTH=800000,SUBTITLES="subs"',
            "variant/low.m3u8",
            "",
        ]
    )
    logs = []

    def fake_fetch(url, headers, method="GET", stream=False, max_retries=3, **kwargs):
        del url, headers, method, stream, max_retries, kwargs
        return FakeResponse(text=master)

    def fake_info(component, action, status="ok", **fields):
        logs.append((component, action, status, fields))

    monkeypatch.setattr(hls_proxy, "fetch_with_retry", fake_fetch)
    monkeypatch.setattr(hls_proxy.diagnostics, "info", fake_info)

    response = hls_proxy.app.test_client().get(
        "/stream/master.m3u8",
        query_string={
            "url": master_url,
            "headers": hls_proxy.encode_headers_token(
                {
                    "Referer": "https://player.videasy.net/",
                    "Origin": "https://player.videasy.net",
                }
            ),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    stream_urls = _proxy_urls(response.get_data(as_text=True), "stream")
    params_by_url = [_query_params(url)["url"][0] for url in stream_urls]
    assert "https://yoru.midwesteagle.com/path/subs/en.m3u8?token=sub" in params_by_url
    assert "https://yoru.midwesteagle.com/path/variant/low.m3u8" in params_by_url

    subtitle_logs = [item for item in logs if item[1] == "subtitles_found"]
    assert len(subtitle_logs) == 1
    assert subtitle_logs[0][3]["upstream_domain"] == "yoru.midwesteagle.com"
    assert subtitle_logs[0][3]["count"] == 1
    assert subtitle_logs[0][3]["tracks"] == [
        {
            "name": "English",
            "lang": "en",
            "uri": "https://yoru.midwesteagle.com/path/subs/en.m3u8?token=sub",
        }
    ]


def test_videasy_playlist_logs_zero_subtitles_when_absent(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    master_url = "https://floral.tylerfisher55.workers.dev/session/master-token"
    master = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=800000",
            "variant/low.m3u8",
            "",
        ]
    )
    logs = []

    def fake_fetch(url, headers, method="GET", stream=False, max_retries=3, **kwargs):
        del url, headers, method, stream, max_retries, kwargs
        return FakeResponse(text=master)

    def fake_info(component, action, status="ok", **fields):
        logs.append((component, action, status, fields))

    monkeypatch.setattr(hls_proxy, "fetch_with_retry", fake_fetch)
    monkeypatch.setattr(hls_proxy.diagnostics, "info", fake_info)

    response = hls_proxy.app.test_client().get(
        "/stream/master.m3u8",
        query_string={
            "url": master_url,
            "headers": hls_proxy.encode_headers_token(
                {
                    "Referer": "https://player.videasy.net/",
                    "Origin": "https://player.videasy.net",
                }
            ),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    subtitle_log = [item for item in logs if item[1] == "subtitles_found"][0]
    assert subtitle_log[3]["upstream_domain"] == "floral.tylerfisher55.workers.dev"
    assert subtitle_log[3]["count"] == 0
    assert subtitle_log[3]["reason"] == "not_in_manifest"


def test_videasy_media_playlist_propagates_headers_to_segments_keys_and_init(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    media_url = "https://cdn.videasy.test/hls/variant/index.m3u8?token=playlist"
    media = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-TARGETDURATION:6",
            '#EXT-X-KEY:METHOD=AES-128,URI="../keys/key.bin?kt=abc.def"',
            '#EXT-X-MAP:URI="init.mp4?it=123"',
            "#EXTINF:6,",
            "seg-1.ts?sig=a.b-c&expires=1",
            "#EXTINF:6,",
            "https://cdn.videasy.test/hls/variant/seg-2.m4s?token=keep&x=1",
            "#EXT-X-ENDLIST",
            "",
        ]
    )
    fetched = []

    def fake_fetch(url, headers, method="GET", stream=False, max_retries=3, **kwargs):
        del method, stream, max_retries, kwargs
        fetched.append((url, dict(headers)))
        return FakeResponse(text=media)

    monkeypatch.setattr(hls_proxy, "fetch_with_retry", fake_fetch)
    headers = {
        "User-Agent": "Test UA",
        "Accept": "*/*",
        "Referer": "https://player.videasy.net/",
        "Origin": "https://player.videasy.net",
    }
    response = hls_proxy.app.test_client().get(
        "/stream/index.m3u8",
        query_string={
            "url": media_url,
            "headers": hls_proxy.encode_headers_token(headers),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    proxied_urls = _proxy_urls(response.get_data(as_text=True), "ts")
    assert len(proxied_urls) == 4

    decoded_header_sets = []
    target_urls = []
    for proxied_url in proxied_urls:
        params = _query_params(proxied_url)
        assert params["context"] == ["videasy"]
        decoded_header_sets.append(hls_proxy.decode_headers_param(params["headers"][0]))
        target_urls.append(params["url"][0])

    assert all(headers == decoded_header_sets[0] for headers in decoded_header_sets)
    assert decoded_header_sets[0] == fetched[0][1]
    assert "https://cdn.videasy.test/hls/keys/key.bin?kt=abc.def" in target_urls
    assert "https://cdn.videasy.test/hls/variant/init.mp4?it=123" in target_urls
    assert "https://cdn.videasy.test/hls/variant/seg-1.ts?sig=a.b-c&expires=1" in target_urls
    assert "https://cdn.videasy.test/hls/variant/seg-2.m4s?token=keep&x=1" in target_urls


def test_videasy_raw_rewrite_preserves_segment_path_token(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    manifest_token = "m" * 496
    segment_token = "s" * 453
    media_url = f"https://floral.tylerfisher55.workers.dev/{uuid}/{manifest_token}"
    segment_url = f"https://floral.tylerfisher55.workers.dev/s/{uuid}/{segment_token}"
    media = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-TARGETDURATION:6",
            "#EXTINF:6,",
            f"/s/{uuid}/{segment_token}",
            "",
        ]
    )

    def fake_fetch(url, headers, method="GET", stream=False, max_retries=3, **kwargs):
        del url, headers, method, stream, max_retries, kwargs
        return FakeResponse(text=media)

    monkeypatch.setattr(hls_proxy, "fetch_with_retry", fake_fetch)
    response = hls_proxy.app.test_client().get(
        "/stream/index.m3u8",
        query_string={
            "url": media_url,
            "headers": hls_proxy.encode_headers_token(
                {
                    "Referer": "https://player.videasy.net/",
                    "Origin": "https://player.videasy.net",
                }
            ),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    proxied_urls = _proxy_urls(response.get_data(as_text=True), "ts")
    assert len(proxied_urls) == 1
    assert _query_params(proxied_urls[0])["url"] == [segment_url]


def test_videasy_reuses_same_upstream_session_for_manifest_and_segment(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    with hls_proxy._hls_sessions_lock:
        hls_proxy._hls_sessions.clear()

    media_url = "https://floral.tylerfisher55.workers.dev/session/manifest-token"
    segment_url = "https://floral.tylerfisher55.workers.dev/s/session/segment-token"
    media = "\n".join(["#EXTM3U", "#EXTINF:6,", "/s/session/segment-token", ""])
    sessions = []
    calls = []

    class FakeSession:
        def __init__(self, session_id):
            self.session_id = session_id
            self.headers = {}

        def request(self, method, url, headers=None, stream=False, timeout=None):
            del method, timeout
            calls.append((self.session_id, url, stream, dict(headers or {})))
            if url == media_url:
                return FakeResponse(text=media)
            if url == segment_url:
                return FakeResponse(status_code=200)
            return FakeResponse(status_code=404)

        def close(self):
            pass

    def fake_create_session(headers=None):
        session = FakeSession(len(sessions) + 1)
        session.headers.update(headers or {})
        sessions.append(session)
        return session

    monkeypatch.setattr(hls_proxy, "create_session", fake_create_session)
    client = hls_proxy.app.test_client()
    response = client.get(
        "/stream/index.m3u8",
        query_string={
            "url": media_url,
            "headers": hls_proxy.encode_headers_token(
                {
                    "Referer": "https://player.videasy.net/",
                    "Origin": "https://player.videasy.net",
                }
            ),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    proxied_url = _proxy_urls(response.get_data(as_text=True), "ts")[0]
    params = _query_params(proxied_url)
    assert params["url"] == [segment_url]
    assert params[hls_proxy._HLS_SESSION_PARAM]

    parsed = urllib.parse.urlparse(proxied_url)
    segment_response = client.get(f"{parsed.path}?{parsed.query}")

    assert segment_response.status_code == 200
    assert len(sessions) == 1
    assert [call[1] for call in calls] == [media_url, segment_url]
    assert {call[0] for call in calls} == {1}


def test_videasy_segment_reuses_manifest_cookies_and_drops_client_range(monkeypatch):
    monkeypatch.setattr(hls_proxy, "PROXY_PORT", 45678)
    with hls_proxy._hls_sessions_lock:
        hls_proxy._hls_sessions.clear()

    media_url = "https://floral.tylerfisher55.workers.dev/session/manifest-token"
    segment_url = "https://floral.tylerfisher55.workers.dev/s/session/segment-token"
    media = "\n".join(["#EXTM3U", "#EXTINF:6,", "/s/session/segment-token", ""])
    sessions = []
    calls = []

    class CookieSession:
        def __init__(self, session_id):
            self.session_id = session_id
            self.headers = {}
            self.cookies = {}

        def request(self, method, url, headers=None, stream=False, timeout=None):
            del method, timeout
            outgoing = dict(headers or {})
            if self.cookies:
                outgoing["Cookie"] = "; ".join(
                    f"{key}={value}" for key, value in self.cookies.items()
                )
            calls.append((self.session_id, url, stream, outgoing))
            if url == media_url:
                self.cookies["videasy_session"] = "abc123"
                return FakeResponse(
                    text=media,
                    headers={"Set-Cookie": "videasy_session=abc123; Path=/; HttpOnly"},
                )
            if url == segment_url:
                status = 200 if outgoing.get("Cookie") == "videasy_session=abc123" and "Range" not in outgoing else 403
                return FakeResponse(status_code=status)
            return FakeResponse(status_code=404)

        def close(self):
            pass

    def fake_create_session(headers=None):
        session = CookieSession(len(sessions) + 1)
        session.headers.update(headers or {})
        sessions.append(session)
        return session

    monkeypatch.setattr(hls_proxy, "create_session", fake_create_session)
    client = hls_proxy.app.test_client()
    response = client.get(
        "/stream/index.m3u8",
        query_string={
            "url": media_url,
            "headers": hls_proxy.encode_headers_token(
                {
                    "Referer": "https://cineby.gd/",
                    "Origin": "https://cineby.gd",
                }
            ),
            "context": "videasy",
        },
    )

    assert response.status_code == 200
    proxied_url = _proxy_urls(response.get_data(as_text=True), "ts")[0]
    parsed = urllib.parse.urlparse(proxied_url)
    segment_response = client.get(
        f"{parsed.path}?{parsed.query}",
        headers={"Range": "bytes=0-1"},
    )

    assert segment_response.status_code == 200
    assert len(sessions) == 1
    manifest_headers = calls[0][3]
    segment_headers = calls[1][3]
    for header in (
        "User-Agent",
        "Accept",
        "Referer",
        "Origin",
        "Sec-Fetch-Dest",
        "Sec-Fetch-Mode",
        "Sec-Fetch-Site",
    ):
        assert segment_headers[header] == manifest_headers[header]
    assert segment_headers["Referer"] == "https://player.videasy.net/"
    assert segment_headers["Origin"] == "https://player.videasy.net"
    assert segment_headers["Cookie"] == "videasy_session=abc123"
    assert "Range" not in segment_headers


def test_videasy_url_diagnostics_mask_tokens_and_show_expiry():
    fields = hls_proxy._url_diagnostic_fields(
        (
            "https://floral.tylerfisher55.workers.dev/s/"
            "123e4567-e89b-12d3-a456-426614174000/"
            + ("p" * 453)
            + "?token=secret-token&expires=4102444800&sig=abc.def.ghi"
        )
    )
    sanitized = diagnostics.sanitize_fields(fields)
    text = repr(sanitized)

    assert "secret-token" not in text
    assert "abc.def.ghi" not in text
    assert "p" * 80 not in text
    assert sanitized["upstream_url"].endswith("?...")
    assert sanitized["query_keys"] == ["token", "expires", "sig"]
    assert sanitized["signed_query_keys"] == ["token", "expires", "sig"]
    assert any(
        item["param"] == "expires" and "expires_in_s" in item["value"]
        for item in sanitized["query_meta"]
    )
