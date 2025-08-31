import subprocess
import sys
from pathlib import Path
import os
import shutil
import time
import socket
from typing import Optional


# ========= 基础工具 =========
class TimeUtils:
    @staticmethod
    def ts() -> str:
        return time.strftime("%H:%M:%S")


class ConsolePrinter:
    @staticmethod
    def print(prefix: str, msg: str):
        print(f"{TimeUtils.ts()} [{prefix}]: {msg}", flush=True)


class OutputConfigurator:
    @staticmethod
    def configure():
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass


class Bootstrapper:
    """引导安装：按需安装第三方库（pip）"""

    @staticmethod
    def ensure_library_installed(pip_name: str, import_name: str = None, index_url: str = None):
        mod = import_name or pip_name
        try:
            __import__(mod)
            return
        except ImportError:
            ConsolePrinter.print("Bootstrap", f"Installing {pip_name}...")
            cmd = [sys.executable, "-m", "pip", "install", pip_name]
            if index_url:
                cmd += ["-i", index_url]
            subprocess.run(cmd, check=True)


# 初始化输出 -> 三方库确保
OutputConfigurator.configure()
Bootstrapper.ensure_library_installed(
    "python-dotenv", import_name="dotenv", index_url="https://pypi.tuna.tsinghua.edu.cn/simple"
)
Bootstrapper.ensure_library_installed(
    "psutil", import_name="psutil", index_url="https://pypi.tuna.tsinghua.edu.cn/simple"
)

# 其余导入在安装后进行
import psutil


# ========= Windows 原生弹窗 =========
class Dialogs:
    """
    1) yes_no_cancel: MessageBox(Yes/No/Cancel)，支持默认按钮与可选超时
    2) ask_directory: Shell 文件夹选择对话框（新样式）
    """

    import os as _pyos

    _is_win = _pyos.name == "nt"

    @staticmethod
    def _owner_hwnd():
        if not Dialogs._is_win:
            return 0
        import ctypes

        u32 = ctypes.windll.user32
        h = u32.GetForegroundWindow()
        if not h:
            h = u32.GetConsoleWindow()
        return h

    @staticmethod
    def yes_no_cancel(
        title: str,
        message: str,
        timeout_sec: int = 0,
        default: str = "no",
        icon_emoji: str = None,  # 为兼容原签名，无实际作用
        theme: str = None,  # 为兼容原签名，无实际作用
    ) -> str:
        """
        返回: "yes" / "no" / "cancel"
        1) timeout_sec > 0：尝试 MessageBoxTimeoutW，到时选默认项
        2) default: "yes" or "no"
        """
        if not Dialogs._is_win:
            try:
                ans = input(f"{title}\n{message}\n[y]是 / [n]否 / [c]取消 > ").strip().lower()
                return {"y": "yes", "n": "no", "c": "cancel"}.get(ans, default)
            except Exception:
                return default

        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        MB_YESNOCANCEL = 0x00000003
        MB_ICONQUESTION = 0x00000020
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        MB_DEFBUTTON1 = 0x00000000
        MB_DEFBUTTON2 = 0x00000100

        defbtn = MB_DEFBUTTON2 if (default or "no").lower() == "no" else MB_DEFBUTTON1
        style = MB_YESNOCANCEL | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND | defbtn

        IDYES, IDNO, IDCANCEL = 6, 7, 2

        owner = Dialogs._owner_hwnd()
        lpText = ctypes.c_wchar_p(message)
        lpTitle = ctypes.c_wchar_p(title)

        if timeout_sec and timeout_sec > 0:
            try:
                # BOOL MessageBoxTimeoutW(HWND, LPCWSTR, LPCWSTR, UINT, WORD, DWORD)
                u32.MessageBoxTimeoutW.restype = ctypes.c_int
                u32.MessageBoxTimeoutW.argtypes = [
                    wintypes.HWND,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.UINT,
                    wintypes.WORD,
                    wintypes.DWORD,
                ]
                ret = u32.MessageBoxTimeoutW(owner, lpText, lpTitle, style, 0, int(timeout_sec * 1000))
            except Exception:
                ret = u32.MessageBoxW(owner, lpText, lpTitle, style)
        else:
            ret = u32.MessageBoxW(owner, lpText, lpTitle, style)

        if ret == IDYES:
            return "yes"
        if ret == IDNO:
            return "no"
        if ret == IDCANCEL:
            return "cancel"
        return (default or "no").lower()

    @staticmethod
    def ask_directory(title: str) -> str:
        """返回选择的文件夹路径，取消返回空字符串。"""
        if not Dialogs._is_win:
            try:
                return input(f"{title}\n请输入目录路径（留空取消）： ").strip()
            except Exception:
                return ""

        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        try:
            ole32.CoInitialize(None)
        except Exception:
            pass

        BIF_RETURNONLYFSDIRS = 0x00000001
        BIF_NEWDIALOGSTYLE = 0x00000040
        BIF_VALIDATE = 0x00000020

        class BROWSEINFO(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", wintypes.LPWSTR),
                ("lpszTitle", wintypes.LPWSTR),
                ("ulFlags", ctypes.c_uint),
                ("lpfn", ctypes.c_void_p),
                ("lParam", ctypes.c_void_p),
                ("iImage", ctypes.c_int),
            ]

        owner = Dialogs._owner_hwnd()
        display_name = ctypes.create_unicode_buffer(260)

        bi = BROWSEINFO()
        bi.hwndOwner = owner
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(display_name, wintypes.LPWSTR)
        bi.lpszTitle = ctypes.c_wchar_p(title)
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_VALIDATE
        bi.lpfn = None
        bi.lParam = None
        bi.iImage = 0

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass
            return ""

        path_buf = ctypes.create_unicode_buffer(1024)
        ok = shell32.SHGetPathFromIDListW(pidl, path_buf)
        try:
            ctypes.windll.ole32.CoTaskMemFree(pidl)
        except Exception:
            pass
        try:
            ole32.CoUninitialize()
        except Exception:
            pass

        return path_buf.value if ok else ""


