from autoflix_cli import proxy
from autoflix_cli.app.encoding import encode_payload
from autoflix_cli.app.playback import prepare_playback


def test_videasy_hls_playback_exposes_browser_passthrough(monkeypatch):
    upstream_url = "https://floral.tylerfisher55.workers.dev/session/manifest-token"
    monkeypatch.setattr(proxy, "PROXY_URL", "http://127.0.0.1:45678")
    monkeypatch.setattr(proxy, "PROXY_PORT", 45678)

    source_id = encode_payload(
        {
            "source_name": "Videasy (1MOVIES)",
            "url": upstream_url,
            "headers": {
                "Referer": "https://player.videasy.net/",
                "Origin": "https://player.videasy.net",
            },
            "is_direct": True,
            "source_type": "M3U8",
            "stream_context": "videasy",
            "quality": "360p",
            "subtitles": [],
            "progress": {},
        }
    )

    playback = prepare_playback(source_id)

    assert playback["media_kind"] == "hls"
    assert playback["stream_url"].startswith("http://127.0.0.1:45678/stream/")
    assert playback["passthrough_stream_url"] == upstream_url
    assert playback["passthrough_mode"] == "browser_direct_hls"


def test_iframe_playback_does_not_auto_inject_saved_progress():
    source_id = encode_payload(
        {
            "source_name": "Iframe",
            "url": "https://player.test/embed/abc",
            "headers": {},
            "is_direct": False,
            "source_type": "iframe",
            "quality": "",
            "subtitles": [],
            "progress": {"position": 120, "duration": 240},
        }
    )

    playback = prepare_playback(source_id)

    assert playback["media_kind"] == "iframe"
    assert playback["stream_url"] == "https://player.test/embed/abc"
