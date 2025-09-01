# app/core/agents/coordinator_agent.py

from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import COORDINATOR_PROMPT
import json
import re
from app.utils.log_util import logger
from app.schemas.A2A import CoordinatorToModeler

# 统一的文本清洗工具
from app.tools.text_sanitizer import TextSanitizer as TS

# 通用 JSON 提取/修复/解析工具
from app.tools.json_fixer import JsonFixer


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
        """
        将用户原始问题经 LLM 结构化为 questions(JSON)：
        1) 调用 LLM 生成结构化回答
        2) 使用 JsonFixer：提取 + 修复非法转义/围栏/坏 JSON + 解析
           - 内置本地兜底与（可选）一次 LLM 重建修复
        3) 校验字段并推断 ques_count
        4) 返回 CoordinatorToModeler
        """
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

        # 基础清理（尽量不破坏可读性；后续 JsonFixer 内部还有一轮清洗与抽取）
        prepared_text = TS.clean_control_chars(raw_text, keep_whitespace=True)
        prepared_text = TS.normalize_common_glitches(prepared_text)

        # 统一走 JsonFixer：一步完成 提取→修复→解析（含一次 LLM 重建与本地兜底）
        questions, stage = await JsonFixer.fix_and_parse(
            raw=prepared_text,
            llm=self.model,  # 若不希望二次调用 LLM 进行“重建修复”，可改为 llm=None
            agent_name="CoordinatorAgent.JsonFixer",
        )
        logger.info(f"[CoordinatorAgent] JsonFixer stage: {stage}")

        if questions is None:
            # 记录原始文本，便于排查
            logger.error(f"[CoordinatorAgent] 无法解析为 JSON。raw_text preview: {raw_text[:500]}")
            raise ValueError(f"JSON 解析错误（{stage}）")

        if not isinstance(questions, dict):
            raise ValueError("解析结果不是 JSON 对象（dict）。")

        # 兜底：确保 ques_count 存在且为 int
        ques_count = questions.get("ques_count")
        if not isinstance(ques_count, int):
            # 自动推断 quesN 键数量
            ques_keys = [k for k in questions.keys() if re.fullmatch(r"ques\d+", k)]
            if not ques_keys:
                raise ValueError("缺少 ques_count 且未找到任何 quesN 键。")
            ques_count = max(int(k[4:]) for k in ques_keys)
            questions["ques_count"] = ques_count

        logger.info(f"[CoordinatorAgent] questions: {json.dumps(questions, ensure_ascii=False)[:800]}")

        # 返回给后续的 ModelerAgent
        return CoordinatorToModeler(questions=questions, ques_count=ques_count)
