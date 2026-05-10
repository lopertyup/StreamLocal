import base64
import hashlib
import threading
import socket
import json
import secrets
import time
import urllib.parse
import re
from datetime import datetime, timezone
from flask import Flask, request, Response, stream_with_context
from curl_cffi import requests, CurlOpt
import m3u8

from .app import diagnostics


def encode_headers_token(headers: dict) -> str:
    """Encode headers as urlsafe base64 (no dots, no padding) for use in proxy URLs."""
    raw = json.dumps(headers or {}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


_BASENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def basename_from_uri(uri: str, default: str = "file") -> str:
    """Extract a safe basename from a URL path for use in proxy paths.

    ffmpeg's HLS demuxer parses the URL path (before query string) to detect
    the file extension. Embedding the original segment basename in our proxy
    path makes the .m3u8/.m4s/.ts/.mp4 extension visible to ffmpeg.
    """
    try:
        path = urllib.parse.urlparse(uri).path or ""
        candidate = path.rsplit("/", 1)[-1] or default
        cleaned = _BASENAME_SAFE.sub("", candidate)
        if not cleaned:
            return default
        return cleaned[:80]
    except Exception:
        return default


def decode_headers_param(value: str) -> dict:
    """Decode the headers query param. Tries base64url first, then legacy JSON."""
    if not value:
        return {}
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode("utf-8")
        result = json.loads(decoded)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}

# Global Configuration
PROXY_PORT = 0
PROXY_HOST = "127.0.0.1"
PROXY_URL = None
_server_instance = None  # To store the server for shutdown
_segment_error_count = 0
_segment_error_last_log = 0.0
_SEGMENT_ERROR_EVERY = 10
_SEGMENT_ERROR_SECONDS = 20.0
_hls_url_log_counts = {}
_hls_sessions = {}
_hls_sessions_lock = threading.Lock()
_hls_pts_lock = threading.Lock()
_latest_videasy_pts_start = None
_HLS_URL_SAMPLE_LIMITS = {
    ("stream", "start"): 8,
    ("ts", "start"): 8,
    ("ts", "upstream_error"): 24,
}
_HLS_SESSION_PARAM = "hls_session"
_HLS_SESSION_TTL_SECONDS = 20 * 60
_VIDEASY_DEFAULT_MPEGTS = 900000

# Web Player State
player_finished_event = threading.Event()
player_heartbeat_time = 0

app = Flask(__name__)

# Requested Google DNS Options
DNS_OPTIONS = {
    CurlOpt.DOH_URL: "https://1.1.1.1/dns-query",
    CurlOpt.DOH_SSL_VERIFYPEER: 0,
    CurlOpt.DOH_SSL_VERIFYHOST: 0,
}

_HLS_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_PROXY_CONTEXT_KEYS = {
    "__autoflix_context",
    "context",
    "hls_context",
    "stream_context",
    "x-autoflix-context",
}
_VIDEASY_CONTEXT = "videasy"
_VIDEASY_ORIGIN = "https://player.videasy.net"
_VIDEASY_REFERER = _VIDEASY_ORIGIN + "/"
_VIDEASY_LEGACY_ORIGINS = {"https://cineby.gd"}
_TOKENISH_QUERY_KEY_PARTS = (
    "auth",
    "expires",
    "hash",
    "key",
    "policy",
    "sig",
    "token",
)
_EXPIRY_QUERY_KEYS = {
    "e",
    "exp",
    "expire",
    "expires",
    "expires_at",
    "expiresat",
    "expiry",
    "x-amz-expires",
}


def _header_value(headers, name):
    lower_name = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == lower_name:
            return str(value)
    return ""


def _replace_header(headers, name, value):
    lower_name = name.lower()
    for key in list(headers.keys()):
        if str(key).lower() == lower_name and key != name:
            del headers[key]
    headers[name] = value


def _delete_header(headers, name):
    lower_name = name.lower()
    for key in list(headers.keys()):
        if str(key).lower() == lower_name:
            del headers[key]


def _clean_proxy_headers(headers):
    result = {}
    for key, value in (headers or {}).items():
        header_name = str(key).strip()
        if not header_name or value is None:
            continue
        if header_name.lower() in _PROXY_CONTEXT_KEYS:
            continue
        result[header_name] = str(value)
    return result


def _browser_like_user_agent(value):
    lower_value = str(value or "").strip().lower()
    if "python" in lower_value:
        return False
    return "mozilla/" in lower_value and (
        "chrome/" in lower_value
        or "firefox/" in lower_value
        or "safari/" in lower_value
    )


def _looks_like_videasy_hls(target_url, headers, context=""):
    if str(context or "").strip().lower() == _VIDEASY_CONTEXT:
        return True
    upstream_domain = diagnostics.domain_of(target_url)
    if "videasy" in upstream_domain:
        return True
    referer = _header_value(headers, "Referer").lower()
    origin = _header_value(headers, "Origin").lower().rstrip("/")
    return (
        "videasy.net" in referer
        or "cineby.gd" in referer
        or origin == _VIDEASY_ORIGIN
        or origin in _VIDEASY_LEGACY_ORIGINS
    )


def _normalize_hls_headers(target_url, headers, route="stream", context=""):
    result = _clean_proxy_headers(headers)
    if not _looks_like_videasy_hls(target_url, result, context):
        return result

    user_agent = _header_value(result, "User-Agent")
    if not _browser_like_user_agent(user_agent):
        user_agent = _HLS_CHROME_USER_AGENT
    accept = _header_value(result, "Accept") or "*/*"

    _replace_header(result, "User-Agent", user_agent)
    _replace_header(result, "Accept", accept)
    _replace_header(result, "Referer", _VIDEASY_REFERER)
    _replace_header(result, "Origin", _VIDEASY_ORIGIN)
    _replace_header(result, "Sec-Fetch-Dest", "empty")
    _replace_header(result, "Sec-Fetch-Mode", "cors")
    _replace_header(result, "Sec-Fetch-Site", "cross-site")
    _delete_header(result, "Range")
    return result


