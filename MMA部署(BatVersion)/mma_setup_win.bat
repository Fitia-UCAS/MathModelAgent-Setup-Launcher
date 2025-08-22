@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

REM ==========================================================
REM 启用 Windows 10+ ANSI 转义序列支持
REM ==========================================================
for /f "tokens=2 delims=: " %%a in ('reg query HKCU\Console ^| findstr VirtualTerminalLevel') do set VT=%%a
if not defined VT reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul

REM ==========================================================
REM 彩色定义
REM ==========================================================
set "RED=[31m"
set "GREEN=[32m"
set "YELLOW=[33m"
set "CYAN=[36m"
set "RESET=[0m"

REM ==========================================================
REM === MathModelAgent 启动脚本（便携版，逐步调试模式）
REM ==========================================================
goto :main

:theend
echo.
echo %CYAN%===== 脚本执行结束（即使发生错误） =====%RESET%
pause
cmd /k


:main
echo %YELLOW%===== 启动 MathModelAgent 系统 (本地版本 / 调试模式) =====%RESET%

REM ==========================================================
REM === Python 检查
REM ==========================================================
set PY_VERSION=3.12.10
set PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip
set PY_PORTABLE_DIR=%cd%\python312-portable
set PY_SUB_DIR=%PY_PORTABLE_DIR%
set PY_EXE=%PY_SUB_DIR%\python.exe

call :EnsurePython "%PY_EXE%" "%PY_VERSION%" "%PY_URL%" "%PY_PORTABLE_DIR%" "%PY_SUB_DIR%"
if errorlevel 1 (
  echo %RED%[错误] Python 环境初始化失败。%RESET%
  goto :theend
)

:after_python_setup
set PATH=%PY_SUB_DIR%;%PATH%
echo %GREEN%使用便携版 Python: %PY_EXE%%RESET%
python --version || (echo %RED%[警告] Python 无法运行，请手动检查%RESET%)
echo %CYAN%=== 检查点: Python 已确认 ===%RESET%


REM ==========================================================
REM === Node.js + pnpm 检查
REM ==========================================================
echo %CYAN%=== 检查点: Node.js 检查前 ===%RESET%
set "NODE_PORTABLE_DIR=%cd%\nodejs-portable"
set "NODE_SUB_DIR=%NODE_PORTABLE_DIR%\node-v22.18.0-win-x64"
set "NODE_EXE=%NODE_SUB_DIR%\node.exe"

call :EnsureNode "%NODE_EXE%" "%NODE_PORTABLE_DIR%" "%NODE_SUB_DIR%"
if errorlevel 1 (
  echo %RED%[错误] Node.js 初始化失败。%RESET%
  goto :theend
)

set "PATH=%NODE_SUB_DIR%;%PATH%"
set "NPM_CMD=%NODE_SUB_DIR%\npm.cmd"
set "PNPM_EXE=%NODE_SUB_DIR%\pnpm.exe"

call :EnsurePnpm "%PNPM_EXE%" "%NODE_SUB_DIR%"
if errorlevel 1 (
  echo %RED%[错误] pnpm 初始化失败。%RESET%
  goto :theend
)

set "PNPM_CMD=%PNPM_EXE%"
for /f "tokens=*" %%i in ('"%PNPM_CMD%" -v 2^>nul') do set CURRENT_PNPM_VER=%%i
echo %GREEN%已检测到 pnpm 版本 %CURRENT_PNPM_VER%（正常）%RESET%
echo %CYAN%=== 检查点: Node.js 已确认 ===%RESET%


REM ==========================================================
REM === Redis 检查
REM ==========================================================
set REDIS_PORTABLE_DIR=%cd%\redis-portable
set REDIS_EXE=%REDIS_PORTABLE_DIR%\redis-server.exe

call :EnsureRedis "%REDIS_EXE%" "%REDIS_PORTABLE_DIR%"
if errorlevel 1 (
  echo %RED%[错误] Redis 初始化失败。%RESET%
  goto :theend
)

:after_redis_setup
set REDIS_PATH=%REDIS_PORTABLE_DIR%
echo %CYAN%=== 检查点: Redis 已确认 ===%RESET%


REM ==========================================================
REM === 配置文件检查
REM ==========================================================
if not exist .\backend\.env.dev (
  echo %YELLOW%后端配置文件未找到，正在复制示例配置...%RESET%
  copy .\backend\.env.dev.example .\backend\.env.dev
)
if not exist .\frontend\.env.development (
  echo %YELLOW%前端配置文件未找到，正在复制示例配置...%RESET%
  copy .\frontend\.env.example .\frontend\.env.development
)


REM ==========================================================
REM === 启动 Redis
REM ==========================================================
echo %YELLOW%正在启动 Redis 服务...%RESET%
start "Redis Server" cmd /k "%REDIS_PATH%\redis-server.exe"


