from typing import Literal, Union, Optional, List, Dict, Any
from app.schemas.enums import AgentType
from pydantic import BaseModel, Field
from uuid import uuid4

# ========= 兼容 v1/v2 的 validator 导入 =========
try:
    from pydantic import validator  # v1 写法
    _V2 = False
except ImportError:
    from pydantic import field_validator as _field_validator  # v2 写法
    _V2 = True

    # 起别名，保证下文统一写 @validator
    def validator(field_name: str, *, pre=True, always=True):
        def decorator(func):
            return _field_validator(field_name, mode="before", always=always)(func)
        return decorator


# ========== 公共工具：把 None/非字符串统一转成字符串 ==========
def _to_str_or_empty(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return str(v)
    except Exception:
        return ""


# ========== 基础消息模型 ==========
class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # system msg | agent message | user message | tool message（注意：这是“前端/总线层”的消息类型，不是 OpenAI 的 role）
    msg_type: Literal["system", "agent", "user", "tool"]
    content: Optional[str] = ""  # 统一默认空串，避免 None

    @validator("content", pre=True, always=True)
    def _ensure_content_not_none(cls, v):
        return _to_str_or_empty(v)


# ========== 工具消息（前端展示/链路追踪用，不等同于 OpenAI 的 function role） ==========
class ToolMessage(Message):
    msg_type: str = "tool"
    tool_name: Literal["execute_code", "search_scholar"]
    input: Optional[Dict[str, Any]] = None
    output: Optional[List[Any]] = None


# ========== 系统/用户/代理 消息 ==========
class SystemMessage(Message):
    msg_type: str = "system"
    type: Literal["info", "warning", "success", "error"] = "info"


class UserMessage(Message):
    msg_type: str = "user"


class AgentMessage(Message):
    msg_type: str = "agent"
    agent_type: AgentType  # CoordinatorAgent | ModelerAgent | CoderAgent | WriterAgent


class ModelerMessage(AgentMessage):
    agent_type: AgentType = AgentType.MODELER


class CoordinatorMessage(AgentMessage):
    agent_type: AgentType = AgentType.COORDINATOR


# ========== 代码执行结果结构 ==========
class CodeExecution(BaseModel):
    res_type: Literal["stdout", "stderr", "result", "error"]
    msg: Optional[str] = None

    @validator("msg", pre=True, always=True)
    def _ensure_msg_not_none(cls, v):
        return _to_str_or_empty(v)


class StdOutModel(CodeExecution):
    res_type: str = "stdout"


class StdErrModel(CodeExecution):
    res_type: str = "stderr"


class ResultModel(CodeExecution):
    res_type: str = "result"
    format: Literal[
        "text",
        "html",
        "markdown",
        "png",
        "jpeg",
        "svg",
        "pdf",
        "latex",
        "json",
        "javascript",
    ]


class ErrorModel(CodeExecution):
    res_type: str = "error"
    name: str
    value: str
    traceback: str


OutputItem = Union[StdOutModel, StdErrModel, ResultModel, ErrorModel]


# ========== 具体工具消息类型 ==========
class ScholarMessage(ToolMessage):
    tool_name: str = "search_scholar"
    input: Optional[Dict[str, Any]] = None
    output: Optional[List[str]] = None


class InterpreterMessage(ToolMessage):
    tool_name: str = "execute_code"
    input: Optional[Dict[str, Any]] = None
    output: Optional[List[OutputItem]] = None


# ========== 各 Agent 的消息 ==========
class CoderMessage(AgentMessage):
    agent_type: AgentType = AgentType.CODER


class WriterMessage(AgentMessage):
    agent_type: AgentType = AgentType.WRITER
    sub_title: Optional[str] = None

    @validator("content", pre=True, always=True)
    def _ensure_writer_content(cls, v):
        return _to_str_or_empty(v)


MessageType = Union[
    SystemMessage,
    UserMessage,
    ModelerMessage,
    CoderMessage,
    WriterMessage,
    CoordinatorMessage,
]
