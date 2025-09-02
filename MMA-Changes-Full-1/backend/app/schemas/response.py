# app/schemas/response.py

# 1 模块概述
# 1.1 本模块定义系统内部与前端/WS 交互使用的消息模型（基于 Pydantic）。
# 1.2 命名说明：
# 1.2.1 Message / ToolMessage / SystemMessage / UserMessage / AgentMessage 为通用消息基类与派生类型
# 1.2.2 CodeExecution 及其子类用于描述代码执行的结构化输出
# 1.2.3 InterpreterMessage / ScholarMessage 为工具类型消息的具体定义
# - CoderMessage / WriterMessage / CoordinatorMessage / ModelerMessage 为代理（Agent）类型消息
# 1.3 目标：
# 1.3.1 保证消息可序列化、易于在 Redis/WebSocket 传输
# 1.3.2 支持结构化 content（dict/list/str 等）

from typing import Literal, Union, Any
from app.schemas.enums import AgentType
from pydantic import BaseModel, Field
from uuid import uuid4


# 2 基础消息模型
# 2.1 Message：所有消息的基类（携带 id、类型与任意 content）
class Message(BaseModel):
    # 2.1.1 唯一 id（默认 UUID4 字符串）
    id: str = Field(default_factory=lambda: str(uuid4()))
    # 2.1.2 消息种类（system | agent | user | tool）
    msg_type: Literal["system", "agent", "user", "tool"]  # system msg | agent message | user message | tool message
    # 2.1.3 允许任意结构化内容：str / dict / list / None
    content: Any = None


# 3 工具消息（ToolMessage）与特化
# 3.1 ToolMessage：工具调用的通用封装（input/output 均为结构化类型）
class ToolMessage(Message):
    msg_type: str = "tool"
    # 3.1.1 支持的工具名（按需扩展）
    tool_name: Literal["execute_code", "search_scholar"]
    # 3.1.2 输入与输出保留为结构化类型，方便后续解析
    input: dict
    output: list


# 4 系统 / 用户 / 代理 消息
# 4.1 SystemMessage：用于广播给前端的系统提示（带 level/type）
class SystemMessage(Message):
    msg_type: str = "system"
    type: Literal["info", "warning", "success", "error"] = "info"


# 4.2 UserMessage：用户发起的消息（保持最小结构）
class UserMessage(Message):
    msg_type: str = "user"


# 4.3 AgentMessage：代理（Agent）产生的消息（带 agent_type，便于路由与展示）
class AgentMessage(Message):
    msg_type: str = "agent"
    agent_type: AgentType  # CoordinatorAgent | ModelerAgent | CoderAgent | WriterAgent


# 4.4 便捷子类：为常见 agent 类型指定默认 agent_type
class ModelerMessage(AgentMessage):
    agent_type: AgentType = AgentType.MODELER


class CoordinatorMessage(AgentMessage):
    agent_type: AgentType = AgentType.COORDINATOR


# 5 代码执行结果模型（结构化）
# 5.1 CodeExecution：基类，标注结果类型与消息文本
class CodeExecution(BaseModel):
    res_type: Literal["stdout", "stderr", "result", "error"]
    msg: str | None = None


# 5.2 StdOutModel / StdErrModel / ResultModel / ErrorModel：具体类型
class StdOutModel(CodeExecution):
    res_type: str = "stdout"


class StdErrModel(CodeExecution):
    res_type: str = "stderr"


class ResultModel(CodeExecution):
    res_type: str = "result"
    # 5.2.1 format：ResultModel 的具体输出格式（文本/图片/二进制导出等）
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
    # 5.2.2 error 详情
    name: str
    value: str
    traceback: str


# 6 代码执行输出的联合类型（便于类型注解）
OutputItem = Union[StdOutModel, StdErrModel, ResultModel, ErrorModel]


# 7 工具专用消息的具体化（继承 ToolMessage）
class ScholarMessage(ToolMessage):
    # 7.1 搜索学术工具的返回结构（可为 None）
    tool_name: str = "search_scholar"
    input: dict | None = None  # query
    output: list[str] | None = None  # cites


class InterpreterMessage(ToolMessage):
    # 7.2 代码解释器工具的返回结构（输出为结构化的 OutputItem 列表）
    tool_name: str = "execute_code"
    input: dict | None = None  # code
    output: list[OutputItem] | None = None  # code_results


# 8 Agent 消息的简短具体化（保留 content 以供前端显示）
class CoderMessage(AgentMessage):
    agent_type: AgentType = AgentType.CODER


class WriterMessage(AgentMessage):
    agent_type: AgentType = AgentType.WRITER
    # 8.1 可选子标题（用于前端显示章节名）
    sub_title: str | None = None


# 9 合并类型：方便类型注解和静态检查
MessageType = Union[
    SystemMessage,
    UserMessage,
    ModelerMessage,
    CoderMessage,
    WriterMessage,
    CoordinatorMessage,
]
