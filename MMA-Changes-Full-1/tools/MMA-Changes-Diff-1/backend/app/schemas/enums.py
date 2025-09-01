# app/schemas/enums.py

from enum import Enum


class CompTemplate(str, Enum):
    CHINA: str = "CHINA"
    AMERICAN: str = "AMERICAN"


class FormatOutPut(str, Enum):
    Markdown: str = "Markdown"
    LaTeX: str = "LaTeX"


class AgentType(str, Enum):
    COORDINATOR = "CoordinatorAgent"
    MODELER = "ModelerAgent"
    CODER = "CoderAgent"
    WRITER = "WriterAgent"
    SYSTEM = "SystemAgent"


class AgentStatus(str, Enum):
    START = "start"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"
    SUCCESS = "success"
