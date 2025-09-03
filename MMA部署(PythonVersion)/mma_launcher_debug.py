# mma_launcher.py

"""
MathModelAgent 启动器（完整版，含统一日志落盘 + ANSI 颜色处理）
------------------------------------------------
目标：
1) 启动并托管 Redis / Backend(Uvicorn) / Frontend(Vite)；
2) 将所有关键控制台输出合流写入 backend/logs/launcher/：
   - NNN.log        : 纯文本（去色）
   - NNN.ansi.log   : 保留 ANSI 颜色码（可选，默认开启）
   其中 NNN 与 backend/project/work_dir 的 task_id（数字目录）保持一致：按当前最大目录号 + 1 生成；
   若 work_dir 不存在或为空，退回扫描 backend/logs/messages 的编号；仍无则退回扫描 launcher/*.log。
3) 仍保持实时回显到当前终端（带颜色）；
4) 前端依赖安装/运行时的冗余噪声适度过滤。
备注：日志文件中会去掉空行（包含只含空白/ANSI 的行）。
"""

import re
import subprocess
import sys
from pathlib import Path
import os
import shutil
import time
import socket
from typing import Optional, List
import threading


# ========= 基础工具 =========
class TimeUtils:
    @staticmethod
    def ts() -> str:
        return time.strftime("%H:%M:%S")


# === ANSI 处理 ===
_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")  # 覆盖常见 CSI 序列


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def decode_best_effort(b: bytes) -> str:
    """按 UTF-8 -> GBK -> Latin-1 兜底解码一行字节，尽量避免乱码。"""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return b.decode("gbk")
        except UnicodeDecodeError:
            return b.decode("latin1", errors="replace")


class _GlobalFileLogger:
    """
    线程安全的文件日志器：单文件编号，多通道输出。
    backend/logs/launcher/ 下生成：
      - 001.log        : 去色纯文本
      - 001.ansi.log   : 保留 ANSI 颜色码（LOG_WRITE_ANSI!=0 时生成）
    编号策略（与 task_id 一致）：
      1) 首选 backend/project/work_dir 下的数字目录，取最大 + 1；
      2) 次选 backend/logs/messages 下的 NNN.json，取最大 + 1；
      3) 退回 backend/logs/launcher 下 *.log 的编号自增。
    """

    def __init__(self, project_root: Path):
        self.log_dir = project_root / "backend" / "logs" / "launcher"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.write_ansi = os.getenv("LOG_WRITE_ANSI", "1").strip().lower() not in ("0", "false", "no")
        base = self._next_base(project_root)
        self.path_plain = self.log_dir / f"{base}.log"
        self.path_ansi = self.log_dir / f"{base}.ansi.log"
        self.fp_plain = open(self.path_plain, "a", encoding="utf-8", buffering=1)
        self.fp_ansi = open(self.path_ansi, "a", encoding="utf-8", buffering=1) if self.write_ansi else None
        self.write_line(f"===== Log started at {time.strftime('%Y-%m-%d %H:%M:%S')} =====")

    @staticmethod
    def _max_numeric_name(children: List[Path]) -> int:
        max_n = 0
        for p in children:
            m = re.fullmatch(r"(\d+)", p.name)
            if m:
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except Exception:
                    pass
        return max_n

    @staticmethod
    def _max_numeric_file(children: List[Path], pattern: str) -> int:
        max_n = 0
        rx = re.compile(pattern)
        for p in children:
            m = rx.search(p.name)
            if m:
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except Exception:
                    pass
        return max_n

    def _next_base(self, project_root: Path) -> str:
        # 1) work_dir 目录扫描
        try:
            work_dir = project_root / "backend" / "project" / "work_dir"
            if work_dir.exists() and work_dir.is_dir():
                max_n = self._max_numeric_name([p for p in work_dir.iterdir() if p.is_dir()])
                if max_n >= 0:
                    return f"{max_n + 1:03d}"
        except Exception:
            pass

        # 2) messages 目录扫描
        try:
            msg_dir = project_root / "backend" / "logs" / "messages"
            if msg_dir.exists() and msg_dir.is_dir():
                max_n = self._max_numeric_file(list(msg_dir.iterdir()), r"(\d+)\.json$")
                if max_n >= 0:
                    return f"{max_n + 1:03d}"
        except Exception:
            pass

        # 3) launcher 目录自身扫描
        try:
            idx = 1
            for p in self.log_dir.glob("*.log"):
                m = re.match(r"(\d{3})\.log$", p.name)
                if m:
                    try:
                        idx = max(idx, int(m.group(1)) + 1)
                    except Exception:
                        pass
            return f"{idx:03d}"
        except Exception:
            return "001"

    @property
    def path(self) -> Path:
        # 兼容旧接口：返回纯文本日志路径
        return self.path_plain

    def write_line(self, line: str):
        """
        写纯文本（去色）与 ANSI 原文；过滤空行。
        约定：传入的 line 不应包含换行符；但为兼容性，这里仍会去掉所有尾随 \r\n。
        """
        # 统一去掉尾随换行和回车
        line_no_nl = line.rstrip("\r\n")

        # 过滤“只含空白/ANSI”的行
        if strip_ansi(line_no_nl).strip() == "":
            return

        # 只在这里追加一次换行
        final_line = line_no_nl + "\n"
        with self.lock:
            try:
                self.fp_plain.write(strip_ansi(final_line))
            except Exception:
                pass
            if self.fp_ansi:
                try:
                    self.fp_ansi.write(final_line)
                except Exception:
                    pass

    def close(self):
        with self.lock:
            try:
                tail = f"===== Log closed at {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
                self.fp_plain.write(strip_ansi(tail))
                self.fp_plain.flush()
                self.fp_plain.close()
            except Exception:
                pass
            if self.fp_ansi:
                try:
                    self.fp_ansi.write(tail)
                    self.fp_ansi.flush()
                    self.fp_ansi.close()
                except Exception:
                    pass


