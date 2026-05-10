import json
import urllib.parse
from typing import Any, Dict, Tuple

from curl_cffi import requests

from .. import proxy
from ..player_manager import DEFAULT_USER_AGENT
from ..proxy import basename_from_uri, encode_headers_token
from ..scraping import player as player_scraper
from ..scraping.goldenanime import goldenanime
from . import diagnostics
from .encoding import decode_payload
from .models import ProviderError


def _player_match(url: str) -> Tuple[str, Dict[str, Any]]:
    for player_name, config in player_scraper.players.items():
        if player_name in url.lower():
            return player_name, config
    return "", {}


def _player_config(url: str) -> Dict[str, Any]:
    return _player_match(url)[1]


def _domain(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except Exception:
        return ""


def _resolve_relative_url(original_url: str, stream_url: str) -> str:
    if not stream_url or not stream_url.startswith("/"):
        return stream_url
    domain = _domain(original_url)
    if not domain:
        return stream_url
    return f"https://{domain}{stream_url}"


def _resolve_special_direct_source(url: str, headers: Dict[str, str]) -> str:
    sudatchi_domain = goldenanime.sudatchi_base.replace("https://", "").replace("http://", "")
    if sudatchi_domain + "/api/streams" in url or "sudatchi.com/api/streams" in url:
        response = requests.get(url, headers=headers, impersonate="chrome", timeout=10)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list) and payload:
            return payload[0].get("url") or url
    return url


def _referer_for_source(
    original_url: str,
    headers: Dict[str, str],
    config: Dict[str, Any],
    is_direct: bool,
) -> str:
    if is_direct:
        return headers.get("Referer", "")

    domain = _domain(original_url)
    referer = f"https://{domain}" if domain else headers.get("Referer", "")
    configured = config.get("referrer")
    if configured == "full":
        referer = original_url
    elif configured == "path":
        referer = f"https://{domain}/" if domain else referer
    elif isinstance(configured, str):
        referer = configured

    if referer and not referer.endswith("/"):
        referer += "/"
    return referer


def _proxy_headers(
    original_url: str,
    headers: Dict[str, str],
    config: Dict[str, Any],
    referer: str,
) -> Dict[str, str]:
    result = dict(headers or {})
    if referer:
        result["Referer"] = referer

    domain = _domain(original_url)
    if config.get("alt-used") is True and domain:
        result["Alt-Used"] = domain

    sec_headers = config.get("sec_headers")
    if isinstance(sec_headers, str):
        for part in sec_headers.split(";"):
            if ":" in part:
                key, value = part.split(":", 1)
                result[key.strip()] = value.strip()

    if "User-Agent" not in result:
        result["User-Agent"] = DEFAULT_USER_AGENT

    return result


def _is_mp4_like(source_type: str, stream_url: str, config: Dict[str, Any]) -> bool:
    lower_url = (stream_url or "").lower()
    lower_type = (source_type or "").lower()
    if config.get("ext") == "mp4":
        return True
    if lower_type == "mp4":
        return True
    if lower_type == "video" and ".m3u8" not in lower_url and "master" not in lower_url:
        return True
    return lower_url.endswith(".mp4")


