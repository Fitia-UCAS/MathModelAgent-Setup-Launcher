# app/core/agents/modeler_agent.py

from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import MODELER_PROMPT
from app.schemas.A2A import CoordinatorToModeler, ModelerToCoder
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager  # 用于右侧面板
from app.schemas.response import ModelerMessage  # 右侧“建模手册”用
import json
from icecream import ic

# 将正则/清理逻辑集中到 TextSanitizer
from app.tools.text_sanitizer import TextSanitizer as TS
from app.tools.json_fixer import JsonFixer


# === 工具函数（行为与原实现等价，但委托给 TS） ===


def _cleanup_control_chars(s: str) -> str:
    """去掉会导致 json.loads 失败的控制字符"""
    return TS.clean_control_chars(s, keep_whitespace=True)


def _strip_fences(s: str) -> str:
    """去掉 ```json / ``` 围栏"""
    return TS.strip_fences_outer_or_all(s)


def _extract_first_json(text: str) -> str:
    """
    用“栈法”提取首个配平的 JSON 对象字符串，比 {.*} 更稳健：
    """
    if not text:
        return ""
    # 原实现先去掉围栏再做栈法；为了等价，直接让 TS 在外部不重复 strip（我们已在调用处 strip 过）
    # 这里假定传入 text 已为去栅栏后的字符串（与原实现一致）。
    return TS.extract_first_json_block(text, strip_fences_first=False)


def _try_json_loads(s: str):
    """尝试解析 JSON，失败返回 None"""
    try:
        return json.loads(s)
    except Exception:
        return None


class ModelerAgent(Agent):  # 继承自Agent类
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 600,  # 添加最大对话轮次限制
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = MODELER_PROMPT

    async def run(self, coordinator_to_modeler: CoordinatorToModeler) -> ModelerToCoder:
        """
        解析大模型返回的 JSON：
        - 清理 Markdown 围栏与控制字符
        - JsonFixer 提取+修复+解析（含 LLM 重建 & 本地兜底）
        - 严格校验为 dict，成功后发布 ModelerMessage
        """
        # 只注入一次 system
        if not self._inited:
            await self.append_chat_history({"role": "system", "content": self.system_prompt})
            self._inited = True

        await self.append_chat_history(
            {
                "role": "user",
                "content": json.dumps(coordinator_to_modeler.questions, ensure_ascii=False),
            }
        )

        # 调用模型（无需工具）
        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,
        )

        raw_content = getattr(response.choices[0].message, "content", "") or ""
        logger.debug(f"[ModelerAgent] raw preview: {raw_content[:2000]}")

        # Step1: 清理围栏与控制字符
        content = _strip_fences(_cleanup_control_chars(raw_content))

        # Step2: 一步到位：提取 + 修复 + 解析（含 LLM 重建与本地兜底）
        questions_solution, stage = await JsonFixer.fix_and_parse(
            raw=content,
            llm=self.model,  # 允许用当前 LLM 做一次“重建修复”；若不想用，可改为 llm=None
            agent_name=f"{self.__class__.__name__}.JsonFixer",
        )
        logger.info(f"[ModelerAgent] JsonFixer stage: {stage}")

        # Step3: 严格校验
        if questions_solution is None:
            raise ValueError(f"JSON 修复完全失败，无法解析（{stage}）。")
        if not isinstance(questions_solution, dict):
            raise ValueError("解析结果不是 JSON 对象（dict）。")

        ic(questions_solution)

        # Step4: 显式发布结构化 ModelerMessage —— 右侧“建模手册”面板需要这一条
        try:
            await redis_manager.publish_message(
                self.task_id,
                ModelerMessage(content=questions_solution),
            )
        except Exception as e:
            logger.warning(f"发布 ModelerMessage 失败（继续返回给后续流程）: {e}")

        # 返回给下一步（CoderAgent）
        return ModelerToCoder(questions_solution=questions_solution)
