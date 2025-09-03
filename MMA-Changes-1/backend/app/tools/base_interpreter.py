# app/tools/base_interpreter.py

# 1. 标准库与依赖导入
import abc
import re
import json
from app.tools.notebook_serializer import NotebookSerializer
from app.services.redis_manager import redis_manager
from app.utils.log_util import logger
from app.schemas.response import (
    OutputItem,
    InterpreterMessage,
)


# 2. BaseCodeInterpreter
# 2.1 目的：提供解释器基类，管理任务目录、笔记本序列化与输出分段存储
class BaseCodeInterpreter(abc.ABC):
    def __init__(
        self,
        task_id: str,
        work_dir: str,
        notebook_serializer: NotebookSerializer,
    ):
        # 2.1.1 基本属性：任务ID、工作目录、Notebook 序列化器
        self.task_id = task_id
        self.work_dir = work_dir
        self.notebook_serializer = notebook_serializer
        # 2.1.2 输出结构：按 section 存储 content 与 images
        self.section_output: dict[str, dict[str, list[str]]] = {}
        self.last_created_images = set()

    # 3 抽象方法：子类需实现
    @abc.abstractmethod
    async def initialize(self):
        # 3.1 初始化解释器，必要时上传文件、启动内核等
        ...

    @abc.abstractmethod
    async def _pre_execute_code(self):
        # 3.2 执行初始化代码（如在内核启动后需要运行的准备脚本）
        ...

    @abc.abstractmethod
    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        # 3.3 执行一段代码，返回 (输出文本, 是否出错, 错误信息)
        ...

    @abc.abstractmethod
    async def cleanup(self):
        # 3.4 清理资源，比如关闭沙箱或内核
        ...

    @abc.abstractmethod
    async def get_created_images(self, section: str) -> list[str]:
        # 3.5 获取当前 section 创建的图片列表
        ...

    # 4 公共方法：与消息推送、section 管理相关
    async def _push_to_websocket(self, content_to_display: list[OutputItem] | None):
        # 4.1 日志：已推送到 WebSocket（模拟/占位）
        logger.info("执行结果已推送到WebSocket")

        agent_msg = InterpreterMessage(
            output=content_to_display,
        )
        await redis_manager.publish_message(
            self.task_id,
            agent_msg,
        )

    def add_section(self, section_name: str) -> None:
        # 4.2 确保 section 结构存在（含 content & images）
        if section_name not in self.section_output:
            self.section_output[section_name] = {"content": [], "images": []}

    def add_content(self, section: str, text: str) -> None:
        # 4.3 向指定 section 添加文本内容（自动创建 section）
        self.add_section(section)
        self.section_output[section]["content"].append(text)

    def get_code_output(self, section: str) -> str:
        # 4.4 获取指定 section 的代码输出（拼接为单个字符串）
        return "\n".join(self.section_output[section]["content"])

    def delete_color_control_char(self, string):
        # 4.5 删除 ANSI/颜色控制字符（用于清洗终端输出）
        ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")
        return ansi_escape.sub("", string)

    def _truncate_text(self, text: str, max_length: int = 1000) -> str:
        # 4.6 截断文本，保留开头和结尾的重要信息，避免传输过长内容
        if len(text) <= max_length:
            return text

        half_length = max_length // 2
        return text[:half_length] + "\n... (内容已截断) ...\n" + text[-half_length:]
