import urllib.parse
from pathlib import Path

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


def test_hls_ffmpeg_command_allows_obfuscated_segment_extensions():
    cmd = downloads.build_ffmpeg_command(
        "ffmpeg",
        "hls",
        "http://127.0.0.1:12345/stream/index-f3-v1-a1.txt",
        Path("episode.mp4"),
    )

    option_index = cmd.index("-allowed_segment_extensions")
    assert cmd[option_index + 1] == "ALL"
    picky_index = cmd.index("-extension_picky")
    assert cmd[picky_index + 1] == "0"


def test_hls_ffmpeg_command_can_send_provider_headers():
    cmd = downloads.build_ffmpeg_command(
        "ffmpeg",
        "hls",
        "https://cdn.test/video.m3u8",
        Path("episode.mp4"),
        headers={
            "Origin": "https://player.videasy.net",
            "Referer": "https://player.videasy.net/",
        },
    )

    headers_index = cmd.index("-headers")
    input_index = cmd.index("-i")

    assert headers_index < input_index
    assert "Origin: https://player.videasy.net\r\n" in cmd[headers_index + 1]
    assert "Referer: https://player.videasy.net/\r\n" in cmd[headers_index + 1]


def test_videasy_download_uses_passthrough_hls_without_changing_playback_url():
    playback = {
        "stream_url": "http://127.0.0.1:12345/stream/video.m3u8?url=proxied",
        "passthrough_stream_url": "https://yoru.midwesteagle.com/video.m3u8?q=token",
        "media_kind": "hls",
    }
    source_payload = {
        "stream_context": "videasy",
        "headers": {"Referer": "https://player.videasy.net/"},
    }

    stream_url, media_kind, headers = downloads.download_stream_from_playback(
        playback,
        source_payload,
    )

    assert stream_url == "https://yoru.midwesteagle.com/video.m3u8?q=token"
    assert media_kind == "hls"
    assert headers == {"Referer": "https://player.videasy.net/"}
