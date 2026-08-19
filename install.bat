@echo off
REM ============================================================
REM  COA Reviewer Web App - Windows installer
REM  Installs Python dependencies, the Playwright Chromium
REM  browser, and the Visual C++ 2013 runtime that pyzbar
REM  needs for barcode scanning. Run once per machine before
REM  launching Run.pyw.
REM  Detailed output is written to install.log next to this file.
REM ============================================================

setlocal

set "LOG=%~dp0install.log"
echo [%date% %time%] === install.bat starting === > "%LOG%"

echo.
echo === Installing Python packages (details in install.log) ===
echo [%date% %time%] Upgrading pip >> "%LOG%"
python -m pip install --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 goto :error

echo [%date% %time%] Installing Python packages >> "%LOG%"
python -m pip install ^
    "flask>=3.0.0" ^
    "PyJWT>=2.8.0" ^
    "requests>=2.31.0" ^
    "playwright>=1.40.0" ^
    "pymupdf>=1.23.0" ^
    "pyzbar>=0.1.9" ^
    "pystray>=0.19.0" ^
    "Pillow>=10.0.0" ^
    "pytest>=8.0" >> "%LOG%" 2>&1
if errorlevel 1 goto :error

echo.
echo === Installing Playwright Chromium ===
echo [%date% %time%] Installing Playwright Chromium >> "%LOG%"
python -m playwright install chromium >> "%LOG%" 2>&1
if errorlevel 1 goto :error

echo.
echo === Installing Visual C++ 2013 runtime (needed by pyzbar barcode scanning) ===
if exist "%SystemRoot%\System32\msvcr120.dll" (
    echo Already installed - skipping.
    echo [%date% %time%] VC++ 2013 runtime already present - skipped >> "%LOG%"
    goto :vcredist_done
)
echo [%date% %time%] Downloading vcredist_x64.exe >> "%LOG%"
curl -L -o "%TEMP%\vcredist_x64.exe" https://aka.ms/highdpimfc2013x64enu >> "%LOG%" 2>&1
if errorlevel 1 goto :error
echo [%date% %time%] Running vcredist_x64.exe /install /quiet /norestart >> "%LOG%"
"%TEMP%\vcredist_x64.exe" /install /quiet /norestart
set "VC_EXIT=%ERRORLEVEL%"
del "%TEMP%\vcredist_x64.exe"
echo [%date% %time%] vcredist exit code: %VC_EXIT% >> "%LOG%"
REM 0 = installed, 3010 = installed but reboot pending, 1638 = newer version already present
if not "%VC_EXIT%"=="0" if not "%VC_EXIT%"=="3010" if not "%VC_EXIT%"=="1638" goto :error
:vcredist_done

echo.
echo === Install complete ===
echo [%date% %time%] === Install complete === >> "%LOG%"
echo Starting COA Reviewer (Run.pyw)...
echo [%date% %time%] Launching Run.pyw >> "%LOG%"
REM start "" launches detached via the .pyw file association (pythonw,
REM no console); /D pins the working directory to this folder so the
REM launcher's relative paths (server.log, app.py watch list) resolve.
start "" /D "%~dp0" "%~dp0Run.pyw"
exit /b 0

:error
echo [%date% %time%] *** Install FAILED *** >> "%LOG%"
echo.
echo *** Install failed. Details are in install.log next to this script. ***
pause
exit /b 1
