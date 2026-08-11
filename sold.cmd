@echo off
setlocal EnableDelayedExpansion
rem Archive a SOLD listing: photos and closing details into Google Drive.
rem
rem   sold                          asks for the link
rem   sold <zillow-url>             scrape -> review -> photos -> Drive
rem
rem No video is rendered. A sold listing is a record, not an advertisement.
rem
rem If Zillow asks for the human check, this opens a browser and waits for you
rem to clear it. The cookie is shared with reel.cmd.
rem
rem Anything extra is passed through:  sold <url> --no-drive
rem Other modes:                       sold --manual listing.json

set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
set "PY=%HERE%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo First-time setup ^(once^):
    echo     cd /d "%HERE%" ^&^& py -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

if "%ZILLOW_PROFILE%"=="" set "ZILLOW_PROFILE=%USERPROFILE%\.zillow-profile"

set "DRIVE="
if not exist "%USERPROFILE%\.config\zillow-reels\client_secret.json" (
    if not exist "client_secret.json" set "DRIVE=--no-drive"
)

if /i "%~1"=="--manual" goto manual
if /i "%~1"=="-h"       goto help
if /i "%~1"=="--help"   goto help

set "ARGS=%*"
if "%ARGS%"=="" (
    set /p "URL=Paste the Zillow SOLD listing link: "
    if "!URL!"=="" (
        echo No link given.
        exit /b 1
    )
    set "ARGS=!URL!"
)

set "BROWSER=--fetch browser --browser-channel chrome --browser-profile "%ZILLOW_PROFILE%" --review"

"%PY%" -m zillow_reels sold %ARGS% %BROWSER% %DRIVE% --no-fallback-template
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
    "%PY%" -m zillow_reels sold %ARGS% %BROWSER% %DRIVE% --show-browser --solve-challenge
    set "STATUS=!ERRORLEVEL!"
)

exit /b %STATUS%

:manual
"%PY%" -m zillow_reels sold %* --review %DRIVE%
exit /b %ERRORLEVEL%

:help
"%PY%" -m zillow_reels sold --help
exit /b 0
