# app/core/agents/writer_agent.py

import json
import uuid
import re
import time
from typing import List, Tuple
from icecream import ic

from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import get_writer_prompt
from app.schemas.enums import CompTemplate, FormatOutPut
from app.tools.openalex_scholar import OpenAlexScholar, paper_to_footnote_tuple
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager
from app.schemas.response import SystemMessage, WriterMessage
from app.core.functions import writer_tools  # 预留：当前 WriterAgent 不暴露工具
from app.schemas.A2A import WriterResponse

# 1 全局设置
# 1.1 与其他 Agent 保持一致的“轻清洗”思路（去控制字符、剥外层围栏）
from app.tools.text_sanitizer import TextSanitizer as TS


# 2 工具函数
# 2.1 兼容对象/字典的嵌套取值（先 getattr，再 dict.get）
def _dig(obj, *keys, default=None):
    cur = obj
    for k in keys:
        if cur is None:
            return default
        try:
            cur = getattr(cur, k)
            continue
        except Exception:
            pass
        try:
            if isinstance(cur, dict):
                cur = cur.get(k, default)
                continue
        except Exception:
            return default
        return default
    return cur if cur is not None else default


# 3 Agent 实现
# 3.1 角色与策略：
#     3.1.1 写作手不调用外部工具，只产出 Markdown 正文
#     3.1.2 对模型输出做轻清洗（控制字符、围栏），不改写语义内容
#     3.1.3 校验图片引用路径：仅允许来自可用图片清单，且每张只引用一次
class WriterAgent(Agent):
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

        # 3.1.4 图片校验正则
        self._img_regex = re.compile(r"!\[.*?\]\((.*?)\)")
        self._allowed_prefix_re = re.compile(r"^(eda|sensitivity_analysis|ques\d+)/figures/")

    # 3.2 主流程
    async def run(
        self,
        prompt: str,
        available_images: list[str] = None,
        sub_title: str = None,
    ) -> WriterResponse:
        logger.info(f"WriterAgent subtitle: {sub_title}")

        # 3.2.1 首轮注入 system
        if self.is_first_run:
            self.is_first_run = False
            await self.append_chat_history({"role": "system", "content": self.system_prompt})

        # 3.2.2 注入可用图片清单到上下文（仅文本提示）
        if available_images:
            self.available_images = available_images
            image_list = "\n".join(available_images)
            prompt = f"""{prompt}
可用图片清单（仅可引用下列图片，且每张图片在整篇只可引用一次）：
{image_list}

写作要求：
1. 仅从上述清单中选择图片，且每张只引用一次。
2. 必须使用图片的相对路径，示例：`![说明](ques2/figures/fig_model_performance.png)`。
"""
            logger.info(f"image_prompt prepared with {len(available_images)} images")
        else:
            self.available_images = []

        # 3.2.3 增加轮次并写入 user
        self.current_chat_turns += 1
        await self.append_chat_history({"role": "user", "content": prompt})

        # 3.2.4 禁用工具，直接生成正文
        response = await self.model.chat(
            history=self.chat_history,
            tools=[],  # 不暴露任何工具
            tool_choice=None,  # 不允许选择工具
            agent_name=self.__class__.__name__,
            sub_title=sub_title,
        )

        footnotes: List[Tuple[str, str]] = []

        assistant_msg_obj = response.choices[0].message
        assistant_content_raw = getattr(assistant_msg_obj, "content", "") or ""
        assistant_tool_calls = getattr(assistant_msg_obj, "tool_calls", None)

        # 3.2.5 轻清洗（控制字符 → 常见瑕疵 → 外层围栏）
        assistant_content_clean = TS.clean_control_chars(assistant_content_raw, keep_whitespace=True)
        assistant_content_clean = TS.normalize_common_glitches(assistant_content_clean)
        assistant_content_clean = TS.strip_fences_outer_or_all(assistant_content_clean)

        # 3.2.6 若模型仍产生 tool_calls：与 id 成对响应后忽略，再催促直接给正文
        if assistant_tool_calls:
            logger.info("WriterAgent 收到工具调用（已禁用，将成对配对后忽略）")

            await self.append_chat_history(
                {"role": "assistant", "content": assistant_content_clean, "tool_calls": assistant_tool_calls}
            )

            for tc in assistant_tool_calls:
                tc_id = _dig(tc, "id")
                fn_name = _dig(tc, "function", "name") or "unknown"
                await self.append_chat_history(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": """WriterAgent: 所有外部工具已禁用，忽略此次调用。""",
                    }
                )

            if not assistant_content_clean.strip():
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(
                        content="""写作手已禁用工具：请直接返回完整正文（不要再调用任何工具）。""", type="info"
                    ),
                )
                await self.append_chat_history(
                    {
                        "role": "user",
                        "content": """工具已禁用。请直接输出完整的最终文章（纯文本 Markdown），不要包含任何工具调用或多余说明。""",
                    }
                )
                retry_resp = await self.model.chat(
                    history=self.chat_history,
                    tools=[],
                    tool_choice=None,
                    agent_name=self.__class__.__name__,
                    sub_title=sub_title,
                )
                retry_raw = getattr(retry_resp.choices[0].message, "content", "") or ""
                assistant_content_clean = TS.strip_fences_outer_or_all(
                    TS.normalize_common_glitches(TS.clean_control_chars(retry_raw, keep_whitespace=True))
                )
                await self.append_chat_history({"role": "assistant", "content": assistant_content_clean})
        else:
            await self.append_chat_history({"role": "assistant", "content": assistant_content_clean})

        response_content = assistant_content_clean or ""

        # 3.2.7 图片引用校验与纠错迭代
        max_fix_attempts = 5
        attempt = 0
        while attempt <= max_fix_attempts:
            img_paths = self._extract_image_paths(response_content)
            invalids, duplicates = self._validate_image_paths(img_paths)

            if not invalids and not duplicates:
                logger.info("WriterAgent: 图片引用校验通过")
                break

            attempt += 1
            lines = []
            if invalids:
                lines.append("以下图片引用不在可用图片清单或路径前缀不合法：")
                for p in invalids:
                    lines.append(f"  - {p}")
            if duplicates:
                lines.append("以下图片被重复引用（每张图片只能引用一次）：")
                for p in duplicates:
                    lines.append(f"  - {p}")
            error_msg = "\n".join(lines)
            logger.warning(f"图片引用校验未通过（尝试 {attempt}/{max_fix_attempts}）：\n{error_msg}")

            level = "warning" if attempt < max_fix_attempts else "error"
            await redis_manager.publish_message(
                self.task_id, SystemMessage(content=f"""写作校验：图片引用问题，{error_msg}""", type=level)
            )

            correction_prompt = f"""检测到图片引用不合规。请根据可用图片清单修正文章中的图片引用：
                                    1. 仅从下列可用图片中选择并使用（每张图片只能引用一次）：
                                    {'\n'.join(self.available_images or [])}

                                    2. 对于不在清单中的引用：必须删除整行引用，或替换为清单中的合法图片。
                                    3. 对于重复引用：只保留第一次引用，其余删除。

                                    请仅返回修正后的完整文章（纯文本，不要包含额外说明）。
                                """
            await self.append_chat_history({"role": "user", "content": correction_prompt})

            fix_resp = await self.model.chat(
                history=self.chat_history,
                tools=[],
                tool_choice=None,
                agent_name=self.__class__.__name__,
                sub_title=sub_title,
            )
            fix_assistant_raw = getattr(fix_resp.choices[0].message, "content", "") or ""
            fix_assistant = TS.strip_fences_outer_or_all(
                TS.normalize_common_glitches(TS.clean_control_chars(fix_assistant_raw, keep_whitespace=True))
            )
            await self.append_chat_history({"role": "assistant", "content": fix_assistant})
            response_content = fix_assistant

        # 3.2.8 返回（正文与脚注占位，脚注格式保留为 List[Tuple[str, str]]）
        return WriterResponse(response_content=response_content, footnotes=footnotes)

    # 4 图片工具
    # 4.1 提取 Markdown 图片路径
    def _extract_image_paths(self, text: str) -> List[str]:
        if not text:
            return []
        matches = self._img_regex.findall(text)
        return [m.strip() for m in matches if m and isinstance(m, str)]

    # 4.2 校验路径前缀与重复引用
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
            # 4.2.1 前缀合法性
            if not self._allowed_prefix_re.match(p):
                invalids.append(p)
                continue
            # 4.2.2 若提供了可用清单，则必须在清单中
            if allowed_set and p not in allowed_set:
                invalids.append(p)

        return invalids, duplicates