def _prepare_hls_headers(target_url, headers, route, explicit_context=""):
    result = _clean_proxy_headers(headers)
    context = str(explicit_context or "").strip().lower()
    if _looks_like_videasy_hls(target_url, result, context):
        context = _VIDEASY_CONTEXT
    result = _normalize_hls_headers(target_url, result, route=route, context=context)
    if context == _VIDEASY_CONTEXT:
        diagnostics.info(
            "PROXY",
            "hls_headers",
            upstream_domain=diagnostics.domain_of(target_url),
            route=route,
            headers_sent=result,
            has_referer=bool(_header_value(result, "Referer")),
            has_origin=bool(_header_value(result, "Origin")),
            context=context,
        )
    return result, context


def _short_hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _is_tokenish_query_key(key):
    lower_key = str(key or "").lower()
    return any(part in lower_key for part in _TOKENISH_QUERY_KEY_PARTS)


def _safe_path_segment(segment):
    if not segment:
        return segment
    if len(segment) > 48 or re.fullmatch(r"[A-Za-z0-9_-]{32,}", segment):
        return f"<len={len(segment)},sha256={_short_hash(segment)}>"
    return segment


def _path_tail(path, count=4):
    parts = [part for part in (path or "").split("/") if part]
    return [_safe_path_segment(part) for part in parts[-count:]]


def _masked_diagnostic_url(parsed):
    path_parts = [part for part in (parsed.path or "").split("/") if part]
    safe_tail = _path_tail(parsed.path)
    prefix = ["..."] if len(path_parts) > len(safe_tail) else []
    safe_path = "/" + "/".join(prefix + safe_tail) if safe_tail else (parsed.path or "/")
    query = "..." if parsed.query else ""
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, safe_path, "", query, "")
    )


def _numeric_expiry_meta(key, value, now=None):
    lower_key = str(key or "").lower()
    if lower_key not in _EXPIRY_QUERY_KEYS:
        return {}
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,16}", text):
        return {}

    now_ts = time.time() if now is None else now
    number = int(text)
    if lower_key == "x-amz-expires":
        return {"duration_s": number}

    if number > 10_000_000_000:
        expires_at = number / 1000
    elif number > 1_000_000_000:
        expires_at = number
    else:
        return {}
    return {
        "epoch_s": int(expires_at),
        "expires_in_s": int(expires_at - now_ts),
    }


def _aws_expiry_meta(params, now=None):
    lower_params = {key.lower(): value for key, value in params}
    date_value = lower_params.get("x-amz-date")
    duration_value = lower_params.get("x-amz-expires")
    if not date_value or not duration_value:
        return {}
    try:
        signed_at = datetime.strptime(date_value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        expires_at = signed_at.timestamp() + int(duration_value)
    except Exception:
        return {}
    now_ts = time.time() if now is None else now
    return {
        "signed_at": signed_at.isoformat(timespec="seconds"),
        "epoch_s": int(expires_at),
        "expires_in_s": int(expires_at - now_ts),
    }


def _jwt_time_claims(value, now=None):
    text = str(value or "")
    if text.count(".") != 2:
        return {}
    try:
        payload = text.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}

    now_ts = time.time() if now is None else now
    result = {}
    for claim in ("exp", "nbf", "iat"):
        value = claims.get(claim)
        if isinstance(value, (int, float)):
            result[f"jwt_{claim}_s"] = int(value)
            if claim == "exp":
                result["jwt_expires_in_s"] = int(value - now_ts)
            elif claim == "nbf":
                result["jwt_valid_in_s"] = int(value - now_ts)
            elif claim == "iat":
                result["jwt_age_s"] = int(now_ts - value)
    return result


def _query_value_meta(key, value):
    text = str(value or "")
    meta = {
        "len": len(text),
        "sha256": _short_hash(text),
    }
    meta.update(_numeric_expiry_meta(key, text))
    meta.update(_jwt_time_claims(text))
    return meta


def _url_diagnostic_fields(target_url):
    parsed = urllib.parse.urlparse(target_url or "")
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_meta = []
    for key, value in params[:24]:
        meta = _query_value_meta(key, value)
        query_meta.append({"param": key, "value": meta})

    aws_expiry = _aws_expiry_meta(params)
    if aws_expiry:
        query_meta.append({"param": "x-amz-signed-url", "value": aws_expiry})

    query_keys = [key for key, _value in params]
    signed_keys = [key for key in query_keys if _is_tokenish_query_key(key)]
    return {
        "upstream_url": _masked_diagnostic_url(parsed),
        "path_tail": _path_tail(parsed.path),
        "query_param_count": len(params),
        "query_keys": query_keys[:24],
        "signed_query_keys": signed_keys[:24],
        "query_meta": query_meta,
    }


def _should_log_hls_url(route, status):
    key = (route, status)
    limit = _HLS_URL_SAMPLE_LIMITS.get(key, 0)
    if limit <= 0:
        return False
    count = _hls_url_log_counts.get(key, 0)
    if count >= limit:
        return False
    _hls_url_log_counts[key] = count + 1
    return True