_GLOBAL_LOGGER: Optional[_GlobalFileLogger] = None


class ConsolePrinter:
    """
    统一的控制台打印：终端彩色（原样），日志文件双路（去色 + 可选保留 ANSI）。
    去空行策略：若消息在去 ANSI 后仅为空白，则不输出、不落盘。
    """

    @staticmethod
    def print(prefix: str, msg: str):
        raw = f"{TimeUtils.ts()} [{prefix}]: {msg}"
        if strip_ansi(raw).strip() == "":
            return
        print(raw, flush=True)
        if _GLOBAL_LOGGER:
            _GLOBAL_LOGGER.write_line(raw)

    @staticmethod
    def raw_from_proc(prefix: str, raw_line: str):
        """
        子进程原样行输出 -> 终端（彩色） + 文件（去色 + 可选 ANSI），统一加前缀。
        保证仅添加一次换行：这里不再附加换行，交由 _GlobalFileLogger.write_line 统一追加。
        """
        # 规范：移除所有尾随 \r\n；避免“空白+ANSI”造成的空行
        s = raw_line.rstrip("\r\n")
        if strip_ansi(s).strip() == "":
            return

        out = f"{TimeUtils.ts()} [{prefix}] {s}"

        # 控制台打印一行（print 自带换行）
        print(out, flush=True)

        # 文件输出（不含多余换行，由 write_line 统一追加）
        if _GLOBAL_LOGGER:
            _GLOBAL_LOGGER.write_line(out)


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
    def ensure_library_installed(pip_name: str, import_name: Optional[str] = None, index_url: Optional[str] = None):
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
import psutil  # noqa: E402


