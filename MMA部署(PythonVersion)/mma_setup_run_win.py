import subprocess
import sys
from pathlib import Path
import os
import shutil
import site
import time
import signal
import tkinter as tk
from tkinter import filedialog, messagebox
import logging
import socket
from urllib.parse import urlparse


def ensure_library_installed(pip_name: str, import_name: str = None, index_url: str = None):
    """
    确保第三方库已安装（缺失时静默安装）。
    参数:
        pip_name: pip 包名（如 'python-dotenv'）
        import_name: 导入名（如 'dotenv'），不填则与 pip_name 同名
        index_url: 指定镜像地址（可选）
    说明:
        已安装则直接返回；未安装时使用 pip 安装，安装失败会抛出异常。
    """
    mod = import_name or pip_name
    try:
        __import__(mod)
        return
    except ImportError:
        print(f"Installing {pip_name}...")
        cmd = [sys.executable, "-m", "pip", "install", pip_name]
        if index_url:
            cmd += ["-i", index_url]
        subprocess.run(cmd, check=True)


ensure_library_installed("python-dotenv", import_name="dotenv", index_url="https://pypi.tuna.tsinghua.edu.cn/simple")
ensure_library_installed("psutil", import_name="psutil", index_url="https://pypi.tuna.tsinghua.edu.cn/simple")


def _resolve_uv_cmd() -> list[str] | None:
    """
    返回可执行 uv 的命令列表：
    - 若找到 uv 可执行文件，返回 [<uv_full_path>]
    - 否则若可作为模块运行，返回 [sys.executable, "-m", "uv"]
    - 找不到时返回 None
    """
    # 1) PATH 中寻找
    which_uv = shutil.which("uv") or shutil.which("uv.exe")
    if which_uv:
        return [which_uv]

    # 2) 常见候选目录
    candidates = [
        Path(sys.executable).parent / "Scripts" / "uv.exe",
        Path(sys.executable).parent / "uv.exe",
        Path(site.getuserbase()) / "Scripts" / "uv.exe",  # --user 安装位置
    ]
    for p in candidates:
        if p.exists():
            return [str(p)]

    # 3) 尝试作为模块运行
    try:
        __import__("uv")
        return [sys.executable, "-m", "uv"]
    except Exception:
        return None


from dotenv import load_dotenv, set_key
import psutil

required_vars = [
    "COORDINATOR_API_KEY",
    "COORDINATOR_MODEL",
    "MODELER_API_KEY",
    "MODELER_MODEL",
    "CODER_API_KEY",
    "CODER_MODEL",
    "WRITER_API_KEY",
    "WRITER_MODEL",
]

project_root = Path.cwd()
log_dir = project_root / "log"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "mma_setup_run_win.log"

file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="w")
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s - [%(filename)s:%(lineno)d]")
file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

env_path = Path(".") / ".env"
load_dotenv(dotenv_path=env_path)

frontend_process = None
backend_process = None
redis_process = None


def select_directory(title: str) -> str:
    """
    打开系统目录选择器，返回用户选择的目录路径。
    参数:
        title: 对话框标题
    返回:
        目录的字符串路径；若取消则返回空串。
    """
    root = tk.Tk()
    root.withdraw()
    directory = filedialog.askdirectory(title=title)
    root.destroy()
    return directory


def get_user_input(env_var: str, title: str) -> str:
    """
    从环境变量或目录选择器获取路径，并写回 .env。
    参数:
        env_var: 环境变量名（如 'REDIS_PATH'）
        title: 目录选择器标题
    返回:
        有效路径字符串。无效选择会提示并要求重新选择。
    副作用:
        当用户选择有效目录时，写入 .env 文件。
    """
    value = os.getenv(env_var)
    while not value or not Path(value).exists():
        value = select_directory(title)
        if value and Path(value).exists():
            set_key(env_path, env_var, value, quote_mode="never")
            logger.info(f"Set {env_var} to {value}")
        else:
            messagebox.showerror("Error", f"Please select a valid directory for {env_var}.")
    return value


