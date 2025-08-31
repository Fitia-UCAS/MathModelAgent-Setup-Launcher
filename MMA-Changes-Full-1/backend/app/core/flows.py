from app.models.user_output import UserOutput
from app.tools.base_interpreter import BaseCodeInterpreter
from app.schemas.A2A import ModelerToCoder  # 修正导入，避免循环依赖


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
                "coder_prompt": (
                    "参考建模手给出的解决方案："
                    f"{qs.get('eda', '（未提供 EDA 方案，按常规流程完成数据概览、缺失/异常处理与可视化）')}\n"
                    "对当前目录下数据进行 EDA 分析（数据清洗、可视化），清洗后的数据保存当前目录下"
                    "【分析规划四步】\n"
                    "1) 数据质量评估与清洗策略：缺失值、数据类型、异常值、重复值 → 输出清洗方案。\n"
                    "2) 单变量分析：数值列直方图+KDE、类别列频数统计+条形图。\n"
                    "3) 双/多变量分析：相关性热力图、散点图、箱型图、小提琴图、交叉表、平行坐标等。\n"
                    "4) 综合洞察：总结核心发现，形成分析计划。\n"
                    "【可视化清单（示例，能生成多少生成多少，缺失则在报告中说明）】\n"
                    "a) 行列数与内存占用条形图 → eda/figures/fig_shape_memory_bar.png\n"
                    "b) 缺失热力图 → eda/figures/<table>_missing_heatmap.png\n"
                    "c) 数值分布（直方图+KDE）→ eda/figures/<table>_<col>_hist_kde.png\n"
                    "d) 山脊图（若存在时间或分组）→ eda/figures/<table>_ridgeline.png\n"
                    "e) 类别分布条形图 → eda/figures/<table>_<col>_top20_bar.png\n"
                    "f) 箱型图（整体+分组）→ eda/figures/<table>_boxplot.png / eda/figures/<table>_<numcol>_by_<catcol>_box.png\n"
                    "g) 相关性热力图 → eda/figures/<table>_corr_heatmap.png\n"
                    "h) 成对关系 → eda/figures/<table>_pairplot.png\n"
                    "i) 时间序列折线/滚动 → eda/figures/<table>_<numcol>_by_time.png / eda/figures/<table>_<numcol>_rolling.png\n"
                    "j) 平行坐标图 → eda/figures/<table>_parallel_coordinates.png\n"
                    "【美学规范】中文标签用微软雅黑；保存为 PNG，300DPI；图题含中英；坐标轴清晰；网格线淡灰；图例不遮挡。\n"
                    "【输出物】\n"
                    "1) 清洗后数据：保存到 eda/datasets/cleaned.csv（或 cleaned_<table>.csv）。\n"
                    "2) 结构化报告：保存到 eda/reports/report_eda.txt，包含：数据清单、清洗策略与理由、关键洞察要点、每张图的图注与发现。\n"
                    "3) 程序需在末尾 print() 输出：已发现表清单、清洗方案摘要、Top-5 关键洞察。\n"
                    "【限制】不引入复杂模型（此处的复杂指不做训练/预测），仅统计与可视化。"
                ),
            },
            **ques_flow,
            "sensitivity_analysis": {
                "coder_prompt": (
                    "参考建模手给出的解决方案："
                    f"{qs.get('sensitivity_analysis', '（未提供敏感性方案，按关键参数±10% 做单因素敏感性与可视化）')}\n"
                    "1) 选定 y（目标/指标），列出候选敏感因子 X（≤10个，来自EDA相关性与业务意义）。\n"
                    "2) 单因素敏感性：对每个 x_i 做 ±10% 扰动，计算 Δy 与弹性系数 E_i （若 y 或 x_i 含零/负值，则退化为 Δy/Δx_i 并在报告中说明）。\n"
                    "3) 多因素（可选）：采样 N=200（若计算量受限降至 N=100），计算 Sobol 近似或方差归因。\n"
                    "【可视化清单】\n"
                    "a) Tornado 图 → sensitivity_analysis/figures/tornado_sensitivity.png\n"
                    "b) 雷达图 → sensitivity_analysis/figures/radar_sensitivity.png\n"
                    "c) 响应曲线（Top-3 因子）→ sensitivity_analysis/figures/response_<xi>.png\n"
                    "d) 多因素可选图（PDP 等）→ sensitivity_analysis/figures/pdp_<xi>.png\n"
                    "【输出物】\n"
                    "1) 敏感性表格 CSV → sensitivity_analysis/datasets/sensitivity_summary.csv\n"
                    "2) 报告 → sensitivity_analysis/reports/report_sensitivity.txt\n"
                    "3) 程序末尾 print() 输出：Top-5 关键因子与弹性。\n"
                    "完成敏感性分析。"
                ),
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
