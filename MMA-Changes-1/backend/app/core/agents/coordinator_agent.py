# app/core/agents/coordinator_agent.py

import json
import re

from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import COORDINATOR_PROMPT
from app.utils.log_util import logger
from app.schemas.A2A import CoordinatorToModeler

# 1 全局设置
# 1.1 严格模式：仅接受严格 JSON 对象（dict）
# 1.2 轻度清洗：只移除控制字符、剥掉最外层围栏，不改写语义
STRICT_JSON_ONLY = True
LIGHT_CLEANING = True

# 2 文本处理工具
# 2.1 统一的文本清洗工具（轻清洗：控制字符、最外层围栏）
from app.tools.text_sanitizer import TextSanitizer as TS

# 2.2 通用 JSON 提取/修复/解析工具（在严格模式下不调用 LLM 重建）
from app.tools.json_fixer import JsonFixer


# 3 Agent 实现
# 3.1 角色：将用户原始问题经 LLM 结构化为 questions(JSON) 并返回给 Modeler
class CoordinatorAgent(Agent):
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 600,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = COORDINATOR_PROMPT

    # 3.2 主流程
    async def run(self, ques_all: str) -> CoordinatorToModeler:
        """
        3.2.1 注入 system（一次性）
        3.2.2 将用户原始问题作为 user 消息交给 LLM
        3.2.3 轻清洗 LLM 文本输出
        3.2.4 严格解析为 JSON 对象（dict），失败直接抛错
        3.2.5 校验/推断 ques_count，返回 CoordinatorToModeler
        """
        # 3.2.1 注入 system（只注入一次，避免重复堆叠）
        if not self._inited:
            await self.append_chat_history({"role": "system", "content": self.system_prompt})
            self._inited = True

        # 3.2.2 用户问题作为 user 消息
        await self.append_chat_history({"role": "user", "content": ques_all})

        # 3.2.3 调用模型（右侧面板的 publish 由 LLM 层处理）
        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,
        )
        raw_text = getattr(response.choices[0].message, "content", "") or ""

        # 3.2.4 轻清洗（仅移除控制字符与最外层围栏；不做规范化重写）
        prepared_text = raw_text
        if LIGHT_CLEANING:
            prepared_text = TS.clean_control_chars(prepared_text, keep_whitespace=True)
            prepared_text = TS.strip_fences_outer_or_all(prepared_text)

        # 3.2.5 严格解析为 JSON（严格模式下禁用 LLM 重建）
        #       JsonFixer.fix_and_parse 在 llm=None 时仅做本地解析/最小修复（如支持）
        questions, stage = await JsonFixer.fix_and_parse(
            raw=prepared_text,
            llm=None if STRICT_JSON_ONLY else self.model,
            agent_name="CoordinatorAgent.JsonFixer",
        )
        logger.info(f"[CoordinatorAgent] JsonFixer stage: {stage}")

        # 3.2.6 解析结果校验
        if questions is None:
            logger.error(f"[CoordinatorAgent] 无法解析为 JSON（strict）。raw_text preview: {raw_text[:2000]}")
            raise ValueError("""JSON 解析错误：请确保仅输出严格 JSON 对象（不含说明/围栏/多余文本）。""")

        if not isinstance(questions, dict):
            raise ValueError("""解析结果不是 JSON 对象（dict）。请仅输出一个对象字面量。""")

        # 3.2.7 ques_count 校验与推断
        ques_count = questions.get("ques_count")
        if not isinstance(ques_count, int):
            ques_keys = [k for k in questions.keys() if re.fullmatch(r"ques\d+", k)]
            if not ques_keys:
                raise ValueError(
                    """缺少 ques_count 且未找到任何 quesN 键。请在 JSON 中包含 ques_count 或 ques1/ques2/...。"""
                )
            ques_count = max(int(k[4:]) for k in ques_keys)
            questions["ques_count"] = ques_count

        # 3.2.8 日志（仅 stringify 预览，不回传给 LLM）
        try:
            logger.info(
                f"[CoordinatorAgent] questions(log preview): {json.dumps(questions, ensure_ascii=False)[:2000]}"
            )
        except Exception:
            logger.info("[CoordinatorAgent] questions(log preview) 无法 stringify，用 str() 代替。")
            logger.info(f"[CoordinatorAgent] questions(str): {str(questions)[:2000]}")

        # 3.2.9 返回给 Modeler（保持为 Python 对象）
        return CoordinatorToModeler(questions=questions, ques_count=ques_count)
