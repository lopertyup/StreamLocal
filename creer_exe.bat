@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "APP_EXE=dist\AutoFlix.exe"
set "APP_NAME=AutoFlix"
set "PYTHON_DOWNLOAD_URL=https://www.python.org/downloads/windows/"
set "VENV_DIR=.venv"
set "VENV_PY=.venv\Scripts\python.exe"

echo === AutoFlix - creation de l'exe ===
echo Dossier: %cd%
echo.

if not exist "pyproject.toml" (
  echo ERREUR: lance ce fichier depuis la racine du dossier telecharge depuis GitHub.
  goto FAIL
)

call :CHECK_REQUIRED "build\autoflix.spec"
if errorlevel 1 goto FAIL
call :CHECK_REQUIRED "build\launcher.py"
if errorlevel 1 goto FAIL
call :CHECK_REQUIRED "src\autoflix_cli\app\static\app.js"
if errorlevel 1 goto FAIL
call :CHECK_REQUIRED "src\autoflix_cli\app\static\index.html"
if errorlevel 1 goto FAIL

call :CHECK_UNLOCKED "%APP_EXE%" "%APP_NAME%"
if errorlevel 1 goto FAIL

echo [1/5] Recherche de Python 3.12...
call :FIND_PYTHON
if errorlevel 1 goto PYTHON_MISSING
echo Python trouve: %PY_VERSION%

echo [2/5] Preparation de l'environnement local...
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Le venv existant n'utilise pas Python 3.12, recreation...
    rmdir /s /q "%VENV_DIR%"
  ) else (
    echo Venv existant OK.
  )
)

if not exist "%VENV_PY%" (
  echo Creation du venv...
  %PYTHON_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 goto FAIL
)

"%VENV_PY%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo Activation de pip...
  "%VENV_PY%" -m ensurepip --upgrade
  if errorlevel 1 goto FAIL
)

echo [3/5] Verification des dependances...
"%VENV_PY%" -c "import PyInstaller, bs4, curl_cffi, flask, html5lib, jsbeautifier, m3u8, platformdirs, pystray, readchar, rich, webview; import PIL, Crypto; import autoflix_cli" >nul 2>nul
if errorlevel 1 (
  echo Installation ou mise a jour des dependances avec pip...
  "%VENV_PY%" -m pip install --upgrade pip setuptools wheel
  if errorlevel 1 goto FAIL
  "%VENV_PY%" -m pip install -e . pyinstaller
  if errorlevel 1 goto FAIL
) else (
  echo Dependances deja installees.
)

echo [4/5] Verification rapide du code Python...
"%VENV_PY%" -m py_compile ^
  src\autoflix_cli\desktop.py ^
  src\autoflix_cli\app\store.py ^
  src\autoflix_cli\app\autostart.py ^
  src\autoflix_cli\app\downloads.py ^
  src\autoflix_cli\app\tracker_service.py ^
  src\autoflix_cli\scraping\goldenanime.py
if errorlevel 1 goto FAIL

echo [5/5] Build PyInstaller...
"%VENV_PY%" -m PyInstaller build\autoflix.spec --clean --noconfirm
if errorlevel 1 goto FAIL

if not exist "%APP_EXE%" (
  echo ERREUR: le build a fini sans creer %APP_EXE%.
  goto FAIL
)

echo.
echo === Termine ===
echo Exe cree: %APP_EXE%
dir "%APP_EXE%"
goto DONE

:FIND_PYTHON
set "PYTHON_CMD="
set "PY_VERSION="

py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py -3.12"
  goto PYTHON_FOUND
)

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=python"
  goto PYTHON_FOUND
)

exit /b 1

:PYTHON_FOUND
for /f "delims=" %%V in ('%PYTHON_CMD% -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"') do set "PY_VERSION=%%V"
exit /b 0

:PYTHON_MISSING
echo.
echo ERREUR: Python 3.12 est introuvable.
echo Telecharge Python 3.12 pour Windows ici:
echo   %PYTHON_DOWNLOAD_URL%
echo.
echo Pendant l'installation, coche "Add python.exe to PATH".
echo.
set /p "OPEN_PY=Ouvrir la page de telechargement maintenant ? [O/N] "
if /I "%OPEN_PY%"=="O" start "" "%PYTHON_DOWNLOAD_URL%"
goto FAIL

:CHECK_REQUIRED
if exist "%~1" exit /b 0
echo ERREUR: %~1 est introuvable.
exit /b 1

:CHECK_UNLOCKED
if not exist "%~1" exit /b 0
set "LOCK_CHECK_PATH=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p=(Resolve-Path -LiteralPath $env:LOCK_CHECK_PATH).ProviderPath; $s=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None); $s.Close(); exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
  echo ERREUR: %~1 est verrouille.
  echo Ferme %~2 avant de relancer ce fichier.
  exit /b 1
)
exit /b 0

:FAIL
echo.
echo ECHEC: l'exe n'a pas ete cree.
if "%AUTOFLIX_NO_PAUSE%"=="" pause
exit /b 1

:DONE
echo.
if "%AUTOFLIX_NO_PAUSE%"=="" pause
exit /b 0