def _log_hls_url_diagnostic(
    route,
    target_url,
    headers,
    context="",
    status="start",
    http_status=None,
    force=False,
):
    if not _looks_like_videasy_hls(target_url, headers, context):
        return
    if not force and not _should_log_hls_url(route, status):
        return

    log_fields = _url_diagnostic_fields(target_url)
    log_fields.update(
        {
            "route": route,
            "context": _VIDEASY_CONTEXT,
            "upstream_domain": diagnostics.domain_of(target_url),
            "has_referer": bool(_header_value(headers, "Referer")),
            "has_origin": bool(_header_value(headers, "Origin")),
        }
    )
    if http_status is not None:
        log_fields["http_status"] = http_status

    if status == "upstream_error":
        diagnostics.warning("PROXY", "hls_url", status=status, **log_fields)
    else:
        diagnostics.info("PROXY", "hls_url", status=status, **log_fields)


def _replace_uri_attribute(text, make_proxy_url, endpoint):
    def replace(match):
        quote = match.group(1)
        uri = match.group(2)
        return f'URI={quote}{make_proxy_url(endpoint, uri)}{quote}'

    return re.sub(r'URI=(["\'])(.*?)\1', replace, text, count=1)


def _split_hls_attribute_list(text):
    parts = []
    current = []
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
        if char == "," and not in_quotes:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _parse_hls_attribute_list(text):
    attrs = {}
    for part in _split_hls_attribute_list(text):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        attrs[key.strip().upper()] = value
    return attrs


def parse_hls_subtitle_tracks(content, base_uri=""):
    tracks = []
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _parse_hls_attribute_list(stripped.split(":", 1)[1])
        if attrs.get("TYPE", "").upper() != "SUBTITLES":
            continue
        uri = attrs.get("URI", "")
        absolute_uri = urllib.parse.urljoin(base_uri, uri) if uri else ""
        tracks.append(
            {
                "name": attrs.get("NAME") or attrs.get("LANGUAGE") or "Sous-titres",
                "lang": attrs.get("LANGUAGE") or attrs.get("LANG") or "",
                "uri": absolute_uri,
            }
        )
    return tracks


def _log_hls_manifest_subtitles(content, base_uri, headers, context):
    if not _looks_like_videasy_hls(base_uri, headers, context):
        return
    tracks = parse_hls_subtitle_tracks(content, base_uri)
    fields = {
        "upstream_domain": diagnostics.domain_of(base_uri),
        "count": len(tracks),
    }
    if tracks:
        fields["tracks"] = tracks
    else:
        fields["reason"] = "not_in_manifest"
    diagnostics.info("PROXY", "subtitles_found", status="ok", **fields)


def _split_hls_line(line):
    body = line.rstrip("\r\n")
    newline = line[len(body):]
    return body, newline


def _rewrite_playlist_content(content, m3u8_obj, make_proxy_url):
    is_master = bool(m3u8_obj.playlists)
    rewritten = []
    for line in content.splitlines(keepends=True):
        body, newline = _split_hls_line(line)
        stripped = body.strip()
        if not stripped:
            rewritten.append(line)
            continue

        if stripped.startswith("#"):
            if (
                is_master
                and stripped.startswith(("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF"))
                and "URI=" in body
            ):
                body = _replace_uri_attribute(body, make_proxy_url, "stream")
            elif (
                not is_master
                and stripped.startswith(("#EXT-X-KEY", "#EXT-X-MAP"))
                and "URI=" in body
            ):
                body = _replace_uri_attribute(body, make_proxy_url, "ts")
            rewritten.append(body + newline)
            continue

        endpoint = "stream" if is_master else "ts"
        rewritten.append(make_proxy_url(endpoint, stripped) + newline)
    return "".join(rewritten)


def _raw_playlist_uri_samples(content, is_master, limit=6):
    samples = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if (
                is_master
                and stripped.startswith(("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF"))
                and "URI=" in stripped
            ):
                match = re.search(r'URI=(["\'])(.*?)\1', stripped)
                if match:
                    kind = (
                        "iframe"
                        if stripped.startswith("#EXT-X-I-FRAME-STREAM-INF")
                        else "media"
                    )
                    samples.append((kind, match.group(2)))
            elif (
                not is_master
                and stripped.startswith(("#EXT-X-KEY", "#EXT-X-MAP"))
                and "URI=" in stripped
            ):
                match = re.search(r'URI=(["\'])(.*?)\1', stripped)
                if match:
                    kind = "key" if stripped.startswith("#EXT-X-KEY") else "map"
                    samples.append((kind, match.group(2)))
            if len(samples) >= limit:
                break
            continue

        samples.append(("variant" if is_master else "segment", stripped))
        if len(samples) >= limit:
            break
    return samples


def _log_playlist_url_samples(content, m3u8_obj, base_uri, headers, context):
    if not _looks_like_videasy_hls(base_uri, headers, context):
        return
    is_master = bool(m3u8_obj.playlists)
    samples = []
    for kind, uri in _raw_playlist_uri_samples(content, is_master):
        absolute_url = urllib.parse.urljoin(base_uri, uri)
        samples.append(
            {
                "kind": kind,
                "absolute": bool(urllib.parse.urlparse(uri).scheme),
                "target": _url_diagnostic_fields(absolute_url),
            }
        )
    if not samples:
        return
    diagnostics.info(
        "PROXY",
        "hls_playlist_urls",
        status="success",
        context=_VIDEASY_CONTEXT,
        playlist_type="master" if is_master else "media",
        upstream_domain=diagnostics.domain_of(base_uri),
        samples=samples,
    )


def _new_hls_session_id():
    return secrets.token_urlsafe(18)


def _cleanup_hls_sessions(now=None):
    now = time.time() if now is None else now
    expired = []
    for session_id, entry in list(_hls_sessions.items()):
        if now - entry.get("last_used", now) > _HLS_SESSION_TTL_SECONDS:
            expired.append((session_id, entry))

    for session_id, entry in expired:
        _hls_sessions.pop(session_id, None)
        try:
            entry["session"].close()
        except Exception:
            pass


