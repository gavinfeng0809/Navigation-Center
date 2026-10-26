@echo off
setlocal

REM ===========================================================================
REM  notice-hub - launch Winbox from the nav portal (local protocol)
REM
REM  Just double-click this file. It fetches the newest handler from the
REM  portal automatically, so you normally do NOT need to download
REM  nh-open.ps1 by hand.
REM
REM    nh-setup.cmd                        interactive install (auto-detect
REM                                        winbox.exe, or pick it in a dialog)
REM    nh-setup.cmd "C:\Tools\winbox.exe"  set the path directly (no prompts)
REM    nh-setup.cmd -Show                  show current config + handler version
REM    nh-setup.cmd -Uninstall             remove the protocol registration
REM    nh-setup.cmd -Offline               do not fetch from the portal; use the
REM                                        nh-open.ps1 sitting next to this file
REM    nh-setup.cmd -BaseUrl http://HOST:PORT
REM                                        fetch from another portal address
REM
REM  Writes only to HKCU and %LOCALAPPDATA% - no administrator rights needed.
REM
REM  NOTE: this file must stay pure ASCII with CRLF line endings.
REM        cmd.exe reads batch files using the system ANSI codepage, so UTF-8
REM        text here turns into mojibake and gets executed as commands.
REM ===========================================================================

if not exist "%~dp0nh-setup.ps1" (
    echo.
    echo  [ERROR] nh-setup.ps1 not found.
    echo          Put nh-setup.cmd, nh-setup.ps1 and nh-open.ps1
    echo          in the SAME folder, then run this file again.
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0nh-setup.ps1" %*
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
    echo.
    echo  [exit code %RC%] Install did not finish. Please send a screenshot
    echo  of the messages above.
)

echo.
pause
exit /b %RC%
