@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

REM ==========================================================
REM === MathModelAgent 启动脚本（Portable 版，Step-by-Step Debug）
REM ==========================================================
goto :main

:theend
echo.
echo ===== Script finished (even if error) =====
pause
cmd /k


:main
echo ===== Starting MathModelAgent System (Local Version / STEP-BY-STEP DEBUG) =====

REM ==========================================================
REM === Python Check
REM ==========================================================
set PY_VERSION=3.12.10
set PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip
set PY_PORTABLE_DIR=%cd%\python312-portable
set PY_SUB_DIR=%PY_PORTABLE_DIR%
set PY_EXE=%PY_SUB_DIR%\python.exe

call :EnsurePython "%PY_EXE%" "%PY_VERSION%" "%PY_URL%" "%PY_PORTABLE_DIR%" "%PY_SUB_DIR%"
if errorlevel 1 (
  echo [ERROR] Python setup failed.
  goto :theend
)

:after_python_setup
set PATH=%PY_SUB_DIR%;%PATH%
echo Using Portable Python: %PY_EXE%
python --version || (echo [WARNING] Python not working, please check manually)
echo === CHECKPOINT: after Python check ===


REM ==========================================================
REM === Node.js + pnpm Check
REM ==========================================================
echo === CHECKPOINT: before Node.js check ===
set "NODE_PORTABLE_DIR=%cd%\nodejs-portable"
set "NODE_SUB_DIR=%NODE_PORTABLE_DIR%\node-v22.18.0-win-x64"
set "NODE_EXE=%NODE_SUB_DIR%\node.exe"

call :EnsureNode "%NODE_EXE%" "%NODE_PORTABLE_DIR%" "%NODE_SUB_DIR%"
if errorlevel 1 (
  echo [ERROR] Node.js setup failed.
  goto :theend
)

REM === PATH + NPM/PNPM 配置 ===
set "PATH=%NODE_SUB_DIR%;%PATH%"
set "NPM_CMD=%NODE_SUB_DIR%\npm.cmd"
set "PNPM_EXE=%NODE_SUB_DIR%\pnpm.exe"

call :EnsurePnpm "%PNPM_EXE%" "%NODE_SUB_DIR%"
if errorlevel 1 (
  echo [ERROR] pnpm setup failed.
  goto :theend
)

set "PNPM_CMD=%PNPM_EXE%"
for /f "tokens=*" %%i in ('"%PNPM_CMD%" -v 2^>nul') do set CURRENT_PNPM_VER=%%i
echo Detected pnpm version %CURRENT_PNPM_VER% (OK)
echo === CHECKPOINT: after Node.js check ===


REM ==========================================================
REM === Redis Check
REM ==========================================================
set REDIS_PORTABLE_DIR=%cd%\redis-portable
set REDIS_EXE=%REDIS_PORTABLE_DIR%\redis-server.exe

call :EnsureRedis "%REDIS_EXE%" "%REDIS_PORTABLE_DIR%"
if errorlevel 1 (
  echo [ERROR] Redis setup failed.
  goto :theend
)

:after_redis_setup
set REDIS_PATH=%REDIS_PORTABLE_DIR%
echo === CHECKPOINT: after Redis check ===


REM ==========================================================
REM === 配置文件检查
REM ==========================================================
if not exist .\backend\.env.dev (
  echo Backend config not found. Copying example config...
  copy .\backend\.env.dev.example .\backend\.env.dev
)
if not exist .\frontend\.env.development (
  echo Frontend config not found. Copying example config...
  copy .\frontend\.env.example .\frontend\.env.development
)


REM ==========================================================
REM === 启动 Redis
REM ==========================================================
echo Starting Redis server...
start "Redis Server" cmd /k "%REDIS_PATH%\redis-server.exe"


REM ==========================================================
REM === Backend - 确保 uv.exe 存在
REM ==========================================================
set UV_TOOLS_DIR=%cd%\uv-tools
if not exist "%UV_TOOLS_DIR%" mkdir "%UV_TOOLS_DIR%"
set UV_EXE=%UV_TOOLS_DIR%\uv.exe