def _close_hls_sessions():
    with _hls_sessions_lock:
        entries = list(_hls_sessions.values())
        _hls_sessions.clear()

    for entry in entries:
        try:
            entry["session"].close()
        except Exception:
            pass


def _valid_hls_session_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{12,64}", str(value or "")))


def _get_hls_session_entry(target_url, headers, context, requested_id=""):
    if context != _VIDEASY_CONTEXT:
        return None, ""

    now = time.time()
    session_id = str(requested_id or "").strip()
    if not _valid_hls_session_id(session_id):
        session_id = _new_hls_session_id()

    with _hls_sessions_lock:
        _cleanup_hls_sessions(now)
        entry = _hls_sessions.get(session_id)
        status = "reused" if entry else "created"
        if not entry:
            entry = {
                "session": create_session(headers),
                "lock": threading.Lock(),
                "created_at": now,
                "last_used": now,
            }
            _hls_sessions[session_id] = entry
        else:
            entry["last_used"] = now
            try:
                entry["session"].headers.update(headers)
            except Exception:
                pass

    if status == "created":
        diagnostics.info(
            "PROXY",
            "hls_session",
            status=status,
            context=context,
            upstream_domain=diagnostics.domain_of(target_url),
            session_age_s=0,
        )
    return entry, session_id


def _release_response_session_lock(resp):
    lock = getattr(resp, "_autoflix_session_lock", None)
    if not lock:
        return
    try:
        lock.release()
    except RuntimeError:
        pass
    try:
        delattr(resp, "_autoflix_session_lock")
    except Exception:
        pass


def _decode_pes_pts(data):
    if len(data) < 14 or data[:3] != b"\x00\x00\x01":
        return None
    pts_dts_flags = (data[7] >> 6) & 0x03
    if pts_dts_flags not in (2, 3):
        return None
    pts = data[9:14]
    if len(pts) < 5:
        return None
    return (
        (((pts[0] >> 1) & 0x07) << 30)
        | (pts[1] << 22)
        | (((pts[2] >> 1) & 0x7F) << 15)
        | (pts[3] << 7)
        | ((pts[4] >> 1) & 0x7F)
    )


def _decode_pcr_base(data):
    if len(data) < 6:
        return None
    return (
        (data[0] << 25)
        | (data[1] << 17)
        | (data[2] << 9)
        | (data[3] << 1)
        | (data[4] >> 7)
    )


def _iter_ts_packets(data):
    if not data:
        return
    start = 0
    for offset in range(min(188, len(data))):
        if data[offset] != 0x47:
            continue
        if offset + 188 >= len(data) or data[offset + 188] == 0x47:
            start = offset
            break
    for index in range(start, len(data) - 187, 188):
        packet = data[index : index + 188]
        if packet[0] == 0x47:
            yield packet


def extract_mpegts_start_timestamp(data):
    first_pcr = None
    for packet in _iter_ts_packets(data):
        payload_unit_start = bool(packet[1] & 0x40)
        adaptation_control = (packet[3] >> 4) & 0x03
        pos = 4
        if adaptation_control in (2, 3):
            adaptation_length = packet[pos]
            if adaptation_length and pos + 1 + adaptation_length <= len(packet):
                flags = packet[pos + 1]
                if flags & 0x10 and adaptation_length >= 7 and first_pcr is None:
                    first_pcr = _decode_pcr_base(packet[pos + 2 : pos + 8])
            pos += 1 + adaptation_length
        if adaptation_control not in (1, 3) or pos >= len(packet):
            continue
        if payload_unit_start:
            pts = _decode_pes_pts(packet[pos:])
            if pts is not None:
                return pts
    return first_pcr


def _record_videasy_pts_start(pts_start, target_url, hls_session=None):
    if pts_start is None:
        pts_start = _VIDEASY_DEFAULT_MPEGTS
    try:
        pts_start = int(pts_start)
    except (TypeError, ValueError):
        pts_start = _VIDEASY_DEFAULT_MPEGTS

    if hls_session is not None:
        if hls_session.get("pts_logged"):
            return pts_start
        hls_session["pts_logged"] = True
        hls_session["pts_start"] = pts_start

    global _latest_videasy_pts_start
    with _hls_pts_lock:
        _latest_videasy_pts_start = pts_start

    diagnostics.info(
        "PROXY",
        "pts_detected",
        status="ok",
        upstream_domain=diagnostics.domain_of(target_url),
        pts_start=pts_start,
        pts_offset_s=round(pts_start / 90000, 3),
    )
    return pts_start


def get_videasy_pts_start(default=_VIDEASY_DEFAULT_MPEGTS):
    with _hls_pts_lock:
        return _latest_videasy_pts_start if _latest_videasy_pts_start is not None else default


def find_free_port():
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def get_base_url(url):
    """Extracts the base URL to resolve relative paths."""
    return url.rsplit("/", 1)[0] + "/"


def create_session(headers_dict=None):
    """Creates a curl_cffi session optimized for anonymity."""
    session = requests.Session(impersonate="chrome")
    # Apply specific DNS options
    session.curl_options.update(DNS_OPTIONS)

    if headers_dict:
        session.headers.update(headers_dict)

    return session


def _log_segment_error(reason, url="", status_code=None):
    global _segment_error_count, _segment_error_last_log
    _segment_error_count += 1
    now = time.time()
    should_log = (
        _segment_error_count == 1
        or _segment_error_count % _SEGMENT_ERROR_EVERY == 0
        or now - _segment_error_last_log >= _SEGMENT_ERROR_SECONDS
    )
    if should_log:
        _segment_error_last_log = now
        diagnostics.error(
            "PROXY",
            "segment_errors",
            count=_segment_error_count,
            reason=reason,
            status_code=status_code,
            upstream_domain=diagnostics.domain_of(url),
        )


