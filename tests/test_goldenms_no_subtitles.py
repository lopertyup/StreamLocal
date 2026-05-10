import pytest

from autoflix_cli import proxy
from autoflix_cli.app import providers
from autoflix_cli.app.encoding import decode_payload
from autoflix_cli.app.playback import prepare_playback
from autoflix_cli.app.providers import ProviderService
from autoflix_cli.app.store import DesktopStore
from autoflix_cli.scraping import goldenms


VIDLINK_URL = "https://stream.vidlink.test/path/master.m3u8?token=abc"
VIDLINK_HEADERS = {
    "User-Agent": "Test UA",
    "Referer": "https://vidlink.pro/",
    "Origin": "https://vidlink.pro",
}
VIDEASY_URL = "https://yoru.midwesteagle.com/hls/master.m3u8?token=abc"
VIDEASY_HEADERS = {
    "User-Agent": "Test UA",
    "Referer": "https://player.videasy.net/",
    "Origin": "https://player.videasy.net",
}


def _store(tmp_path, language="fr"):
    store = DesktopStore(tmp_path / "desktop.json")
    store.data["preferences"]["language"] = language
    return store


def _episode_payload(is_movie=True, season_number=2, episode_number=5):
    return {
        "provider_id": "goldenms",
        "provider": "GoldenMS",
        "content_id": "content-token",
        "series_title": "Example Title",
        "season_title": "Film" if is_movie else f"Saison {season_number}",
        "episode_title": "Film" if is_movie else f"Episode {episode_number}",
        "series_url": "tmdb:12345|imdb:tt1234567",
        "season_url": "",
        "episode_url": "",
        "logo_url": "",
        "language": "multi",
        "episode_number": episode_number,
        "history_key": "goldenms:content-token:episode",
        "is_movie": is_movie,
        "tmdb_id": "12345",
        "imdb_id": "tt1234567",
        "year": "2025",
        "title": "Example Title",
        "season_number": season_number,
    }


def _vidlink_source(**changes):
    source = {
        "source": "Vidlink",
        "quality": "Multi",
        "url": VIDLINK_URL,
        "type": "M3U8",
        "headers": dict(VIDLINK_HEADERS),
    }
    source.update(changes)
    return source


def _videasy_hls_source(**changes):
    source = {
        "source": "Videasy (CDN)",
        "quality": "1080p",
        "url": VIDEASY_URL,
        "type": "M3U8",
        "headers": dict(VIDEASY_HEADERS),
        "stream_context": "videasy",
        "subtitles": [],
    }
    source.update(changes)
    return source


def _videasy_1movies_source(**changes):
    source = _videasy_hls_source(
        source="Videasy (1MOVIES)",
        url="https://floral.tylerfisher55.workers.dev/session/manifest-token",
    )
    source.update(changes)
    return source


def _stub_goldenms_extract(
    monkeypatch,
    raw_sources,
    videasy_sources=None,
    videasy_calls=None,
    manifest_subtitles=None,
):
    del manifest_subtitles

    def fake_extract(**kwargs):
        del kwargs
        return raw_sources

    def fake_search_videasy(*args, **kwargs):
        if videasy_calls is not None:
            videasy_calls.append({"args": args, "kwargs": kwargs})
        return videasy_sources or []

    monkeypatch.setattr(providers.goldenms_extractor, "extract", fake_extract)
    monkeypatch.setattr(providers.goldenms_extractor, "search_videasy", fake_search_videasy)


def _source_named(sources, name):
    return next(source for source in sources if source.name == name)


def _source_with_prefix(sources, prefix):
    return next(source for source in sources if source.name.startswith(prefix))


@pytest.mark.parametrize("is_movie", [True, False])
def test_goldenms_movie_series_sources_do_not_inject_external_subtitles(
    monkeypatch,
    tmp_path,
    is_movie,
):
    _stub_goldenms_extract(monkeypatch, [_vidlink_source()])

    service = ProviderService(_store(tmp_path, language="fr"))
    sources = service._sources_goldenms(
        "content-token",
        "episode-token",
        _episode_payload(is_movie=is_movie),
    )

    source = _source_named(sources, "Vidlink")
    assert source.subtitles == []
    assert source.has_subtitles is False
    assert source.subtitle_languages == []
    assert source.subtitle_source is None
    assert decode_payload(source.id)["subtitles"] == []


def test_goldenms_movie_series_strips_provider_subtitles_from_raw_sources(
    monkeypatch,
    tmp_path,
):
    _stub_goldenms_extract(
        monkeypatch,
        [
            _vidlink_source(
                subtitles=[
                    {"lang": "fr", "url": "https://subs.test/provider.vtt"},
                ],
                subtitle_source="provider",
            )
        ],
    )

    service = ProviderService(_store(tmp_path, language="fr"))
    sources = service._sources_goldenms(
        "content-token",
        "episode-token",
        _episode_payload(is_movie=True),
    )
    source = _source_named(sources, "Vidlink")

    assert source.subtitles == []
    assert source.has_subtitles is False
    assert decode_payload(source.id)["subtitles"] == []


