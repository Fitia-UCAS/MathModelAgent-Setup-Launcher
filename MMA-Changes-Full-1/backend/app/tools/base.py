# app/tools/base.py

# 1. 导入与类型定义
from typing import Dict, Any, List, Callable
import inspect
from app.schemas.tool_result import ToolResult


# 2. tool 装饰器
# 2.1 作用：注册工具并为函数附加元信息（名称/描述/参数 schema）
def tool(
    name: str,
    description: str,
    parameters: Dict[str, Dict[str, Any]],
    required: List[str],
) -> Callable:
    # 2.1.1 返回装饰器
    def decorator(func):
        # 2.1.2 直接使用调用方提供的参数构建 schema（不自动推断）
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": parameters,
                    "required": required,
                },
            },
        }

        # 2.1.3 在函数对象上挂载工具信息，供运行时发现
        func._function_name = name
        func._tool_description = description
        func._tool_schema = schema

        return func

    return decorator


# 3. BaseTool
# 3.1 目的：提供工具集合发现、存在性校验与异步调用的基础实现
class BaseTool:
    name: str = ""

    def __init__(self):
        # 3.1.1 内部缓存：已注册工具列表（避免重复扫描）
        self._tools_cache = None

    # 3.2 获取已注册工具列表
    def get_tools(self) -> List[Dict[str, Any]]:
        # 3.2.1 若缓存存在则直接返回
        if self._tools_cache is not None:
            return self._tools_cache

        # 3.2.2 扫描当前实例的方法，收集带 _tool_schema 的方法
        tools = []
        for _, method in inspect.getmembers(self, inspect.ismethod):
            if hasattr(method, "_tool_schema"):
                tools.append(method._tool_schema)

        # 3.2.3 缓存并返回
        self._tools_cache = tools
        return tools

    # 3.3 判断工具函数是否存在
    def has_function(self, function_name: str) -> bool:
        for _, method in inspect.getmembers(self, inspect.ismethod):
            if hasattr(method, "_function_name") and method._function_name == function_name:
                return True
        return False

    # 3.4 异步调用指定工具函数
    async def invoke_function(self, function_name: str, **kwargs) -> ToolResult:
        # 3.4.1 在实例方法中查找匹配的工具并调用
        for _, method in inspect.getmembers(self, inspect.ismethod):
            if hasattr(method, "_function_name") and method._function_name == function_name:
                return await method(**kwargs)

        # 3.4.2 未找到工具则抛错
        raise ValueError(f"Tool '{function_name}' not found")
