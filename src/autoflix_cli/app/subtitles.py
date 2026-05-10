import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

from curl_cffi import requests

from ..player_manager import DEFAULT_USER_AGENT
from .encoding import decode_payload, encode_payload
from .models import ProviderError


MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
DEFAULT_VIDEASY_MPEGTS = 900000
_VTT_TIMESTAMP_RE = re.compile(r"(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})")

def external_subtitle_track_url(
    source_url: str,
    headers: Dict[str, str] | None = None,
    context: str = "",
) -> str:
    payload: Dict[str, Any] = {"url": source_url}
    if headers:
        payload["headers"] = headers
    if context:
        payload["context"] = context
    return f"/api/subtitles/proxy/{encode_payload(payload)}.vtt"


def proxied_external_subtitles(
    subtitles: Iterable[Dict[str, Any]],
    headers: Dict[str, str] | None = None,
    context: str = "",
) -> List[Dict[str, Any]]:
    proxied: List[Dict[str, Any]] = []
    for subtitle in subtitles or []:
        source_url = str(subtitle.get("url") or "").strip()
        if not source_url:
            continue
        item = {key: value for key, value in subtitle.items() if key != "url"}
        item["url"] = external_subtitle_track_url(source_url, headers=headers, context=context)
        item["subtitle_source"] = "external"
        proxied.append(item)
    return proxied


def subtitle_url_from_token(token: str) -> str:
    try:
        payload = decode_payload(token)
    except Exception as exc:
        raise ProviderError("invalid_subtitle", "Piste de sous-titres invalide.", 422) from exc

    url = str(payload.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderError("invalid_subtitle", "URL de sous-titres invalide.", 422)
    return url


def subtitle_request_from_token(token: str) -> Dict[str, Any]:
    try:
        payload = decode_payload(token)
    except Exception as exc:
        raise ProviderError("invalid_subtitle", "Piste de sous-titres invalide.", 422) from exc

    url = str(payload.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderError("invalid_subtitle", "URL de sous-titres invalide.", 422)

    headers = payload.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    return {
        "url": url,
        "headers": {str(key): str(value) for key, value in headers.items()},
        "context": str(payload.get("context") or ""),
    }


def _decode_subtitle_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def subtitle_text_to_vtt(content: bytes) -> str:
    text = _decode_subtitle_bytes(content)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.lstrip("\ufeff").lstrip().upper().startswith("WEBVTT"):
        return text

    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text + "\n"


def _coerce_offset_ms(offset_ms: Any) -> int:
    try:
        return int(float(offset_ms or 0))
    except (TypeError, ValueError):
        return 0


def _timestamp_match_to_ms(match: re.Match[str]) -> int:
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + milliseconds


def _format_vtt_timestamp(total_ms: int) -> str:
    total_ms = max(0, int(total_ms))
    milliseconds = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def offset_vtt_timestamps(vtt_content: str, offset_ms: Any = 0) -> str:
    offset = _coerce_offset_ms(offset_ms)
    if not offset:
        return vtt_content

    text = (vtt_content or "").replace("\r\n", "\n").replace("\r", "\n")
    had_trailing_newline = text.endswith("\n")
    shifted_lines = []
    for line in text.split("\n"):
        if "-->" not in line:
            shifted_lines.append(line)
            continue

        def replace(match: re.Match[str]) -> str:
            return _format_vtt_timestamp(_timestamp_match_to_ms(match) + offset)

        shifted_lines.append(_VTT_TIMESTAMP_RE.sub(replace, line))

    shifted = "\n".join(shifted_lines)
    if had_trailing_newline and not shifted.endswith("\n"):
        shifted += "\n"
    return shifted


def inject_vtt_timestamp_map(vtt_content: str, mpegts: int) -> str:
    try:
        mpegts = int(mpegts)
    except (TypeError, ValueError):
        mpegts = DEFAULT_VIDEASY_MPEGTS

    text = (vtt_content or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    text = re.sub(r"^WEBVTT[^\n]*\n?", "WEBVTT\n", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"^X-TIMESTAMP-MAP=[^\n]*\n+", "", text, count=1, flags=re.IGNORECASE | re.MULTILINE)
    body = text.split("\n", 1)[1] if "\n" in text else ""
    body = body.lstrip("\n")
    return (
        "WEBVTT\n"
        f"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{mpegts}\n\n"
        f"{body}"
    ).rstrip() + "\n"


def _videasy_mpegts_start() -> int:
    try:
        from .. import proxy

        return int(proxy.get_videasy_pts_start(DEFAULT_VIDEASY_MPEGTS))
    except Exception:
        return DEFAULT_VIDEASY_MPEGTS


def _subtitle_request_headers(headers: Dict[str, str], context: str) -> Dict[str, str]:
    request_headers = dict(headers or {})
    if context.strip().lower() == "videasy":
        request_headers.update(
            {
                "Origin": "https://player.videasy.net",
                "Referer": "https://player.videasy.net/",
            }
        )
    if request_headers and "User-Agent" not in request_headers:
        request_headers["User-Agent"] = DEFAULT_USER_AGENT
    return request_headers


def fetch_subtitle_as_vtt(
    source_url: str,
    headers: Dict[str, str] | None = None,
    context: str = "",
    offset_ms: Any = 0,
) -> str:
    request_headers = _subtitle_request_headers(headers or {}, context)
    request_kwargs: Dict[str, Any] = {"timeout": 10, "impersonate": "chrome"}
    if request_headers:
        request_kwargs["headers"] = request_headers
    response = requests.get(source_url, **request_kwargs)
    response.raise_for_status()
    content = response.content or b""
    if len(content) > MAX_SUBTITLE_BYTES:
        raise ProviderError("subtitle_too_large", "Fichier de sous-titres trop volumineux.", 502)
    vtt_content = subtitle_text_to_vtt(content)
    if context.strip().lower() == "videasy":
        vtt_content = inject_vtt_timestamp_map(vtt_content, _videasy_mpegts_start())
    vtt_content = offset_vtt_timestamps(vtt_content, offset_ms)
    return vtt_content
