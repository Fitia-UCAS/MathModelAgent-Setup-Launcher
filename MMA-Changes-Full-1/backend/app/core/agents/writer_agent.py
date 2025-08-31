from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import get_writer_prompt
from app.schemas.enums import CompTemplate, FormatOutPut
from app.tools.openalex_scholar import OpenAlexScholar, paper_to_footnote_tuple
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager
from app.schemas.response import SystemMessage, WriterMessage
import json
import uuid
from app.core.functions import writer_tools
from icecream import ic
from app.schemas.A2A import WriterResponse
import re
from typing import List, Tuple


class WriterAgent(Agent):
    """
    写作手：
    - 统一使用 OpenAI 兼容的工具流：
      assistant.tool_calls -> 工具结果消息（role="tool", tool_call_id, content）
    - 对工具返回进行体积限制，避免超长上下文
    - 图片引用校验与自动纠错迭代
    """

    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 600,
        comp_template: CompTemplate = CompTemplate,
        format_output: FormatOutPut = FormatOutPut.Markdown,
        scholar: OpenAlexScholar = None,
        max_memory: int = 100,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns, max_memory)
        self.format_out_put = format_output
        self.comp_template = comp_template
        self.scholar = scholar
        self.is_first_run = True
        self.system_prompt = get_writer_prompt(format_output)
        self.available_images: List[str] = []

        # 图片校验
        self._img_regex = re.compile(r"!\[.*?\]\((.*?)\)")
        self._allowed_prefixes = ("eda/figures/", "sensitivity_analysis/figures/")

    async def run(
        self,
        prompt: str,
        available_images: list[str] = None,
        sub_title: str = None,
    ) -> WriterResponse:
        logger.info(f"WriterAgent subtitle: {sub_title}")

        # 首次注入 system
        if self.is_first_run:
            self.is_first_run = False
            await self.append_chat_history({"role": "system", "content": self.system_prompt})

        # 注入可用图片清单（仅作为上下文）
        if available_images:
            self.available_images = available_images
            image_list = "\n".join(available_images)
            prompt = (
                prompt
                + "\n可用图片清单（仅可引用下列图片，且每张图片在整篇只可引用一次）：\n"
                + image_list
                + "\n\n写作时请严格使用这些图片的相对路径（示例：`![说明](ques2/figures/fig_model_performance.png)`），且不要重复引用同一张图片。"
            )
            logger.info(f"image_prompt prepared with {len(available_images)} images")

        # 轮次 + user
        self.current_chat_turns += 1
        await self.append_chat_history({"role": "user", "content": prompt})

        # 禁用工具暴露（不允许任何外部工具被调用）
        response = await self.model.chat(
            history=self.chat_history,
            tools=[],          # 不暴露任何工具
            tool_choice=None,  # 不允许选择工具
            agent_name=self.__class__.__name__,
            sub_title=sub_title,
        )

        # 从源头即使用严格类型：List[Tuple[str, str]]
        footnotes: List[Tuple[str, str]] = []
        assistant_msg_obj = response.choices[0].message
        assistant_content = getattr(assistant_msg_obj, "content", "") or ""
        assistant_tool_calls = getattr(assistant_msg_obj, "tool_calls", None)

        # 若模型仍“幻想”生成了 tool_calls，记录并忽略，继续正文
        if assistant_tool_calls:
            logger.info("WriterAgent 收到工具调用（已禁用，忽略）")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content="写作手收到工具调用，但已禁用所有外部工具，已忽略。", type="warning"),
            )
            await self.append_chat_history({
                "role": "tool",
                "name": "disabled",
                "tool_call_id": f"call_{uuid.uuid4().hex[:12]}",
                "content": "所有外部工具已禁用，忽略此次调用。",
            })

        # 直接追加 assistant 内容
        await self.append_chat_history({"role": "assistant", "content": assistant_content})

        # 最终文本
        response_content = assistant_content or ""

        # 图片引用校验与纠错迭代
        max_fix_attempts = 100
        attempt = 0
        while attempt <= max_fix_attempts:
            img_paths = self._extract_image_paths(response_content)
            invalids, duplicates = self._validate_image_paths(img_paths)

            if not invalids and not duplicates:
                logger.info("WriterAgent: 图片引用校验通过")
                break

            attempt += 1
            error_lines = []
            if invalids:
                error_lines.append("以下图片引用不在可用图片清单或路径前缀不合法：")
                for p in invalids:
                    error_lines.append(f"  - {p}")
            if duplicates:
                error_lines.append("以下图片被重复引用（每张图片只能引用一次）：")
                for p in duplicates:
                    error_lines.append(f"  - {p}")

            error_msg = "\n".join(error_lines)
            logger.warning(f"图片引用校验未通过（尝试 {attempt}/{max_fix_attempts}）：\n{error_msg}")
            await redis_manager.publish_message(self.task_id, SystemMessage(content=f"写作校验：图片引用问题，{error_msg}", type="error"))

            correction_prompt = (
                "检测到图片引用不合规。请根据可用图片清单修正文章中的图片引用：\n"
                "1. 只从下列可用图片中选择并使用（每张图片只能引用一次）：\n"
                + "\n".join(self.available_images)
                + "\n\n"
                "2. 对于当前不在清单中的引用，请用占位格式替换：\n"
                "   （占位：请在 <合法前缀>/figures/<期望文件名.png> 生成图后替换本段图片引用）\n"
                "3. 对于重复引用，请保留第一次引用并将后续引用替换为占位或删除。\n"
                "请仅返回修正后的完整文章（纯文本，不要包含额外说明）。"
            )

            await self.append_chat_history({"role": "user", "content": correction_prompt})
            # 纠错对话也禁用工具
            fix_resp = await self.model.chat(
                history=self.chat_history,
                tools=[],          # 不暴露任何工具
                tool_choice=None,  # 不允许选择工具
                agent_name=self.__class__.__name__,
                sub_title=sub_title,
            )
            fix_assistant = getattr(fix_resp.choices[0].message, "content", "") or ""
            await self.append_chat_history({"role": "assistant", "content": fix_assistant})
            response_content = fix_assistant

        # 源头即严格类型，无需再归一化
        return WriterResponse(response_content=response_content, footnotes=footnotes)


    # ============== 图片工具 ==============

    def _extract_image_paths(self, text: str) -> List[str]:
        if not text:
            return []
        matches = self._img_regex.findall(text)
        return [m.strip() for m in matches if m and isinstance(m, str)]

    def _validate_image_paths(self, img_paths: List[str]) -> Tuple[List[str], List[str]]:
        invalids: List[str] = []
        duplicates: List[str] = []
        if not img_paths:
            return invalids, duplicates

        counts = {}
        for p in img_paths:
            counts[p] = counts.get(p, 0) + 1

        for p, c in counts.items():
            if c > 1:
                duplicates.append(p)

        allowed_set = set(self.available_images or [])
        for p in counts.keys():
            if p not in allowed_set:
                invalids.append(p)
                continue
            ok_prefix = False
            if p.startswith("eda/figures/") or p.startswith("sensitivity_analysis/figures/"):
                ok_prefix = True
            else:
                if re.match(r"^ques\d+/figures/", p):
                    ok_prefix = True
            if not ok_prefix:
                invalids.append(p)

        return invalids, duplicates
