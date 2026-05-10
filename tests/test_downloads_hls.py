import urllib.parse

from autoflix_cli.app import downloads
from autoflix_cli.proxy import decode_headers_param, encode_headers_token


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_resolve_best_hls_variant_rewrites_proxy_to_highest_bandwidth(monkeypatch):
    master_url = "https://cdn.test/hls/master.m3u8?token=master"
    master = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360",
            "low.m3u8",
            "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080",
            "high.m3u8?token=two",
        ]
    )
    headers = {"Referer": "https://autoflix.test/watch"}
    calls = []

    def fake_get(url, headers=None, timeout=None, impersonate=None):
        calls.append((url, headers, timeout, impersonate))
        return FakeResponse(master)

    monkeypatch.setattr(downloads.cffi_requests, "get", fake_get)

    proxy_url = "http://127.0.0.1:12345/stream/master.m3u8?" + urllib.parse.urlencode(
        {
            "url": master_url,
            "headers": encode_headers_token(headers),
        }
    )
    resolved = downloads.resolve_best_hls_variant(proxy_url)

    parsed = urllib.parse.urlparse(resolved)
    params = urllib.parse.parse_qs(parsed.query)

    assert parsed.path == "/stream/high.m3u8"
    assert params["url"] == ["https://cdn.test/hls/high.m3u8?token=two"]
    assert decode_headers_param(params["headers"][0]) == headers
    assert calls == [(master_url, headers, 20, "chrome")]
