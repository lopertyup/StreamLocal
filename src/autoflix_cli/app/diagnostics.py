from __future__ import annotations

import platform
import re
import sys
import threading
import time
import traceback
import uuid
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlparse


SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "headers",
    "password",
    "proxy-authorization",
    "set-cookie",
    "token",
    "x-api-key",
}
MAX_VALUE_LENGTH = 180
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_session: Optional["DiagnosticSession"] = None


def _now() -> datetime:
    return datetime.now()


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEYS)


def _mask_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value

    path = parsed.path or "/"
    if len(path) > 48:
        path = path[:45] + "..."
    suffix = "?..." if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{suffix}"


def mask_urls(value: str) -> str:
    return URL_RE.sub(lambda match: _mask_url(match.group(0)), value)


def domain_of(value: str) -> str:
    parsed = urlparse(value or "")
    return parsed.netloc.lower()


def sanitize_value(value: Any, key: str = "") -> Any:
    if _is_sensitive_key(key):
        if isinstance(value, dict):
            return {
                str(item_key): sanitize_value(item_value, str(item_key))
                for item_key, item_value in value.items()
            }
        if value:
            return "<masked>"
        return value

    if isinstance(value, dict):
        return {
            str(item_key): sanitize_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, key) for item in value[:12]]
    if isinstance(value, str):
        cleaned = mask_urls(value.replace("\r", " ").replace("\n", " "))
        if len(cleaned) > MAX_VALUE_LENGTH:
            cleaned = cleaned[: MAX_VALUE_LENGTH - 3] + "..."
        return cleaned
    return value


def sanitize_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {key: sanitize_value(value, key) for key, value in fields.items()}


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        text = repr(value)
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    if not text:
        return '""'
    if re.search(r"\s|=", text):
        text = text.replace('"', '\\"')
        return f'"{text}"'
    return text


def _short_traceback(exc: BaseException) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tail = [line.strip() for line in lines[-6:] if line.strip()]
    return sanitize_value(" | ".join(tail), "traceback")


