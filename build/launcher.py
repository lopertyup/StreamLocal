"""Production entry point for the packaged AutoFlix Desktop .exe.

Invokes desktop.main() while filtering dev-only flags so the bundled binary
can still accept runtime flags such as --background from Windows autostart.
"""
import sys

from autoflix_cli.desktop import main


if __name__ == "__main__":
    sys.argv = [sys.argv[0], *[arg for arg in sys.argv[1:] if arg != "--dev"]]
    main()