def check_path_valid(path: str, required_files: list) -> bool:
    """
    校验路径存在且包含必需文件。
    参数:
        path: 待校验目录
        required_files: 必需文件列表（相对 path）
    返回:
        True 表示通过校验，否则 False（并记录日志）。
    """
    path_obj = Path(path)
    if not path_obj.exists():
        logger.error(f"Directory does not exist: {path}")
        return False
    missing_files = [file for file in required_files if not (path_obj / file).exists()]
    if missing_files:
        logger.error(f"Missing files in {path}: {', '.join(missing_files)}")
        return False
    logger.info(f"Path {path} is valid with all required files present")
    return True


def start_redis(redis_path: str) -> bool:
    """
    启动 Redis 服务器进程。
    参数:
        redis_path: Redis 安装目录（需包含 redis-server.exe）
    返回:
        成功返回 True；失败返回 False（失败时清理可能的子进程）。
    说明:
        调用后会短暂等待以确认启动，再记录日志。
    """
    global redis_process
    redis_server = Path(redis_path) / "redis-server.exe"
    if not redis_server.exists():
        logger.error(f"Redis server not found at {redis_server}")
        messagebox.showerror("Error", f"Redis server not found at {redis_server}. Please check REDIS_PATH.")
        return False

    logger.info(f"Starting Redis server: {redis_server}")
    try:
        redis_process = subprocess.Popen(
            [str(redis_server)],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=str(redis_path),
        )
        time.sleep(2)
        logger.info("Redis started successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to start Redis: {e}")
        if redis_process and redis_process.poll() is None:
            terminate_process_tree(redis_process.pid)
        return False


def configure_env_files(project_root: Path):
    """
    配置 backend/.env.dev 与 frontend/.env.development：
    1) 若不存在则从示例复制或创建并填充占位键。
    2) 加载并校验后端必需变量，缺失则退出。
    3) 确保前端环境文件存在（无示例则退出）。
    参数:
        project_root: 项目根目录 Path
    """
    backend_env = project_root / "backend" / ".env.dev"
    frontend_env = project_root / "frontend" / ".env.development"
    backend_env_example = project_root / "backend" / ".env.dev.example"
    frontend_env_example = project_root / "frontend" / ".env.example"

    if not backend_env.exists():
        if backend_env_example.exists():
            shutil.copy(backend_env_example, backend_env)
            logger.info(f"Created {backend_env} from {backend_env_example}")
        else:
            with backend_env.open("w") as f:
                f.write("# Please set the following required variables\n")
                for var in required_vars:
                    f.write(f"{var}=\n")
                f.write("\n# Optional variables with defaults\n")
                f.write("MAX_CHAT_TURNS=60\n")
                f.write("MAX_RETRIES=5\n")
                f.write("SERVER_HOST=http://localhost:8000\n")
                f.write("LOG_LEVEL=DEBUG\n")
                f.write("DEBUG=true\n")
                f.write("REDIS_URL=redis://localhost:6379/0\n")
                f.write("REDIS_MAX_CONNECTIONS=20\n")
                f.write("CORS_ALLOW_ORIGINS=http://localhost:5173,http://localhost:3000\n")
            logger.info(f"Created new {backend_env} with placeholders")
    else:
        logger.info(f"Backend .env already exists at {backend_env}")

    load_dotenv(dotenv_path=backend_env, override=True)
    missing_vars = [var for var in required_vars if not os.getenv(var, "").strip()]
    if missing_vars:
        logger.error(f"Missing required variables in {backend_env}: {', '.join(missing_vars)}")
        logger.error("Please edit the file and re-run the script.")
        sys.exit(1)
    else:
        logger.info("All required variables are set. Proceeding.")

    if not frontend_env.exists():
        if frontend_env_example.exists():
            shutil.copy(frontend_env_example, frontend_env)
            logger.info(f"Created {frontend_env} from {frontend_env_example}")
        else:
            logger.error(f"Frontend .env.example not found at {frontend_env_example}. Cannot create .env.development.")
            sys.exit(1)
    else:
        logger.info(f"Frontend .env.development already exists at {frontend_env}")