def fetch_with_retry(
    url,
    headers,
    method="GET",
    stream=False,
    max_retries=3,
    hls_session=None,
    hold_session_lock=False,
):
    """
    Performs a request with an automatic retry system.
    Handles timeouts and network errors to avoid breaking the stream.
    """
    attempt = 0
    session = hls_session["session"] if hls_session else create_session(headers)
    session_lock = hls_session.get("lock") if hls_session else None

    while attempt < max_retries:
        lock_acquired = False
        try:
            # Forward the Range header if present (for MP4 seeking)
            req_headers = headers.copy() if headers else {}

            # Handle the Range header coming from the client (VLC)
            if "Range" in request.headers and not _looks_like_videasy_hls(url, req_headers):
                req_headers["Range"] = request.headers["Range"]

            if session_lock:
                session_lock.acquire()
                lock_acquired = True

            response = session.request(
                method=method,
                url=url,
                headers=req_headers,
                stream=stream,
                timeout=15,  # Reasonable timeout
            )

            # If 429 error (Rate Limit) or 5xx, retry
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.RequestsError(f"Status {response.status_code}")

            if (
                lock_acquired
                and stream
                and hold_session_lock
                and response.status_code < 400
            ):
                response._autoflix_session_lock = session_lock
                lock_acquired = False
            return response

        except Exception as e:
            attempt += 1
            # Simple backoff: waits 0.5s, then 1s, etc.
            time.sleep(0.5 * attempt)
            if attempt >= max_retries:
                return None
        finally:
            if lock_acquired and session_lock:
                session_lock.release()


