@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "EXPECTED_REMOTE=https://github.com/lopertyup/StreamLocal.git"
set "DEFAULT_COMMIT=Update AutoFlix production version"

echo === AutoFlix production push ===
echo Folder: %cd%
echo.

if not exist ".git" (
  echo ERROR: ce fichier doit etre lance depuis la racine du depot Git.
  goto FAIL
)

for %%I in ("%cd%") do set "REPO_FOLDER=%%~nxI"
if /I "%REPO_FOLDER%"=="StreamLocal_dev" (
  echo ERROR: tu es dans StreamLocal_dev. Ouvre ce fichier depuis StreamLocal_git uniquement.
  goto FAIL
)

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "BRANCH=%%B"
if /I not "%BRANCH%"=="main" (
  echo ERROR: branche courante: %BRANCH%
  echo Passe sur main avant de pousser la version propre.
  goto FAIL
)

for /f "delims=" %%R in ('git remote get-url origin 2^>nul') do set "REMOTE=%%R"
if /I not "%REMOTE%"=="%EXPECTED_REMOTE%" (
  echo ERROR: remote origin inattendu:
  echo   %REMOTE%
  echo Attendu:
  echo   %EXPECTED_REMOTE%
  goto FAIL
)

echo [1/7] Verification des marqueurs dev...
git grep -n -F "AutoFlix Dev" -- src build tests
if %errorlevel% equ 0 (
  echo ERROR: marqueur "AutoFlix Dev" trouve dans la version prod.
  goto FAIL
)
if %errorlevel% geq 2 goto FAIL

git grep -n -F "AutoFlixDev" -- src build tests
if %errorlevel% equ 0 (
  echo ERROR: marqueur "AutoFlixDev" trouve dans la version prod.
  goto FAIL
)
if %errorlevel% geq 2 goto FAIL

echo [2/7] Verification des fichiers non suivis...
set "HAS_UNTRACKED=0"
for /f "delims=" %%U in ('git ls-files --others --exclude-standard') do (
  if /I not "%%U"=="push_prod.bat" if /I not "%%U"=="creer_exe.bat" (
    set "HAS_UNTRACKED=1"
    echo   %%U
  )
)
if "%HAS_UNTRACKED%"=="1" (
  echo ERROR: fichiers non suivis detectes. Ajoute-les volontairement ou ignore-les avant de relancer.
  goto FAIL
)

echo [3/7] Checks rapides...
node --check src\autoflix_cli\app\static\app.js
if %errorlevel% neq 0 goto FAIL

python -m py_compile ^
  src\autoflix_cli\desktop.py ^
  src\autoflix_cli\app\store.py ^
  src\autoflix_cli\app\autostart.py ^
  src\autoflix_cli\app\downloads.py ^
  src\autoflix_cli\app\tracker_service.py
if %errorlevel% neq 0 goto FAIL

echo [4/7] Tests cibles...
pytest tests\test_static_encoding.py tests\test_scan_api.py tests\test_desktop_background.py
if %errorlevel% neq 0 goto FAIL

echo [5/7] Preparation du commit...
git add -u
git add push_prod.bat creer_exe.bat
if %errorlevel% neq 0 goto FAIL

git diff --cached --quiet
if %errorlevel% equ 0 (
  echo Rien a committer.
  goto DONE
)

echo.
set /p "COMMIT_MSG=Message de commit [Update AutoFlix production version]: "
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=%DEFAULT_COMMIT%"

echo [6/7] Commit...
git commit -m "%COMMIT_MSG%"
if %errorlevel% neq 0 goto FAIL

echo [7/7] Push vers GitHub...
git push origin main
if %errorlevel% neq 0 goto FAIL

goto DONE

:FAIL
echo.
echo ECHEC: rien n'a ete pousse.
pause
exit /b 1

:DONE
echo.
echo Termine.
pause
exit /b 0