# ========= Windows 原生弹窗 =========
class Dialogs:
    """
    1) yes_no_cancel: MessageBox(Yes/No/Cancel)，支持默认按钮与可选超时
    2) ask_directory: Shell 文件夹选择对话框
    """

    import os as _pyos

    _is_win = _pyos.name == "nt"

    @staticmethod
    def _owner_hwnd():
        if not Dialogs._is_win:
            return 0
        import ctypes  # noqa: WPS433

        u32 = ctypes.windll.user32  # noqa: WPS441
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
        icon_emoji: Optional[str] = None,
        theme: Optional[str] = None,
    ) -> str:
        if not Dialogs._is_win:
            try:
                ans = input(f"{title}\n{message}\n[y]是 / [n]否 / [c]取消 > ").strip().lower()
                return {"y": "yes", "n": "no", "c": "cancel"}.get(ans, default)
            except Exception:
                return default

        import ctypes  # noqa: WPS433
        from ctypes import wintypes  # noqa: WPS433

        u32 = ctypes.windll.user32  # noqa: WPS441
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
                u32.MessageBoxTimeoutW.restype = ctypes.c_int
                u32.MessageBoxTimeoutW.argtypes = [
                    wintypes.HWND,
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
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
        if not Dialogs._is_win:
            try:
                return input(f"{title}\n请输入目录路径（留空取消）： ").strip()
            except Exception:
                return ""

        import ctypes  # noqa: WPS433
        from ctypes import wintypes  # noqa: WPS433

        shell32 = ctypes.windll.shell32  # noqa: WPS441
        ole32 = ctypes.windll.ole32  # noqa: WPS441

        try:
            ole32.CoInitialize(None)
        except Exception:
            pass

        BIF_RETURNONLYFSDIRS = 0x00000001
        BIF_NEWDIALOGSTYLE = 0x00000040
        BIF_VALIDATE = 0x00000020

        class BROWSEINFO(ctypes.Structure):  # noqa: WPS430
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
from dotenv import load_dotenv, set_key, dotenv_values  # noqa: E402


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
    def _check_path_valid(path: str, required_files: List[str]) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        return all((p / f).exists() for f in required_files)

    @staticmethod
    def pick_and_validate(cfg: ConfigManager, env_var: str, title: str, required_files: List[str]) -> str:
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
    def _resolve_uv_cmd() -> Optional[List[str]]:
        import shutil as _shutil
        import sys as _sys
        import site as _site

        which_uv = _shutil.which("uv") or _shutil.which("uv.exe")
        if which_uv:
            return [which_uv]
        candidates = [
            Path(_sys.executable).parent / "Scripts" / "uv.exe",
            Path(_sys.executable).parent / "uv.exe",
            Path(_site.getuserbase()) / "Scripts" / "uv.exe",
        ]
        for p in candidates:
            if p.exists():
                return [str(p)]
        try:
            __import__("uv")
            return [_sys.executable, "-m", "uv"]
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
        backend_dir = project_root / "backend"
        venv_dir = backend_dir / ".venv"
        os.chdir(backend_dir)

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

        if BackendInstaller._venv_ready(venv_dir) and BackendInstaller._locks_unchanged(backend_dir, venv_dir):
            ConsolePrinter.print(
                BackendInstaller.name, "Backend deps unchanged (uv.lock matches .venv.stamp) -> skip uv sync"
            )
            ConsolePrinter.print(BackendInstaller.name, "Virtual environment ready")
            return venv_dir

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
        env.setdefault("UV_LINK_MODE", "copy")

        try:
            subprocess.run(uv_cmd + ["sync"], check=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            ConsolePrinter.print(
                BackendInstaller.name, f"Failed to sync backend dependencies (uv). Return code={e.returncode}"
            )
            sys.exit(1)

        ConsolePrinter.print(BackendInstaller.name, "Backend dependencies installed successfully")

        if not BackendInstaller._venv_ready(venv_dir):
            ConsolePrinter.print(BackendInstaller.name, "Virtual environment not created or python missing")
            sys.exit(1)

        BackendInstaller._write_stamp(backend_dir, venv_dir)
        ConsolePrinter.print(BackendInstaller.name, "Virtual environment ready")
        return venv_dir


class FrontendInstaller:
    name = "FrontendInstaller"

    @staticmethod
    def _stream(cmd: List[str], env: dict, cwd: Optional[str] = None) -> int:
        NOISE_PATTERNS = []
        _noise = [re.compile(p) for p in NOISE_PATTERNS]

        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,  # 二进制模式，后面手动解码，防乱码
            bufsize=0,
        )
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = decode_best_effort(raw)
            plain = strip_ansi(line).strip()
            if not plain:
                continue
            if any(rx.search(plain) for rx in _noise):
                continue
            ConsolePrinter.raw_from_proc("pnpm", line)
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


# === 通用子进程输出抓取器 ===
class ProcStreamer:
    def __init__(self, name: str, proc: subprocess.Popen, suppress: Optional[List[re.Pattern]] = None):
        self.name = name
        self.proc = proc
        self.suppress = suppress or []
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            assert self.proc.stdout is not None
            # 逐行读取，但先规范掉多余的 \r\n；若一行内仍含多行，继续拆分
            for raw in iter(self.proc.stdout.readline, b""):
                line = decode_best_effort(raw).rstrip("\r\n")

                # 过滤空白/ANSI-only 行
                if strip_ansi(line).strip() == "":
                    continue

                # 某些进程一次性输出多行（带内嵌 \n），拆分后逐条处理
                for part in line.splitlines():
                    if strip_ansi(part).strip() == "":
                        continue
                    ConsolePrinter.raw_from_proc(self.name, part)
        except Exception as e:
            ConsolePrinter.print(self.name, f"[pump] error: {e}")


class RedisService:
    name = "RedisService"

    def __init__(self, port_guard: PortGuard, port: int = 6379):
        self.port_guard = port_guard
        self.port = port
        self.proc: Optional[subprocess.Popen] = None
        self.stream: Optional[ProcStreamer] = None

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
                cwd=str(redis_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,  # 二进制模式
                bufsize=0,
            )
            self.stream = ProcStreamer("Redis", self.proc)
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
        self.stream = None


class BackendService:
    name = "BackendService"

    def __init__(self, port_guard: PortGuard, port: int = 8000, host: str = "localhost"):
        self.port_guard = port_guard
        self.port = port
        self.host = host
        self.proc: Optional[subprocess.Popen] = None
        self.stream: Optional[ProcStreamer] = None

    def start(self, project_root: Path):
        backend_dir = project_root / "backend"

        override_py = (os.getenv("BACKEND_PYTHON_EXE") or "").strip()
        venv_python: Optional[Path] = None
        if override_py:
            override_py = os.path.expanduser(override_py)
            venv_python = Path(override_py)
            if venv_python.exists():
                ConsolePrinter.print(self.name, f"Using BACKEND_PYTHON_EXE: {venv_python}")
            else:
                ConsolePrinter.print(
                    self.name,
                    f"BACKEND_PYTHON_EXE is set but not found: {venv_python}. Will try .venv or system Python.",
                )
                venv_python = None

        if venv_python is None:
            venv_python = (
                backend_dir
                / ".venv"
                / ("Scripts" if os.name == "nt" else "bin")
                / ("python.exe" if os.name == "nt" else "python")
            )
            if not venv_python.exists():
                ConsolePrinter.print(
                    self.name,
                    f"Virtual environment Python not found at {venv_python}, using system Python",
                )
                venv_python = Path(sys.executable)

        self.port_guard.ensure_free(self.port)

        env_path_local = backend_dir / ".env.dev"
        load_dotenv(dotenv_path=env_path_local, override=True)
        ConsolePrinter.print(self.name, f"REDIS_URL set to {os.getenv('REDIS_URL')}")

        env = os.environ.copy()
        env["ENV"] = "DEV"
        # 关键：强制 Python 子进程用 UTF-8 写日志/标准流，避免 GBK
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

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
                "--log-level",
                "info",
            ],
            cwd=str(backend_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,  # 二进制模式
            bufsize=0,
        )
        self.stream = ProcStreamer("Uvicorn", self.proc)

        if not self.port_guard.wait_until_open(self.port):
            ConsolePrinter.print(self.name, f"Backend failed to start on port {self.port}")
            sys.exit(1)
        ConsolePrinter.print(self.name, f"Backend successfully started on port {self.port}")


