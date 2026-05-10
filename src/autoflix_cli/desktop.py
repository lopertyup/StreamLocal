import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Optional

from . import proxy
from .app import autostart, diagnostics
from .app.server import LocalAppServer, create_app
from .app.store import APP_ID, APP_NAME, DesktopStore


def _project_version() -> str:
    try:
        return version("autoflix-cli")
    except PackageNotFoundError:
        return "dev"


class DesktopApi:
    def __init__(self) -> None:
        self._window = None
        self._fullscreen = False

    def bind_window(self, window) -> None:
        self._window = window

    def set_fullscreen(self, enabled: bool) -> dict[str, object]:
        if not self._window or not hasattr(self._window, "toggle_fullscreen"):
            return {"supported": False, "fullscreen": self._fullscreen}

        desired = bool(enabled)
        if desired != self._fullscreen:
            try:
                self._window.toggle_fullscreen()
            except Exception as exc:  # pragma: no cover - depends on native GUI backend
                return {
                    "supported": False,
                    "fullscreen": self._fullscreen,
                    "error": str(exc),
                }
            self._fullscreen = desired

        return {"supported": True, "fullscreen": self._fullscreen}


class DesktopRuntime:
    def __init__(self, proxy_module=proxy) -> None:
        self.proxy_module = proxy_module
        self.server: Optional[LocalAppServer] = None
        self._stopped = False
        self._lock = threading.RLock()

    def start_proxy(self) -> None:
        self.proxy_module.start_proxy_server()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                if self.server:
                    self.server.stop()
            finally:
                self.server = None
                self.proxy_module.stop_proxy_server()


def _windows_autoflix_process_snapshot() -> list[dict[str, object]]:
    if sys.platform != "win32":
        return []

    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in @('python.exe','pythonw.exe') -or $_.Name -like '*AutoFlix*' } | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creationflags,
        )
    except Exception as exc:
        diagnostics.warning("DESKTOP", "process_snapshot_failed", error=str(exc))
        return []

    payload = result.stdout.strip()
    if result.returncode != 0 or not payload:
        return []

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        diagnostics.warning("DESKTOP", "process_snapshot_parse_failed", error=str(exc))
        return []

    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _is_autoflix_serve_only_command(command_line: object) -> bool:
    command = str(command_line or "").lower()
    if "--serve-only" not in command:
        return False
    return "autoflix_cli.desktop" in command or "autoflix" in command


