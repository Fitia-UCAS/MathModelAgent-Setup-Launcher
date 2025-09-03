# app/utils/log_util.py

from pathlib import Path
from loguru import logger as _logger
import sys
import re


class LoggerInitializer:
    def __init__(self):
        # backend 根目录：.../backend
        backend_dir = Path(__file__).resolve().parents[2]
        self.logs_root = backend_dir / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True)

        # messages 目录（保留，供其它模块使用时保证存在）
        (self.logs_root / "messages").mkdir(parents=True, exist_ok=True)

        # 不再创建 errors 目录（按用户要求）
        self.errors_dir = None  # 保留属性以防外部引用，但不创建目录

    def _next_seq(self, folder: Path, pattern: str, pad: int = 3) -> str:
        """
        计算下一个序号（001、002、...）。pattern 例如：r"error(\\d+)\\.txt$"
        该函数现在保留但不会被本模块用于创建单条错误文件。
        """
        max_n = 0
        try:
            for p in folder.iterdir():
                m = re.search(pattern, p.name)
                if m:
                    try:
                        n = int(m.group(1))
                        if n > max_n:
                            max_n = n
                    except ValueError:
                        pass
        except Exception:
            # 如果 folder 为 None 或不存在，则直接返回 001
            return f"{1:0{pad}d}"
        return f"{max_n + 1:0{pad}d}"

    # 错误专用 sink（保留函数签名但不写文件）
    def __error_sink(self, message):
        # 变更说明：原实现会把每条 ERROR 写入 logs/errors/errorNNN.txt。
        # 按要求，我们取消该行为（no-op）。
        return

    def init_log(self):
        logger = _logger
        logger.remove()

        # 控制台
        logger.add(sys.stdout, level="INFO", enqueue=False, backtrace=False, diagnose=False)

        # 主日志文件：logs/app.log（按 50MB 轮转 + zip 压缩）
        app_log = self.logs_root / "app.log"
        logger.add(
            app_log,
            rotation="50 MB",
            encoding="utf-8",
            enqueue=False,
            backtrace=False,
            diagnose=False,
            compression="zip",
        )

        # 不再注册单独的 ERROR sink（不创建 errors/ 也不写 errorNNN.txt）。
        # ERROR 级别仍会写入上面的 app.log。

        return logger


# 全局 logger
log_initializer = LoggerInitializer()
logger = log_initializer.init_log()