# ---------------------------------------------------------------------------
# Route: /stream (For .m3u8 files)
# ---------------------------------------------------------------------------
@app.route("/stream")
@app.route("/stream/<path:filename>")
def proxy_stream(filename=None):
    started = time.perf_counter()
    target_url = request.args.get("url")
    headers_str = request.args.get("headers", "")

    if not target_url:
        diagnostics.error("PROXY", "m3u8_fetch", error="missing_url", http_status=400)
        return "Missing URL parameter", 400

    headers = decode_headers_param(headers_str)
    headers, hls_context = _prepare_hls_headers(
        target_url,
        headers,
        route="stream",
        explicit_context=request.args.get("context", ""),
    )
    hls_session, hls_session_id = _get_hls_session_entry(
        target_url,
        headers,
        hls_context,
        requested_id=request.args.get(_HLS_SESSION_PARAM, ""),
    )
    _log_hls_url_diagnostic("stream", target_url, headers, hls_context)

    diagnostics.info(
        "PROXY",
        "m3u8_fetch",
        status="start",
        upstream_domain=diagnostics.domain_of(target_url),
    )

    # 1. Fetch original M3U8 content
    resp = fetch_with_retry(target_url, headers, hls_session=hls_session)
    if not resp or resp.status_code not in [200, 206]:
        _log_hls_url_diagnostic(
            "stream",
            target_url,
            headers,
            hls_context,
            status="upstream_error",
            http_status=getattr(resp, "status_code", None),
            force=True,
        )
        diagnostics.error(
            "PROXY",
            "m3u8_fetch",
            error="upstream_failed",
            upstream_domain=diagnostics.domain_of(target_url),
            http_status=getattr(resp, "status_code", None),
        )
        return "Error fetching upstream m3u8", 502

    content = resp.text
    base_uri = get_base_url(target_url)

    # 2. Parsing with m3u8 library
    try:
        m3u8_obj = m3u8.loads(content, uri=target_url)
    except Exception as e:
        # If parsing fails, return as is (fallback)
        diagnostics.exception(
            "PROXY",
            "m3u8_parse",
            e,
            upstream_domain=diagnostics.domain_of(target_url),
        )
        return Response(content, mimetype="application/vnd.apple.mpegurl")

    # Helper function to build the proxy URL to our routes
    def make_proxy_url(endpoint, original_uri):
        # Absolute URL resolution if relative
        absolute_url = urllib.parse.urljoin(base_uri, original_uri)
        encoded_url = urllib.parse.quote(absolute_url)
        # base64url has no dots, so ffmpeg's strrchr(url, '.') correctly finds
        # the segment file extension (.m4s/.ts) instead of a dot in the headers.
        encoded_headers = encode_headers_token(headers)
        encoded_context = urllib.parse.quote(hls_context) if hls_context else ""
        encoded_session = urllib.parse.quote(hls_session_id) if hls_session_id else ""
        # Embed the basename in the proxy path so ffmpeg sees the real extension
        # (.m4s/.ts/.m3u8) directly in the URL path, not buried in the query string.
        basename = basename_from_uri(absolute_url, "seg")
        context_param = f"&context={encoded_context}" if encoded_context else ""
        session_param = (
            f"&{_HLS_SESSION_PARAM}={encoded_session}" if encoded_session else ""
        )
        return (
            f"http://{PROXY_HOST}:{PROXY_PORT}/{endpoint}/{basename}"
            f"?url={encoded_url}&headers={encoded_headers}{context_param}{session_param}"
        )

    if hls_context == _VIDEASY_CONTEXT:
        _log_playlist_url_samples(content, m3u8_obj, base_uri, headers, hls_context)
        _log_hls_manifest_subtitles(content, base_uri, headers, hls_context)
        new_content = _rewrite_playlist_content(content, m3u8_obj, make_proxy_url)
        return Response(
            new_content,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # 3. Rewriting segments (.ts)
    # We directly modify the m3u8 object or perform string replace if the object is too complex.
    # The most reliable approach is often rewriting the text, but m3u8 obj allows managing keys.

    # If it's a Master Playlist (contains other playlists)
    if m3u8_obj.playlists:
        for p in m3u8_obj.playlists:
            p.uri = make_proxy_url("stream", p.uri)

        # Handle Media (Alternative Audio/Subtitles)
        for m in m3u8_obj.media:
            if m.uri:
                m.uri = make_proxy_url("stream", m.uri)
        diagnostics.info(
            "PROXY",
            "rewrite_master",
            status="success",
            duration_ms=(time.perf_counter() - started) * 1000,
            playlists=len(m3u8_obj.playlists),
            media=len(m3u8_obj.media),
            upstream_domain=diagnostics.domain_of(target_url),
        )

    # If it's a Media Playlist (contains segments)
    else:
        # Rewrite encryption keys (AES-128 etc)
        # CRUCIAL: keys must pass through the proxy otherwise 403/CORS
        for key in m3u8_obj.keys:
            if key and key.uri:
                key.uri = make_proxy_url(
                    "ts", key.uri
                )  # Using /ts to fetch the key (it's just a binary)

        # Rewrite initialization segment (for fMP4 HLS)
        # We also use a regex fallback at the end because m3u8 library sometimes fails to dump the changes to segment_map
        if hasattr(m3u8_obj, "segment_map"):
            for seg_map in m3u8_obj.segment_map:
                if seg_map and seg_map.uri:
                    seg_map.uri = make_proxy_url("ts", seg_map.uri)

        # Rewrite segments
        for segment in m3u8_obj.segments:
            segment.uri = make_proxy_url("ts", segment.uri)
        diagnostics.info(
            "PROXY",
            "rewrite_media",
            status="success",
            duration_ms=(time.perf_counter() - started) * 1000,
            segments=len(m3u8_obj.segments),
            keys=len([key for key in m3u8_obj.keys if key and key.uri]),
            upstream_domain=diagnostics.domain_of(target_url),
        )

    # 4. Rebuild M3U8
    new_content = m3u8_obj.dumps()

    # Regex Fallback for EXT-X-MAP if m3u8 library didn't update the text
    def replace_map_uri(match):
        original_uri = match.group(1)
        # If already proxied (by the object manipulation), skip
        parsed_uri = urllib.parse.urlparse(original_uri)
        if (
            parsed_uri.netloc == f"{PROXY_HOST}:{PROXY_PORT}"
            and (parsed_uri.path == "/ts" or parsed_uri.path.startswith("/ts/"))
        ):
            return match.group(0)

        # It's an un-proxied URI provided by dumps()
        new_uri = make_proxy_url("ts", original_uri)
        return f'#EXT-X-MAP:URI="{new_uri}"'

    new_content = re.sub(r'#EXT-X-MAP:URI="([^"]+)"', replace_map_uri, new_content)

    return Response(
        new_content,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ---------------------------------------------------------------------------
# Catch-all for debugging 404s
# ---------------------------------------------------------------------------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def catch_all(path):
    diagnostics.warning("PROXY", "not_found", path=path, http_status=404)
    return f"Not Found: {path}", 404


# ---------------------------------------------------------------------------
# Route: /ts (For video segments and keys)
# ---------------------------------------------------------------------------
@app.route("/ts")
@app.route("/ts/<path:filename>")
def proxy_ts(filename=None):
    target_url = request.args.get("url")
    headers_str = request.args.get("headers", "")

    if not target_url:
        _log_segment_error("missing_url")
        return "Missing URL", 400

    headers = decode_headers_param(headers_str)
    headers, _hls_context = _prepare_hls_headers(
        target_url,
        headers,
        route="ts",
        explicit_context=request.args.get("context", ""),
    )
    hls_session, _hls_session_id = _get_hls_session_entry(
        target_url,
        headers,
        _hls_context,
        requested_id=request.args.get(_HLS_SESSION_PARAM, ""),
    )
    _log_hls_url_diagnostic("ts", target_url, headers, _hls_context)

    # Fetch in stream mode
    resp = fetch_with_retry(
        target_url,
        headers,
        stream=True,
        hls_session=hls_session,
        hold_session_lock=bool(hls_session),
    )
    if not resp:
        _log_hls_url_diagnostic(
            "ts",
            target_url,
            headers,
            _hls_context,
            status="upstream_error",
        )
        _log_segment_error("fetch_failed", target_url)
        return "Error fetching segment", 502
    if resp.status_code >= 400:
        _log_hls_url_diagnostic(
            "ts",
            target_url,
            headers,
            _hls_context,
            status="upstream_error",
            http_status=resp.status_code,
        )
        _log_segment_error(f"status_{resp.status_code}", target_url, resp.status_code)

    # Force Content-Type so VLC doesn't bug if the server sends .html
    # video/mp2t is the standard for TS segments
    response_headers = {
        "Content-Type": "video/mp2t",
        "Access-Control-Allow-Origin": "*",
    }

    # Use stream_with_context to return chunks as they come
    # This is where memory efficiency happens
    def generate():
        pts_probe = b""
        pts_checked = bool(hls_session and hls_session.get("pts_logged"))
        parsed_target = urllib.parse.urlparse(target_url)
        target_path = (parsed_target.path or "").lower()
        should_probe_pts = _hls_context == _VIDEASY_CONTEXT and not pts_checked
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    if should_probe_pts and not pts_checked:
                        pts_probe = (pts_probe + chunk)[: 188 * 64]
                        pts_start = extract_mpegts_start_timestamp(pts_probe)
                        ts_like = (
                            pts_probe.startswith(b"\x47")
                            or target_path.endswith(".ts")
                            or "/seg" in target_path
                        )
                        if pts_start is not None or (ts_like and len(pts_probe) >= 188 * 3):
                            _record_videasy_pts_start(
                                pts_start,
                                target_url,
                                hls_session=hls_session,
                            )
                            pts_checked = True
                    yield chunk
        finally:
            _release_response_session_lock(resp)

    return Response(
        stream_with_context(generate()),
        status=resp.status_code,
        headers=response_headers,
    )


# ---------------------------------------------------------------------------
# Route: /video (For single MP4 files with Seeking)
# ---------------------------------------------------------------------------
@app.route("/video")
@app.route("/video/<path:filename>")
def proxy_video(filename=None):
    started = time.perf_counter()
    target_url = request.args.get("url")
    headers_str = request.args.get("headers", "")

    if not target_url:
        diagnostics.error("PROXY", "mp4_fetch", error="missing_url", http_status=400)
        return "Missing URL", 400

    headers = decode_headers_param(headers_str)

    # Fetch stream
    range_header = request.headers.get("Range", "")
    diagnostics.info(
        "PROXY",
        "mp4_fetch",
        status="start",
        upstream_domain=diagnostics.domain_of(target_url),
        range=range_header,
    )
    resp = fetch_with_retry(target_url, headers, stream=True)
    if not resp:
        diagnostics.error(
            "PROXY",
            "mp4_fetch",
            error="upstream_failed",
            upstream_domain=diagnostics.domain_of(target_url),
        )
        return "Error fetching video", 502

    # Handle response headers for seeking
    excluded_headers = [
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    ]
    response_headers = [
        (k, v) for k, v in resp.headers.items() if k.lower() not in excluded_headers
    ]

    # Forward Content-Length if available so VLC knows duration/size
    if "Content-Length" in resp.headers:
        response_headers.append(("Content-Length", resp.headers["Content-Length"]))

    # Support for Range Request (Partial Content 206)
    status_code = resp.status_code
    diagnostics.info(
        "PROXY",
        "mp4_fetch",
        status="success" if status_code < 400 else "error",
        duration_ms=(time.perf_counter() - started) * 1000,
        upstream_domain=diagnostics.domain_of(target_url),
        http_status=status_code,
        range=range_header,
    )

    def generate():
        for chunk in resp.iter_content(
            chunk_size=16384
        ):  # Slightly larger chunks for MP4
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()), status=status_code, headers=response_headers
    )