# ========= 配置管理 =========
from dotenv import load_dotenv, set_key, dotenv_values


class ConfigManager:
    def __init__(self, env_file: Path):
        self.env_file = env_file
        self._file_values = {}
        self.reload()

    def reload(self):
        load_dotenv(dotenv_path=self.env_file, override=True)
        try:
            self._file_values = dotenv_values(self.env_file) if self.env_file.exists() else {}
        except Exception:
            self._file_values = {}

    def get(self, key: str, default: str = "") -> str:
        v = os.getenv(key)
        if v is not None:
            return v
        return self._file_values.get(key, default)

    def set(self, key: str, value: str):
        set_key(self.env_file, key, value, quote_mode="never")
        os.environ[key] = value

    def exists(self, key: str) -> bool:
        return (os.getenv(key) not in (None, "")) or (key in self._file_values and self._file_values[key] != "")


# ========= 组件 =========
class CacheCleaner:
    name = "CacheCleaner"

    @staticmethod
    def clear(project_root: Path):
        def _skip_venv(p: Path) -> bool:
            parts = {part.lower() for part in p.parts}
            return ".venv" in parts or "venv" in parts

        removed = 0
        for d in project_root.rglob("__pycache__"):
            if d.is_dir() and not _skip_venv(d):
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        for pattern in ("*.pyc", "*.pyo"):
            for f in project_root.rglob(pattern):
                if not _skip_venv(f):
                    try:
                        f.unlink(missing_ok=True)
                        removed += 1
                    except Exception:
                        pass
        ConsolePrinter.print(CacheCleaner.name, f"Removed {removed} Python cache items from {project_root}")


