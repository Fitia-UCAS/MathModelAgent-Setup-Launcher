@echo off
chcp 65001 >nul
setlocal ENABLEDELAYEDEXPANSION

REM ===========================================
REM 启用 Windows 10+ ANSI 转义序列支持
REM ===========================================
for /f "tokens=2 delims=: " %%a in ('reg query HKCU\Console ^| findstr VirtualTerminalLevel') do set VT=%%a
if not defined VT reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul

REM ===========================================
REM 颜色定义
REM ===========================================
set "RED=[31m"
set "GREEN=[32m"
set "YELLOW=[33m"
set "RESET=[0m"

REM 切换到脚本所在目录
cd /d "%~dp0"

echo %YELLOW%正在检查 Docker 是否运行...%RESET%
docker info >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo %RED%错误: Docker 未运行或未安装，请先启动 Docker Desktop。%RESET%
    pause
    exit /b 1
)

echo %YELLOW%正在检测 compose 命令...%RESET%
docker compose version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "COMPOSE_CMD=docker compose"
) else (
    docker-compose version >nul 2>&1 || (
        echo %RED%错误: 未找到 "docker compose" 或 "docker-compose"。%RESET%
        pause
        exit /b 1
    )
    set "COMPOSE_CMD=docker-compose"
)

echo.
set /p CHOICE=是否执行彻底重启（停止并删除容器后再启动）? (y/n，回车默认=n): 

if /i "%CHOICE%"=="y" (
    echo.
    echo %YELLOW%执行【彻底重启】...%RESET%
    %COMPOSE_CMD% down
    if %ERRORLEVEL% neq 0 (
        echo %RED%错误: 停止容器失败。%RESET%
        pause
        exit /b 1
    )
    %COMPOSE_CMD% up -d
) else (
    echo.
    echo %YELLOW%执行【轻量重启】（仅重新加载 env，强制重建容器）...%RESET%
    %COMPOSE_CMD% up -d --force-recreate
)

if %ERRORLEVEL% neq 0 (
    echo %RED%错误: 容器重启失败，请检查 "%COMPOSE_CMD% logs"。%RESET%
    pause
    exit /b 1
)

echo.
echo %GREEN%容器已成功启动，当前运行中的容器:%RESET%

REM 使用 --format 提取字段并上色：容器ID 容器名 状态 端口
for /f "tokens=1,2,3,*" %%a in ('docker ps --format "{{.ID}} {{.Names}} {{.Status}} {{.Ports}}" ^| findstr mathmodelagent_') do (
    echo 容器ID: %%a   名称: %GREEN%%%b%RESET%   状态: %YELLOW%%%c %d%RESET%
)

echo.
echo %GREEN%服务访问地址:%RESET%
echo - Redis:    localhost:6379
echo - Backend:  http://localhost:8000
echo - Frontend: http://localhost:5173

echo.
pause
endlocal