REM ==========================================================
REM === 后端 - 确保 uv.exe 存在
REM ==========================================================
set UV_TOOLS_DIR=%cd%\uv-tools
if not exist "%UV_TOOLS_DIR%" mkdir "%UV_TOOLS_DIR%"
set UV_EXE=%UV_TOOLS_DIR%\uv.exe

if not exist "%UV_EXE%" (
    echo %YELLOW%正在下载 uv.zip ...%RESET%
    powershell -Command ^
    "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/download/0.8.12/uv-x86_64-pc-windows-msvc.zip' -OutFile 'uv.zip'" || (echo %RED%[错误] 下载 uv.zip 失败%RESET% & goto :theend)

    echo %YELLOW%正在解压 uv.zip ...%RESET%
    powershell -Command ^
    "Expand-Archive -Path 'uv.zip' -DestinationPath '%UV_TOOLS_DIR%' -Force" || (echo %RED%[错误] 解压 uv.zip 失败%RESET% & goto :theend)

    if not exist "%UV_EXE%" (
        echo %RED%[错误] 解压后未找到 uv.exe%RESET%
        goto :theend
    )
)

set PATH=%UV_TOOLS_DIR%;%PATH%

cd backend
if not exist .venv (
  "%UV_EXE%" venv .venv || (echo %RED%[错误] 创建虚拟环境失败%RESET% & goto :back_to_root)
)
call .venv\Scripts\activate.bat
set UV_LINK_MODE=copy
"%UV_EXE%" sync || (echo %RED%[错误] uv sync 失败%RESET% & goto :back_to_root)

start "Backend Server" cmd /k "call .venv\Scripts\activate.bat && set ENV=DEV && uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-ping-interval 60 --ws-ping-timeout 120"

:back_to_root
cd ..


REM ==========================================================
REM === 前端
REM ==========================================================
cd frontend

echo %YELLOW%正在安装前端依赖（使用便携 pnpm）...%RESET%
call "%PNPM_CMD%" install

echo %YELLOW%正在执行 approve-builds（旧版本无 --all 参数）...%RESET%
call "%PNPM_CMD%" approve-builds

echo %YELLOW%正在启动前端开发服务器...%RESET%
start "Frontend Server" cmd /k ""%PNPM_CMD%" run dev"

cd ..

echo.
echo %GREEN%===== MathModelAgent 系统已成功启动 =====%RESET%
echo - 后端 API:  http://localhost:8000
echo - 前端页面: http://localhost:5173
echo.
goto :theend


REM ==========================================================
REM === 子程序
REM ==========================================================
:EnsurePython
setlocal
if exist "%~1" (
  echo %GREEN%已找到 Python: "%~1"%RESET%
  endlocal & exit /b 0
)
echo %YELLOW%未找到 Python，正在下载...%RESET%
powershell -Command "& {Invoke-WebRequest -Uri '%~3' -OutFile 'python312.zip' -UseBasicParsing}" || (endlocal & exit /b 1)
powershell -Command "& {Expand-Archive -Path 'python312.zip' -DestinationPath '%~4%' -Force}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1

:EnsureNode
setlocal
if exist "%~1" (
  echo %GREEN%已找到 Node.js: "%~1"%RESET%
  endlocal & exit /b 0
)
echo %YELLOW%未找到 Node.js，正在下载...%RESET%
powershell -Command "& {Invoke-WebRequest -Uri 'https://nodejs.org/dist/v22.18.0/node-v22.18.0-win-x64.zip' -OutFile 'node.zip' -UseBasicParsing}" || (endlocal & exit /b 1)
powershell -Command "& {Expand-Archive -Path 'node.zip' -DestinationPath '%~2%' -Force}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1

:EnsureRedis
setlocal
if exist "%~1" (
  echo %GREEN%已找到 Redis: "%~1"%RESET%
  endlocal & exit /b 0
)
echo %YELLOW%未找到 Redis，正在下载...%RESET%
powershell -Command "& {Invoke-WebRequest -Uri 'https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip' -OutFile 'redis-portable.zip' -UseBasicParsing}" || (endlocal & exit /b 1)
powershell -Command "& {Expand-Archive -Path 'redis-portable.zip' -DestinationPath '%~2%' -Force}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1

:EnsurePnpm
setlocal
if exist "%~1" (
  echo %GREEN%已找到 pnpm: "%~1"%RESET%
  endlocal & exit /b 0
)
echo %YELLOW%未找到 pnpm，正在下载便携版...%RESET%
powershell -Command "& {Invoke-WebRequest -Uri 'https://github.com/pnpm/pnpm/releases/latest/download/pnpm-win-x64.exe' -OutFile '%~2\pnpm.exe' -UseBasicParsing}" || (endlocal & exit /b 1)
if exist "%~1" (endlocal & exit /b 0)
endlocal & exit /b 1
