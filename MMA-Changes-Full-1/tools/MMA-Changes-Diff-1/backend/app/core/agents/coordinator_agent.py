from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import COORDINATOR_PROMPT
import json
import re
from app.utils.log_util import logger
from app.schemas.A2A import CoordinatorToModeler


def _cleanup_control_chars(s: str) -> str:
    """去除会导致 json.loads 失败的控制字符"""
    return re.sub(r"[\x00-\x1F\x7F]", "", s or "")


def _strip_fences(s: str) -> str:
    """去掉常见代码栅栏"""
    s = s or ""
    return s.replace("```json", "").replace("```", "").strip()


def _extract_first_json_block(s: str) -> str:
    """
    用栈法提取首个 {...} JSON 块，比正则 {.*} 更稳健。
    - 支持跳过字符串中的花括号
    - 找不到时返回去栅栏后的原文（让后续报错更可读）
    """
    if not s:
        return ""
    s = _strip_fences(s)
    start = s.find("{")
    if start == -1:
        return s.strip()
    stack = []
    in_str = False
    esc = False
    for i, ch in enumerate(s[start:], start):
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
                    return s[start : i + 1].strip()
    # 如果栈没清空，返回原文（让上层抛 JSONDecodeError）
    return s.strip()


def _normalize_common_glitches(s: str) -> str:
    """
    规范常见“小毛病”：
    1) "qu es2" -> "ques2"
    2) 去栅栏
    """
    s = _strip_fences(s)
    s = re.sub(r'"qu\s+es(\d+)"', r'"ques\1"', s)  # "qu es2" -> "ques2"
    return s.strip()


class CoordinatorAgent(Agent):
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 100,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = COORDINATOR_PROMPT

    async def run(self, ques_all: str) -> CoordinatorToModeler:
        """用户输入问题 使用LLM 格式化 questions"""
        # 只注入一次 system，避免重复堆叠
        if not self._inited:
            await self.append_chat_history({"role": "system", "content": self.system_prompt})
            self._inited = True

        # 用户问题作为 user 消息
        await self.append_chat_history({"role": "user", "content": ques_all})

        # 调用模型
        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,  # LLM 层会做统一 metadata 处理
        )
        raw_text = getattr(response.choices[0].message, "content", "") or ""

        # 1) 基础清理
        raw_text = _cleanup_control_chars(raw_text)

        # 2) 提取 JSON 主体（栈法）
        json_text = _extract_first_json_block(raw_text)

        # 3) 规范常见问题
        json_text = _normalize_common_glitches(json_text)

        if not json_text:
            raise ValueError("返回的 JSON 字符串为空，请检查模型输出或上游提示。")

        # 4) 解析为对象
        try:
            questions = json.loads(json_text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误，原始字符串: {json_text}")
            raise ValueError(f"JSON 解析错误: {e}") from e

        # 5) 兜底：确保 ques_count 存在且为 int
        ques_count = questions.get("ques_count")
        if not isinstance(ques_count, int):
            ques_keys = [k for k in questions.keys() if re.fullmatch(r"ques\d+", k)]
            if not ques_keys:
                raise ValueError("缺少 ques_count 且未找到任何 quesN 键。")
            ques_count = max(int(k[4:]) for k in ques_keys)
            questions["ques_count"] = ques_count

        logger.info(f"questions:{questions}")

        # 6) 返回给后续的 ModelerAgent
        return CoordinatorToModeler(questions=questions, ques_count=ques_count)