def install_backend_dependencies(project_root: Path):
    backend_dir = project_root / "backend"
    os.chdir(backend_dir)

    # --- 新的 uv 解析逻辑开始 ---
    uv_cmd = _resolve_uv_cmd()
    if uv_cmd is None:
        logger.info("uv not found, installing with pip (user)...")
        try:
            # 优先 --user，避免权限问题
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "uv"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            # 尝试不加 --user 的兜底（可能在 venv 中）
            logger.warning(f"pip install uv --user failed: {e}. Retrying without --user...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "uv"],
                check=True,
                capture_output=True,
                text=True,
            )

        # 安装后重新解析一次
        uv_cmd = _resolve_uv_cmd()

    if uv_cmd is None:
        message = (
            "Failed to locate 'uv' after installation. "
            "Please ensure your User Scripts directory is in PATH "
            "or install uv manually."
        )
        logger.error(message)
        messagebox.showerror("Error", message)
        sys.exit(1)

    logger.info(f"Using uv command: {' '.join(uv_cmd)}")

    # --- 使用解析出的 uv 命令执行 sync ---
    logger.info("Installing backend dependencies...")
    try:
        subprocess.run(
            uv_cmd + ["sync"],
            check=True,  # 失败直接抛异常
            text=True,
            # 不设置 capture_output，这样 uv 的进度在控制台和 log 文件都能看到
        )
    except subprocess.CalledProcessError as e:
        logger.error("Failed to sync backend dependencies (uv). Return code=%s", e.returncode)
        logger.error("Tip: 检查网络、uv.lock/pyproject.toml、以及是否需要代理/镜像源。")
        sys.exit(1)

    logger.info("Backend dependencies installed successfully")
    venv_dir = backend_dir / ".venv"
    if not venv_dir.exists():
        logger.error("Virtual environment not created")
        sys.exit(1)
    logger.info("Virtual environment ready")
    return venv_dir


def get_global_bin_dir(npm_path: Path) -> Path:
    """
    获取 npm 全局前缀对应的 bin 目录。
    参数:
        npm_path: npm.cmd 路径
    返回:
        Windows: 前缀目录；Unix: 前缀/bin
    失败:
        获取失败将退出程序（已记录日志）。
    """
    try:
        result = subprocess.run(
            [str(npm_path), "config", "get", "prefix"],
            capture_output=True,
            text=True,
            check=True,
        )
        prefix = result.stdout.strip()
        bin_dir = Path(prefix) if os.name == "nt" else Path(prefix) / "bin"
        logger.info(f"Global bin directory: {bin_dir}")
        return bin_dir
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get global prefix: {e}")
        sys.exit(1)


def get_pnpm_path(npm_path: Path) -> Path:
    """
    根据 npm 全局 bin 目录定位 pnpm 命令路径，不存在则全局安装。
    参数:
        npm_path: npm.cmd 路径
    返回:
        pnpm.cmd 的 Path
    失败:
        安装失败会提示并退出。
    """
    bin_dir = get_global_bin_dir(npm_path)
    pnpm_path = bin_dir / "pnpm.cmd"
    logger.info(f"Checking if pnpm exists at {pnpm_path}")
    if not pnpm_path.exists():
        logger.info("pnpm not found, attempting to install globally...")
        result = subprocess.run(
            [str(npm_path), "install", "-g", "pnpm"],
            capture_output=True,
            text=True,
            check=True,
        )
        if result.returncode != 0 or not pnpm_path.exists():
            logger.error(f"Failed to install pnpm: {result.stderr}")
            messagebox.showerror("Error", "Failed to install pnpm. Please install it manually using npm.")
            sys.exit(1)
        logger.info("pnpm installed successfully")
    return pnpm_path


