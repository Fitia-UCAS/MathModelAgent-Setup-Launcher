# app/utils/RichPrinter.py

# 1 导入依赖
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typing import Optional, List, Any, Dict
from rich import print as rprint
from app.utils.log_util import logger


# 2 富文本打印工具
class RichPrinter:
    # 2.1 样式配置
    _styles = {
        "success": {"emoji": "✅", "color": "green", "prefix": "成功"},
        "error": {"emoji": "❌", "color": "red", "prefix": "错误"},
        "warning": {"emoji": "⚠️", "color": "yellow", "prefix": "警告"},
        "info": {"emoji": "ℹ️", "color": "blue", "prefix": "信息"},
        "debug": {"emoji": "🐞", "color": "magenta", "prefix": "调试"},
    }

    # 2.2 Console 实例
    _console = Console()

    # 2.3 格式化消息
    @classmethod
    def _format_message(
        cls,
        message: str,
        style_type: str,
        color: Optional[str] = None,
        emoji: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> Text:
        """格式化消息为统一样式"""
        style = cls._styles.get(style_type, {})
        emoji = emoji or style.get("emoji", "")
        color = color or style.get("color", "white")
        prefix = prefix or style.get("prefix", "")

        formatted = Text()
        if emoji:
            formatted.append(f"{emoji} ", style="bold")
        if prefix:
            formatted.append(f"{prefix}: ", style=f"bold {color}")
        formatted.append(message, style=color)
        return formatted

    # 2.4 快捷消息接口
    @classmethod
    def success(cls, message: str, **kwargs):
        cls._print_panel(message, style_type="success", **kwargs)

    @classmethod
    def error(cls, message: str, **kwargs):
        cls._print_panel(message, style_type="error", **kwargs)

    @classmethod
    def warning(cls, message: str, **kwargs):
        cls._print_panel(message, style_type="warning", **kwargs)

    # 2.5 Agent 消息
    @staticmethod
    def print_agent_msg(message: str, agent_name: str):
        logger.info(f"{agent_name}: {message}")
        if agent_name == "CoderAgent":
            rprint(f"[bold purple on green]{agent_name}[/bold purple on green]: {message}")
        elif agent_name == "WriterAgent":
            rprint(f"[bold purple on yellow]{agent_name}[/bold purple on yellow]: {message}")
        elif agent_name == "test_agent":
            rprint(f"[bold white on blue]{agent_name}[/bold white on blue]: {message}")
        else:
            rprint(f"[bold white]{agent_name}[/bold white]: {message}")

    # 2.6 面板消息
    @classmethod
    def _print_panel(
        cls,
        message: str,
        style_type: str,
        title: Optional[str] = None,
        color: Optional[str] = None,
        emoji: Optional[str] = None,
        prefix: Optional[str] = None,
        panel_kwargs: Optional[Dict] = None,
    ):
        """通用带面板样式的打印方法"""
        text = cls._format_message(message, style_type, color, emoji, prefix)
        default_panel_args = {
            "title": title or style_type.upper(),
            "border_style": color or cls._styles[style_type]["color"],
            "padding": (1, 4),
        }
        panel_args = {**default_panel_args, **(panel_kwargs or {})}
        cls._console.print(Panel.fit(text, **panel_args))

    # 2.7 表格输出
    @classmethod
    def table(
        cls,
        headers: List[str],
        rows: List[List[Any]],
        title: str = "数据表格",
        column_styles: Optional[List[str]] = None,
    ):
        """快速打印表格"""
        table = Table(title=title, show_header=True, header_style="bold cyan")
        column_styles = column_styles or ["magenta"] * len(headers)

        for header, style in zip(headers, column_styles):
            table.add_column(header, style=style)

        for row in rows:
            table.add_row(*[str(item) for item in row])

        cls._console.print(table)

    # 2.8 工作流开始/结束
    @classmethod
    def workflow_start(cls):
        """打印工作流开始信息"""
        cls._console.print()
        formatted = Text()
        formatted.append("🚀 ", style="bold")
        formatted.append("开始执行工作流", style="bold blue")
        cls._console.print(Panel.fit(formatted, border_style="blue", padding=(1, 4)))
        logger.info("\n=======================开始执行工作流=======================\n")

    @classmethod
    def workflow_end(cls):
        """打印工作流结束信息"""
        cls._console.print()
        formatted = Text()
        formatted.append("✨ ", style="bold")
        formatted.append("工作流执行完成", style="bold green")
        cls._console.print(Panel.fit(formatted, border_style="green", padding=(1, 4)))
        logger.info("\n=======================工作流执行完成=======================\n")

    # 2.9 Agent 开始/结束
    @classmethod
    def agent_start(cls, agent_name: str):
        """打印 Agent 开始信息"""
        cls._console.print()
        formatted = Text()
        formatted.append("🤖 ", style="bold")
        formatted.append(f"Agent: {agent_name} ", style="bold cyan")
        formatted.append("开始执行", style="bold blue")
        cls._console.print(Panel.fit(formatted, border_style="blue", padding=(1, 4)))
        logger.info(f"\n================Agent: {agent_name}开始=================\n")

    @classmethod
    def agent_end(cls, agent_name: str):
        """打印 Agent 结束信息"""
        cls._console.print()
        formatted = Text()
        formatted.append("✨ ", style="bold")
        formatted.append(f"Agent: {agent_name} ", style="bold cyan")
        formatted.append("执行完成", style="bold green")
        cls._console.print(Panel.fit(formatted, border_style="green", padding=(1, 4)))
        logger.info(f"\n================Agent: {agent_name}结束==================\n")
