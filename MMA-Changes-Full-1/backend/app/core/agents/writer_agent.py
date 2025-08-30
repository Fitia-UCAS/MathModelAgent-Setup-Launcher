from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import get_writer_prompt
from app.schemas.enums import CompTemplate, FormatOutPut
from app.tools.openalex_scholar import OpenAlexScholar
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
    - 统一使用 OpenAI/DeepSeek 兼容的工具流：
      assistant.tool_calls -> function 响应（role="function", name, tool_call_id, content）
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

        # 首轮生成/或发起工具
        response = await self.model.chat(
            history=self.chat_history,
            tools=writer_tools,
            tool_choice="auto",
            agent_name=self.__class__.__name__,
            sub_title=sub_title,
        )

        footnotes: List[str] = []
        assistant_msg_obj = response.choices[0].message
        assistant_content = getattr(assistant_msg_obj, "content", "") or ""
        assistant_tool_calls = getattr(assistant_msg_obj, "tool_calls", None)

        if assistant_tool_calls:
            logger.info("检测到工具调用 in WriterAgent")

            # 规范化写入 assistant（content 为空可省略该键）
            safe_tool_calls = []
            try:
                for tc in assistant_tool_calls:
                    tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
                    fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else {}) or {}
                    fn_name = (
                        getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None) or "unknown"
                    )
                    raw_args = getattr(fn, "arguments", None) or (fn.get("arguments") if isinstance(fn, dict) else None)
                    if isinstance(raw_args, (dict, list)):
                        fn_args = json.dumps(raw_args, ensure_ascii=False)
                    elif raw_args is None:
                        fn_args = ""
                    else:
                        fn_args = str(raw_args)
                    if not isinstance(tc_id, str) or not tc_id:
                        tc_id = f"call_{uuid.uuid4().hex[:12]}"
                    safe_tool_calls.append({"id": tc_id, "type": "function", "function": {"name": fn_name, "arguments": fn_args}})
            except Exception:
                safe_tool_calls = None

            if safe_tool_calls is not None:
                assistant_msg = {"role": "assistant", "tool_calls": safe_tool_calls}
                if (assistant_content or "").strip():
                    assistant_msg["content"] = assistant_content
                await self.append_chat_history(assistant_msg)
            else:
                await self.append_chat_history({"role": "assistant", "content": assistant_content})

            # 仅处理第一个工具（可扩展为并行/串行多工具）
            tool_call = assistant_tool_calls[0]
            tool_id = getattr(tool_call, "id", None) or (tool_call.get("id") if isinstance(tool_call, dict) else None)
            fn_obj = getattr(tool_call, "function", None) or (tool_call.get("function") if isinstance(tool_call, dict) else {}) or {}
            fn_name = getattr(fn_obj, "name", None) or (fn_obj.get("name") if isinstance(fn_obj, dict) else None)

            if fn_name == "search_papers":
                logger.info("WriterAgent 调用 search_papers")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"写作手调用{fn_name}工具"),
                )

                # 解析 query
                try:
                    raw_args = getattr(fn_obj, "arguments", None) or (fn_obj.get("arguments") if isinstance(fn_obj, dict) else None)
                    if isinstance(raw_args, str):
                        args_obj = json.loads(raw_args) if raw_args.strip() else {}
                    elif isinstance(raw_args, (dict, list)):
                        args_obj = raw_args
                    else:
                        args_obj = {}
                    query = args_obj.get("query")
                except Exception as e:
                    query = None
                    logger.exception("解析 search_papers 参数失败")
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(content=f"解析 search_papers 参数失败: {e}", type="error"),
                    )
                    await self.append_chat_history(
                        {
                            "role": "function",
                            "name": "search_papers",
                            "tool_call_id": tool_id or f"call_{uuid.uuid4().hex[:12]}",
                            "content": f"解析 search_papers 参数失败: {e}",
                        }
                    )
                    return WriterResponse(response_content=f"解析 search_papers 参数失败: {e}", footnotes=footnotes)

                await redis_manager.publish_message(
                    self.task_id,
                    WriterMessage(input={"query": query}),
                )

                # ✅ scholar 为空兜底
                if self.scholar is None:
                    msg = "search_papers 工具不可用：scholar 实例为空。"
                    logger.error(msg)
                    await self.append_chat_history(
                        {
                            "role": "function",
                            "name": "search_papers",
                            "tool_call_id": tool_id or f"call_{uuid.uuid4().hex[:12]}",
                            "content": msg,
                        }
                    )
                    return WriterResponse(response_content=msg, footnotes=footnotes)

                # 调用 scholar 搜索文献
                try:
                    papers = await self.scholar.search_papers(query)
                except Exception as e:
                    error_msg = f"搜索文献失败: {str(e)}"
                    logger.error(error_msg)
                    await self.append_chat_history(
                        {
                            "role": "function",
                            "name": "search_papers",
                            "tool_call_id": tool_id or f"call_{uuid.uuid4().hex[:12]}",
                            "content": error_msg,
                        }
                    )
                    return WriterResponse(response_content=error_msg, footnotes=footnotes)

                # ✅ 体积限制：仅取前 N 篇 + 总字符上限
                TOP_N = 25
                try:
                    top_papers = papers[:TOP_N] if isinstance(papers, list) else papers
                except Exception:
                    top_papers = papers

                papers_str = self.scholar.papers_to_str(top_papers)
                if not isinstance(papers_str, str):
                    try:
                        papers_str = json.dumps(papers_str, ensure_ascii=False)
                    except Exception:
                        papers_str = str(papers_str)

                MAX_CHARS = 30000
                if len(papers_str) > MAX_CHARS:
                    papers_str = papers_str[:MAX_CHARS] + "\n\n[...检索结果过长，已截断，建议缩小查询范围或二次筛选...]"

                await self.append_chat_history(
                    {
                        "role": "function",
                        "name": "search_papers",
                        "tool_call_id": tool_id or f"call_{uuid.uuid4().hex[:12]}",
                        "content": papers_str,
                    }
                )

                # footnotes（可选）
                try:
                    if isinstance(papers, list):
                        for p in papers[:5]:
                            title = getattr(p, "title", None) or (p.get("title") if isinstance(p, dict) else None)
                            authors = getattr(p, "authors", None) or (p.get("authors") if isinstance(p, dict) else None)
                            footnotes.append(f"{title} — {authors}")
                except Exception:
                    pass

                # 继续对话
                next_response = await self.model.chat(
                    history=self.chat_history,
                    tools=writer_tools,
                    tool_choice="auto",
                    agent_name=self.__class__.__name__,
                    sub_title=sub_title,
                )
                assistant_msg_obj = next_response.choices[0].message
                assistant_content = getattr(assistant_msg_obj, "content", "") or ""
                await self.append_chat_history({"role": "assistant", "content": assistant_content})
            else:
                logger.warning(f"WriterAgent 收到未知工具调用: {fn_name}, 仅记录不执行")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"写作手收到未知工具调用: {fn_name}", type="warning"),
                )
                await self.append_chat_history(
                    {
                        "role": "function",
                        "name": fn_name or "unknown",
                        "tool_call_id": tool_id or f"call_{uuid.uuid4().hex[:12]}",
                        "content": "收到未知工具调用，未执行。请改用已注册的工具或直接继续写作。",
                    }
                )
        else:
            # 无工具调用，直接追加 assistant 内容
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
            fix_resp = await self.model.chat(
                history=self.chat_history,
                tools=writer_tools,
                tool_choice="auto",
                agent_name=self.__class__.__name__,
                sub_title=sub_title,
            )
            fix_assistant = getattr(fix_resp.choices[0].message, "content", "") or ""
            await self.append_chat_history({"role": "assistant", "content": fix_assistant})
            response_content = fix_assistant

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
