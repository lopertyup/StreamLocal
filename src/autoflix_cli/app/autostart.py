import subprocess
import sys
from typing import Optional

from . import diagnostics


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "AutoFlix"


def build_autostart_command() -> str:
    """Return the command registered in Windows Run for background startup."""
    if getattr(sys, "frozen", False):
        parts = [sys.executable, "--background"]
    else:
        parts = [sys.executable, "-m", "autoflix_cli.desktop", "--background"]
    return subprocess.list2cmdline(parts)


def set_start_with_windows(enabled: bool, command: Optional[str] = None) -> bool:
    """Create or remove the per-user Windows Run entry.

    Returns False on unsupported platforms or registry failures so callers can
    save the preference without treating registry access as a fatal settings
    error.
    """
    if sys.platform != "win32":
        return False

    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    RUN_VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    command or build_autostart_command(),
                )
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        diagnostics.info(
            "AUTOSTART",
            "sync",
            status="ok",
            enabled=bool(enabled),
        )
        return True
    except Exception as exc:
        diagnostics.warning(
            "AUTOSTART",
            "sync_failed",
            enabled=bool(enabled),
            error=str(exc),
        )
        return False


def sync_preferences(preferences: dict) -> bool:
    return set_start_with_windows(bool(preferences.get("start_with_windows")))
