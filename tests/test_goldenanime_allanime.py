import base64
import hashlib
import json

from Crypto.Cipher import AES

from autoflix_cli.scraping import goldenanime as goldenanime_module
from autoflix_cli.scraping.goldenanime import ALLANIME_KEY_PHRASE, goldenanime


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _encrypt_allanime_payload(payload):
    nonce = b"123456789012"
    key = hashlib.sha256(ALLANIME_KEY_PHRASE).digest()
    cipher = AES.new(key, AES.MODE_CTR, nonce=nonce, initial_value=2)
    encrypted = cipher.encrypt(json.dumps(payload).encode("utf-8"))
    raw = b"\x01" + nonce + encrypted + (b"0" * 16)
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _xor_allanime_url(url):
    return "--" + "".join(f"{ord(char) ^ 56:02x}" for char in url)


def test_allanime_decodes_tobeparsed_clock_sources(monkeypatch):
    episode_requests = []
    encrypted_episode = _encrypt_allanime_payload(
        {
            "episode": {
                "sourceUrls": [
                    {
                        "sourceUrl": _xor_allanime_url("/apivtwo/clock?id=abc"),
                        "sourceName": "Default",
                        "type": "iframe",
                        "priority": 8.5,
                    }
                ]
            }
        }
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        del headers, timeout
        if url == goldenanime.allanime_api:
            variables = json.loads(params["variables"])
            if "search" in variables:
                return FakeResponse(
                    {
                        "data": {
                            "shows": {
                                "edges": [
                                    {
                                        "_id": "season-3",
                                        "name": "One Punch Man 3",
                                        "englishName": "One-Punch Man Season 3",
                                    },
                                    {
                                        "_id": "opm",
                                        "name": "One Punch Man",
                                        "englishName": "One Punch Man",
                                    },
                                ]
                            }
                        }
                    }
                )

            episode_requests.append(variables)
            return FakeResponse({"data": {"_m": "b7", "tobeparsed": encrypted_episode}})

        assert url == "https://allanime.day/apivtwo/clock.json?id=abc"
        return FakeResponse(
            {
                "links": [
                    {
                        "link": "https://cdn.test/show/master.m3u8",
                        "hls": True,
                        "resolutionStr": "Hls",
                    }
                ]
            }
        )

    monkeypatch.setattr(goldenanime_module.scraper, "get", fake_get)

    sources = goldenanime.search_allanime("One-Punch Man", episode=1)

    assert episode_requests[0]["showId"] == "opm"
    assert sources == [
        {
            "source": "Allanime (Default Hls)",
            "quality": "Hls",
            "url": "https://cdn.test/show/master.m3u8",
            "type": "M3U8",
            "headers": {
                "Referer": goldenanime.allanime_referer + "/",
                "Origin": "https://youtu-chan.com",
            },
            "direct": True,
            "score": 90,
        }
    ]


def test_extract_vo_skips_slow_fallbacks_when_allanime_has_direct_stream(monkeypatch):
    fallback_calls = []

    monkeypatch.setattr(
        goldenanime,
        "search_allanime",
        lambda title, episode=1: [
            {
                "source": "Allanime",
                "quality": "Hls",
                "url": "https://cdn.test/master.m3u8",
                "type": "M3U8",
            }
        ],
    )
    monkeypatch.setattr(
        goldenanime,
        "search_sudatchi",
        lambda *args, **kwargs: fallback_calls.append("sudatchi") or [],
    )
    monkeypatch.setattr(
        goldenanime,
        "search_anizone",
        lambda *args, **kwargs: fallback_calls.append("anizone") or [],
    )
    monkeypatch.setattr(
        goldenanime,
        "search_animetsu",
        lambda *args, **kwargs: fallback_calls.append("animetsu") or [],
    )

    sources = goldenanime.extract_vo("One-Punch Man", anilist_id=21087, episode=1)

    assert sources[0]["url"] == "https://cdn.test/master.m3u8"
    assert fallback_calls == []