class FrontendService:
    name = "FrontendService"

    def __init__(self, port_guard: PortGuard, nodejs_path: str, port: int = 5173, host: str = "localhost"):
        self.port_guard = port_guard
        self.nodejs_path = nodejs_path
        self.port = port
        self.host = host
        self.proc: Optional[subprocess.Popen] = None
        self.stream: Optional[ProcStreamer] = None

    @staticmethod
    def _noise_patterns() -> List[re.Pattern]:
        pats = []
        return [re.compile(p) for p in pats]

    def start(self, project_root: Path):
        frontend_dir = project_root / "frontend"
        os.chdir(frontend_dir)
        npm_path = Path(self.nodejs_path) / "npm.cmd"
        node_exe = Path(self.nodejs_path) / "node.exe"

        if not node_exe.exists() or not npm_path.exists():
            ConsolePrinter.print(self.name, f"Node.js path invalid: {self.nodejs_path}")
            sys.exit(1)

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
                "--logLevel",
                "info",
            ],
            shell=False,
            env=env,
            cwd=str(frontend_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,  # 二进制模式
            bufsize=0,
        )

        suppress = self._noise_patterns()
        self.stream = ProcStreamer("Vite", self.proc, suppress=suppress)

        if not self.port_guard.wait_until_open(self.port):
            ConsolePrinter.print(self.name, f"Frontend failed to start on port {self.port}")
            sys.exit(1)
        ConsolePrinter.print(self.name, f"Frontend successfully started on port {self.port}")

    def stop(self):
        if self.proc and self.proc.poll() is not None:
            return
        if self.proc:
            ProcessUtils.terminate_tree(self.proc.pid)
        self.proc = None
        self.stream = None


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


