# AutoFlix CLI

AutoFlix is a Python CLI and desktop app for searching movies, series, anime,
and scan/manga chapters from supported providers. It resolves playable sources,
uses a local proxy for HLS/MP4 playback, and can launch streams in a browser,
mpv, VLC, or the built-in desktop player.

The app stores user preferences and history locally. No account is required.
AniList integration is optional and only uses a token provided by the user at
runtime.

## Features

- Interactive terminal mode with provider search and playback.
- Desktop UI with search, history, favorites, scan reader, tracking, and downloads.
- Local HLS/MP4 proxy for stream playback and range requests.
- Optional AniList progress sync and media mapping.
- Optional FFmpeg-backed downloads from the desktop UI.
- Windows tray mode, background startup, and Windows executable build support.

## Requirements

- Python 3.9.2 or newer.
- `uv` recommended for development and reproducible installs.
- A media player such as mpv, VLC, or a browser.
- FFmpeg in `PATH` if you want to use desktop downloads.

## Install From Source

```powershell
git clone https://github.com/PaulExplorer/autoflix-cli.git
cd autoflix-cli
uv sync
```

Run without installing globally:

```powershell
uv run autoflix
uv run autoflix-desktop
```

With pip:

```powershell
python -m pip install .
autoflix
autoflix-desktop
```

If the package is published on PyPI, you can also install it as a tool:

```powershell
uv tool install autoflix-cli
```

## Usage

CLI:

```powershell
autoflix
```

Desktop UI:

```powershell
autoflix-desktop
autoflix-desktop --browser
autoflix-desktop --background
autoflix-desktop --serve-only
```

`--background` starts AutoFlix hidden in the Windows tray so tracking and
downloads can keep running. `--serve-only` starts only the local server and
prints the URL.

## Configuration And Data

Project configuration files live in `data/` and provide default provider URLs
and player extraction settings. User state is stored in the operating system
user-data directory through `platformdirs`; it is not meant to be committed.

Local user data can include:

- watch history and resume data;
- language and player preferences;
- favorites, downloads, tracking settings, and notifications;
- AniList token and AniList mappings if the user enables AniList.

Never commit local runtime files, logs, `.env` files, or user-data JSON files.

## AniList

AniList support is optional. Add a token from the desktop settings or CLI
settings only on your own machine. The repository must not contain real AniList
tokens, authorization headers, cookies, or API keys.

## Build The Windows Executable

The repository keeps only the build sources:

- `build/build.bat`
- `build/autoflix.spec`
- `build/launcher.py`
- `build/autoflix.ico`
- `build/make_icon.py`

Build:

```powershell
build\build.bat
```

The generated executable is written to `dist\AutoFlix.exe`. Generated `dist/`
and PyInstaller work directories are ignored by Git.

## Development

Install dependencies and run checks:

```powershell
uv sync
uv run python -m compileall src
uv run --with pytest python -m pytest
```

Run the app from the source tree:

```powershell
uv run autoflix
uv run autoflix-desktop
```

To add a scan or manga provider, see
`docs/scan-provider-implementation.md`.

## Project Layout

```text
src/autoflix_cli/
  main.py              CLI entry point
  desktop.py           desktop launcher
  proxy.py             local playback proxy
  tracker.py           CLI local state
  handlers/            CLI provider handlers
  scraping/            provider scrapers and player extraction
  app/                 desktop Flask API and static UI
data/                  public default provider configuration
docs/                  developer notes
build/                 Windows packaging sources
tests/                 automated tests
```

## Security

Do not publish secrets or private runtime data. Before opening a pull request or
publishing a fork, search for:

```text
token
secret
password
api_key
authorization
cookie
local user paths
```

## License

This project is licensed under GPL-3.0. See `LICENSE`.

## Disclaimer

AutoFlix is a personal-use tool that indexes third-party websites. Provider
availability, URLs, and playback behavior can change at any time. Use the
software only where you have the right to access the content, and respect the
laws and terms that apply in your jurisdiction.
