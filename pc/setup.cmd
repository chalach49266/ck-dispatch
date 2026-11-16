@echo off
setlocal DisableDelayedExpansion
set "DRY_ARG="
if "%~1"=="--dry-run" set "DRY_ARG=--dry-run"
if not "%~1"=="" if not "%~1"=="--dry-run" goto usage
set "KIT=%~dp0"
set "BIN=%USERPROFILE%\.local\bin"
set "HERDR=%USERPROFILE%\.config\herdr-app"
if defined DRY_ARG goto preview
if not exist "%BIN%" mkdir "%BIN%"
if errorlevel 1 exit /b 1
copy /Y "%KIT%windows\bin\*.cmd" "%BIN%\" >nul
if errorlevel 1 exit /b 1
if not exist "%HERDR%\layouts" mkdir "%HERDR%\layouts"
copy /Y "%KIT%windows\herdr\layouts\main-ck.json" "%HERDR%\layouts\main-ck.json" >nul
if errorlevel 1 exit /b 1
xcopy /E /I /Y "%KIT%windows\herdr\mods\agents-ck" "%HERDR%\mods\agents-ck" >nul
if errorlevel 1 exit /b 1
xcopy /E /I /Y "%KIT%windows\herdr\mods\tabs-ck" "%HERDR%\mods\tabs-ck" >nul
if errorlevel 1 exit /b 1
xcopy /E /I /Y "%KIT%windows\herdr\mods\pane-ck" "%HERDR%\mods\pane-ck" >nul
if errorlevel 1 exit /b 1
rem Use the ck look (green done, yellow needs-you, agents-ck, tabs-ck) when Herdr is installed; best effort.
if exist "%LOCALAPPDATA%\Programs\herdr-app\herdr-app.exe" "%LOCALAPPDATA%\Programs\herdr-app\herdr-app.exe" do layout.switch --name main-ck >nul 2>&1
set "USER_PATH="
for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v Path 2^>nul') do set "USER_PATH=%%B"
rem Quoted echo keeps spaces and ampersands in the existing PATH as data.
echo(";%USER_PATH%;" | findstr /I /L /C:";%BIN%;" /C:";%BIN%\;" >nul
if not errorlevel 1 goto wsl
if defined USER_PATH goto append_path
set "USER_PATH=%BIN%"
goto save_path
:append_path
set "USER_PATH=%USER_PATH%;%BIN%"
:save_path
reg add HKCU\Environment /v Path /t REG_EXPAND_SZ /d "%USER_PATH%" /f >nul
if errorlevel 1 exit /b 1
echo User PATH updated. Open a new terminal after setup.
goto wsl
:preview
echo DRY-RUN MKDIR "%BIN%"
for %%F in ("%KIT%windows\bin\*.cmd") do echo DRY-RUN COPY "%%~fF" to "%BIN%\%%~nxF"
echo DRY-RUN ENSURE user PATH includes "%BIN%" using reg.exe
for /R "%KIT%windows\herdr" %%F in (*) do echo DRY-RUN COPY "%%F" under "%HERDR%" keeping its relative folder
:wsl
rem bash receives the kit path as an argument; wslpath handles Windows paths; strip WSL share prefixes for a Linux clone.
wsl.exe -d Ubuntu --exec bash -c "kit=$(wslpath -u \"$1\"); kit=${kit#//wsl.localhost/Ubuntu}; kit=${kit#//wsl$/Ubuntu}; exec bash \"$kit/setup.sh\" --win-user \"$2\" $3" setup "%~dp0." "%USERNAME%" "%DRY_ARG%"
if errorlevel 1 exit /b 1
if defined DRY_ARG echo Dry-run finished. No setup changes were made.
if defined DRY_ARG exit /b 0
echo Setup finished. Restart Herdr and open a new Windows terminal.
exit /b 0
:usage
echo Usage: setup.cmd [--dry-run]
exit /b 2
