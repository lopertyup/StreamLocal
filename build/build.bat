@echo off
REM AutoFlix Desktop - build single-file Windows .exe
REM Output: dist\AutoFlix.exe

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo === AutoFlix build ===
echo Working dir: %cd%
echo.

call :CHECK_UNLOCKED "dist\AutoFlix.exe" "AutoFlix"
if errorlevel 1 exit /b 1

where uv >nul 2>nul
if %errorlevel% equ 0 goto USE_UV
goto USE_PIP

:USE_UV
echo [1/2] Syncing locked project deps with uv...
uv sync --locked --inexact --no-install-project
if %errorlevel% neq 0 goto FAIL
echo [2/2] Running pyinstaller via uv run...
uv run --locked --no-sync --with pyinstaller pyinstaller build\autoflix.spec --clean --noconfirm
if %errorlevel% neq 0 goto FAIL
goto DONE

:USE_PIP
echo uv not found, trying direct pyinstaller.
echo Make sure project dependencies are installed first:
echo   pip install -e . pyinstaller
echo.
where pyinstaller >nul 2>nul
if %errorlevel% neq 0 goto NOPYINST
pyinstaller build\autoflix.spec --clean --noconfirm
if %errorlevel% neq 0 goto FAIL
goto DONE

:NOPYINST
echo pyinstaller not found.
echo Install: pip install pyinstaller
echo Or: uv tool install pyinstaller
exit /b 1

:FAIL
echo BUILD FAILED.
exit /b 1

:DONE
echo.
echo === Build done ===
echo Binary: dist\AutoFlix.exe
if exist "dist\AutoFlix.exe" dir "dist\AutoFlix.exe"
echo.
echo Make a desktop shortcut:
echo   right-click dist\AutoFlix.exe -^> Send to -^> Desktop
endlocal
exit /b 0

:CHECK_UNLOCKED
if not exist "%~1" exit /b 0
set "LOCK_CHECK_PATH=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p=(Resolve-Path -LiteralPath $env:LOCK_CHECK_PATH).ProviderPath; $s=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None); $s.Close(); exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 (
  echo %~1 is currently locked.
  echo Close %~2 before rebuilding, then run build\build.bat again.
  exit /b 1
)
exit /b 0
