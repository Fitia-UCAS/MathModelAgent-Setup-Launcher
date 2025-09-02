# app/core/flows.py

from app.models.user_output import UserOutput
from app.tools.base_interpreter import BaseCodeInterpreter
from app.schemas.A2A import ModelerToCoder


# 1 职责与概览
# 1.1 负责编排不同阶段的子任务顺序（EDA、quesN、敏感性分析、写作部分等）
# 1.2 负责生成 Coder/Writer 的提示文本（含模板与建模手结果的融合）
# 1.3 不与 LLM 的“严格参数/清洗”耦合，仅进行本地字符串组织


class Flows:
    # 2 构造与基础状态
    # 2.1 questions：来自 Coordinator 的结构化问题对象（含 background、quesN、ques_count 等）
    def __init__(self, questions: dict[str, str | int]):
        self.flows: dict[str, dict] = {}
        self.questions: dict[str, str | int] = questions

    # 3 初始化执行顺序
    # 3.1 生成标准执行序列（封面/重述/分析/假设/符号/EDA/quesN/敏感性分析/评价）
    def set_flows(self, ques_count: int):
        ques_str = [f"ques{i}" for i in range(1, ques_count + 1)]
        seq = [
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "eda",
            *ques_str,
            "sensitivity_analysis",
            "judge",
        ]
        self.flows = {key: {} for key in seq}

    # 4 生成“代码手”子任务提示
    # 4.1 使用 Modeler 输出（questions_solution）与原始 questions 共同生成各 quesN 的 coder_prompt
    # 4.2 对缺失键做兜底，避免 KeyError
    def get_solution_flows(self, questions: dict[str, str | int], modeler_response: ModelerToCoder):
        qs = dict(modeler_response.questions_solution or {})

        questions_quesx = {
            key: value for key, value in questions.items() if key.startswith("ques") and key != "ques_count"
        }

        ques_flow = {
            key: {
                "coder_prompt": (
                    "参考建模手给出的解决方案："
                    f"{qs.get(key, '（未提供该题的方案，先进行合理建模假设与问题拆解）')}\n"
                    f"完成如下问题：{value}"
                ),
            }
            for key, value in questions_quesx.items()
        }

        flows = {
            "eda": {
                "coder_prompt": f"""
                                参考建模手给出的解决方案：{qs.get("eda", "（无）")}对当前目录下数据进行EDA分析(数据清洗,可视化),清洗后的数据保存当前目录下,**不需要复杂的模型**
                                """,
            },
            **ques_flow,
            "sensitivity_analysis": {
                "coder_prompt": f"""
                                参考建模手给出的解决方案：{qs.get("sensitivity_analysis", "（无）")}
                                完成敏感性分析
                                """,
            },
        }
        return flows

    # 5 生成“写作手”总纲提示
    # 5.1 根据模板生成封面/重述/分析/假设/符号/评价等写作分块提示（不需要代码）
    def get_write_flows(self, user_output: UserOutput, config_template: dict, bg_ques_all: str):
        model_build_solve = user_output.get_model_build_solve()

        flows = {
            "firstPage": (
                f"问题背景：{bg_ques_all}，不需要编写代码。"
                f"根据模型求解信息：{model_build_solve}，按照模板撰写：{config_template.get('firstPage', '（模板缺失）')}，"
                "完成标题、摘要、关键词。"
            ),
            "RepeatQues": (
                f"问题背景：{bg_ques_all}，不需要编写代码。"
                f"根据模型求解信息：{model_build_solve}，按照模板撰写：{config_template.get('RepeatQues', '（模板缺失）')}，"
                "完成问题重述。"
            ),
            "analysisQues": (
                f"问题背景：{bg_ques_all}，不需要编写代码。"
                f"根据模型求解信息：{model_build_solve}，按照模板撰写：{config_template.get('analysisQues', '（模板缺失）')}，"
                "完成问题分析。"
            ),
            "modelAssumption": (
                f"问题背景：{bg_ques_all}，不需要编写代码。"
                f"根据模型求解信息：{model_build_solve}，按照模板撰写：{config_template.get('modelAssumption', '（模板缺失）')}，"
                "完成模型假设。"
            ),
            "symbol": (
                "不需要编写代码。"
                f"根据模型求解信息：{model_build_solve}，按照模板撰写：{config_template.get('symbol', '（模板缺失）')}，"
                "完成符号说明部分。"
            ),
            "judge": (
                "不需要编写代码。"
                f"根据模型求解信息：{model_build_solve}，按照模板撰写：{config_template.get('judge', '（模板缺失）')}，"
                "完成模型评价部分。"
            ),
        }
        return flows

    # 6 Writer 的单块提示生成
    # 6.1 根据 key 生成最终写作提示，附带 Coder 的文字结果与代码输出（长文本做截断）
    def get_writer_prompt(
        self,
        key: str,
        coder_response: str,
        code_interpreter: BaseCodeInterpreter,
        config_template: dict,
    ) -> str:
        def _truncate(s: str | None, limit: int = 12000) -> str:
            s = "" if s is None else str(s)
            return s if len(s) <= limit else s[:limit] + "...[TRUNCATED]"

        code_output = _truncate(code_interpreter.get_code_output(key))
        coder_response_safe = _truncate(coder_response)
        bgc = _truncate(self.questions.get("background", "（未提供问题背景）"))

        questions_quesx_keys = self.get_questions_quesx_keys()

        quesx_writer_prompt = {
            k: (
                f"问题背景：{bgc}\n"
                f"不需要编写代码。代码手得到的结果：{coder_response_safe}；代码执行产出：{code_output}\n"
                f"请按照如下模板撰写：{config_template.get(k, '（模板缺失）')}"
            )
            for k in questions_quesx_keys
        }

        writer_prompt = {
            "eda": (
                f"问题背景：{bgc}\n"
                f"不需要编写代码。代码手得到的结果：{coder_response_safe}；代码执行产出：{code_output}\n"
                f"请按照如下模板撰写：{config_template.get('eda', '（模板缺失）')}"
            ),
            **quesx_writer_prompt,
            "sensitivity_analysis": (
                f"问题背景：{bgc}\n"
                f"不需要编写代码。代码手得到的结果：{coder_response_safe}；代码执行产出：{code_output}\n"
                f"请按照如下模板撰写：{config_template.get('sensitivity_analysis', '（模板缺失）')}"
            ),
        }

        if key in writer_prompt:
            return writer_prompt[key]
        else:
            raise ValueError(f"未知的任务类型: {key}")

    # 7 工具：获取 quesN 键与映射
    # 7.1 返回 ques1/ques2/... 的键列表
    def get_questions_quesx_keys(self) -> list[str]:
        return list(self.get_questions_quesx().keys())

    # 7.2 返回 ques1/ques2/... 的键值映射
    def get_questions_quesx(self) -> dict[str, str]:
        return {key: value for key, value in self.questions.items() if key.startswith("ques") and key != "ques_count"}

    # 8 工具：获取执行顺序占位表
    # 8.1 返回一个有序的 dict，值以空字符串占位（供流程配置使用）
    def get_seq(self, ques_count: int) -> dict[str, str]:
        ques_str = [f"ques{i}" for i in range(1, ques_count + 1)]
        seq = [
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "eda",
            *ques_str,
            "sensitivity_analysis",
            "judge",
        ]
        return {key: "" for key in seq}