def test_goldenms_vidlink_stream_metadata_stays_playable_without_subtitles(
    monkeypatch,
    tmp_path,
):
    raw_source = _vidlink_source()
    _stub_goldenms_extract(monkeypatch, [raw_source])
    monkeypatch.setattr(proxy, "PROXY_URL", "http://127.0.0.1:45678")
    monkeypatch.setattr(proxy, "PROXY_PORT", 45678)

    service = ProviderService(_store(tmp_path, language="fr"))
    sources = service._sources_goldenms(
        "content-token",
        "episode-token",
        _episode_payload(is_movie=True),
    )
    source = _source_named(sources, "Vidlink")

    token = decode_payload(source.id)
    assert token["url"] == raw_source["url"]
    assert token["headers"] == raw_source["headers"]
    assert token["is_direct"] is True
    assert token["source_type"] == raw_source["type"]
    assert token["stream_context"] == ""

    playback = prepare_playback(source.id)
    assert playback["subtitles"] == []
    assert playback["passthrough_stream_url"] == ""
    assert playback["passthrough_mode"] == ""


def test_goldenms_videasy_movie_hls_source_uses_local_stream_metadata(monkeypatch, tmp_path):
    calls = []
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[_videasy_hls_source()],
        videasy_calls=calls,
    )

    service = ProviderService(_store(tmp_path, language="fr"))
    sources = service._sources_goldenms(
        "content-token",
        "episode-token",
        _episode_payload(is_movie=True),
    )

    source = _source_with_prefix(sources, "Videasy")
    token = decode_payload(source.id)
    assert calls[0]["args"] == ("Example Title",)
    assert calls[0]["kwargs"]["tmdb_id"] == "12345"
    assert calls[0]["kwargs"]["imdb_id"] == "tt1234567"
    assert source.source_type == "M3U8"
    assert source.score == 4
    assert token["url"] == VIDEASY_URL
    assert token["source_type"] == "M3U8"
    assert token["is_direct"] is True
    assert token["stream_context"] == "videasy"
    assert token["subtitles"] == []
    assert sources[0].name == "Vidlink"


def test_goldenms_videasy_restores_cdn_and_filters_1movies(monkeypatch, tmp_path):
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[
            _videasy_1movies_source(),
            _videasy_hls_source(quality="4K"),
        ],
    )

    service = ProviderService(_store(tmp_path, language="fr"))
    sources = service._sources_goldenms(
        "content-token",
        "episode-token",
        _episode_payload(is_movie=True),
    )
    names = [source.name for source in sources]
    videasy = _source_named(sources, "Videasy (CDN)")
    token = decode_payload(videasy.id)

    assert "Videasy (CDN)" in names
    assert "Videasy (1MOVIES)" not in names
    assert videasy.quality == "4K"
    assert "yoru.midwesteagle.com" in token["url"]


def test_goldenms_videasy_source_proxies_api_subtitles(monkeypatch, tmp_path):
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[
            _videasy_hls_source(
                subtitles=[
                    {"lang": "en", "label": "English", "url": "https://cdn.videasy.test/subs/en.vtt"},
                    {"lang": "fr", "label": "Français", "url": "https://cdn.videasy.test/subs/fr.vtt"},
                ],
                subtitle_source="videasy_api",
            )
        ],
    )

    service = ProviderService(_store(tmp_path, language="fr"))
    source = _source_with_prefix(
        service._sources_goldenms(
            "content-token",
            "episode-token",
            _episode_payload(is_movie=True),
        ),
        "Videasy",
    )
    token = decode_payload(source.id)

    assert source.has_subtitles is True
    assert source.subtitle_languages == ["en", "fr"]
    assert source.subtitle_source == "videasy_api"
    assert all(track["url"].startswith("/api/subtitles/proxy/") for track in token["subtitles"])
    assert [
        {key: value for key, value in track.items() if key != "url"}
        for track in token["subtitles"]
    ] == [
        {"lang": "en", "label": "English", "subtitle_source": "external"},
        {"lang": "fr", "label": "Français", "subtitle_source": "external"},
    ]


