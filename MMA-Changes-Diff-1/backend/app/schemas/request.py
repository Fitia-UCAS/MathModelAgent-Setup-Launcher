# app/schemas/request.py

from pydantic import BaseModel
from app.schemas.enums import CompTemplate, FormatOutPut

# 1 模块说明
# 1.1 本文件定义与外部接口交互使用的 Pydantic 请求模型（用于 FastAPI 接口）
# 1.2 仅包含示例请求与建模任务请求的数据结构，便于数据校验与序列化


# 2 示例请求
# 2.1 ExampleRequest：用于加载示例题库时的参数封装
class ExampleRequest(BaseModel):
    example_id: str
    source: str


# 3 主业务请求：建模任务
# 3.1 Problem：提交建模任务的表单/JSON 结构
class Problem(BaseModel):
    # 3.1.1 task_id：由后端生成的任务唯一标识
    task_id: str
    # 3.1.2 ques_all：用户提交的题面原文（可能包含多问），默认空字符串
    ques_all: str = ""
    # 3.1.3 comp_template：写作模板选择（枚举）
    comp_template: CompTemplate = CompTemplate.CHINA
    # 3.1.4 format_output：写作输出格式（枚举）
    format_output: FormatOutPut = FormatOutPut.Markdown

    # 3.2 兼容 pydantic v1/v2：对外导出时确保枚举被序列化为其 value
    def model_dump(self, **kwargs):
        data = super().model_dump(**kwargs)
        data["comp_template"] = self.comp_template.value
        data["format_output"] = self.format_output.value
        return data
