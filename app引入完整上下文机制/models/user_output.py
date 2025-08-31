import os
from app.utils.data_recorder import DataRecorder


class UserOutput:
    def __init__(self, work_dir: str, data_recorder: DataRecorder | None = None):
        self.work_dir = work_dir
        self.res: dict[str, str] = {
            # "eda": "",
            # "ques1": "",
        }
        self.data_recorder = data_recorder
        self.cost_time = 0.0
        self.initialized = True

    def set_res(self, key: str, value: str):
        self.res[key] = value  # TODO： 换种数据类型有顺序

    def get_res(self):
        return self.res

    def get_model_build_solve(self) -> str:
        """获取模型求解"""
        model_build_solve = ",".join(
            f"{key}-{value}"
            for key, value in self.res.items()
            if key.startswith("ques") and key != "ques_count"
        )

        return model_build_solve

    def get_result_to_save(self, ques_count):
        # 动态顺序获取拼接res value，正确拼接顺序
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
            "reference",
        ]
        return "\n".join([self.res.get(key, "") for key in seq])

    def save_result(self, ques_count):
        res_path = os.path.join(self.work_dir, "res.md")
        with open(res_path, "w", encoding="utf-8") as f:
            f.write(self.get_result_to_save(ques_count))