def install_frontend_dependencies(project_root: Path, nodejs_path: str):
    """
    使用 pnpm 安装前端依赖。
    参数:
        project_root: 项目根目录 Path
        nodejs_path: Node.js 安装目录（需包含 node.exe 与 npm.cmd）
    失败:
        任何一步失败将退出进程。
    """
    frontend_dir = project_root / "frontend"
    os.chdir(frontend_dir)

    npm_path = Path(nodejs_path) / "npm.cmd"
    node_path = Path(nodejs_path) / "node.exe"
    if not npm_path.exists() or not node_path.exists():
        logger.error(f"npm or node not found at {nodejs_path}")
        sys.exit(1)

    logger.info(f"node_path: {node_path}")
    logger.info(f"npm_path: {npm_path}")

    env = os.environ.copy()
    env["PATH"] = str(nodejs_path) + os.pathsep + env["PATH"]

    try:
        subprocess.run([str(npm_path), "--version"], check=True, capture_output=True, env=env)
        logger.info("npm is functioning correctly")
    except subprocess.CalledProcessError:
        logger.error("npm is not functioning correctly. Please check Node.js installation.")
        sys.exit(1)

    pnpm_path = get_pnpm_path(npm_path)
    logger.info("Installing frontend dependencies with pnpm...")
    result = subprocess.run(
        [str(pnpm_path), "install"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=env,
    )
    if result.returncode == 0:
        logger.info("Frontend dependencies installed successfully")
    else:
        logger.error(f"Failed to install frontend dependencies: {result.stderr}")
        sys.exit(1)


def run_frontend(project_root: Path, nodejs_path: str) -> subprocess.Popen:
    """
    启动前端开发服务器（pnpm run dev）。
    参数:
        project_root: 项目根目录 Path
        nodejs_path: Node.js 安装目录
    返回:
        前端子进程对象（subprocess.Popen）
    失败:
        Node.js 路径无效会直接退出。
    """
    global frontend_process
    frontend_dir = project_root / "frontend"
    os.chdir(frontend_dir)
    npm_path = Path(nodejs_path) / "npm.cmd"
    node_exe = Path(nodejs_path) / "node.exe"

    logger.info(f"nodejs_path: {nodejs_path}")
    logger.info(f"node.exe exists: {node_exe.exists()}")
    logger.info(f"npm.cmd exists: {npm_path.exists()}")
    if not node_exe.exists() or not npm_path.exists():
        logger.error(f"Node.js path invalid: {nodejs_path}")
        sys.exit(1)

    pnpm_path = get_pnpm_path(npm_path)

    env = os.environ.copy()
    env["PATH"] = str(nodejs_path) + os.pathsep + env["PATH"]
    env["NODE"] = str(node_exe)

    logger.info("Starting frontend server with 'pnpm run dev'...")
    frontend_process = subprocess.Popen(
        [str(pnpm_path), "run", "dev"],
        shell=False,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        env=env,
    )
    return frontend_process


def find_available_port(start_port: int = 8000, max_tries: int = 50) -> int:
    """
    从指定起始端口扫描可用端口，必要时尝试清理被占用端口。
    参数:
        start_port: 起始端口
        max_tries: 最大尝试次数
    返回:
        可用端口号
    异常:
        连续扫描失败将抛出 RuntimeError。
    """
    port = start_port
    for _ in range(max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                logger.info(f"Port {port} is available")
                return port
            except OSError as e:
                if e.errno in (98, 10048):
                    if clear_port(port):
                        time.sleep(1)
                        try:
                            s.bind(("0.0.0.0", port))
                            logger.info(f"Port {port} has been cleared and is available")
                            return port
                        except OSError as retry_e:
                            logger.warning(f"Retry bind failed for port {port}: {retry_e}")
                            port += 1
                    else:
                        logger.info(f"Failed to clear port {port}, trying next port")
                        port += 1
                else:
                    logger.error(f"Unexpected error binding to port {port}: {e}")
                    raise
    logger.error(f"No available ports found between {start_port} and {port-1}")
    raise RuntimeError("No available ports found")


def clear_port(port: int) -> bool:
    """
    尝试通过终止占用该端口的进程来清理端口。
    参数:
        port: 端口号
    返回:
        若发现并清理了占用进程则返回 True，否则 False。
    注意:
        仅处理处于 LISTEN/TIME_WAIT 且可获取到 PID 的连接。
    """
    try:
        cleared = False
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status in ("LISTEN", "TIME_WAIT"):
                process = psutil.Process(conn.pid)
                logger.info(f"Terminating process {conn.pid} using port {port}")
                process.terminate()
                try:
                    process.wait(timeout=3)
                    cleared = True
                except psutil.TimeoutExpired:
                    process.kill()
                    cleared = True
        return cleared
    except Exception as e:
        logger.warning(f"Failed to clear port {port}: {e}")
        return False


def run_backend(project_root: Path, port: int) -> subprocess.Popen:
    """
    在指定端口启动后端（uvicorn 热重载）。
    参数:
        project_root: 项目根目录 Path
        port: 监听端口
    返回:
        后端子进程对象（subprocess.Popen）
    过程:
        优先使用 backend/.venv 下的 python；加载 .env.dev；轮询端口可用性直至成功或失败退出。
    """
    global backend_process
    backend_dir = project_root / "backend"
    venv_python = backend_dir / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python.exe"
    if not venv_python.exists():
        logger.warning(f"Virtual environment Python not found at {venv_python}, using system Python")
        venv_python = sys.executable

    env_path_local = backend_dir / ".env.dev"
    load_dotenv(dotenv_path=env_path_local, override=True)
    logger.info(f"REDIS_URL set to {os.getenv('REDIS_URL')}")

    env = os.environ.copy()
    env["ENV"] = "DEV"

    logger.info(f"Starting backend server on port {port} using {venv_python}...")
    backend_process = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--reload",
            "--ws-ping-interval",
            "60",
            "--ws-ping-timeout",
            "120",
        ],
        cwd=str(backend_dir),
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("localhost", port))
                logger.info(f"Backend successfully started on port {port}")
                break
        except (ConnectionRefusedError, socket.timeout):
            if attempt < max_attempts - 1:
                time.sleep(1)
            else:
                logger.error("Backend failed to start after multiple attempts.")
                sys.exit(1)
    return backend_process


