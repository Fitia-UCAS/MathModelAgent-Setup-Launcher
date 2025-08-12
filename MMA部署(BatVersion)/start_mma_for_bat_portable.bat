@echo off
setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul

rem 根目录与可执行文件
set "ROOT=%~dp0"
set "REDIS_DIR=%ROOT%redis-portable"
set "REDIS_SERVER=%REDIS_DIR%\redis-server.exe"
set "REDIS_CLI=%REDIS_DIR%\redis-cli.exe"

set "BACKEND_PY=%ROOT%backend\.venv\Scripts\python.exe"
set "NODE_DIR=%ROOT%nodejs-portable\node-v20.17.0-win-x64"
set "PNPM_CMD=%NODE_DIR%\pnpm.cmd"

echo === 启动 Redis ===
if not exist "%REDIS_SERVER%" (
  echo [ERROR] 未找到 %REDIS_SERVER%
  pause & exit /b 1
)
rem ✅ 正确：用空标题 + 直接引用可执行文件
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

echo === 启动后端 ===
pushd "%ROOT%backend"
set "ENV=DEV"
if not exist ".venv\Scripts\python.exe" (
  echo [INFO] 创建虚拟环境并安装依赖...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  pip install uv
  uv sync
)
rem ✅ 正确：用 cmd /k 时，内层再包一层引号
start "Backend Server" cmd /k ""%BACKEND_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-ping-interval 60 --ws-ping-timeout 120 --reload"
popd

echo === 启动前端 ===
pushd "%ROOT%frontend"
if not exist "%PNPM_CMD%" (
  echo [ERROR] 未找到 %PNPM_CMD% （请确认 nodejs-portable 已解压并安装过 pnpm）
  pause & popd & exit /b 1
)
call "%PNPM_CMD%" i
rem ✅ 正确：cmd /k + 内层引号包可执行
start "Frontend Server" cmd /k ""%PNPM_CMD%" run dev"
popd

echo === 所有服务已启动 ===
pause
