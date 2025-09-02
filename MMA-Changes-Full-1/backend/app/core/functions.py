# functions.py

# 1 工具清单定义区域
# 1.1 该列表用于向 LLM / 调用框架注册可用函数工具（此处仅注册 execute_code）
# 1.2 保持原有提示词和 schema 不做修改，仅在上方/旁边添加注释说明
coder_tools = [
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": (
                "This function allows you to execute Python code and retrieve the terminal output. "
                "If the code generates image output, the function will return the text '[image]'. "
                "The code is sent to a Jupyter kernel for execution. The kernel will remain active after execution, "
                "retaining all variables in memory. "
                "You cannot show rich outputs like plots or images, but you can store them in the working directory "
                "and point the user to them."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "The code text"}},
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
]

# 2 已安装库与待办事项（仅供开发/运行时参考）
# 2.1 已安装（或应确保在执行环境中安装）：numpy scipy pandas matplotlib seaborn scikit-learn xgboost
# 2.2 待办（可能需要在部署时完成或记录）：pip install python / read files / get_cites
# 2.3 这些注释不会改变工具描述或 schema，仅作为说明保留

# have installed: numpy scipy pandas matplotlib seaborn scikit-learn xgboost
# TODO: pip install python
# TODO: read files
# TODO: get_cites


# 3 writeragent 专用工具（当前禁用外部检索以避免写作阶段中断）
# 3.1 保持为空列表，表示写作阶段不暴露外部工具
# 3.2 如果将来需要启用，请在此处添加相应工具描述（同 coder_tools 格式）
## writeragent tools
# 外部文献检索功能已禁用，防止写作阶段因检索失败中断
writer_tools = []