def terminate_process_tree(pid: int):
    """
    递归终止指定进程及其子进程（优先 terminate，超时后 kill）。
    参数:
        pid: 进程号
    说明:
        对于不存在的进程会记录并忽略；出现异常时尝试强制 kill。
    """
    try:
        process = psutil.Process(pid)
        for child in process.children(recursive=True):
            try:
                child.terminate()
                child.wait(timeout=3)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                child.kill()
                logger.info(f"Killed child process {child.pid} after termination timeout")
        process.terminate()
        process.wait(timeout=3)
        logger.info(f"Process {pid} terminated successfully")
    except psutil.NoSuchProcess:
        logger.info(f"Process {pid} does not exist, no action taken")
    except Exception as e:
        logger.error(f"Failed to terminate process {pid}: {e}")
        try:
            process.kill()
            logger.info(f"Forced kill of process {pid}")
        except psutil.NoSuchProcess:
            pass


def clear_python_cache(project_root: Path):
    """
    递归清理 Python 缓存（__pycache__ 目录与 .pyc 文件）。
    参数:
        project_root: 清理的根目录 Path
    """
    logger.info(f"Clearing Python cache files in {project_root}...")
    cache_count = 0
    for pycache_dir in project_root.glob("__pycache__"):
        shutil.rmtree(pycache_dir, ignore_errors=True)
        cache_count += 1
    for pycache_dir in project_root.glob("**/__pycache__"):
        shutil.rmtree(pycache_dir, ignore_errors=True)
        cache_count += 1
    for pyc_file in project_root.glob("**/*.pyc"):
        pyc_file.unlink(missing_ok=True)
        cache_count += 1
    logger.info(f"Removed {cache_count} Python cache items.")


def shutdown_services():
    """
    关闭前端、后端与 Redis 服务进程，最终记录完成状态。
    说明:
        对仍在运行的进程调用 terminate/kill；已退出的进程跳过。
    """
    global frontend_process, backend_process, redis_process
    logger.info("Shutting down services...")

    if backend_process:
        if backend_process.poll() is None:
            logger.info("Terminating backend server...")
            terminate_process_tree(backend_process.pid)
        else:
            logger.info("Backend process already terminated")

    if frontend_process:
        if frontend_process.poll() is None:
            logger.info("Terminating frontend server...")
            terminate_process_tree(frontend_process.pid)
        else:
            logger.info("Frontend process already terminated")

    if redis_process:
        if redis_process.poll() is None:
            logger.info("Terminating Redis server...")
            terminate_process_tree(redis_process.pid)
        else:
            logger.info("Redis process already terminated")

    logger.info("All services stopped.")