# ---------------------------------------------------------------------------
# Routes: Web Player & Heartbeat
# ---------------------------------------------------------------------------
@app.route("/player")
def proxy_player_ui():
    html_content = """<!DOCTYPE html>
<html>
<head>
    <title>AutoFlix Web Player</title>
    <meta charset="utf-8">
    <link rel="stylesheet" href="https://cdn.plyr.io/3.7.8/plyr.css" />
    <style>
        body, html { margin: 0; padding: 0; width: 100%; height: 100%; background-color: #000; overflow: hidden; font-family: sans-serif; }
        .plyr { width: 100%; height: 100%; }
        #controls-overlay { position: absolute; top: 20px; right: 20px; z-index: 1000; opacity: 0; transition: opacity 0.3s; }
        body:hover #controls-overlay, .plyr--active #controls-overlay { opacity: 1; }
        .action-btn { background-color: rgba(255, 0, 0, 0.7); color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-size: 16px; font-weight: bold; }
        .action-btn:hover { background-color: rgba(255, 0, 0, 1); }
        .message { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: white; font-size: 24px; display: none; text-align: center; z-index: 2000; }
        .message button { margin-top: 20px; padding: 10px 20px; font-size: 18px; cursor: pointer; }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script src="https://cdn.plyr.io/3.7.8/plyr.js"></script>
</head>
<body>
    <div id="controls-overlay">
        <button id="closeBtn" class="action-btn">Mark as watched & Close</button>
    </div>
    
    <video id="video" controls crossorigin="anonymous" playsinline>
        <!-- Title and captions will be injected via JS -->
    </video>
    
    <div id="finishedMsg" class="message">
        Video finished! You can safely close this tab.<br>
        <button onclick="window.close()">Close Tab</button>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", () => {
            const video = document.getElementById('video');
            const urlParams = new URLSearchParams(window.location.search);
            const source = urlParams.get('url');
            const subPath = urlParams.get('sub_path');
            
            const isMp4 = source && source.indexOf('/video') !== -1;
            const closeBtn = document.getElementById('closeBtn');

            // Setup subtitle track if provided
            if (subPath) {
                const track = document.createElement('track');
                track.kind = 'captions';
                track.label = 'Subtitles';
                track.src = '/player/subtitle?path=' + encodeURIComponent(subPath);
                track.default = true;
                video.appendChild(track);
            }

            const defaultOptions = {
                captions: { active: true, update: true, language: 'auto' },
                controls: [
                    'play-large', 'play', 'progress', 'current-time', 'mute', 'volume',
                    'captions', 'settings', 'pip', 'airplay', 'fullscreen'
                ],
                settings: ['captions', 'quality', 'speed']
            };

            let player;

            if (source) {
                if (isMp4 || !Hls.isSupported()) {
                    // Native playback for MP4 or native HLS (Safari)
                    video.src = source;
                    player = new Plyr(video, defaultOptions);
                    player.play();
                } else {
                    // hls.js for M3U8 with quality selection
                    const hls = new Hls({
                        xhrSetup: function(xhr, url) {
                            xhr.withCredentials = false; // Important to avoid CORS issues if not needed
                        }
                    });
                    
                    hls.loadSource(source);
                    hls.attachMedia(video);
                    
                    hls.on(Hls.Events.MANIFEST_PARSED, function (event, data) {
                        // Extract available qualities
                        const availableQualities = hls.levels.map((l) => l.height);
                        // Add Auto option
                        availableQualities.unshift(0); 

                        defaultOptions.quality = {
                            default: 0, // 0 means auto
                            options: availableQualities,
                            forced: true,
                            onChange: (e) => updateQuality(e),
                        };
                        // Custom labels for the qualities
                        defaultOptions.i18n = {
                            qualityLabel: {
                                0: 'Auto',
                            },
                        };

                        player = new Plyr(video, defaultOptions);
                        
                        // Play immediately after setup
                        player.play();
                    });

                    // Recover from errors
                    hls.on(Hls.Events.ERROR, function(event, data) {
                        if (data.fatal) {
                            switch (data.type) {
                                case Hls.ErrorTypes.NETWORK_ERROR:
                                    console.error("Fatal network error encountered, try to recover");
                                    hls.startLoad();
                                    break;
                                case Hls.ErrorTypes.MEDIA_ERROR:
                                    console.error("Fatal media error encountered, try to recover");
                                    hls.recoverMediaError();
                                    break;
                                default:
                                    hls.destroy();
                                    break;
                            }
                        }
                    });

                    function updateQuality(newQuality) {
                        if (newQuality === 0) {
                            window.hls.currentLevel = -1; // -1 triggers auto level
                        } else {
                            // Find the index of the level matching the requested height
                            const levelIndex = hls.levels.findIndex((l) => l.height === newQuality);
                            if (levelIndex !== -1) {
                                hls.currentLevel = levelIndex;
                            }
                        }
                    }
                    window.hls = hls; // Make available globally for quality update
                }
            }

            // Heartbeat logic
            let heartbeatInterval = setInterval(() => {
                fetch('/player/heartbeat').catch(e => console.log('Heartbeat failed'));
            }, 2000);

            function endPlayback() {
                clearInterval(heartbeatInterval);
                fetch('/player/end').then(() => {
                    document.getElementById('finishedMsg').style.display = 'block';
                    document.getElementById('controls-overlay').style.display = 'none';
                    if(player) {
                        player.destroy();
                    } else {
                        video.style.display = 'none';
                    }
                    // Try to close tab automatically
                    setTimeout(() => window.close(), 1000);
                }).catch(e => {
                    // Fallback UI
                    document.getElementById('finishedMsg').style.display = 'block';
                    if(player) {
                        player.destroy();
                    } else {
                        video.style.display = 'none';
                    }
                });
            }

            // Listen to video native 'ended' event
            video.addEventListener('ended', endPlayback);
            closeBtn.addEventListener('click', endPlayback);
        });
    </script>
</body>
</html>"""
    return Response(html_content, mimetype="text/html")