# ========= 源码快照到 launcher/（与日志编号一致） =========
class PySourceDumper:
    IGNORE_FOLDERS = {".git", "__pycache__", ".venv", "env", "venv"}
    IGNORE_FILES = {"py_contents.py", "__init__.py"}

    @staticmethod
    def _generate_directory_structure(startpath: Path, indent: str = "") -> str:
        """
        生成目录结构（仅 .py 文件），按文件夹→文件排序
        """
        structure = ""
        try:
            items = sorted(list(startpath.iterdir()), key=lambda p: (not p.is_dir(), p.name.lower()))
        except FileNotFoundError:
            return structure

        if not items:
            return f"{indent}|-- (空目录)\n"

        for item in items:
            if item.is_dir():
                if item.name in PySourceDumper.IGNORE_FOLDERS:
                    continue
                structure += f"{indent}|-- 文件夹: {item.name}\n"
                structure += PySourceDumper._generate_directory_structure(item, indent + "|   ")
            else:
                if item.suffix == ".py" and item.name not in PySourceDumper.IGNORE_FILES:
                    structure += f"{indent}|-- 文件: {item.name}\n"
        return structure

    @staticmethod
    def _clean_content(content: str) -> str:
        # 原样返回：无需清洗，保持源码完整
        return content

    @staticmethod
    def _iter_py_files(root: Path):
        for cur_root, dirs, files in os.walk(root):
            # 过滤目录
            dirs[:] = [d for d in dirs if d not in PySourceDumper.IGNORE_FOLDERS]
            # 只要 .py
            py_files = [f for f in files if f.endswith(".py") and f not in PySourceDumper.IGNORE_FILES]
            py_files.sort(key=lambda x: x.lower())
            for fname in py_files:
                yield Path(cur_root) / fname

    @staticmethod
    def write_backend_app_snapshot(project_root: Path, out_txt: Path):
        """
        将 backend/app 下所有 .py 源码快照写入 out_txt。
        文件名应形如：源码快照_NNN.txt（NNN 与本次 launcher 日志编号一致）
        """
        scan_dir = project_root / "backend" / "app"
        out_txt.parent.mkdir(parents=True, exist_ok=True)

        with open(out_txt, "w", encoding="utf-8") as fp:
            # 目录结构
            fp.write("目录结构 (仅 .py 文件):\n")
            fp.write(PySourceDumper._generate_directory_structure(scan_dir))
            fp.write("\n\n")

            # 源码快照正文
            sep = "=" * 80
            for fpath in PySourceDumper._iter_py_files(scan_dir):
                try:
                    try:
                        content = fpath.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, IsADirectoryError):
                        content = fpath.read_text(encoding="latin1")
                except Exception:
                    continue

                cleaned = PySourceDumper._clean_content(content)
                fp.write(f"{sep}\n")
                fp.write(f"{fpath} 的内容:\n")
                fp.write(f"{sep}\n")
                fp.write(cleaned)
                fp.write("\n\n")


