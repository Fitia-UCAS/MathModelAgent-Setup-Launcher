@echo off
chcp 65001 >nul
setlocal ENABLEDELAYEDEXPANSION

REM ===========================================
REM 启用 Windows 10+ ANSI 转义序列支持
REM ===========================================
for /f "tokens=2 delims=: " %%a in ('reg query HKCU\Console ^| findstr VirtualTerminalLevel') do set VT=%%a
if not defined VT reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul

REM ===========================================
REM 彩色定义
REM ===========================================
set "RED=[31m"
set "GREEN=[32m"
set "YELLOW=[33m"
set "CYAN=[36m"
set "RESET=[0m"

REM ================================
REM 基础检查
REM ================================
echo %YELLOW%正在检查 Docker 是否已安装并运行...%RESET%
docker --version >nul 2>&1 || (
    echo %RED%Docker 未安装或未运行，请先安装并启动 Docker Desktop！%RESET%
    pause
    exit /b 1
)

echo %YELLOW%正在验证项目目录...%RESET%
cd /d "%~dp0"
if not exist "docker-compose.yml" (
    echo %RED%未找到 docker-compose.yml，请确认当前目录是否正确！%RESET%
    pause
    exit /b 1
)

REM ================================
REM 选择 compose 命令（v2 或 legacy）
REM ================================
docker compose version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set COMPOSE_CMD=docker compose
) else (
    docker-compose version >nul 2>&1 || (
        echo %RED%未找到 "docker compose" 或 "docker-compose"！%RESET%
        pause
        exit /b 1
    )
    set COMPOSE_CMD=docker-compose
)

REM ================================
REM 启用 BuildKit
REM ================================
set DOCKER_BUILDKIT=1
set COMPOSE_DOCKER_CLI_BUILD=1

REM ================================
REM 镜像加速器配置（可选）
REM ================================
echo(
echo %YELLOW%是否配置 Docker 镜像加速器？ (y/n，默认 n)%RESET%
set /p SET_MIRROR=
if /i "%SET_MIRROR%"=="y" (
    if not exist "%USERPROFILE%\.docker" mkdir "%USERPROFILE%\.docker" 2>nul
    > "%USERPROFILE%\.docker\daemon.json" echo { "registry-mirrors": ["https://docker.1ms.run", "https://docker.xuanyuan.me", "https://hub.rat.dev", "https://dislabaiot.xyz", "https://doublezonline.cloud", "https://xdark.top"] }
    echo %GREEN%镜像加速器已写入 %USERPROFILE%\.docker\daemon.json%RESET%
    echo %CYAN%如果 Docker Desktop 没有自动重启，请手动重启以生效。%RESET%
)

REM ================================
REM 停止现有容器
REM ================================
echo(
echo %YELLOW%正在停止并删除已有容器...%RESET%
%COMPOSE_CMD% down
echo %CYAN%注意：数据存储在 volumes 中，删除容器不会导致数据丢失。%RESET%

REM ================================
REM 清理缓存（可选）
REM ================================
echo(
echo %YELLOW%是否清理所有 Docker 缓存（构建缓存、未使用的镜像/容器/网络等）？ (y/n，默认 n)%RESET%
set /p CLEAR_CACHE=
if /i "%CLEAR_CACHE%"=="y" (
    echo %YELLOW%正在清理所有 Docker 缓存...%RESET%
    docker system prune -a --volumes -f
    echo %GREEN%Docker 缓存已清理完成。%RESET%
) else (
    echo %CYAN%跳过缓存清理。%RESET%
)

REM ================================
REM 是否使用构建缓存
REM ================================
echo(
echo %YELLOW%构建镜像时是否使用缓存？ (y/n，默认 n)%RESET%
set /p BUILD_WITH_CACHE=
if /i "%BUILD_WITH_CACHE%"=="y" (
    set BUILD_OPTIONS=
) else (
    set BUILD_OPTIONS=--no-cache
)

REM ================================
REM 构建镜像
REM ================================
echo(
echo %YELLOW%正在通过 %COMPOSE_CMD% build %BUILD_OPTIONS% 构建镜像...%RESET%
%COMPOSE_CMD% build %BUILD_OPTIONS% || (
    echo %RED%镜像构建失败！%RESET%
    pause
    exit /b 1
)
echo %GREEN%镜像构建完成。%RESET%

REM ================================
REM 启动服务
REM ================================
echo(
echo %YELLOW%正在通过 %COMPOSE_CMD% up -d 启动服务...%RESET%
%COMPOSE_CMD% up -d || (
    echo %RED%服务启动失败！请使用 "%COMPOSE_CMD% logs" 查看详细日志。%RESET%
    pause
    exit /b 1
)

REM ================================
REM 日志提示
REM ================================
echo(
echo %GREEN%Docker 服务已成功启动！%RESET%
echo %CYAN%日志查看方法：%RESET%
echo   %COMPOSE_CMD% logs -f redis
echo   %COMPOSE_CMD% logs -f backend
echo   %COMPOSE_CMD% logs -f frontend

echo(
echo %GREEN%Docker 环境已配置完成！%RESET%
echo 按任意键退出...
pause
endlocal