class SingleInstanceManager:
    """Coordinates handoff between successive AutoFlix desktop launches."""

    RUNTIME_FILENAME = "desktop-runtime.json"

    def __init__(
        self,
        data_dir: Path,
        request_timeout: float = 1.5,
        grace_seconds: float = 2.0,
        urlopen: Optional[Callable[..., object]] = None,
        kill_func: Optional[Callable[[int, int], None]] = None,
        process_snapshot_func: Optional[Callable[[], list[dict[str, object]]]] = None,
        sleep_func: Callable[[float], None] = time.sleep,
        pid_func: Callable[[], int] = os.getpid,
        parent_pid_func: Callable[[], int] = os.getppid,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.runtime_file = self.data_dir / self.RUNTIME_FILENAME
        self.request_timeout = request_timeout
        self.grace_seconds = grace_seconds
        self.token = secrets.token_urlsafe(24)
        self._urlopen = urlopen or urllib.request.urlopen
        self._kill = kill_func or os.kill
        self._process_snapshot = process_snapshot_func or _windows_autoflix_process_snapshot
        self._sleep = sleep_func
        self._pid_func = pid_func
        self._parent_pid_func = parent_pid_func

    def close_previous(self) -> bool:
        previous = self._read_runtime(unlink_invalid=True)
        if not previous:
            return False

        pid = self._clean_pid(previous.get("pid"))
        url = str(previous.get("url") or "").strip()
        token = str(previous.get("token") or "").strip()
        if pid == self._pid_func():
            return False

        responded = False
        if url and token:
            responded = self._request_shutdown(url, token)

        if pid:
            if responded:
                self._wait_until_dead(pid)
            if not responded or self._pid_exists(pid):
                self._terminate_pid(pid)

        return responded

    def close_legacy_serve_only_instances(self) -> int:
        current_pid = self._pid_func()
        parent_pid = self._parent_pid_func()
        closed = 0
        for process in self._process_snapshot():
            pid = self._clean_pid(process.get("ProcessId"))
            if not pid or pid in {current_pid, parent_pid}:
                continue
            if not _is_autoflix_serve_only_command(process.get("CommandLine")):
                continue
            if self._terminate_pid(pid):
                closed += 1
        if closed:
            diagnostics.info("DESKTOP", "legacy_serve_only_closed", status="ok", count=closed)
        return closed

    def write_current(self, url: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": self._pid_func(),
            "url": url,
            "token": self.token,
            "updated_at": time.time(),
        }
        with open(self.runtime_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def clear_current(self) -> None:
        current = self._read_runtime(unlink_invalid=False)
        if not current:
            return
        if current.get("pid") != self._pid_func() or current.get("token") != self.token:
            return
        try:
            self.runtime_file.unlink()
        except OSError:
            pass

    def _read_runtime(self, unlink_invalid: bool) -> Optional[dict[str, object]]:
        if not self.runtime_file.exists():
            return None
        try:
            with open(self.runtime_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        if unlink_invalid:
            try:
                self.runtime_file.unlink()
            except OSError:
                pass
        return None

    def _request_shutdown(self, url: str, token: str) -> bool:
        endpoint = url.rstrip("/") + "/api/internal/shutdown"
        body = json.dumps({"token": token}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-AutoFlix-Token": token,
            },
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self.request_timeout) as response:
                code = response.getcode() if hasattr(response, "getcode") else 200
                return 200 <= int(code) < 300
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            return False

    def _wait_until_dead(self, pid: int) -> bool:
        deadline = time.monotonic() + max(0.0, self.grace_seconds)
        while time.monotonic() < deadline:
            if not self._pid_exists(pid):
                return True
            self._sleep(0.1)
        return not self._pid_exists(pid)

    def _pid_exists(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            self._kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def _terminate_pid(self, pid: int) -> bool:
        if pid <= 0 or pid == self._pid_func():
            return False
        try:
            self._kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False

    @staticmethod
    def _clean_pid(value: object) -> Optional[int]:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None


class TrayController:
    def __init__(
        self,
        title: str = APP_NAME,
        on_quit: Optional[Callable[[], None]] = None,
        minimize_to_tray: bool = True,
    ) -> None:
        self.title = title
        self.on_quit = on_quit
        self.minimize_to_tray = minimize_to_tray
        self.icon = None
        self.window = None
        self._pystray = None
        self._quitting = False

    def start(self) -> bool:
        try:
            import pystray
        except ImportError as exc:
            diagnostics.warning("TRAY", "unavailable", error=str(exc))
            return False

        image = self._create_image()
        if image is None:
            return False

        self._pystray = pystray
        menu = pystray.Menu(
            pystray.MenuItem(f"Ouvrir {self.title}", self._open_from_menu, default=True),
            pystray.MenuItem("Quitter", self._quit_from_menu),
        )
        self.icon = pystray.Icon(APP_ID, image, self.title, menu)
        try:
            if hasattr(self.icon, "run_detached"):
                self.icon.run_detached()
            else:
                threading.Thread(target=self.icon.run, daemon=True).start()
            diagnostics.info("TRAY", "start", status="ok")
            return True
        except Exception as exc:
            diagnostics.warning("TRAY", "start_failed", error=str(exc))
            self.icon = None
            return False

    def stop(self) -> None:
        icon = self.icon
        self.icon = None
        if icon:
            try:
                icon.stop()
            except Exception as exc:
                diagnostics.warning("TRAY", "stop_failed", error=str(exc))

    def attach_window(self, window) -> None:
        self.window = window
        try:
            window.events.closing += self._on_window_closing
        except Exception as exc:
            diagnostics.warning("TRAY", "closing_hook_failed", error=str(exc))

    def show_window(self) -> None:
        if not self.window:
            return
        try:
            self.window.show()
            if hasattr(self.window, "restore"):
                self.window.restore()
            if hasattr(self.window, "maximize"):
                self.window.maximize()
        except Exception as exc:
            diagnostics.warning("TRAY", "show_failed", error=str(exc))

    def hide_window(self) -> None:
        if not self.window:
            return
        try:
            self.window.hide()
            diagnostics.info("TRAY", "window_hidden", status="ok")
        except Exception as exc:
            diagnostics.warning("TRAY", "hide_failed", error=str(exc))

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.stop()
        if self.window:
            try:
                self.window.destroy()
            except Exception as exc:
                diagnostics.warning("TRAY", "destroy_failed", error=str(exc))
            finally:
                self.window = None
        if self.on_quit:
            self.on_quit()

    def _on_window_closing(self):
        if self._quitting or not self.minimize_to_tray or not self.icon:
            return None
        threading.Timer(0.05, self.hide_window).start()
        return False

    def _open_from_menu(self, icon=None, item=None) -> None:
        del icon, item
        self.show_window()

    def _quit_from_menu(self, icon=None, item=None) -> None:
        del icon, item
        self.quit()

    def _create_image(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError as exc:
            diagnostics.warning("TRAY", "pillow_unavailable", error=str(exc))
            return None

        for icon_path in _candidate_icon_paths():
            if not icon_path.is_file():
                continue
            try:
                with Image.open(icon_path) as image:
                    return image.convert("RGBA")
            except Exception:
                continue

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, 60, 60), fill=(216, 59, 52, 255))
        draw.rectangle((19, 17, 29, 47), fill=(255, 255, 255, 255))
        draw.polygon([(34, 17), (50, 32), (34, 47)], fill=(255, 255, 255, 255))
        return image


def _candidate_icon_paths():
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    candidates = [
        base_dir / "build" / "autoflix.ico",
        Path(__file__).resolve().parents[2] / "build" / "autoflix.ico",
        Path(sys.executable).with_suffix(".ico"),
    ]
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            yield candidate


def _wait_forever() -> None:
    while True:
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Lance {APP_NAME} Desktop.")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Ouvrir l'interface dans le navigateur au lieu d'une fenetre native.",
    )
    parser.add_argument(
        "--serve-only",
        action="store_true",
        help="Demarrer seulement le serveur local, sans ouvrir de fenetre.",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Demarrer cache dans la zone de notification pour garder le suivi actif.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port HTTP local. 0 choisit un port libre automatiquement.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Activer les logs diagnostics (terminal + logs/) et les DevTools natifs.",
    )
    args = parser.parse_args()

    session = None
    if args.dev:
        session = diagnostics.configure(Path("logs"))
        diagnostics.info("SESSION", "start", status="start")

    store = DesktopStore()
    single_instance = SingleInstanceManager(store.data_file.parent)
    single_instance.close_previous()
    single_instance.close_legacy_serve_only_instances()
    runtime = DesktopRuntime()
    tray: Optional[TrayController] = None
    stopped = False
    stop_lock = threading.RLock()

    def stop_current_instance() -> None:
        nonlocal stopped
        with stop_lock:
            if stopped:
                return
            stopped = True
            if tray:
                tray.stop()
            runtime.stop()
            single_instance.clear_current()
            single_instance.close_legacy_serve_only_instances()

    def exit_current_instance(delay: float = 0.0) -> None:
        if delay > 0:
            time.sleep(delay)
        try:
            stop_current_instance()
            if args.dev:
                diagnostics.info("SESSION", "stop", status="ok")
                diagnostics.emit_summary()
                diagnostics.shutdown()
        finally:
            os._exit(0)

    def shutdown_current_instance() -> None:
        exit_current_instance(delay=0.1)

    try:
        runtime.start_proxy()

        app = create_app(
            store=store,
            shutdown_token=single_instance.token,
            shutdown_callback=shutdown_current_instance,
        )
        runtime.server = LocalAppServer(app=app, port=args.port)
        url = runtime.server.start()
        single_instance.write_current(url)
        autostart.sync_preferences(store.get_preferences())

        if args.dev and session:
            diagnostics.emit_runtime_banner(
                version=_project_version(),
                data_dir=store.data_file.parent,
                app_url=url,
                proxy_url=proxy.PROXY_URL or "-",
                log_file=session.log_file,
            )

        if args.serve_only:
            print(f"{APP_NAME} Desktop: {url}", flush=True)
            if args.dev:
                diagnostics.info("BROWSER", "skip", status="ok", reason="--serve-only")
            _wait_forever()

        elif args.browser and not args.background:
            opened = webbrowser.open(url)
            print(f"{APP_NAME} Desktop: {url}")
            if args.dev:
                diagnostics.info(
                    "BROWSER",
                    "open",
                    status="ok" if opened else "warning",
                    url=url,
                    target="external",
                )
            _wait_forever()
        else:
            try:
                import webview
            except ImportError as exc:
                if args.background:
                    if args.dev:
                        diagnostics.info("BROWSER", "background_failed", status="error", reason="pywebview missing")
                    raise SystemExit(
                        "Impossible de lancer AutoFlix en arriere-plan sans fenetre native. "
                        "Installe pywebview ou lance `autoflix-desktop --browser`."
                    ) from exc
                raise SystemExit(
                    "pywebview n'est pas installe. Installe les dependances ou lance "
                    "`autoflix-desktop --browser`."
                ) from exc

            preferences = store.get_preferences()
            minimize_to_tray = bool(preferences.get("minimize_to_tray", True)) or args.background
            tray = TrayController(on_quit=exit_current_instance, minimize_to_tray=minimize_to_tray)
            tray_started = tray.start()
            if args.background and not tray_started:
                if args.dev:
                    diagnostics.info("TRAY", "background_failed", status="error", reason="tray unavailable")
                raise SystemExit(
                    "Impossible de lancer AutoFlix en arriere-plan sans icone de notification."
                )

            desktop_api = DesktopApi()
            window = webview.create_window(
                APP_NAME,
                url,
                width=1280,
                height=820,
                min_size=(980, 640),
                text_select=True,
                maximized=not args.background,
                hidden=args.background,
                js_api=desktop_api,
            )
            desktop_api.bind_window(window)
            if tray_started:
                tray.attach_window(window)
            if args.dev:
                diagnostics.info(
                    "BROWSER",
                    "open",
                    status="ok",
                    url=url,
                    target="pywebview",
                    devtools=True,
                )
            webview.start(debug=args.dev)
    except KeyboardInterrupt:
        if args.dev:
            diagnostics.info("SESSION", "interrupt", status="ok")
    finally:
        stop_current_instance()
        if args.dev:
            diagnostics.info("SESSION", "stop", status="ok")
            diagnostics.emit_summary()
            diagnostics.shutdown()


if __name__ == "__main__":
    main()