# ========= 启动器 =========
class MathModelAgentLauncher:
    name = "Launcher"

    def __init__(self):
        self.project_root = Path.cwd()
        self.cfg = ConfigManager(self.project_root / ".env")
        self.port_guard = PortGuard()

        # === 初始化全局日志器 ===
        global _GLOBAL_LOGGER
        _GLOBAL_LOGGER = _GlobalFileLogger(self.project_root)
        ConsolePrinter.print(self.name, f"Log file: {_GLOBAL_LOGGER.path}")

        # 与日志同编号输出源码快照到 backend/logs/launcher/
        try:
            base = _GLOBAL_LOGGER.path.stem  # 例如 "002"
            snapshot_path = _GLOBAL_LOGGER.path.parent / f"源码快照_{base}.txt"
            PySourceDumper.write_backend_app_snapshot(self.project_root, snapshot_path)
            ConsolePrinter.print(self.name, f"Wrote backend/app sources to {snapshot_path}")
        except Exception as e:
            ConsolePrinter.print(self.name, f"Source snapshot failed: {e}")

    def run(self):
        supervisor: Optional[ServiceSupervisor] = None
        try:
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

            # .env + 依赖
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

            # 守护循环
            try:
                back_fail, front_fail = 0, 0
                while True:
                    time.sleep(1)

                    back_ok = self.port_guard._is_open_localhost(backend_port)
                    front_ok = self.port_guard._is_open_localhost(frontend_port)

                    back_fail = 0 if back_ok else back_fail + 1
                    front_fail = 0 if front_ok else front_fail + 1

                    if backend.proc and backend.proc.poll() is not None and back_fail >= 3:
                        raise RuntimeError("Backend crashed")
                    if frontend.proc and frontend.proc.poll() is not None and front_fail >= 3:
                        raise RuntimeError("Frontend crashed")
            except KeyboardInterrupt:
                ConsolePrinter.print(self.name, "KeyboardInterrupt -> 正在优雅退出...")
            except RuntimeError as e:
                ConsolePrinter.print(self.name, f"Shutting down due to {e}")

        except KeyboardInterrupt:
            ConsolePrinter.print(self.name, "KeyboardInterrupt during startup -> 正在优雅退出...")
        finally:
            if supervisor is not None:
                supervisor.shutdown_all()
            CacheCleaner.clear(self.project_root)
            if _GLOBAL_LOGGER:
                _GLOBAL_LOGGER.close()


if __name__ == "__main__":
    try:
        MathModelAgentLauncher().run()
    except KeyboardInterrupt:
        pass