class PathPicker:
    name = "PathPicker"

    @staticmethod
    def _check_path_valid(path: str, required_files: list) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        return all((p / f).exists() for f in required_files)

    @staticmethod
    def pick_and_validate(cfg: ConfigManager, env_var: str, title: str, required_files: list) -> str:
        current = cfg.get(env_var, "")
        if current and PathPicker._check_path_valid(current, required_files):
            ConsolePrinter.print(PathPicker.name, f"{env_var} already set: {current}")
            return current

        while True:
            chosen = Dialogs.ask_directory(title)
            if not chosen:
                ans = Dialogs.yes_no_cancel(
                    "未选择目录", f"没有选择任何目录。\n是否重试选择 {env_var} ？", timeout_sec=0
                )
                if ans == "yes":
                    continue
                sys.exit(1)

            if PathPicker._check_path_valid(chosen, required_files):
                cfg.set(env_var, chosen)
                ConsolePrinter.print(PathPicker.name, f"Set {env_var} to {chosen}")
                return chosen
            else:
                ans = Dialogs.yes_no_cancel(
                    "目录无效", f"目录缺少：{', '.join(required_files)}\n是否重新选择？", timeout_sec=0
                )
                if ans != "yes":
                    sys.exit(1)


class EnvFileManager:
    name = "EnvFileManager"

    @staticmethod
    def copy_envs(project_root: Path):
        backend_env = project_root / "backend" / ".env.dev"
        frontend_env = project_root / "frontend" / ".env.development"
        backend_env_example = project_root / "backend" / ".env.dev.example"
        frontend_env_example = project_root / "frontend" / ".env.example"

        if not backend_env.exists():
            if backend_env_example.exists():
                shutil.copy(backend_env_example, backend_env)
                ConsolePrinter.print(EnvFileManager.name, f"Created {backend_env} from {backend_env_example}")
            else:
                ConsolePrinter.print(
                    EnvFileManager.name, f"Backend .env.dev.example not found at {backend_env_example}"
                )
                sys.exit(1)
        else:
            ConsolePrinter.print(EnvFileManager.name, f"Backend .env already exists at {backend_env}")

        if not frontend_env.exists():
            if frontend_env_example.exists():
                shutil.copy(frontend_env_example, frontend_env)
                ConsolePrinter.print(EnvFileManager.name, f"Created {frontend_env} from {frontend_env_example}")
            else:
                ConsolePrinter.print(EnvFileManager.name, f"Frontend .env.example not found at {frontend_env_example}")
                sys.exit(1)
        else:
            ConsolePrinter.print(EnvFileManager.name, f"Frontend .env.development already exists at {frontend_env}")