if not exist "%UV_EXE%" (
    echo Downloading uv.zip ...
    powershell -Command ^
    "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/download/0.8.12/uv-x86_64-pc-windows-msvc.zip' -OutFile 'uv.zip'" || (echo [ERROR] Failed to download uv.zip & goto :theend)

    echo Extracting uv.zip ...
    powershell -Command ^
    "Expand-Archive -Path 'uv.zip' -DestinationPath '%UV_TOOLS_DIR%' -Force" || (echo [ERROR] Failed to extract uv.zip & goto :theend)

    if not exist "%UV_EXE%" (
        echo [ERROR] uv.exe not found after extraction.
        goto :theend
    )
)

set PATH=%UV_TOOLS_DIR%;%PATH%

REM === 配置并启动后端 ===
cd backend
if not exist .venv (
  "%UV_EXE%" venv .venv || (echo [ERROR] Failed to create venv & goto :back_to_root)
)
call .venv\Scripts\activate.bat
set UV_LINK_MODE=copy
"%UV_EXE%" sync || (echo [ERROR] uv sync failed & goto :back_to_root)

start "Backend Server" cmd /k "call .venv\Scripts\activate.bat && set ENV=DEV && uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-ping-interval 60 --ws-ping-timeout 120"

:back_to_root
cd ..


REM ==========================================================
REM === Frontend
REM ==========================================================
cd frontend

REM === 安装依赖 (强制用 portable pnpm) ===
call "%PNPM_CMD%" install

REM === approve-builds - 固定旧版本用法，无 --all 参数 ===
call "%PNPM_CMD%" approve-builds

REM === 启动前端开发服务器 ===
start "Frontend Server" cmd /k ""%PNPM_CMD%" run dev"

cd ..

echo.
echo ===== MathModelAgent System Started Successfully =====
echo - Backend API: http://localhost:8000
echo - Frontend:    http://localhost:5173
echo.
goto :theend


REM ==========================================================
REM === Subroutines
REM ==========================================================

:EnsurePython
REM %1=PY_EXE %2=PY_VERSION %3=PY_URL %4=PY_PORTABLE_DIR %5=PY_SUB_DIR
setlocal
if exist "%~1" (
  echo Found Python at "%~1"
  endlocal & exit /b 0
)
echo Python not found, downloading...
powershell -Command "& {Invoke-WebRequest -Uri '%~3' -OutFile 'python312.zip' -UseBasicParsing}" || (endlocal & exit /b 1)
powershell -Command "& {Expand-Archive -Path 'python312.zip' -DestinationPath '%~4%' -Force}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1

:EnsureNode
REM %1=NODE_EXE %2=NODE_PORTABLE_DIR %3=NODE_SUB_DIR
setlocal
if exist "%~1" (
  echo Found Node.js at "%~1"
  endlocal & exit /b 0
)
echo Node.js not found, downloading...
powershell -Command "& {Invoke-WebRequest -Uri 'https://nodejs.org/dist/v22.18.0/node-v22.18.0-win-x64.zip' -OutFile 'node.zip' -UseBasicParsing}" || (endlocal & exit /b 1)
powershell -Command "& {Expand-Archive -Path 'node.zip' -DestinationPath '%~2%' -Force}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1

:EnsureRedis
REM %1=REDIS_EXE %2=REDIS_PORTABLE_DIR
setlocal
if exist "%~1" (
  echo Found Redis at "%~1"
  endlocal & exit /b 0
)
echo Redis not found, downloading...
powershell -Command "& {Invoke-WebRequest -Uri 'https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip' -OutFile 'redis-portable.zip' -UseBasicParsing}" || (endlocal & exit /b 1)
powershell -Command "& {Expand-Archive -Path 'redis-portable.zip' -DestinationPath '%~2%' -Force}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1

:EnsurePnpm
REM %1=PNPM_EXE %2=NODE_SUB_DIR
setlocal
if exist "%~1" (
  echo Found pnpm at "%~1"
  endlocal & exit /b 0
)
echo pnpm not found, downloading portable exe...
powershell -Command "& {Invoke-WebRequest -Uri 'https://github.com/pnpm/pnpm/releases/latest/download/pnpm-win-x64.exe' -OutFile '%~2\pnpm.exe' -UseBasicParsing}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1