@app.route("/player/subtitle")
def proxy_player_subtitle():
    import os

    sub_path = request.args.get("path")
    if not sub_path or not os.path.exists(sub_path):
        return "Subtitle not found", 404

    try:
        with open(sub_path, "rb") as f:
            content = f.read()

        # Standardize SRT to WebVTT for HTML5 video <track> compatibility
        if sub_path.lower().endswith(".srt"):
            text = content.decode("utf-8", errors="ignore")
            # Replace timestamps ',' with '.' for VTT format e.g: 00:00:10,500 -> 00:00:10.500
            import re

            vtt_content = "WEBVTT\n\n" + re.sub(
                r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text
            )
            return Response(
                vtt_content,
                mimetype="text/vtt",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        return Response(
            content, mimetype="text/vtt", headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        return f"Error loading subtitle: {e}", 500


@app.route("/player/heartbeat")
def proxy_player_heartbeat():
    global player_heartbeat_time
    # Update global timestamp of the heartbeat
    player_heartbeat_time = time.time()
    return "ok", 200


@app.route("/player/end")
def proxy_player_end():
    global player_finished_event
    player_finished_event.set()
    return "ok", 200


# ---------------------------------------------------------------------------
# Server Launch
# ---------------------------------------------------------------------------
def run_flask(port):
    global _server_instance
    # Disable verbose flask/werkzeug logs for performance
    import logging
    from werkzeug.serving import make_server

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    _server_instance = make_server(PROXY_HOST, port, app, threaded=True)
    _server_instance.serve_forever()


def start_proxy_server(port=0):
    global PROXY_PORT, PROXY_URL

    if port == 0:
        port = find_free_port()

    PROXY_PORT = port
    PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

    # Launch in a Daemon thread (stops when the main program stops)
    t = threading.Thread(target=run_flask, args=(port,))
    t.daemon = True
    t.start()

    diagnostics.info("PROXY", "start", status="success", url=PROXY_URL)
    if not diagnostics.is_enabled():
        print(f"[*] M3U8 Proxy started on http://{PROXY_HOST}:{PROXY_PORT}")
    return port


def stop_proxy_server():
    """Shuts down the proxy server gracefully."""
    global _server_instance
    if _server_instance:
        diagnostics.info("PROXY", "stop", status="ok", url=PROXY_URL)
        _close_hls_sessions()
        _server_instance.shutdown()
        _server_instance = None


# ---------------------------------------------------------------------------
# Usage Example (if run directly)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Start the proxy
    my_port = start_proxy_server(0)

    # This simulates your main application
    print("Main application running... Press Ctrl+C to quit.")

    # Example URL for VLC (only works with a real source URL)
    # url_source = "https://example.com/master.m3u8"
    # headers_source = {"User-Agent": "Mozilla/5.0 ...", "Referer": "https://example.com"}
    # encoded_url = urllib.parse.quote(url_source)
    # encoded_headers = urllib.parse.quote(json.dumps(headers_source))
    # print(f"Link for VLC: http://127.0.0.1:{my_port}/stream?url={encoded_url}&headers={encoded_headers}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping.")
