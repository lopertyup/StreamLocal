import json
import signal
import sys
from types import SimpleNamespace

from autoflix_cli.app import autostart
from autoflix_cli.app.server import create_app
from autoflix_cli.app.store import DesktopStore
from autoflix_cli.desktop import DesktopRuntime, SingleInstanceManager, TrayController


class FakeRegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 1
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.opened = []

    def CreateKeyEx(self, root, path, reserved, access):
        self.opened.append((root, path, reserved, access))
        return FakeRegistryKey()

    def SetValueEx(self, key, name, reserved, value_type, value):
        del key, reserved, value_type
        self.values[name] = value

    def DeleteValue(self, key, name):
        del key
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def stop_app(app):
    app.config["AUTOFLIX_TRACKER"].stop()
    app.config["AUTOFLIX_DOWNLOADS"].stop()


def test_windows_autostart_registry_create_and_delete(monkeypatch):
    fake_winreg = FakeWinreg()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert autostart.set_start_with_windows(True, command=r"C:\AutoFlix.exe --background") is True
    assert fake_winreg.values == {"AutoFlix": r"C:\AutoFlix.exe --background"}
    assert fake_winreg.opened[-1][1] == autostart.RUN_KEY

    assert autostart.set_start_with_windows(False) is True
    assert fake_winreg.values == {}


