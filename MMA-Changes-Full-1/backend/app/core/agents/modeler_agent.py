# app/core/agents/modeler_agent.py

import json
import time
import re
from pathlib import Path
from icecream import ic

from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import MODELER_PROMPT
from app.schemas.A2A import CoordinatorToModeler, ModelerToCoder
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager  # 右侧面板
from app.schemas.response import ModelerMessage  # 右侧“建模手册”

# 1 全局设置
# 1.1 严格模式：仅接受严格 JSON 对象（dict）
# 1.2 轻度清洗：只移除控制字符、剥掉最外层围栏，不改写语义
STRICT_JSON_ONLY = True
LIGHT_CLEANING = True

# 2 文本处理工具
# 2.1 轻清洗：去控制字符、去最外层围栏
from app.tools.text_sanitizer import TextSanitizer as TS

# 2.2 JSON 提取/修复/解析（严格模式下不调用 LLM 重建）
from app.tools.json_fixer import JsonFixer


# 3 Agent 实现
# 3.1 初始化与构造器
class ModelerAgent(Agent):
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 600,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = MODELER_PROMPT

    # 3.2 主流程
    async def run(self, coordinator_to_modeler: CoordinatorToModeler) -> ModelerToCoder:
        """
        3.2.1 注入 system（一次性）
        3.2.2 将 questions 作为 JSON 文本喂给模型（内容层面一次 dumps）
        3.2.3 轻清洗模型输出（不改写语义）
        3.2.4 严格解析为 JSON 对象（dict），失败直接抛错
        3.2.5 成功后发布到右侧面板（仅一次 dumps），并返回给 Coder（Python 对象）
        """
        # 3.2.1 注入 system（只注入一次，避免重复堆叠）
        if not self._inited:
            await self.append_chat_history({"role": "system", "content": self.system_prompt})
            self._inited = True

        # 3.2.2 将 questions 作为 JSON 文本喂给模型
        await self.append_chat_history(
            {
                "role": "user",
                "content": json.dumps(coordinator_to_modeler.questions, ensure_ascii=False),
            }
        )

        # 3.2.3 调用模型（无需工具）
        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,
        )

        raw_content = getattr(response.choices[0].message, "content", "") or ""
        logger.info(f"[ModelerAgent] raw preview: {raw_content[:2000]}")

        # 3.2.4 轻清洗（不改变语义）
        content = raw_content
        if LIGHT_CLEANING:
            content = TS.clean_control_chars(content, keep_whitespace=True)
            content = TS.strip_fences_outer_or_all(content)

        # 3.2.5 严格解析（严格模式下禁用 LLM 重建）
        questions_solution, stage = await JsonFixer.fix_and_parse(
            raw=content,
            llm=None if STRICT_JSON_ONLY else self.model,
            agent_name=f"{self.__class__.__name__}.JsonFixer",
        )
        logger.info(f"[ModelerAgent] JsonFixer stage: {stage}")

        # 3.2.6 严格校验
        if questions_solution is None:
            raise ValueError("""JSON 解析失败：请仅输出严格 JSON 对象（不要说明/围栏/多余文本）。""")
        if not isinstance(questions_solution, dict):
            raise ValueError("""解析结果不是 JSON 对象（dict）。请仅输出一个对象字面量。""")

        ic(questions_solution)

        # 3.2.7 发布到右侧面板（仅一次 dumps，供前端 JSON.parse）
        content_str = json.dumps(questions_solution, ensure_ascii=False)
        try:
            mm_kwargs = {
                "msg_type": "agent",
                "agent_type": "ModelerAgent",
                "content": content_str,  # 仅此处 dumps
                "subtitle": "建模手册",
                "timestamp": int(time.time()),
            }
            mm = ModelerMessage(**mm_kwargs)
            await redis_manager.publish_message(self.task_id, mm)
            logger.info(f"[ModelerAgent] published via ModelerMessage preview: {str(mm_kwargs)[:2000]}")
        except Exception:
            logger.exception("[ModelerAgent] 使用 ModelerMessage 发布失败，降级为 dict payload。")
            payload = {
                "msg_type": "agent",
                "agent_type": "ModelerAgent",
                "content": content_str,  # 同上，仅一次 dumps
                "subtitle": "建模手册",
                "timestamp": int(time.time()),
            }
            try:
                await redis_manager.publish_message(self.task_id, payload)
                logger.info(f"[ModelerAgent] published via fallback dict preview: {str(payload)[:2000]}")
            except Exception:
                logger.exception("[ModelerAgent] 降级 dict 发布失败，写入回退文件。")
                try:
                    Path("logs/modeler_publish_failures").mkdir(parents=True, exist_ok=True)
                    fn = f"logs/modeler_publish_failures/{self.task_id}-{int(time.time())}.json"
                    with open(fn, "w", encoding="utf-8") as wf:
                        wf.write(json.dumps(payload, ensure_ascii=False, indent=2))
                    logger.warning(f"[ModelerAgent] fallback written: {fn}")
                except Exception:
                    logger.exception("[ModelerAgent] 写入 fallback 文件失败")

        # 3.2.8 返回给 Coder（保持为 Python 对象，不 stringify）
        return ModelerToCoder(questions_solution=questions_solution)