def prepare_playback(source_id: str) -> Dict[str, Any]:
    source = decode_payload(source_id)
    original_url = source.get("url")
    if not original_url:
        raise ProviderError("invalid_source", "Source video invalide.", 422)

    progress = source.get("progress") or {}
    title_parts = [
        progress.get("series_title"),
        progress.get("season_title"),
        progress.get("episode_title"),
    ]
    title = " - ".join([part for part in title_parts if part]) or source.get("source_name") or "AutoFlix"
    diagnostics.info(
        "PLAYBACK",
        "start",
        status="start",
        source_name=source.get("source_name"),
        source_type=source.get("source_type"),
        quality=source.get("quality", ""),
        direct=bool(source.get("is_direct")),
        source_domain=diagnostics.domain_of(original_url),
        title=title,
    )

    source_type = source.get("source_type", "")
    if str(source_type).strip().lower() == "iframe":
        stream_url = original_url
        diagnostics.info(
            "PLAYBACK",
            "ready",
            status="success",
            media_kind="iframe",
            title=title,
            source_name=source.get("source_name"),
            source_domain=diagnostics.domain_of(stream_url),
        )
        return {
            "stream_url": stream_url,
            "passthrough_stream_url": "",
            "passthrough_mode": "",
            "media_kind": "iframe",
            "title": title,
            "source_name": source.get("source_name"),
            "quality": source.get("quality", ""),
            "subtitles": [],
            "progress": progress,
        }

    if hasattr(player_scraper, "new_url") and isinstance(player_scraper.new_url, dict):
        for old, new in player_scraper.new_url.items():
            original_url = original_url.replace(old, new)

    headers = source.get("headers") or {}
    player_name, config = _player_match(original_url)
    is_direct = bool(source.get("is_direct"))

    if not proxy.PROXY_URL:
        proxy.start_proxy_server()

    diagnostics.info(
        "PLAYBACK",
        "resolve_start",
        status="start",
        player_domain=diagnostics.domain_of(original_url),
        parser=config.get("type") or ("direct" if is_direct else "unknown"),
        player=player_name or "direct",
        direct=is_direct,
    )

    try:
        if is_direct:
            stream_url = _resolve_special_direct_source(original_url, headers)
        else:
            stream_url = player_scraper.get_hls_link(original_url, headers)
    except Exception as exc:
        diagnostics.exception(
            "PLAYBACK",
            "resolve",
            exc,
            player_domain=diagnostics.domain_of(original_url),
            parser=config.get("type") or "unknown",
        )
        raise ProviderError(
            "stream_resolution_failed",
            f"Impossible de resoudre le stream: {exc}",
            502,
        ) from exc

    stream_url = _resolve_relative_url(original_url, stream_url)
    if not stream_url:
        diagnostics.error(
            "PLAYBACK",
            "resolve",
            error="empty_stream",
            player_domain=diagnostics.domain_of(original_url),
            parser=config.get("type") or "unknown",
        )
        raise ProviderError(
            "stream_resolution_failed",
            "Le lecteur n'a retourne aucun stream exploitable.",
            502,
        )

    referer = _referer_for_source(original_url, headers, config, is_direct)
    proxy_headers = _proxy_headers(original_url, headers, config, referer)
    stream_context = (
        str(source.get("stream_context") or source.get("context") or "")
        .strip()
        .lower()
    )
    encoded_url = urllib.parse.quote(stream_url)
    encoded_headers = encode_headers_token(proxy_headers)
    context_param = (
        f"&context={urllib.parse.quote(stream_context)}" if stream_context else ""
    )

    is_mp4 = _is_mp4_like(source.get("source_type", ""), stream_url, config)
    endpoint = "video" if is_mp4 else "stream"
    passthrough_stream_url = (
        stream_url if stream_context == "videasy" and not is_mp4 else ""
    )
    subtitles = source.get("subtitles") or []
    basename = basename_from_uri(
        stream_url,
        "video.mp4" if is_mp4 else "playlist.m3u8",
    )
    local_stream_url = (
        f"{proxy.PROXY_URL}/{endpoint}/{basename}?url={encoded_url}&headers={encoded_headers}"
        f"{context_param}"
    )

    diagnostics.info(
        "PLAYBACK",
        "resolve_success",
        status="success",
        media_kind="mp4" if is_mp4 else "hls",
        endpoint=endpoint,
        upstream_domain=diagnostics.domain_of(stream_url),
        parser=config.get("type") or ("direct" if is_direct else "unknown"),
        hls_passthrough=bool(passthrough_stream_url),
    )
    diagnostics.info(
        "PLAYBACK",
        "ready",
        status="success",
        media_kind="mp4" if is_mp4 else "hls",
        title=title,
        source_name=source.get("source_name"),
        proxy_url=local_stream_url,
    )

    return {
        "stream_url": local_stream_url,
        "passthrough_stream_url": passthrough_stream_url,
        "passthrough_mode": "browser_direct_hls" if passthrough_stream_url else "",
        "media_kind": "mp4" if is_mp4 else "hls",
        "title": title,
        "source_name": source.get("source_name"),
        "quality": source.get("quality", ""),
        "subtitles": subtitles,
        "progress": progress,
    }