def test_autostart_command_uses_background_mode(monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\Python311\python.exe")
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    source_command = autostart.build_autostart_command()
    assert "-m autoflix_cli.desktop --background" in source_command

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen_command = autostart.build_autostart_command()
    assert frozen_command.endswith("--background")
    assert "autoflix_cli.desktop" not in frozen_command


def test_settings_persist_tray_and_autostart_preferences(tmp_path, monkeypatch):
    store = DesktopStore(tmp_path / "desktop.json")
    calls = []
    monkeypatch.setattr(
        autostart,
        "set_start_with_windows",
        lambda enabled: calls.append(enabled) or True,
    )
    app = create_app(store=store)
    client = app.test_client()

    try:
        defaults = client.get("/api/settings")
        assert defaults.status_code == 200
        assert defaults.get_json()["preferences"]["start_with_windows"] is False
        assert defaults.get_json()["preferences"]["minimize_to_tray"] is True

        response = client.post(
            "/api/settings",
            json={
                "start_with_windows": True,
                "minimize_to_tray": False,
                "check_interval_minutes": 20,
            },
        )
    finally:
        stop_app(app)

    assert response.status_code == 200
    preferences = response.get_json()["preferences"]
    assert preferences["start_with_windows"] is True
    assert preferences["minimize_to_tray"] is False
    assert preferences["check_interval_minutes"] == 20
    assert calls == [True]


def test_single_instance_ignores_invalid_runtime_file(tmp_path):
    calls = []
    manager = SingleInstanceManager(
        tmp_path,
        urlopen=lambda *args, **kwargs: calls.append(("urlopen", args, kwargs)),
        kill_func=lambda pid, sig: calls.append(("kill", pid, sig)),
    )
    manager.runtime_file.write_text("{bad json", encoding="utf-8")

    assert manager.close_previous() is False
    assert calls == []
    assert not manager.runtime_file.exists()


def test_single_instance_requests_shutdown_then_terminates_lingering_pid(tmp_path):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getcode(self):
            return 200

    requests = []
    kills = []

    def fake_urlopen(request, timeout=None):
        requests.append((request.full_url, request.headers, timeout))
        return FakeResponse()

    def fake_kill(pid, sig):
        kills.append((pid, sig))

    manager = SingleInstanceManager(
        tmp_path,
        urlopen=fake_urlopen,
        kill_func=fake_kill,
        sleep_func=lambda seconds: None,
        pid_func=lambda: 111,
        grace_seconds=0,
    )
    manager.runtime_file.write_text(
        json.dumps({"pid": 222, "url": "http://127.0.0.1:54321", "token": "secret"}),
        encoding="utf-8",
    )

    assert manager.close_previous() is True
    assert requests[0][0] == "http://127.0.0.1:54321/api/internal/shutdown"
    headers = {key.lower(): value for key, value in requests[0][1].items()}
    assert headers["x-autoflix-token"] == "secret"
    assert (222, 0) in kills
    assert (222, signal.SIGTERM) in kills


def test_single_instance_closes_legacy_serve_only_processes(tmp_path):
    kills = []
    manager = SingleInstanceManager(
        tmp_path,
        kill_func=lambda pid, sig: kills.append((pid, sig)),
        process_snapshot_func=lambda: [
            {
                "ProcessId": 111,
                "CommandLine": r"C:\Python\python.exe -m autoflix_cli.desktop --serve-only --port 1234",
            },
            {
                "ProcessId": 222,
                "CommandLine": r"C:\Python\python.exe -m autoflix_cli.desktop --serve-only --port 5678",
            },
            {
                "ProcessId": 333,
                "CommandLine": r"C:\Python\python.exe -m autoflix_cli.desktop --background",
            },
            {
                "ProcessId": 444,
                "CommandLine": r"C:\Python\python.exe -m something_else --serve-only",
            },
        ],
        pid_func=lambda: 111,
        parent_pid_func=lambda: 999,
    )

    assert manager.close_legacy_serve_only_instances() == 1
    assert kills == [(222, signal.SIGTERM)]


def test_tray_menu_opens_window_and_quits_runtime(monkeypatch):
    class FakeMenuItem:
        def __init__(self, text, action, default=False):
            self.text = text
            self.action = action
            self.default = default

    class FakeMenu:
        def __init__(self, *items):
            self.items = list(items)

    class FakeIcon:
        instances = []

        def __init__(self, name, image, title, menu):
            self.name = name
            self.image = image
            self.title = title
            self.menu = menu
            self.started = False
            self.stopped = False
            FakeIcon.instances.append(self)

        def run_detached(self):
            self.started = True

        def stop(self):
            self.stopped = True

    fake_pystray = SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem, Icon=FakeIcon)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setattr(TrayController, "_create_image", lambda self: object())

    calls = []
    server = SimpleNamespace(stop=lambda: calls.append("server.stop"))
    fake_proxy = SimpleNamespace(
        start_proxy_server=lambda: None,
        stop_proxy_server=lambda: calls.append("proxy.stop_proxy_server"),
    )
    runtime = DesktopRuntime(proxy_module=fake_proxy)
    runtime.server = server

    controller = TrayController(on_quit=runtime.stop)
    assert controller.start() is True
    icon = FakeIcon.instances[-1]

    assert [item.text for item in icon.menu.items] == ["Ouvrir AutoFlix", "Quitter"]
    assert icon.menu.items[0].default is True
    assert icon.started is True

    window_calls = []
    controller.window = SimpleNamespace(
        show=lambda: window_calls.append("show"),
        restore=lambda: window_calls.append("restore"),
        maximize=lambda: window_calls.append("maximize"),
        destroy=lambda: window_calls.append("destroy"),
    )
    icon.menu.items[0].action(icon, icon.menu.items[0])
    assert window_calls == ["show", "restore", "maximize"]

    icon.menu.items[1].action(icon, icon.menu.items[1])
    assert icon.stopped is True
    assert calls == ["server.stop", "proxy.stop_proxy_server"]
    assert window_calls[-1] == "destroy"


def test_tray_quit_is_idempotent_and_runs_shutdown_after_destroy():
    calls = []
    controller = TrayController(on_quit=lambda: calls.append("shutdown"))
    controller.icon = SimpleNamespace(stop=lambda: calls.append("icon.stop"))
    controller.window = SimpleNamespace(destroy=lambda: calls.append("window.destroy"))

    controller.quit()
    controller.quit()

    assert calls == ["icon.stop", "window.destroy", "shutdown"]
