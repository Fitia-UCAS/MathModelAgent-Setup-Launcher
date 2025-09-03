# app/models/user_output.py

import os
import re
from app.utils.data_recorder import DataRecorder
from app.schemas.A2A import WriterResponse
import json
import uuid


# 1 初始化与成员
# 1.1 work_dir: 工作目录
# 1.2 ques_count: 小问数量
# 1.3 res: 存放各章节生成结果的字典，格式 { key: {"response_content": str, "footnotes": list[str]} }
# 1.4 footnotes: 全局脚注映射 { uuid: {"content": str, "number": int?} }
class UserOutput:
    def __init__(self, work_dir: str, ques_count: int, data_recorder: DataRecorder | None = None):
        self.work_dir = work_dir
        self.res: dict[str, dict] = {}
        self.data_recorder = data_recorder
        self.cost_time = 0.0
        self.initialized = True
        self.ques_count: int = ques_count
        self.footnotes: dict[str, dict] = {}
        self._init_seq()

    # 2 章节序列初始化
    # 2.1 用于最终拼接的章节顺序：firstPage, RepeatQues, analysisQues, modelAssumption, symbol, eda, ques1..quesN, sensitivity_analysis, judge
    def _init_seq(self):
        ques_str = [f"ques{i}" for i in range(1, self.ques_count + 1)]
        self.seq = [
            "firstPage",  # 标题、摘要、关键词
            "RepeatQues",  # 一、问题重述
            "analysisQues",  # 二、问题分析
            "modelAssumption",  # 三、模型假设
            "symbol",  # 四、符号说明
            "eda",  # 数据预处理（EDA）
            *ques_str,  # 模型建立与求解（问题1,2,...）
            "sensitivity_analysis",  # 模型分析与检验
            "judge",  # 模型评价、改进与推广
        ]

    # 3 写入与获取结果
    # 3.1 set_res: 写入某章节（接受 WriterResponse 或兼容 dict）
    def set_res(self, key: str, writer_response: WriterResponse):
        if not isinstance(writer_response, WriterResponse):
            # 容错：允许传入兼容结构的字典
            try:
                rc = (writer_response or {}).get("response_content", "")  # type: ignore
                fn = (writer_response or {}).get("footnotes", [])  # type: ignore
            except Exception:
                rc, fn = "", []
        else:
            rc = writer_response.response_content or ""
            fn = writer_response.footnotes or []

        self.res[key] = {
            "response_content": rc,
            "footnotes": fn,
        }

    # 3.2 get_res: 返回当前所有章节结果（原样）
    def get_res(self):
        return self.res

    # 3.3 get_model_build_solve: 拼接 quesX 的简短摘要供写作 prompt 使用
    def get_model_build_solve(self) -> str:
        parts: list[str] = []
        for k, v in self.res.items():
            if k.startswith("ques"):
                text = (v or {}).get("response_content", "") or ""
                if len(text) > 500:
                    text = text[:1000] + "..."
                parts.append(f"{k}: {text}")
        return " | ".join(parts)

    # 4 引用解析（就地脚注 -> uuid）
    # 4.1 replace_references_with_uuid:
    #     - 匹配形如 {[^1]: 引用内容}，替换为 [<uuid>]，并将内容存进 self.footnotes（去重复用）
    def replace_references_with_uuid(self, text: str) -> str:
        if not text:
            return ""

        pattern = re.compile(r"\{\[\^(\d+)\]:\s*(.*?)\}", re.DOTALL)

        def _find_existing_uuid(content: str) -> str | None:
            for u, data in self.footnotes.items():
                if data.get("content") == content:
                    return u
            return None

        def _repl(m: re.Match) -> str:
            ref_content = (m.group(2) or "").strip().rstrip(".").strip()
            if not ref_content:
                return ""  # 空内容直接移除

            existed = _find_existing_uuid(ref_content)
            if existed:
                return f"[{existed}]"

            new_uuid = str(uuid.uuid4())
            self.footnotes[new_uuid] = {"content": ref_content}
            return f"[{new_uuid}]"

        return pattern.sub(_repl, text)

    # 5 UUID -> 编号并替换为 [^n]
    # 5.1 sort_text_with_footnotes: 按 self.seq 顺序扫描，遇到 uuid 则分配逐次编号并替换正文中的占位
    def sort_text_with_footnotes(self, replace_res: dict) -> dict:
        sort_res: dict = {}
        ref_index = 1
        for seq_key in self.seq:
            section = replace_res.get(seq_key, {"response_content": ""})
            text = section.get("response_content", "") or ""

            uuid_list = re.findall(r"\[([a-f0-9-]{36})\]", text)
            for uid in uuid_list:
                if self.footnotes.get(uid) is not None and self.footnotes[uid].get("number") is None:
                    self.footnotes[uid]["number"] = ref_index
                    ref_index += 1
                number = (self.footnotes.get(uid) or {}).get("number")
                text = text.replace(f"[{uid}]", f"[^{number}]" if number else f"[{uid}]")

            sort_res[seq_key] = {
                "response_content": text,
            }
        return sort_res

    # 6 将编号脚注追加到文末
    # 6.1 append_footnotes_to_text: 仅追加已分配编号的脚注，按编号顺序输出为 "## 参考文献" 下的条目
    def append_footnotes_to_text(self, text: str) -> str:
        numbered = [(u, d) for u, d in self.footnotes.items() if "number" in (d or {})]
        if not numbered:
            return text

        text += "\n\n## 参考文献"
        numbered.sort(key=lambda x: x[1]["number"])
        for _, footnote in numbered:
            text += f"\n\n[^{footnote['number']}]: {footnote['content']}"
        return text

    # 7 汇总与保存
    # 7.1 get_result_to_save:
    #     - 将每章节中的就地定义替换为 uuid（replace_references_with_uuid）
    #     - 为 uuid 分配编号并替换为 [^n]（sort_text_with_footnotes）
    #     - 拼接章节并追加脚注（append_footnotes_to_text）
    def get_result_to_save(self) -> str:
        replace_res: dict[str, dict] = {}

        for key in self.seq:
            section = self.res.get(key, {"response_content": ""})
            original = section.get("response_content", "") or ""
            new_text = self.replace_references_with_uuid(original)
            replace_res[key] = {"response_content": new_text}

        sort_res = self.sort_text_with_footnotes(replace_res)
        full_res_body = "\n\n".join(sort_res.get(k, {}).get("response_content", "") for k in self.seq)
        full_res = self.append_footnotes_to_text(full_res_body)
        return full_res

    # 7.2 save_result: 写入 res.json 与 res.md 到工作目录
    def save_result(self):
        # 保存结构化 JSON
        with open(os.path.join(self.work_dir, "res.json"), "w", encoding="utf-8") as f:
            json.dump(self.res, f, ensure_ascii=False, indent=4)

        # 保存最终 Markdown
        res_path = os.path.join(self.work_dir, "res.md")
        with open(res_path, "w", encoding="utf-8") as f:
            f.write(self.get_result_to_save())