@dataclass
class DiagnosticSession:
    log_file: Path
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: datetime = field(default_factory=_now)
    searches: int = 0
    api_errors: int = 0
    proxy_errors: int = 0
    playback_attempts: int = 0
    playback_success: int = 0
    provider_ok: Counter = field(default_factory=Counter)
    provider_ko: Counter = field(default_factory=Counter)
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=8))
    last_context: str = "-"

    def __post_init__(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.log_file, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._summary_emitted = False

    def close(self) -> None:
        with self._lock:
            self._fh.close()

    def banner(self, fields: Dict[str, Any]) -> None:
        sanitized = sanitize_fields(fields)
        lines = [
            "=" * 78,
            f"AutoFlix dev session {self.session_id}",
            "-" * 78,
        ]
        lines.extend(f"{key}: {_format_value(value)}" for key, value in sanitized.items())
        lines.append("=" * 78)
        block = "\n".join(lines)
        with self._lock:
            print(block, flush=True)
            self._fh.write(block + "\n")
            self._fh.flush()

    def log(
        self,
        level: str,
        component: str,
        action: str,
        status: str = "ok",
        duration_ms: Optional[float] = None,
        **fields: Any,
    ) -> None:
        fields = sanitize_fields(fields)
        if duration_ms is not None:
            fields = {"duration_ms": int(duration_ms), **fields}

        self._record(component, action, status, fields)
        timestamp = _now().strftime("%H:%M:%S.%f")[:-3]
        parts = [
            timestamp,
            level.upper().ljust(5),
            f"sid={self.session_id}",
            f"component={component}",
            f"action={action}",
            f"status={status}",
        ]
        parts.extend(f"{key}={_format_value(value)}" for key, value in fields.items())
        line = " ".join(parts)

        with self._lock:
            print(line, flush=True)
            self._fh.write(line + "\n")
            self._fh.flush()

    def log_exception(
        self,
        component: str,
        action: str,
        exc: BaseException,
        duration_ms: Optional[float] = None,
        **fields: Any,
    ) -> None:
        self.log(
            "ERROR",
            component,
            action,
            status="error",
            duration_ms=duration_ms,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=_short_traceback(exc),
            **fields,
        )

    def summary_lines(self) -> list[str]:
        duration_s = int((_now() - self.started_at).total_seconds())
        providers_ok = ", ".join(
            f"{name}:{count}" for name, count in sorted(self.provider_ok.items())
        ) or "-"
        providers_ko = ", ".join(
            f"{name}:{count}" for name, count in sorted(self.provider_ko.items())
        ) or "-"
        lines = [
            "=" * 78,
            f"AutoFlix dev summary {self.session_id}",
            "-" * 78,
            f"duration_s: {duration_s}",
            f"searches: {self.searches}",
            f"providers_ok: {providers_ok}",
            f"providers_ko: {providers_ko}",
            f"api_errors: {self.api_errors}",
            f"proxy_errors: {self.proxy_errors}",
            f"playback: attempted={self.playback_attempts} succeeded={self.playback_success}",
            f"last_context: {self.last_context}",
        ]
        if self.recent_errors:
            lines.append("recent_errors:")
            lines.extend(f"- {item}" for item in self.recent_errors)
        lines.append("=" * 78)
        return lines

    def emit_summary(self) -> None:
        if self._summary_emitted:
            return
        self._summary_emitted = True
        block = "\n".join(self.summary_lines())
        with self._lock:
            print(block, flush=True)
            self._fh.write(block + "\n")
            self._fh.flush()

    def _record(
        self, component: str, action: str, status: str, fields: Dict[str, Any]
    ) -> None:
        if component == "API":
            http_status = fields.get("http_status")
            if isinstance(http_status, int) and http_status >= 400:
                self.api_errors += 1
            if status == "error":
                self.api_errors += 1
        elif component == "PROXY" and status == "error":
            self.proxy_errors += 1
        elif component == "PROVIDER":
            provider = str(fields.get("provider") or "-")
            if status == "success":
                self.provider_ok[provider] += 1
            elif status == "error":
                self.provider_ko[provider] += 1
        elif component == "UI" and action == "search":
            self.searches += 1
        elif component == "PLAYBACK":
            if action == "start":
                self.playback_attempts += 1
            elif action == "ready" and status == "success":
                self.playback_success += 1

        if fields.get("context"):
            self.last_context = str(fields["context"])
        elif fields.get("title"):
            self.last_context = str(fields["title"])

        if status == "error":
            error = fields.get("error") or fields.get("message") or action
            self.recent_errors.append(f"{component}.{action}: {error}")


def configure(log_dir: Path = Path("logs")) -> DiagnosticSession:
    global _session
    timestamp = _now().strftime("%Y%m%d-%H%M%S")
    _session = DiagnosticSession(log_dir / f"autoflix-desktop-{timestamp}.log")
    return _session


def current() -> Optional[DiagnosticSession]:
    return _session


def is_enabled() -> bool:
    return _session is not None


def log(level: str, component: str, action: str, status: str = "ok", **fields: Any) -> None:
    if _session:
        _session.log(level, component, action, status=status, **fields)


def info(component: str, action: str, status: str = "ok", **fields: Any) -> None:
    log("INFO", component, action, status=status, **fields)


def warning(component: str, action: str, status: str = "warning", **fields: Any) -> None:
    log("WARN", component, action, status=status, **fields)


def error(component: str, action: str, status: str = "error", **fields: Any) -> None:
    log("ERROR", component, action, status=status, **fields)


def exception(component: str, action: str, exc: BaseException, **fields: Any) -> None:
    if _session:
        _session.log_exception(component, action, exc, **fields)


@contextmanager
def operation(component: str, action: str, **fields: Any) -> Iterator[None]:
    if not _session:
        yield
        return

    started = time.perf_counter()
    _session.log("INFO", component, action, status="start", **fields)
    try:
        yield
    except Exception as exc:
        _session.log_exception(
            component,
            action,
            exc,
            duration_ms=(time.perf_counter() - started) * 1000,
            **fields,
        )
        raise
    else:
        _session.log(
            "INFO",
            component,
            action,
            status="success",
            duration_ms=(time.perf_counter() - started) * 1000,
            **fields,
        )


def emit_runtime_banner(
    *,
    version: str,
    data_dir: Path,
    app_url: str,
    proxy_url: str,
    log_file: Path,
) -> None:
    if not _session:
        return
    _session.banner(
        {
            "version": version,
            "python": sys.version.split()[0],
            "os": f"{platform.system()} {platform.release()}",
            "data_dir": str(data_dir),
            "app_url": app_url,
            "proxy_url": proxy_url,
            "started_at": _session.started_at.isoformat(timespec="seconds"),
            "log_file": str(log_file),
        }
    )


def emit_summary() -> None:
    if _session:
        _session.emit_summary()


def shutdown() -> None:
    global _session
    if _session:
        _session.close()
        _session = None
