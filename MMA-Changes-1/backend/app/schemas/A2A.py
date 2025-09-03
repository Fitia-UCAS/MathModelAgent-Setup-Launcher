# app/schemas/A2A.py

from pydantic import BaseModel
from typing import Any


# 1 文件说明
# 1.1 本文件定义 Agent 间传递的数据模型（A2A: Agent-to-Agent）
# 1.2 这些模型用于进程内传参与类型校验（无业务逻辑）


# 2 Coordinator -> Modeler
# 2.1 用途：Coordinator 将解析后的 questions（dict）和 ques_count 传给 Modeler
class CoordinatorToModeler(BaseModel):
    # 2.1.1 questions: 结构化题目信息（title/background/ques1...），类型 dict
    questions: dict
    # 2.1.2 ques_count: 小问数量，类型 int
    ques_count: int


# 3 Modeler -> Coder
# 3.1 用途：Modeler 将建模方案以 questions_solution 形式传给 Coder
class ModelerToCoder(BaseModel):
    # 3.1.1 questions_solution: dict[str, str]
    #       键示例：'eda' / 'ques1' ... / 'sensitivity_analysis'
    #       值为对应的建模思路或方案（字符串）
    questions_solution: dict[str, str]


# 4 Coder -> Writer
# 4.1 用途：Coder 将代码执行的结果与生成的图片信息传给 Writer
class CoderToWriter(BaseModel):
    # 4.1.1 code_response: Coder 对请求的文本回应（例如：执行日志或总结）；可为 None
    code_response: str | None = None
    # 4.1.2 code_output: 代码执行产出的文本（例如：print 的内容）；可为 None
    code_output: str | None = None
    # 4.1.3 created_images: 由代码生成并保存的图片相对路径列表；可为 None
    created_images: list[str] | None = None


# 5 Writer -> 上层/流程
# 5.1 用途：Writer 返回写作正文与可选脚注，保持灵活以兼容不同实现
class WriterResponse(BaseModel):
    # 5.1.1 response_content: 写作正文（通常为字符串，但保留 Any 以兼容不同形式）
    response_content: Any
    # 5.1.2 footnotes: 可选脚注列表（如 [(id, content), ...]）；可为 None
    footnotes: list[tuple[str, str]] | None = None
