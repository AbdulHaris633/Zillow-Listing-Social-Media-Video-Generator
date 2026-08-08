@echo off
setlocal EnableDelayedExpansion
rem One command, whole process: paste a Zillow link, get a video.
rem
rem   reel                          asks for the link
rem   reel <zillow-url>             scrape -> review -> photos -> video -> Drive
rem
rem After scraping it prints what it found and waits: press Enter to accept and
rem build, or type a field number to correct something first.
rem
rem If Zillow asks for the human check, this opens a browser, waits for you to
rem clear it, and carries straight on. The cookie is saved, so it usually only
rem ever asks once.
rem
rem Anything extra is passed through:  reel <url> --max-photos 8 --no-drive
rem Other modes:                       reel batch listings.csv
rem                                    reel --manual listing.json

rem Resolve the project from this script's location but stay in the caller's
rem directory, so relative paths land where the user expects.
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
set "PY=%HERE%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo First-time setup ^(once^):
    echo     cd /d "%HERE%" ^&^& py -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

if "%ZILLOW_PROFILE%"=="" set "ZILLOW_PROFILE=%USERPROFILE%\.zillow-profile"

rem Skip Drive until OAuth credentials exist, so a first run produces a video
rem rather than a wall of setup instructions.
set "DRIVE="
if not exist "%USERPROFILE%\.config\zillow-reels\client_secret.json" (
    if not exist "client_secret.json" set "DRIVE=--no-drive"
)

rem Subcommands that never fetch a page get no browser flags.
if /i "%~1"=="batch"    goto passthrough
if /i "%~1"=="template" goto passthrough
if /i "%~1"=="auth"     goto passthrough
if /i "%~1"=="probe"    goto passthrough
if /i "%~1"=="--manual" goto manual
if /i "%~1"=="-h"       goto help
if /i "%~1"=="--help"   goto help

rem No URL given: just ask for one.
set "ARGS=%*"
if "%ARGS%"=="" (
    set /p "URL=Paste the Zillow listing link: "
    if "!URL!"=="" (
        echo No link given.
        exit /b 1
    )
    set "ARGS=!URL!"
)

set "BROWSER=--fetch browser --browser-channel chrome --browser-profile "%ZILLOW_PROFILE%" --review"

rem First attempt stays quiet about the fallback template: if it fails we retry
rem below, and a template written now would just be confusing litter.
"%PY%" -m zillow_reels make %ARGS% %BROWSER% %DRIVE% --no-fallback-template
set "STATUS=%ERRORLEVEL%"

rem Exit 3 means it could not get the data - nearly always the human check.
if "%STATUS%"=="3" (
    echo.
    echo --------------------------------------------------------------
    echo  Zillow wants to confirm you're human.
    echo  A browser window is opening - press ^& hold the button in it.
    echo  Everything continues automatically once you do.
    echo --------------------------------------------------------------
    echo.
    "%PY%" -m zillow_reels make %ARGS% %BROWSER% %DRIVE% --show-browser --solve-challenge
    set "STATUS=!ERRORLEVEL!"
)

exit /b %STATUS%

:manual
rem Review applies here too: it is skipped when stdin isn't a terminal, and it
rem is the last chance to catch a typo in a hand-filled template.
"%PY%" -m zillow_reels make %* --review %DRIVE%
exit /b %ERRORLEVEL%

:passthrough
"%PY%" -m zillow_reels %*
exit /b %ERRORLEVEL%

:help
"%PY%" -m zillow_reels --help
exit /b 0