def test_goldenms_videasy_subtitle_languages_are_iso_codes(monkeypatch, tmp_path):
    raw_tracks = [
        {"language": "English", "url": "https://subs.test/en.vtt"},
        {"language": "Français", "url": "https://subs.test/fr.vtt"},
        {"language": "Arabic", "url": "https://subs.test/ar.vtt"},
        {"language": "Japan", "url": "https://subs.test/ja.vtt"},
        {"language": "Spanish", "url": "https://subs.test/es.vtt"},
        {"language": "Portuguese", "url": "https://subs.test/pt.vtt"},
        {"language": "German", "url": "https://subs.test/de.vtt"},
        {"language": "Italian", "url": "https://subs.test/it.vtt"},
        {"language": "Russian", "url": "https://subs.test/ru.vtt"},
        {"language": "Korean", "url": "https://subs.test/ko.vtt"},
        {"language": "zh-tw", "url": "https://subs.test/zh-tw.vtt"},
        {"language": "Turkish", "url": "https://subs.test/tr.vtt"},
    ]
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[
            _videasy_hls_source(
                subtitles=raw_tracks,
                subtitle_source="videasy_api",
            )
        ],
    )

    service = ProviderService(_store(tmp_path, language="fr"))
    source = _source_with_prefix(
        service._sources_goldenms(
            "content-token",
            "episode-token",
            _episode_payload(is_movie=True),
        ),
        "Videasy",
    )
    token = decode_payload(source.id)

    expected = ["en", "fr", "ar", "ja", "es", "pt", "de", "it", "ru", "ko", "zh-TW", "tr"]
    assert source.subtitle_languages == expected
    assert [track["lang"] for track in token["subtitles"]] == expected


def test_goldenms_videasy_series_source_uses_tmdb_season_episode(monkeypatch, tmp_path):
    calls = []
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[_videasy_hls_source()],
        videasy_calls=calls,
    )

    service = ProviderService(_store(tmp_path, language="fr"))
    service._sources_goldenms(
        "content-token",
        "episode-token",
        _episode_payload(is_movie=False, season_number=2, episode_number=5),
    )

    assert calls[0]["kwargs"]["tmdb_id"] == "12345"
    assert calls[0]["kwargs"]["season"] == 2
    assert calls[0]["kwargs"]["episode"] == 5


def test_goldenms_does_not_add_videasy_without_tmdb_id(monkeypatch, tmp_path):
    calls = []
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[_videasy_hls_source()],
        videasy_calls=calls,
    )
    payload = _episode_payload(is_movie=True)
    payload["tmdb_id"] = ""

    service = ProviderService(_store(tmp_path, language="fr"))
    sources = service._sources_goldenms("content-token", "episode-token", payload)

    assert [source.name for source in sources] == ["Vidlink"]
    assert calls == []


def test_goldenms_videasy_prepare_playback_returns_hls_for_local_player(monkeypatch, tmp_path):
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[_videasy_hls_source()],
    )
    monkeypatch.setattr(proxy, "PROXY_URL", "http://127.0.0.1:45678")
    monkeypatch.setattr(proxy, "PROXY_PORT", 45678)

    service = ProviderService(_store(tmp_path, language="fr"))
    source = _source_with_prefix(
        service._sources_goldenms(
            "content-token",
            "episode-token",
            _episode_payload(is_movie=True),
        ),
        "Videasy",
    )

    playback = prepare_playback(source.id)

    assert playback["media_kind"] == "hls"
    assert playback["stream_url"].startswith("http://127.0.0.1:45678/stream/")
    assert playback["passthrough_stream_url"] == VIDEASY_URL
    assert playback["passthrough_mode"] == "browser_direct_hls"
    assert playback["subtitles"] == []


def test_goldenms_videasy_hls_source_carries_saved_progress(monkeypatch, tmp_path):
    _stub_goldenms_extract(
        monkeypatch,
        [_vidlink_source()],
        videasy_sources=[_videasy_hls_source()],
    )
    store = _store(tmp_path, language="fr")
    payload = _episode_payload(is_movie=True)
    store.save_progress(
        {
            "provider_id": payload["provider_id"],
            "provider": payload["provider"],
            "content_id": "content-token",
            "episode_id": "episode-token",
            "series_title": payload["series_title"],
            "season_title": payload["season_title"],
            "episode_title": payload["episode_title"],
            "history_key": payload["history_key"],
        },
        current_time=120,
        duration=240,
    )

    service = ProviderService(store)
    source = _source_with_prefix(
        service._sources_goldenms("content-token", "episode-token", payload),
        "Videasy",
    )

    playback = prepare_playback(source.id)

    assert playback["media_kind"] == "hls"
    assert playback["progress"]["position"] == 120.0
    assert playback["progress"]["duration"] == 240.0


def test_goldenms_extract_does_not_include_videasy_by_default(monkeypatch):
    extractor = goldenms.MediaExtractor()
    monkeypatch.setattr(extractor, "search_vidlink", lambda *args, **kwargs: [_vidlink_source()])
    monkeypatch.setattr(extractor, "search_hexa", lambda *args, **kwargs: [])
    monkeypatch.setattr(extractor, "search_xpass", lambda *args, **kwargs: [])
    monkeypatch.setattr(extractor, "search_mapple", lambda *args, **kwargs: [])

    def fail_videasy(*args, **kwargs):
        del args, kwargs
        pytest.fail("Videasy should be opt-in and absent from normal GoldenMS extraction")

    monkeypatch.setattr(extractor, "search_videasy", fail_videasy)

    results = extractor.extract(
        title="Example Title",
        tmdb_id="12345",
        imdb_id="tt1234567",
        year="2025",
    )

    assert results == [_vidlink_source()]
