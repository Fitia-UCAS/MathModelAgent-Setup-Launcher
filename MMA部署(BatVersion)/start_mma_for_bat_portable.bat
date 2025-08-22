@echo off
setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul

REM === 根目录与可执行文件路径设置 ===
set "ROOT=%~dp0"
set "REDIS_DIR=%ROOT%redis-portable"
set "REDIS_SERVER=%REDIS_DIR%\redis-server.exe"
set "REDIS_CLI=%REDIS_DIR%\redis-cli.exe"

set "BACKEND_PY=%ROOT%backend\.venv\Scripts\python.exe"
set "NODE_DIR=%ROOT%nodejs-portable\node-v22.18.0-win-x64"

REM === 优先找 pnpm.exe，其次找 pnpm.cmd ===
if exist "%NODE_DIR%\pnpm.exe" (
  set "PNPM_CMD=%NODE_DIR%\pnpm.exe"
) else (
  set "PNPM_CMD=%NODE_DIR%\pnpm.cmd"
)

REM === 启动 Redis 服务 ===
if not exist "%REDIS_SERVER%" (
  echo [ERROR] 未找到 %REDIS_SERVER%
  pause & exit /b 1
)
start "" "%REDIS_SERVER%"

echo 等待 Redis 启动...
timeout /t 2 /nobreak >nul

echo 检查 Redis 是否运行...
if exist "%REDIS_CLI%" (
  "%REDIS_CLI%" -h 127.0.0.1 -p 6379 ping | findstr /I PONG >nul
  if errorlevel 1 (
    echo [ERROR] Redis 启动失败（端口或权限问题）
    pause & exit /b 1
  )
) else (
  echo [WARN] 未找到 redis-cli.exe，跳过 PING 检查
)

REM === 启动后端服务 ===
pushd "%ROOT%backend"
set "ENV=DEV"

if not exist "%BACKEND_PY%" (
  echo [ERROR] 未找到后端 Python 环境：%BACKEND_PY%
  pause & popd & exit /b 1
)

start "Backend Server" cmd /k ""%BACKEND_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-ping-interval 60 --ws-ping-timeout 120 --reload"
popd

REM === 启动前端服务 ===
pushd "%ROOT%frontend"
if not exist "%PNPM_CMD%" (
  echo [ERROR] 未找到 pnpm（请确认 nodejs-portable 下有 pnpm.exe 或 pnpm.cmd）
  pause & popd & exit /b 1
)

start "Frontend Server" cmd /k ""%PNPM_CMD%" run dev"
popd

REM === 所有服务已启动 ===
pause
