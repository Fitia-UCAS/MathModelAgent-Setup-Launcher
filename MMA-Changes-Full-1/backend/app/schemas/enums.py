# app/schemas/enums.py

from enum import Enum

# 1 枚举总览
# 1.1 本文件定义流程中使用的若干枚举类型，用于配置模板、输出格式、Agent 标识与状态码。
# 1.2 这些枚举仅作常量与类型约束，不包含业务逻辑。


# 2 报告/模板相关
class CompTemplate(str, Enum):
    # 2.1 CHINA: 中国式论文/报告模板（用于选择写作模板）
    CHINA: str = "CHINA"
    # 2.2 AMERICAN: 美式论文/报告模板（用于选择写作模板）
    AMERICAN: str = "AMERICAN"


class FormatOutPut(str, Enum):
    # 2.3 Markdown: 以 Markdown 格式输出（默认常用）
    Markdown: str = "Markdown"
    # 2.4 LaTeX: 以 LaTeX 格式输出（可用于学术期刊/论文）
    LaTeX: str = "LaTeX"


# 3 Agent 类型标识（用于 send/publish 路径分发与日志）
class AgentType(str, Enum):
    # 3.1 CoordinatorAgent：题面解析器
    COORDINATOR = "CoordinatorAgent"
    # 3.2 ModelerAgent：建模手
    MODELER = "ModelerAgent"
    # 3.3 CoderAgent：代码执行器
    CODER = "CoderAgent"
    # 3.4 WriterAgent：写作/排版器
    WRITER = "WriterAgent"
    # 3.5 SystemAgent：系统/默认类型
    SYSTEM = "SystemAgent"


# 4 Agent 运行状态（用于前端/后端状态跟踪）
class AgentStatus(str, Enum):
    # 4.1 启动
    START = "start"
    # 4.2 运行中
    WORKING = "working"
    # 4.3 已完成（逻辑上结束）
    DONE = "done"
    # 4.4 出现错误
    ERROR = "error"
    # 4.5 成功（用于更细粒度的成功标记）
    SUCCESS = "success"