def signal_handler(sig, frame):
    """
    统一处理终止信号（SIGINT/SIGTERM），触发优雅退出。
    参数:
        sig: 信号编号
        frame: 当前栈帧（未使用）
    """
    logger.info(f"Received signal {sig}, initiating shutdown...")
    sys.exit(0)


def main():
    """
    主入口：清理缓存 -> 选择依赖路径 -> 启动 Redis -> 配置 env -> 安装依赖 ->
            选端口 -> 写前端 .env -> 安装前端依赖 -> 启动前后端 -> 运行监控循环 ->
            出错或中断时清理并退出。
    说明:
        循环内每秒检查子进程存活；Python 缓存仅在退出时清理一次。
    """
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    clear_python_cache(project_root)

    redis_path = get_user_input("REDIS_PATH", "Select Redis installation directory")
    nodejs_path = get_user_input("NODEJS_PATH", "Select Node.js installation directory")

    if not check_path_valid(redis_path, ["redis-server.exe", "redis-cli.exe"]):
        messagebox.showerror("Error", f"Invalid Redis path: {redis_path}. Please select again.")
        redis_path = select_directory("Select Redis installation directory")
        set_key(env_path, "REDIS_PATH", redis_path, quote_mode="never")

    if not check_path_valid(nodejs_path, ["npm.cmd"]):
        messagebox.showerror("Error", f"Invalid Node.js path: {nodejs_path}. Please select again.")
        nodejs_path = select_directory("Select Node.js installation directory")
        set_key(env_path, "NODEJS_PATH", nodejs_path, quote_mode="never")

    logger.info("Starting Redis...")
    if not start_redis(redis_path):
        logger.error("Failed to start Redis. Please check the path or start manually.")
        sys.exit(1)

    logger.info("Configuring environment files...")
    configure_env_files(project_root)

    logger.info("Installing backend dependencies...")
    venv_dir = install_backend_dependencies(project_root)
    venv_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python.exe"
    if not venv_python.exists():
        logger.error(f"Virtual environment Python not found: {venv_python}")
        sys.exit(1)

    logger.info("Scanning for available backend port...")
    try:
        port = find_available_port()
        logger.info(f"Selected port {port} for backend")
    except RuntimeError as e:
        logger.error(f"Failed to select port: {e}")
        sys.exit(1)

    frontend_env = project_root / "frontend" / ".env.development"
    set_key(
        frontend_env,
        "VITE_API_BASE_URL",
        f"http://localhost:{port}",
        quote_mode="never",
    )
    set_key(frontend_env, "VITE_WS_URL", f"ws://localhost:{port}", quote_mode="never")
    logger.info(
        f"Updated frontend .env.development: VITE_API_BASE_URL=http://localhost:{port} and VITE_WS_URL=ws://localhost:{port}"
    )

    logger.info("Installing frontend dependencies...")
    install_frontend_dependencies(project_root, nodejs_path)

    logger.info("Starting project services...")
    run_frontend(project_root, nodejs_path)
    run_backend(project_root, port)

    logger.info(f"Backend running at http://0.0.0.0:{port}")
    logger.info("Frontend running at http://localhost:5173 (check console for exact port)")

    try:
        while True:
            time.sleep(1)
            if frontend_process and frontend_process.poll() is not None:
                logger.error(f"Frontend process exited with code {frontend_process.poll()}")
                raise RuntimeError("Frontend crashed")
            if backend_process and backend_process.poll() is not None:
                logger.error(f"Backend process exited with code {backend_process.poll()}")
                raise RuntimeError("Backend crashed")
    except (KeyboardInterrupt, RuntimeError) as e:
        logger.info(f"Shutting down due to {e}")
    finally:
        shutdown_services()
        clear_python_cache(project_root)
        logger.info("Project shutdown complete.")


if __name__ == "__main__":
    main()
