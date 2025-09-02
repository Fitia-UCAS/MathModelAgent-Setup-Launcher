# app/tools/text_sanitizer.py

# 1 模块说明
# 1.1 功能：统一的文本/代码清洗与正则工具
# 1.2 支持：控制字符清理、ANSI 去除、代码栅栏剥离、JSON 提取、执行前规范化、图片路径解析等

from __future__ import annotations
import json
import re
import textwrap
from typing import List, Tuple


# 2 TextSanitizer
# 2.1 提供一系列静态方法与正则，用于文本清洗与解析
class TextSanitizer:
    # 2.2 编译正则常量
    CLEAN_CTRL_STRICT_RE = re.compile(r"[\x00-\x1F\x7F]")
    CLEAN_CTRL_RELAXED_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]")
    ANSI_ESCAPE_RE = re.compile(r"(?:\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")
    CODE_FENCE_OUTER_RE = re.compile(r"^\s*```(?:\s*[^\n`]*)?\s*\n(.*)\n```\s*$", re.S)
    CODE_FENCE_ALL_RE = re.compile(r"```(?:\s*[^\n`]*)?\n(.*?)```", re.S)
    PY_REPL_PROMPT_RE = re.compile(r"^\s*>>>\s?", re.M)
    NB_IN_PROMPT_RE = re.compile(r"^\s*In\[\d+\]:\s?", re.M)
    QUOTED_CODE_RE = re.compile(r'"code"\s*:\s*"((?:\\.|[^"\\])*)"', re.S)
    CODE_KV_LINE_RE = re.compile(r"^\s*code\s*:\s*([^\n\r]+)", re.I | re.M)
    QUES_FIG_PREFIX_RE = re.compile(r"^ques\d+/figures/")

    # 3 基础清理
    @classmethod
    def clean_control_chars(cls, s: str, keep_whitespace: bool = True) -> str:
        if s is None:
            return ""
        re_obj = cls.CLEAN_CTRL_RELAXED_RE if keep_whitespace else cls.CLEAN_CTRL_STRICT_RE
        return re_obj.sub("", s)

    @classmethod
    def strip_ansi(cls, s: str) -> str:
        if s is None:
            return ""
        return cls.ANSI_ESCAPE_RE.sub("", s)

    # 4 工具包壳清理
    @classmethod
    def preclean_tool_wrappers(cls, s: str) -> str:
        if not s:
            return ""
        t = s.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
        t = re.sub(r"```json\b", "```", t, flags=re.I)
        t = re.sub(r"(?im)^\s*json\s*$", "", t)
        t = re.sub(r"(?im)\bjson\s*(?=\{)", "", t)
        t = re.sub(r"(?im)json\s*\n\s*json\s*(?=\{)", "", t)
        return t

    # 5 代码栅栏与 JSON 提取
    @classmethod
    def strip_fences_outer_or_all(cls, s: str) -> str:
        if not s:
            return ""
        s = s.strip()
        m = cls.CODE_FENCE_OUTER_RE.match(s)
        if m:
            return m.group(1).strip()
        return cls.CODE_FENCE_ALL_RE.sub(r"\1", s).strip()

    @classmethod
    def extract_first_json_block(cls, s: str, strip_fences_first: bool = True) -> str:
        if not s:
            return ""
        base = cls.strip_fences_outer_or_all(s) if strip_fences_first else s
        text = cls.preclean_tool_wrappers(base)
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
        return ""

    @classmethod
    def fix_invalid_json_escapes(cls, s: str) -> str:
        if not isinstance(s, str) or not s:
            return ""
        return re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", s)

    @classmethod
    def normalize_common_glitches(cls, s: str) -> str:
        if not s:
            return ""
        s = cls.strip_fences_outer_or_all(s)
        s = re.sub(r'"qu\s+es(\d+)"', r'"ques\1"', s)
        s = re.sub(r'"\s*ques\s*(\d+)\s*"', r'"ques\1"', s)
        return s.strip()

    # 6 执行前规范化
    @classmethod
    def normalize_for_execution(cls, raw: str, language: str = "python") -> str:
        if raw is None:
            return "\n"
        if not isinstance(raw, str):
            raw = str(raw)
        txt = cls.strip_fences_outer_or_all(raw)
        txt = cls.preclean_tool_wrappers(txt)
        txt = txt.lstrip("\ufeff")
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        if "\\n" in txt and "\n" not in txt:
            txt = txt.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        else:
            if "\\r\\n" in txt:
                txt = txt.replace("\\r\\n", "\n")
        txt = cls.PY_REPL_PROMPT_RE.sub("", txt)
        txt = cls.NB_IN_PROMPT_RE.sub("", txt)
        txt = textwrap.dedent(txt).strip("\n") + "\n"
        return txt

    # 7 辅助解码
    @classmethod
    def _decode_string_with_json_if_needed(cls, s: str) -> str:
        if not isinstance(s, str) or not s:
            return ""
        s2 = cls.fix_invalid_json_escapes(s)

        def _hex_sub(m):
            try:
                return chr(int(m.group(1), 16))
            except Exception:
                return m.group(0)

        s2 = re.sub(r"\\x([0-9a-fA-F]{2})", _hex_sub, s2)
        try:
            return json.loads(f'"{s2}"')
        except Exception:
            return s

    # 8 从 tools.arguments 中提取 code
    @classmethod
    def extract_code_from_arguments(cls, args_raw: str) -> str:
        stripped = cls.strip_fences_outer_or_all(args_raw or "")
        stripped = cls.preclean_tool_wrappers(stripped)
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and "code" in obj:
                val = obj.get("code", "")
                return val if isinstance(val, str) else str(val)
        except Exception:
            pass
        m2 = cls.QUOTED_CODE_RE.search(stripped)
        if m2:
            s = m2.group(1)
            if cls.looks_like_literal_escapes(s):
                return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
            has_non_ascii = any(ord(ch) > 127 for ch in s)
            has_escapes = bool(re.search(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\[\"\\/bfnrt]", s))
            if has_non_ascii or has_escapes:
                return cls._decode_string_with_json_if_needed(s)
            return s
        m3 = cls.CODE_KV_LINE_RE.search(stripped)
        if m3:
            return m3.group(1).strip().strip('"').strip("'")
        return stripped

    # 9 Markdown 图片路径解析
    @classmethod
    def extract_markdown_image_paths(cls, text: str) -> List[str]:
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
            if close_br + 1 >= L or text[close_br + 1] != "(":
                i = close_br + 1
                continue
            p = close_br + 2
            depth = 0
            end = -1
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
            if path.startswith("<") and path.endswith(">"):
                path = path[1:-1].strip()
            else:
                qpos = path.find('"')
                if qpos == -1:
                    qpos = path.find("'")
                if qpos != -1:
                    candidate = path[:qpos].strip()
                    if candidate:
                        path = candidate
            path = path.strip().strip('"').strip("'").strip()
            if path:
                paths.append(path)
            i = end + 1
        return paths

    @classmethod
    def normalize_relpath(cls, p: str) -> str:
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
        if not p:
            return False
        p = cls.normalize_relpath(p)
        if any(p.startswith(pref) for pref in allowed_prefixes):
            return True
        return bool(cls.QUES_FIG_PREFIX_RE.match(p)) if allow_ques_prefix else False

    # 10 其它小工具
    @staticmethod
    def looks_like_literal_escapes(s: str) -> bool:
        if not isinstance(s, str) or not s:
            return False
        try:
            s.encode("ascii")
        except UnicodeEncodeError:
            return False
        has_literal = ("\\n" in s) or ("\\r\\n" in s) or ("\\t" in s)
        no_real = ("\n" not in s) and ("\r\n" not in s) and ("\t" not in s)
        return has_literal and no_real and ("```" not in s)