class BackendInstaller:
    name = "BackendInstaller"

    @staticmethod
    def _resolve_uv_cmd() -> list[str] | None:
        import shutil, sys, site

        which_uv = shutil.which("uv") or shutil.which("uv.exe")
        if which_uv:
            return [which_uv]
        candidates = [
            Path(sys.executable).parent / "Scripts" / "uv.exe",
            Path(sys.executable).parent / "uv.exe",
            Path(site.getuserbase()) / "Scripts" / "uv.exe",
        ]
        for p in candidates:
            if p.exists():
                return [str(p)]
        try:
            __import__("uv")
            return [sys.executable, "-m", "uv"]
        except Exception:
            return None

    @staticmethod
    def _venv_python(venv_dir: Path) -> Path:
        return venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")

    @staticmethod
    def _venv_ready(venv_dir: Path) -> bool:
        py = BackendInstaller._venv_python(venv_dir)
        return venv_dir.exists() and py.exists()

    @staticmethod
    def _read_text_safely(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    @staticmethod
    def _locks_unchanged(backend_dir: Path, venv_dir: Path) -> bool:
        """
        若 .venv 存在，且 .venv.stamp 与 uv.lock 内容一致，则视为依赖未变，可跳过安装/同步。
        """
        lock = backend_dir / "uv.lock"
        stamp = venv_dir / ".venv.stamp"
        if not (lock.exists() and stamp.exists()):
            return False
        return BackendInstaller._read_text_safely(lock) == BackendInstaller._read_text_safely(stamp)

    @staticmethod
    def _write_stamp(backend_dir: Path, venv_dir: Path):
        lock = backend_dir / "uv.lock"
        stamp = venv_dir / ".venv.stamp"
        try:
            if lock.exists():
                stamp.write_text(lock.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def install(project_root: Path) -> Path:
        """
        行为策略（从最“保守跳过”到“强制同步”的优先级）：
        1) BACKEND_SKIP_INSTALL=1 且 .venv 就绪  -> 直接跳过
        2) .venv 存在 且 锁文件未变（.venv.stamp 与 uv.lock 相同） -> 跳过
        3) 否则运行 `uv sync`（可能会下载，取决于本地缓存/锁变化）
        """
        backend_dir = project_root / "backend"
        venv_dir = backend_dir / ".venv"
        os.chdir(backend_dir)

        # 1) 强力跳过开关
        if os.getenv("BACKEND_SKIP_INSTALL", "").strip().lower() in ("1", "true", "yes"):
            if BackendInstaller._venv_ready(venv_dir):
                ConsolePrinter.print(BackendInstaller.name, "Skip backend install (BACKEND_SKIP_INSTALL=1)")
                ConsolePrinter.print(BackendInstaller.name, "Virtual environment ready")
                return venv_dir
            else:
                ConsolePrinter.print(
                    BackendInstaller.name,
                    "BACKEND_SKIP_INSTALL=1 但 .venv 不存在或不完整 => 无法跳过，将继续检查锁文件机制/执行安装",
                )

        # 2) 锁文件未变 & venv 存在 -> 跳过
        if BackendInstaller._venv_ready(venv_dir) and BackendInstaller._locks_unchanged(backend_dir, venv_dir):
            ConsolePrinter.print(
                BackendInstaller.name, "Backend deps unchanged (uv.lock matches .venv.stamp) -> skip uv sync"
            )
            ConsolePrinter.print(BackendInstaller.name, "Virtual environment ready")
            return venv_dir

        # 3) 需要同步安装
        uv_cmd = BackendInstaller._resolve_uv_cmd()
        if uv_cmd is None:
            ConsolePrinter.print(BackendInstaller.name, "uv not found, installing with pip (user)...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--user", "uv"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "uv"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            uv_cmd = BackendInstaller._resolve_uv_cmd()

        if uv_cmd is None:
            ConsolePrinter.print(BackendInstaller.name, "Failed to locate 'uv' after installation.")
            sys.exit(1)

        ConsolePrinter.print(BackendInstaller.name, f"Using uv command: {' '.join(uv_cmd)}")
        ConsolePrinter.print(BackendInstaller.name, "Installing backend dependencies...")

        env = os.environ.copy()
        # 强制复制，避免硬链接警告；如需进一步加速，可把 UV_CACHE_DIR 指到与项目同盘
        env.setdefault("UV_LINK_MODE", "copy")

        try:
            subprocess.run(uv_cmd + ["sync"], check=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            ConsolePrinter.print(
                BackendInstaller.name, f"Failed to sync backend dependencies (uv). Return code={e.returncode}"
            )
            sys.exit(1)

        ConsolePrinter.print(BackendInstaller.name, "Backend dependencies installed successfully")

        # 校验 venv
        if not BackendInstaller._venv_ready(venv_dir):
            ConsolePrinter.print(BackendInstaller.name, "Virtual environment not created or python missing")
            sys.exit(1)

        # 写入哨兵：记录当前锁文件状态
        BackendInstaller._write_stamp(backend_dir, venv_dir)

        ConsolePrinter.print(BackendInstaller.name, "Virtual environment ready")
        return venv_dir


class FrontendInstaller:
    name = "FrontendInstaller"

    @staticmethod
    def _stream(cmd, env, cwd=None):
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line.rstrip("\n"), flush=True)
        proc.wait()
        return proc.returncode

    @staticmethod
    def _policy_from_env(cfg: ConfigManager) -> str:
        force_raw = (cfg.get("FRONTEND_FORCE_PROMPT", "") or "").strip().lower()
        if force_raw in ("1", "true", "yes", "y"):
            return "prompt"
        p = (cfg.get("FRONTEND_REINSTALL_POLICY", "prompt") or "").strip().lower()
        return p if p in ("prompt", "skip") else "prompt"

    @staticmethod
    def _persist_policy(cfg: ConfigManager, policy: str):
        cfg.set("FRONTEND_REINSTALL_POLICY", policy)

    @staticmethod
    def _timeout_for_dialog(cfg: ConfigManager, is_first_prompt: bool) -> int:
        if is_first_prompt:
            return 0
        raw = (cfg.get("FRONTEND_DIALOG_TIMEOUT", "") or "").strip()
        if raw.isdigit():
            return int(raw)
        return 0

    @staticmethod
    def install(project_root: Path, nodejs_path: str, cfg: ConfigManager):
        cfg.reload()
        ConsolePrinter.print(FrontendInstaller.name, f"Using .env at: {cfg.env_file}")

        frontend_dir = project_root / "frontend"
        os.chdir(frontend_dir)
        npm_path = Path(nodejs_path) / "npm.cmd"
        node_path = Path(nodejs_path) / "node.exe"

        if not npm_path.exists() or not node_path.exists():
            Dialogs.yes_no_cancel(
                "Node.js 路径无效", f"未在 {nodejs_path} 找到 node.exe/npm.cmd。\n请修改 .env 后重试。", timeout_sec=0
            )
            return

        ConsolePrinter.print(FrontendInstaller.name, f"node_path: {node_path}")
        ConsolePrinter.print(FrontendInstaller.name, f"npm_path: {npm_path}")

        env = os.environ.copy()
        env["PATH"] = str(nodejs_path) + os.pathsep + env.get("PATH", "")
        env.setdefault("FORCE_COLOR", "1")
        registry = os.getenv("NPM_REGISTRY", "").strip()

        try:
            subprocess.run(
                [str(npm_path), "--version"],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            ConsolePrinter.print(FrontendInstaller.name, "npm is functioning correctly")
        except subprocess.CalledProcessError:
            ConsolePrinter.print(
                FrontendInstaller.name, "npm is not functioning correctly. Please check Node.js installation."
            )
            return

        node_modules_dir = frontend_dir / "node_modules"

        has_policy = cfg.exists("FRONTEND_REINSTALL_POLICY")
        policy = FrontendInstaller._policy_from_env(cfg)
        is_first_prompt = not has_policy
        timeout_for_dialog = FrontendInstaller._timeout_for_dialog(cfg, is_first_prompt)

        ConsolePrinter.print(
            FrontendInstaller.name,
            f"policy={policy}, has_policy={has_policy}, force={cfg.get('FRONTEND_FORCE_PROMPT','')!r}, "
            f"timeout={timeout_for_dialog}, node_modules_exists={node_modules_dir.exists()}",
        )

        if node_modules_dir.exists():
            if policy == "skip":
                ConsolePrinter.print(FrontendInstaller.name, "node_modules exists; policy=skip ➜ 跳过安装")
                return

            ans = Dialogs.yes_no_cancel(
                "前端依赖已存在",
                "检测到 frontend/node_modules 已存在。\n\n"
                "是 = 覆盖重装（先删除 node_modules 后全量安装）\n"
                "否 = 跳过并记住以后默认跳过\n取消 = 退出脚本",
                timeout_sec=timeout_for_dialog,
                default="no",
                icon_emoji="📦",
                theme="flatly",
            )
            if ans == "cancel":
                ConsolePrinter.print(FrontendInstaller.name, "User canceled. Exiting.")
                sys.exit(1)
            if ans == "no":
                FrontendInstaller._persist_policy(cfg, "skip")
                ConsolePrinter.print(
                    FrontendInstaller.name, "选择了 否：本次跳过，并将以后默认跳过（FRONTEND_REINSTALL_POLICY=skip）"
                )
                return
            try:
                ConsolePrinter.print(FrontendInstaller.name, "Removing existing node_modules for a clean reinstall...")
                shutil.rmtree(node_modules_dir, ignore_errors=True)
            except Exception as e:
                ConsolePrinter.print(FrontendInstaller.name, f"Failed to remove node_modules: {e}")
            FrontendInstaller._persist_policy(cfg, "prompt")
        else:
            if policy == "skip":
                ConsolePrinter.print(
                    FrontendInstaller.name, "node_modules 不存在，但 policy=skip ➜ 跳过安装（可手动执行一次安装）"
                )
                return
            ans = Dialogs.yes_no_cancel(
                "安装前端依赖",
                "未检测到 frontend/node_modules，是否现在下载并安装依赖？\n"
                "是 = 立即安装\n否 = 跳过并记住以后默认跳过\n取消 = 退出脚本",
                timeout_sec=timeout_for_dialog,
                default="yes",
                icon_emoji="📦",
                theme="flatly",
            )
            if ans == "cancel":
                ConsolePrinter.print(FrontendInstaller.name, "User canceled. Exiting.")
                sys.exit(1)
            if ans == "no":
                FrontendInstaller._persist_policy(cfg, "skip")
                ConsolePrinter.print(
                    FrontendInstaller.name, "选择了 否：本次跳过，并将以后默认跳过（FRONTEND_REINSTALL_POLICY=skip）"
                )
                return
            FrontendInstaller._persist_policy(cfg, "prompt")

        cmd = [str(npm_path)]
        if registry:
            cmd += ["--registry", registry]
        cmd += ["exec", "--yes", "pnpm@9", "install", "--prefer-offline"]

        ConsolePrinter.print(FrontendInstaller.name, "Installing frontend dependencies with pnpm ...")
        rc = FrontendInstaller._stream(cmd, env=env, cwd=str(frontend_dir))
        if rc == 0:
            ConsolePrinter.print(FrontendInstaller.name, "Frontend dependencies installed successfully")
        else:
            ConsolePrinter.print(FrontendInstaller.name, f"Failed to install frontend dependencies, exit code {rc}")
            sys.exit(1)


# ========= 进程/端口与服务模块化 =========
class ProcessUtils:
    name = "ProcessUtils"

    @staticmethod
    def terminate_tree(pid: int):
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            ConsolePrinter.print(ProcessUtils.name, f"Process {pid} does not exist, no action taken")
            return

        try:
            for child in process.children(recursive=True):
                try:
                    child.terminate()
                    child.wait(timeout=3)
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    child.kill()
            process.terminate()
            process.wait(timeout=3)
            ConsolePrinter.print(ProcessUtils.name, f"Process {pid} terminated successfully")
        except Exception:
            try:
                process.kill()
                ConsolePrinter.print(ProcessUtils.name, f"Forced kill of process {pid}")
            except psutil.NoSuchProcess:
                pass


class PortGuard:
    name = "PortGuard"

    @staticmethod
    def _is_open_localhost(port: int, timeout: float = 0.5) -> bool:
        try:
            infos = socket.getaddrinfo("localhost", port, 0, socket.SOCK_STREAM)
        except OSError:
            return False

        for family, socktype, proto, canonname, sockaddr in infos:
            s = None
            try:
                s = socket.socket(family, socktype, proto)
                s.settimeout(timeout)
                s.connect(sockaddr)
                return True
            except OSError:
                continue
            finally:
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
        return False

    @staticmethod
    def kill(port: int) -> bool:
        killed_any = False
        try:
            for conn in psutil.net_connections(kind="inet"):
                if not conn.laddr:
                    continue
                if conn.laddr.port != port:
                    continue
                pid = conn.pid
                if not pid:
                    continue
                try:
                    p = psutil.Process(pid)
                    ConsolePrinter.print(PortGuard.name, f"Killing PID {pid} using port {port} ...")
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        ConsolePrinter.print(PortGuard.name, f"PID {pid} did not terminate, killing ...")
                        p.kill()
                    killed_any = True
                except psutil.NoSuchProcess:
                    pass
        except Exception as e:
            ConsolePrinter.print(PortGuard.name, f"kill_port error: {e}")
        return killed_any

    def ensure_free(self, port: int):
        if self._is_open_localhost(port):
            ConsolePrinter.print(self.name, f"Port {port} is in use. Trying to kill...")
            killed = self.kill(port)
            time.sleep(1.0)
            if self._is_open_localhost(port):
                ConsolePrinter.print(self.name, f"Port {port} still occupied (killed_any={killed}). Exit.")
                sys.exit(1)
            ConsolePrinter.print(self.name, f"Port {port} freed.")

    def wait_until_open(self, port: int, attempts: int = 30, sleep: float = 1.0) -> bool:
        for _ in range(attempts):
            if self._is_open_localhost(port):
                return True
            time.sleep(sleep)
        return False


class RedisService:
    name = "RedisService"

    def __init__(self, port_guard: PortGuard, port: int = 6379):
        self.port_guard = port_guard
        self.port = port
        self.proc: Optional[subprocess.Popen] = None

    def start(self, redis_path: str) -> bool:
        redis_server = Path(redis_path) / "redis-server.exe"
        if not redis_server.exists():
            Dialogs.yes_no_cancel("Redis 路径无效", f"未找到：{redis_server}", timeout_sec=0)
            return False

        if self.port_guard._is_open_localhost(self.port):
            ConsolePrinter.print(
                self.name, f"Redis seems already running on port {self.port}; skip launching a new one."
            )
            return True

        ConsolePrinter.print(self.name, f"Starting Redis server: {redis_server}")
        try:
            self.proc = subprocess.Popen(
                [str(redis_server)],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=str(redis_path),
            )
            time.sleep(2)
            ConsolePrinter.print(self.name, "Redis started successfully")
            return True
        except Exception as e:
            ConsolePrinter.print(self.name, f"Failed to start Redis: {e}")
            self.stop()
            return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            ProcessUtils.terminate_tree(self.proc.pid)
        self.proc = None


class BackendService:
    name = "BackendService"

    def __init__(self, port_guard: PortGuard, port: int = 8000, host: str = "localhost"):
        self.port_guard = port_guard
        self.port = port
        self.host = host
        self.proc: Optional[subprocess.Popen] = None

    def start(self, project_root: Path):
        backend_dir = project_root / "backend"
        venv_python = (
            backend_dir
            / ".venv"
            / ("Scripts" if os.name == "nt" else "bin")
            / ("python.exe" if os.name == "nt" else "python")
        )
        if not venv_python.exists():
            ConsolePrinter.print(
                self.name, f"Virtual environment Python not found at {venv_python}, using system Python"
            )
            venv_python = sys.executable

        # 确保端口空闲
        self.port_guard.ensure_free(self.port)

        # 读取后端环境
        env_path_local = backend_dir / ".env.dev"
        load_dotenv(dotenv_path=env_path_local, override=True)
        ConsolePrinter.print(self.name, f"REDIS_URL set to {os.getenv('REDIS_URL')}")

        env = os.environ.copy()
        env["ENV"] = "DEV"

        ConsolePrinter.print(self.name, f"Starting backend server on {self.host}:{self.port} ...")
        self.proc = subprocess.Popen(
            [
                str(venv_python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                self.host,
                "--port",
                str(self.port),
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

        if not self.port_guard.wait_until_open(self.port):
            ConsolePrinter.print(self.name, f"Backend failed to start on port {self.port}")
            sys.exit(1)
        ConsolePrinter.print(self.name, f"Backend successfully started on port {self.port}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            ProcessUtils.terminate_tree(self.proc.pid)
        self.proc = None


class FrontendService:
    name = "FrontendService"

    def __init__(self, port_guard: PortGuard, nodejs_path: str, port: int = 5173, host: str = "localhost"):
        self.port_guard = port_guard
        self.nodejs_path = nodejs_path
        self.port = port
        self.host = host
        self.proc: Optional[subprocess.Popen] = None

    def start(self, project_root: Path):
        frontend_dir = project_root / "frontend"
        os.chdir(frontend_dir)
        npm_path = Path(self.nodejs_path) / "npm.cmd"
        node_exe = Path(self.nodejs_path) / "node.exe"

        if not node_exe.exists() or not npm_path.exists():
            ConsolePrinter.print(self.name, f"Node.js path invalid: {self.nodejs_path}")
            sys.exit(1)

        # 确保端口空闲
        self.port_guard.ensure_free(self.port)

        env = os.environ.copy()
        env["PATH"] = str(self.nodejs_path) + os.pathsep + env.get("PATH", "")
        env["NODE"] = str(node_exe)
        env.setdefault("FORCE_COLOR", "1")

        ConsolePrinter.print(self.name, f"Starting frontend server on {self.host}:{self.port} ...")
        self.proc = subprocess.Popen(
            [
                str(npm_path),
                "exec",
                "--yes",
                "pnpm@9",
                "run",
                "dev",
                "--",
                "--port",
                str(self.port),
                "--host",
                self.host,
            ],
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            env=env,
        )

        if not self.port_guard.wait_until_open(self.port):
            ConsolePrinter.print(self.name, f"Frontend failed to start on port {self.port}")
            sys.exit(1)
        ConsolePrinter.print(self.name, f"Frontend successfully started on port {self.port}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            ProcessUtils.terminate_tree(self.proc.pid)
        self.proc = None


class ServiceSupervisor:
    name = "Supervisor"

    def __init__(self, backend: BackendService, frontend: FrontendService, redis: RedisService):
        self.backend = backend
        self.frontend = frontend
        self.redis = redis

    def shutdown_all(self):
        ConsolePrinter.print(self.name, "Shutting down services ...")
        for svc_name, proc in (
            ("backend", self.backend.proc),
            ("frontend", self.frontend.proc),
            ("redis", self.redis.proc),
        ):
            if proc and proc.poll() is None:
                ProcessUtils.terminate_tree(proc.pid)
        ConsolePrinter.print(self.name, "All services stopped.")


# ========= 启动器 =========
class MathModelAgentLauncher:
    name = "Launcher"

    def __init__(self):
        self.project_root = Path.cwd()
        self.cfg = ConfigManager(self.project_root / ".env")
        self.port_guard = PortGuard()  # 新：端口管理

    def run(self):
        CacheCleaner.clear(self.project_root)

        # 选路径
        redis_path = PathPicker.pick_and_validate(
            self.cfg,
            "REDIS_PATH",
            "选择 Redis 安装目录（需包含 redis-server.exe、redis-cli.exe）",
            ["redis-server.exe", "redis-cli.exe"],
        )
        nodejs_path = PathPicker.pick_and_validate(
            self.cfg, "NODEJS_PATH", "选择 Node.js 安装目录（需包含 node.exe、npm.cmd）", ["node.exe", "npm.cmd"]
        )

        # .env 准备 + 后端依赖安装 + 前端依赖安装
        EnvFileManager.copy_envs(self.project_root)
        _ = BackendInstaller.install(self.project_root)

        backend_port = int(os.getenv("BACKEND_PORT", "8000"))
        frontend_port = int(os.getenv("FRONTEND_PORT", "5173"))

        FrontendInstaller.install(self.project_root, nodejs_path, self.cfg)

        # 服务实例
        redis = RedisService(self.port_guard, port=6379)
        backend = BackendService(self.port_guard, port=backend_port, host="localhost")
        frontend = FrontendService(self.port_guard, nodejs_path=nodejs_path, port=frontend_port, host="localhost")
        supervisor = ServiceSupervisor(backend, frontend, redis)

        # 启动
        if not redis.start(redis_path):
            sys.exit(1)

        backend.start(self.project_root)
        frontend.start(self.project_root)

        ConsolePrinter.print(self.name, f"Backend running at http://localhost:{backend_port}")
        ConsolePrinter.print(self.name, f"Frontend running at http://localhost:{frontend_port}")

        try:
            while True:
                time.sleep(1)
                if frontend.proc and frontend.proc.poll() is not None:
                    raise RuntimeError("Frontend crashed")
                if backend.proc and backend.proc.poll() is not None:
                    raise RuntimeError("Backend crashed")
        except RuntimeError as e:
            ConsolePrinter.print(self.name, f"Shutting down due to {e}")
        finally:
            supervisor.shutdown_all()
            CacheCleaner.clear(self.project_root)


if __name__ == "__main__":
    MathModelAgentLauncher().run()
