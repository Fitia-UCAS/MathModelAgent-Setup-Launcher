# app/tools/json_fixer.py

from __future__ import annotations
import json
import re
from typing import Optional, Tuple, Any, TYPE_CHECKING

from app.tools.text_sanitizer import TextSanitizer as TS

# 只有在类型检查时才导入 LLM，避免运行时循环导入
if TYPE_CHECKING:
    from app.core.llm.llm import LLM  # pragma: no cover

JSON_FIXER_SYSTEM_PROMPT = (
    "你是严格的 JSON 修复器。\n"
    "要求：\n"
    "1) 仅输出一个 JSON 对象，不能包含解释或额外文本；\n"
    "2) 保证是合法 JSON（双引号、转义符正确），能被 Python json.loads 解析；\n"
    "3) 类型必须是对象（dict），不要数组或多对象。"
)


class JsonFixer:
    """提取 + 修复 + 解析。一旦失败可用 LLM 重建，再做本地兜底。"""

    @staticmethod
    def _escape_raw_newlines_in_json_strings(s: str) -> str:
        """
        仅把 JSON **字符串字面量内部**的真实换行('\n')和回车('\r')替换为 '\\n'。
        不影响字符串外部的换行（JSON 语法允许作为空白）。
        """
        out = []
        in_str = False
        esc = False
        for ch in s:
            if in_str:
                if esc:
                    # 处理「反斜杠 + 真实换行」为 '\\n'
                    if ch == "\n" or ch == "\r":
                        out.append("\\n")
                    else:
                        out.append(ch)
                    esc = False
                else:
                    if ch == "\\":
                        out.append(ch)
                        esc = True
                    elif ch == '"':
                        out.append(ch)
                        in_str = False
                    elif ch == "\n" or ch == "\r":
                        out.append("\\n")
                    else:
                        out.append(ch)
            else:
                out.append(ch)
                if ch == '"':
                    in_str = True
        return "".join(out)

    @staticmethod
    def _force_double_backslashes_in_strings(s: str) -> str:
        r"""
        兜底策略：在 JSON **字符串字面量内部**，把“单个反斜杠”强制双写成 '\\\\'，
        但**保留**以下合法 JSON 转义不再重复转义：
            \"  \\  \/  \b  \f  \n  \r  \t  \uXXXX
        其余如 \left \right \text \quad \( \) \- 等等，统统变成 \\left \\right ...
        仅在前置修复与 LLM 修复都失败时才采用。
        """
        out = []
        in_str = False
        i = 0
        L = len(s)
        while i < L:
            ch = s[i]
            if not in_str:
                out.append(ch)
                if ch == '"':
                    in_str = True
                i += 1
                continue

            # in_str == True
            if ch == '"':
                out.append(ch)
                in_str = False
                i += 1
                continue

            if ch != "\\":
                out.append(ch)
                i += 1
                continue

            # ch == "\\"
            if i + 1 >= L:
                # 字符串末尾的孤立反斜杠 -> 双写避免非法
                out.append("\\\\")
                i += 1
                continue

            nxt = s[i + 1]

            # 情况 A：双反斜杠开头（表示字面一个反斜杠）
            if nxt == "\\":
                # 保留现状（它已经代表字面一个反斜杠）
                out.append("\\\\")
                i += 2
                continue

            # 情况 B：合法 JSON 转义：\" \/ \b \f \n \r \t
            if nxt in ['"', "/", "b", "f", "n", "r", "t"]:
                out.append("\\" + nxt)
                i += 2
                continue

            # 情况 C：\uXXXX（共 6 个字符）
            if nxt == "u" and i + 5 < L and re.match(r"u[0-9a-fA-F]{4}", s[i + 1 : i + 6]):
                out.append(s[i : i + 6])
                i += 6
                continue

            # 其它情况：一律强制双写，避免 Invalid \escape
            out.append("\\\\")
            out.append(nxt)
            i += 2

        return "".join(out)

    @staticmethod
    def _try_parse(json_str: str) -> Optional[Any]:
        try:
            return json.loads(json_str)
        except Exception:
            return None

    @staticmethod
    def _fallback_regex(json_str: str) -> Optional[Any]:
        """
        宽松兜底（顺序重要）：
        1) 去尾逗号（,} / ,]）
        2) 单引号 -> 双引号
        3) **字符串内部**把所有单反斜杠强制双写（保留合法转义不重复）
        """
        safe = re.sub(r",\s*}", "}", json_str)
        safe = re.sub(r",\s*]", "]", safe)
        safe = safe.replace("'", '"')
        safe = JsonFixer._force_double_backslashes_in_strings(safe)
        try:
            return json.loads(safe)
        except Exception:
            return None

    @classmethod
    def _local_first_pass(cls, raw: str) -> Tuple[Optional[str], str]:
        """
        本地第一阶段：清洗围栏/控制字符 → 提取首个 JSON → 修复非法转义
                   → 处理“行尾反斜杠+换行” → 处理字符串内裸换行
        返回 (json_str 或 None, 阶段标签)
        """
        if not raw:
            return None, "empty"

        # 1) 清理围栏与控制字符（保留换行/制表）
        content = TS.strip_fences_outer_or_all(TS.clean_control_chars(raw, keep_whitespace=True))

        # 2) 提取首个配平 JSON（栈法）
        json_str = TS.extract_first_json_block(content, strip_fences_first=False)
        if not json_str:
            return None, "not_found"

        # 3) 修复 JSON 中的非法反斜杠转义（如 LaTeX 的 \text 之类）
        if hasattr(TS, "fix_invalid_json_escapes"):
            json_str = TS.fix_invalid_json_escapes(json_str)
        else:
            json_str = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", json_str)

        # 3.5) 额外兜底：把“反斜杠+行末换行”直接规范为 \\n，避免行尾反斜杠破坏字符串边界
        json_str = re.sub(r"\\\r?\n", r"\\n", json_str)

        # 4) 把“字符串内部”的真实换行/回车转为 \\n，避免 json.loads 因为裸换行失败
        json_str = cls._escape_raw_newlines_in_json_strings(json_str)

        return json_str, "prepared"

    @classmethod
    async def fix_and_parse(
        cls,
        raw: str,
        llm: Optional["LLM"] = None,
        agent_name: str = "JsonFixer",
    ) -> Tuple[Optional[dict], str]:
        json_str, stage = cls._local_first_pass(raw)
        if not json_str:
            return None, f"fail:{stage}"

        # ① 本地直接解析
        obj = cls._try_parse(json_str)
        if isinstance(obj, dict):
            return obj, "parsed"

        # ② 有 LLM → 让模型按严格约束重建（静默，不发布）
        if llm is not None:
            fix_history = [
                {"role": "system", "content": JSON_FIXER_SYSTEM_PROMPT},
                {"role": "user", "content": json_str},
            ]
            fix_resp = await llm.chat(
                history=fix_history,
                agent_name="JsonFixerInternal",
                sub_title="JsonFixer",
                publish=False,  # 关键：不发布，仅拿返回内容
            )
            fixed_raw = getattr(fix_resp.choices[0].message, "content", "") or ""
            fixed = TS.strip_fences_outer_or_all(fixed_raw)
            fixed_json = TS.extract_first_json_block(fixed, strip_fences_first=False)
            if fixed_json:
                if hasattr(TS, "fix_invalid_json_escapes"):
                    fixed_json = TS.fix_invalid_json_escapes(fixed_json)
                else:
                    fixed_json = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", fixed_json)
                fixed_json = re.sub(r"\\\r?\n", r"\\n", fixed_json)
                fixed_json = cls._escape_raw_newlines_in_json_strings(fixed_json)

                obj = cls._try_parse(fixed_json)
                if isinstance(obj, dict):
                    return obj, "llm_fixed"

                obj = cls._fallback_regex(fixed_json)
                if isinstance(obj, dict):
                    return obj, "llm_fallback_parsed"

        # ③ 无 LLM 或仍失败 → 本地宽松兜底
        obj = cls._fallback_regex(json_str)
        if isinstance(obj, dict):
            return obj, "fallback_parsed"

        return None, "error:unparseable"
