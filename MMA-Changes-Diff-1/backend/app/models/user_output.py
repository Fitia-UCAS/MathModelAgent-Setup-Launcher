import os
import re
from app.utils.data_recorder import DataRecorder
from app.schemas.A2A import WriterResponse
import json
import uuid


class UserOutput:
    def __init__(
        self, work_dir: str, ques_count: int, data_recorder: DataRecorder | None = None
    ):
        self.work_dir = work_dir
        # 存储各小节结果：{ key: {"response_content": str, "footnotes": list[str]} }
        self.res: dict[str, dict] = {}
        self.data_recorder = data_recorder
        self.cost_time = 0.0
        self.initialized = True
        self.ques_count: int = ques_count
        # 全局脚注：{ uuid: {"content": str, "number": int?} }
        self.footnotes: dict[str, dict] = {}
        self._init_seq()

    def _init_seq(self):
        """按论文结构定义章节顺序；用于最终拼接"""
        ques_str = [f"ques{i}" for i in range(1, self.ques_count + 1)]
        self.seq = [
            "firstPage",              # 标题、摘要、关键词
            "RepeatQues",             # 一、问题重述
            "analysisQues",           # 二、问题分析
            "modelAssumption",        # 三、模型假设
            "symbol",                 # 四、符号说明
            "eda",                    # 数据预处理（EDA）
            *ques_str,                # 模型建立与求解（问题1,2,...）
            "sensitivity_analysis",   # 模型分析与检验
            "judge",                  # 模型评价、改进与推广
        ]

    def set_res(self, key: str, writer_response: WriterResponse):
        """写入某个章节的生成结果"""
        if not isinstance(writer_response, WriterResponse):
            # 容错：允许传入兼容结构的字典
            try:
                rc = (writer_response or {}).get("response_content", "")  # type: ignore
                fn = (writer_response or {}).get("footnotes", [])        # type: ignore
            except Exception:
                rc, fn = "", []
        else:
            rc = writer_response.response_content or ""
            fn = writer_response.footnotes or []

        self.res[key] = {
            "response_content": rc,
            "footnotes": fn,
        }

    def get_res(self):
        return self.res

    def get_model_build_solve(self) -> str:
        """
        获取“模型建立与求解”的简要串，用于写作 prompt。
        仅拼接各 quesX 的 response_content（截断到较短，以免 prompt 过长）。
        """
        parts: list[str] = []
        for k, v in self.res.items():
            if k.startswith("ques"):
                text = (v or {}).get("response_content", "") or ""
                if len(text) > 400:
                    text = text[:400] + "..."
                parts.append(f"{k}: {text}")
        return " | ".join(parts)

    # ============ 引用解析与合并 ============
    def replace_references_with_uuid(self, text: str) -> str:
        """
        将文中形如 `{[^1]: 引用内容}` 的“就地引用定义”替换为 `[<uuid>]` 占位，
        并把引用内容存入 self.footnotes[uuid] = {"content": ...}。
        若相同内容已存在，则复用已有 uuid（去重）。
        """
        if not text:
            return ""

        # 匹配 {[^数字]: 引用内容}，允许跨行
        pattern = re.compile(r"\{\[\^(\d+)\]:\s*(.*?)\}", re.DOTALL)
        def _find_existing_uuid(content: str) -> str | None:
            for u, data in self.footnotes.items():
                if data.get("content") == content:
                    return u
            return None

        def _repl(m: re.Match) -> str:
            ref_num = m.group(1)
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

    def sort_text_with_footnotes(self, replace_res: dict) -> dict:
        """
        将各章节文本中的 [uuid] 按出现顺序重新编号为 [^1], [^2], ...
        并在 self.footnotes[uuid]['number'] 写入编号。
        """
        sort_res: dict = {}
        ref_index = 1
        for seq_key in self.seq:
            # 章节缺失时用空串兜底
            section = replace_res.get(seq_key, {"response_content": ""})
            text = section.get("response_content", "") or ""

            # 找所有 uuid 形式的引用
            uuid_list = re.findall(r"\[([a-f0-9-]{36})\]", text)
            for uid in uuid_list:
                # 第一次见到该 uuid 才赋编号
                if self.footnotes.get(uid) is not None and self.footnotes[uid].get("number") is None:
                    self.footnotes[uid]["number"] = ref_index
                    ref_index += 1
                # 将所有该 uuid 的占位替换为 [^number]（若没编号，保持原样）
                number = (self.footnotes.get(uid) or {}).get("number")
                text = text.replace(f"[{uid}]", f"[^{number}]" if number else f"[{uid}]")

            sort_res[seq_key] = {
                "response_content": text,
            }
        return sort_res

    def append_footnotes_to_text(self, text: str) -> str:
        """
        将全局脚注按编号追加到文末；仅输出已有 number 的脚注。
        """
        # 仅保留已编号的脚注
        numbered = [(u, d) for u, d in self.footnotes.items() if "number" in (d or {})]
        if not numbered:
            return text

        text += "\n\n## 参考文献"
        # 按编号排序
        numbered.sort(key=lambda x: x[1]["number"])
        for _, footnote in numbered:
            text += f"\n\n[^{footnote['number']}]: {footnote['content']}"
        return text

    # ============ 汇总/保存 ============
    def get_result_to_save(self) -> str:
        """
        1) 将每个章节内联的 {[^n]: ...} 引用替换为 [uuid] 并汇总全局 footnotes
        2) 按出现顺序给 uuid 赋编号，正文替换为 [^n]
        3) 文末追加“参考文献”条目
        """
        replace_res: dict[str, dict] = {}

        # 逐章替换“就地引用定义”为 uuid
        for key in self.seq:
            section = self.res.get(key, {"response_content": ""})
            original = section.get("response_content", "") or ""
            new_text = self.replace_references_with_uuid(original)
            replace_res[key] = {"response_content": new_text}

        # 按出现顺序编号 & 正文替换
        sort_res = self.sort_text_with_footnotes(replace_res)

        # 按章节顺序拼接正文
        full_res_body = "\n\n".join(sort_res.get(k, {}).get("response_content", "") for k in self.seq)

        # 追加参考文献
        full_res = self.append_footnotes_to_text(full_res_body)
        return full_res

    def save_result(self):
        """保存 res.json 与 res.md 到工作目录"""
        # 1) 保存结构化 JSON
        with open(os.path.join(self.work_dir, "res.json"), "w", encoding="utf-8") as f:
            json.dump(self.res, f, ensure_ascii=False, indent=4)

        # 2) 保存最终 Markdown
        res_path = os.path.join(self.work_dir, "res.md")
        with open(res_path, "w", encoding="utf-8") as f:
            f.write(self.get_result_to_save())
