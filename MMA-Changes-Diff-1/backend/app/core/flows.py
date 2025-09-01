# app/core/flows.py

from app.models.user_output import UserOutput
from app.tools.base_interpreter import BaseCodeInterpreter
from app.schemas.A2A import ModelerToCoder


class Flows:
    def __init__(self, questions: dict[str, str | int]):
        self.flows: dict[str, dict] = {}
        self.questions: dict[str, str | int] = questions

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

    def get_solution_flows(self, questions: dict[str, str | int], modeler_response: ModelerToCoder):
        """
        生成针对“代码手（Coder）”的子任务编排提示 flows。
        对 modeler_response.questions_solution 缺失的键做兜底，避免 KeyError。
        """
        qs = modeler_response.questions_solution or {}

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
                # TODO ： 获取当前路径下的所有数据集
                "coder_prompt": f"""
                        参考建模手给出的解决方案{modeler_response.questions_solution["eda"]}
                        对当前目录下数据进行EDA分析(数据清洗,可视化),清洗后的数据保存当前目录下,**不需要复杂的模型**
                    """,
            },
            **ques_flow,
            "sensitivity_analysis": {
                "coder_prompt": f"""
                        参考建模手给出的解决方案{modeler_response.questions_solution["sensitivity_analysis"]}
                        完成敏感性分析
                    """,
            },
        }
        return flows

    def get_write_flows(self, user_output: UserOutput, config_template: dict, bg_ques_all: str):
        """
        生成“写作手（Writer）”的总纲提示 flows（封面页、重述、分析、假设、符号、评价）。
        """
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

    def get_writer_prompt(
        self,
        key: str,
        coder_response: str,
        code_interpreter: BaseCodeInterpreter,
        config_template: dict,
    ) -> str:
        """
        根据不同的 key 生成对应的 writer_prompt。
        对极长文本做截断，避免 tokens 暴涨；对模板缺失做兜底。
        """

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

    def get_questions_quesx_keys(self) -> list[str]:
        """获取问题1,2...的键（ques1、ques2、...）"""
        return list(self.get_questions_quesx().keys())

    def get_questions_quesx(self) -> dict[str, str]:
        """获取问题1,2,3...的键值对"""
        return {key: value for key, value in self.questions.items() if key.startswith("ques") and key != "ques_count"}

    def get_seq(self, ques_count: int) -> dict[str, str]:
        """获取执行顺序（带空字符串占位）"""
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
