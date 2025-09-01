# app/tools/text_sanitizer.py

"""
TextSanitizer: 统一的文本/代码清洗与正则工具
- 控制字符/ANSI 清理
- Markdown 代码栅栏剥离与 JSON 提取
- 代码执行前规范化
- tool.arguments 中兜底抽取 code
- Markdown 图片路径解析与校验

用法示例:
    from app.tools.text_sanitizer import TextSanitizer as TS
    code = TS.normalize_for_execution(raw_text, language="python")
"""

from __future__ import annotations
import json
import re
import textwrap
from typing import List, Tuple


class TextSanitizer:
    # -----------------------
    # 编译好的正则常量（与项目中原先使用的保持语义等价）
    # -----------------------
    # 控制字符清理（严格：去掉 0x00–0x1F 和 DEL；保留/不保留换行/制表可选）
    CLEAN_CTRL_STRICT_RE = re.compile(r"[\x00-\x1F\x7F]")
    # 宽松：保留 \t(\x09), \n(\x0A), \r(\x0D)
    CLEAN_CTRL_RELAXED_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]")

    # ANSI 颜色控制序列（与原 delete_color_control_char 的正则等价）
    ANSI_ESCAPE_RE = re.compile(r"(?:\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")

    # Markdown 代码栅栏
    # 匹配整段被单个 fence 包裹的情况（优先提取内部）
    CODE_FENCE_OUTER_RE = re.compile(r"^\s*```(?:\s*[^\n`]*)?\s*\n(.*)\n```\s*$", re.S)
    # 匹配文本中所有 fenced blocks（提取内部）
    CODE_FENCE_ALL_RE = re.compile(r"```(?:\s*[^\n`]*)?\n(.*?)```", re.S)

    # REPL/Notebook 提示符
    PY_REPL_PROMPT_RE = re.compile(r"^\s*>>>\s?", re.M)
    NB_IN_PROMPT_RE = re.compile(r"^\s*In\[\d+\]:\s?", re.M)

    # tool.arguments 解析辅助
    # 支持转义字符的 "code": "...." 捕获
    QUOTED_CODE_RE = re.compile(r'"code"\s*:\s*"((?:\\.|[^"\\])*)"', re.S)
    # 单行键值对 style: code: something（忽略大小写，行首）
    CODE_KV_LINE_RE = re.compile(r"^\s*code\s*:\s*([^\n\r]+)", re.I | re.M)

    # 图片路径前缀规则（quesN/figures/）
    QUES_FIG_PREFIX_RE = re.compile(r"^ques\d+/figures/")

    # -----------------------
    # 基础清理
    # -----------------------
    @classmethod
    def clean_control_chars(cls, s: str, keep_whitespace: bool = True) -> str:
        """
        去除会导致解析/展示问题的控制字符。
        keep_whitespace=True 时保留 \t \n \r，False 时与严格模式一致。
        返回空字符串而非 None。
        """
        if s is None:
            return ""
        # 选择松/严格正则
        re_obj = cls.CLEAN_CTRL_RELAXED_RE if keep_whitespace else cls.CLEAN_CTRL_STRICT_RE
        return re_obj.sub("", s)

    @classmethod
    def strip_ansi(cls, s: str) -> str:
        """去除 ANSI 颜色控制序列（escape sequences）"""
        if s is None:
            return ""
        return cls.ANSI_ESCAPE_RE.sub("", s)

    # -----------------------
    # 代码栅栏 / JSON 提取
    # -----------------------
    @classmethod
    def strip_fences_outer_or_all(cls, s: str) -> str:
        """
        更稳健地剥离 Markdown 代码栅栏：
        1) 若整段被单个 fence 包裹，优先去掉外层并返回内部（保留内部原样）；
        2) 否则把文中所有 fence 去掉围栏、保留内部内容（非贪婪匹配）。
        返回去围栏后的字符串（strip 后）。
        """
        if not s:
            return ""
        s = s.strip()
        m = cls.CODE_FENCE_OUTER_RE.match(s)
        if m:
            return m.group(1).strip()
        # 若不是完全包裹情况，尽量把所有 fence 的围栏剥离，保留内部文本
        return cls.CODE_FENCE_ALL_RE.sub(r"\1", s).strip()

    @classmethod
    def extract_first_json_block(cls, s: str, strip_fences_first: bool = True) -> str:
        """
        用“栈法”提取首个配平的 JSON 对象字符串（比简单正则更稳健）。
        """
        if not s:
            return ""
        text = cls.strip_fences_outer_or_all(s) if strip_fences_first else s
        start = text.find("{")
        if start == -1:
            return ""
        stack: List[str] = []
        in_str = False
        esc = False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    stack.append("{")
                elif ch == "}":
                    if stack:
                        stack.pop()
                    if not stack:
                        return text[start : i + 1]
        # 未闭合，返回空（调用方可能决定如何 fallback）
        return ""

    @classmethod
    def fix_invalid_json_escapes(cls, s: str) -> str:
        """
        把 JSON 字符串里所有“非法转义”的单反斜杠补成双反斜杠。
        合法转义仅包括: \", \\, \\/, \b, \f, \n, \r, \t, \\uXXXX
        其它一律加一杠，避免 json.loads 报 Invalid \\escape。
        """
        if not isinstance(s, str) or not s:
            return ""
        return re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", s)

    @classmethod
    def normalize_common_glitches(cls, s: str) -> str:
        """
        对常见的小毛病做修复：
        - '"qu es2"' -> '"ques2"'
        - '" ques  3 "' -> '"ques3"'
        - 同时剥离围栏
        """
        if not s:
            return ""
        s = cls.strip_fences_outer_or_all(s)
        # "qu es2" -> "ques2"
        s = re.sub(r'"qu\s+es(\d+)"', r'"ques\1"', s)
        # '" ques  3 "' 或者 '"ques 3"' 等 -> "ques3"
        s = re.sub(r'"\s*ques\s*(\d+)\s*"', r'"ques\1"', s)
        return s.strip()

    # -----------------------
    # 代码执行前规范化（与原项目行为等价）
    # -----------------------
    @classmethod
    def normalize_for_execution(cls, raw: str, language: str = "python") -> str:
        """
        将 raw 文本规范化为可直接执行的多行字符串（以 '\n' 结尾）。
        策略（保守）：
        - 优先抽取 fenced code（如果存在）
        - 规范换行 CRLF/CR -> LF
        - 仅在没有真实换行但存在字面 '\\n' 时保守转换为真实换行
        - 去除常见 REPL/Notebook 提示符（>>> / In[n]:）
        - textwrap.dedent + 去首尾空行，保证以单个换行结尾
        """
        if raw is None:
            return "\n"
        if not isinstance(raw, str):
            raw = str(raw)

        txt = raw

        # 1) 优先提取 fenced code 中的内容
        txt = cls.strip_fences_outer_or_all(txt)

        # 2) 去掉 UTF-8 BOM
        txt = txt.lstrip("\ufeff")

        # 3) 规范行尾：CRLF/CR -> LF
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")

        # 4) 保守处理字面转义的换行/制表
        if "\\n" in txt and "\n" not in txt:
            txt = txt.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        else:
            # 如果文本中既有真实换行也有字面 \r\n，做有限的替换以清理
            if "\\r\\n" in txt:
                txt = txt.replace("\\r\\n", "\n")

        # 5) 去交互式提示符（多行模式）
        txt = cls.PY_REPL_PROMPT_RE.sub("", txt)
        txt = cls.NB_IN_PROMPT_RE.sub("", txt)

        # 6) dedent + 去首尾额外换行，确保以单个 '\n' 结尾
        txt = textwrap.dedent(txt).strip("\n") + "\n"
        return txt

    # --- 在 class TextSanitizer 内新增一个安全解码的小工具 ---
    @classmethod
    def _decode_string_with_json_if_needed(cls, s: str) -> str:
        """
        使用 JSON 解码处理字符串中的转义（\\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX \\xHH），
        在不破坏中文（非 ASCII）的前提下还原。
        方案说明：
        - 先用 fix_invalid_json_escapes 处理非法单反斜杠，避免 json.loads 报错；
        - 再把处理后的内容包在双引号内交给 json.loads，能正确解析常见转义序列且保留 Unicode。
        - 若解析失败，返回原字符串（保守策略）。
        """
        if not isinstance(s, str) or not s:
            return ""
        s2 = cls.fix_invalid_json_escapes(s)
        try:
            return json.loads(f'"{s2}"')
        except Exception:
            # 解析失败时保守返回原样，避免破坏中文或其他 Unicode 内容
            return s

    # --- 替换原有 extract_code_from_arguments 方法 ---
    @classmethod
    def extract_code_from_arguments(cls, args_raw: str) -> str:
        """
        从 tools.arguments（通常是 LLM 返回的字符串）中尽可能安全地抽取 code 字段。
        逻辑顺序：
        1) 尝试严格的 json.loads（首选，不会破坏 Unicode）；
        2) 宽松匹配 "code": "..." ，根据内容类型采取不同处理策略：
           - 纯 ASCII 且仅含字面转义（\\n/\\t 等）时做轻量替换；
           - 含非 ASCII（如中文）且含转义时使用 JSON 解码恢复转义；
           - 仅含标准转义时同样用 JSON 解码；
        3) 匹配 code: xxx 的单行键值对作为最后兜底；
        4) 若均未命中，返回剥围栏后的原文（保守）。
        """
        stripped = cls.strip_fences_outer_or_all(args_raw or "")

        # 1) 尝试严格 JSON（首选路径，零损伤）
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and "code" in obj:
                val = obj.get("code", "")
                return val if isinstance(val, str) else str(val)
        except Exception:
            pass

        # 2) 宽松 JSON: 匹配 "code": "...."
        m2 = cls.QUOTED_CODE_RE.search(stripped)
        if m2:
            s = m2.group(1)

            # 2.1 纯 ASCII 且明显只是字面转义（无真实换行/制表、含 \n/\r\n/\t）
            #     这种情况只做轻量替换，不做 unicode_escape，避免误伤多字节字符
            if cls.looks_like_literal_escapes(s):
                return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")

            # 2.2 含非 ASCII（可能有中文）且带转义：用 JSON 解码最稳妥
            has_non_ascii = any(ord(ch) > 127 for ch in s)
            has_escapes = bool(re.search(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\[\"\\/bfnrt]", s))
            if has_non_ascii and has_escapes:
                return cls._decode_string_with_json_if_needed(s)

            # 2.3 仅有标准转义（即便是 ASCII），也用 JSON 解码统一处理
            if has_escapes:
                return cls._decode_string_with_json_if_needed(s)

            # 2.4 没有任何需要处理的转义，原样返回
            return s

        # 3) 其它兜底：code: xxx 单行键值
        m3 = cls.CODE_KV_LINE_RE.search(stripped)
        if m3:
            return m3.group(1).strip().strip('"').strip("'")

        # 4) 最后兜底：去栅栏后的原文
        return stripped

    # -----------------------
    # Markdown 图片路径解析与校验（支持嵌套括号与 <...> 包裹）
    # -----------------------
    @classmethod
    def extract_markdown_image_paths(cls, text: str) -> List[str]:
        """
        更稳健地解析 Markdown 图片：
        - 解析 '![alt](...)'，支持嵌套括号
        - 处理 <...> 包裹、可选 "title"
        - 返回路径列表，按出现顺序
        """
        if not text:
            return []
        paths: List[str] = []
        i, L = 0, len(text)
        while True:
            start = text.find("![", i)
            if start == -1:
                break
            close_br = text.find("]", start + 2)
            if close_br == -1:
                break
            # 必须紧跟 '(' 才是图片语法
            if close_br + 1 >= L or text[close_br + 1] != "(":
                i = close_br + 1
                continue

            p = close_br + 2
            depth = 0
            end = -1
            # 支持嵌套括号寻找匹配的 ')'
            while p < L:
                ch = text[p]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        end = p
                        break
                    else:
                        depth -= 1
                p += 1
            if end == -1:
                break

            raw_inside = text[close_br + 2 : end].strip()
            path = raw_inside
            # 处理 <...> 包裹
            if path.startswith("<") and path.endswith(">"):
                path = path[1:-1].strip()
            else:
                # 去掉可选 title (例如: path "title")
                qpos = path.find('"')
                if qpos == -1:
                    qpos = path.find("'")
                if qpos != -1:
                    candidate = path[:qpos].strip()
                    if candidate:
                        path = candidate
            # 去两端引号和空白
            path = path.strip().strip('"').strip("'").strip()

            if path:
                paths.append(path)

            i = end + 1

        return paths

    @classmethod
    def normalize_relpath(cls, p: str) -> str:
        """规范化相对路径（去掉开头 ./ 或 /，去首尾空白）"""
        if not p:
            return ""
        p = p.strip()
        if p.startswith("./"):
            p = p[2:]
        if p.startswith("/"):
            p = p[1:]
        return p

    @classmethod
    def is_allowed_image_prefix(
        cls,
        p: str,
        allowed_prefixes: Tuple[str, ...] = (
            "eda/figures/",
            "sensitivity_analysis/figures/",
        ),
        allow_ques_prefix: bool = True,
    ) -> bool:
        """
        路径前缀校验：允许固定白名单前缀；可选允许 quesN/figures/
        """
        if not p:
            return False
        p = cls.normalize_relpath(p)
        if any(p.startswith(pref) for pref in allowed_prefixes):
            return True
        return bool(cls.QUES_FIG_PREFIX_RE.match(p)) if allow_ques_prefix else False

    # -----------------------
    # 其它小工具
    # -----------------------
    @staticmethod
    def looks_like_literal_escapes(s: str) -> bool:
        """
        仅当字符串是 ASCII 且包含典型字面转义（\\n/\\r\\n/\\t），且没有真实换行/制表时返回 True。
        保证不对含中文等多字节字符的字符串返回 True（避免误解码）。
        """
        if not isinstance(s, str) or not s:
            return False
        try:
            s.encode("ascii")
        except UnicodeEncodeError:
            return False
        has_literal = ("\\n" in s) or ("\\r\\n" in s) or ("\\t" in s)
        no_real = ("\n" not in s) and ("\r\n" not in s) and ("\t" not in s)
        # 代码栅栏中不应该进行反解
        return has_literal and no_real and ("```" not in s)
