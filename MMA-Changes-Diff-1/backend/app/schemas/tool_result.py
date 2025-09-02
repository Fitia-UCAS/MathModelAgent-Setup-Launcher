# app/schemas/tool_result.py

from pydantic import BaseModel
from typing import Any, Optional


# 2 ToolResult: 统一工具返回结构
class ToolResult(BaseModel):
    success: bool
    message: Optional[str] = None
    data: Optional[Any] = None
