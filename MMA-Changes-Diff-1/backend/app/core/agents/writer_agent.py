from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import get_writer_prompt
from app.schemas.enums import CompTemplate, FormatOutPut
from app.tools.openalex_scholar import OpenAlexScholar
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager
from app.schemas.response import SystemMessage, WriterMessage
import json
from app.core.functions import writer_tools
from icecream import ic
from app.schemas.A2A import WriterResponse
import re
from typing import List, Tuple


# 长文本
# TODO: 并行 parallel
# TODO: 获取当前文件下的文件
# TODO: 引用cites tool
class WriterAgent(Agent):  # 同样继承自Agent类
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 10,  # 添加最大对话轮次限制
        comp_template: CompTemplate = CompTemplate,
        format_output: FormatOutPut = FormatOutPut.Markdown,
        scholar: OpenAlexScholar = None,
        max_memory: int = 25,  # 添加最大记忆轮次
    ) -> None:
        super().__init__(task_id, model, max_chat_turns, max_memory)
        self.format_out_put = format_output
        self.comp_template = comp_template
        self.scholar = scholar
        self.is_first_run = True
        self.system_prompt = get_writer_prompt(format_output)
        self.available_images: List[str] = []

        # 校验图片引用的正则（匹配 Markdown 图片）
        self._img_regex = re.compile(r"!\[.*?\]\((.*?)\)")

        # 允许的图片路径前缀（严格）
        self._allowed_prefixes = (
            "eda/figures/",
            "sensitivity_analysis/figures/",
        )
        # quesN 前缀用动态匹配

    async def run(
        self,
        prompt: str,
        available_images: list[str] = None,
        sub_title: str = None,
    ) -> WriterResponse:
        """
        执行写作任务
        Args:
            prompt: 写作提示
            available_images: 可用的图片相对路径列表（如 eda/figures/fig_x.png 或 ques1/figures/fig_x.png）
            sub_title: 子任务标题
        """
        logger.info(f"WriterAgent subtitle: {sub_title}")

        # 首次注入 system prompt
        if self.is_first_run:
            self.is_first_run = False
            await self.append_chat_history({"role": "system", "content": self.system_prompt})

        # 接受可用图片（外部已按子任务筛选），并记录在 agent 状态
        if available_images:
            # 统一使用相对路径小写形式比较（但返回保留原样）
            self.available_images = available_images
            # 在 prompt 中以结构化形式提供图片清单（每行一个）
            image_list = "\n".join(available_images)
            image_prompt = (
                "\n可用图片清单（仅可引用下列图片，且每张图片在整篇只可引用一次）：\n"
                f"{image_list}\n\n"
                "写作时请严格使用这些图片的相对路径（示例：`![说明](ques2/figures/fig_model_performance.png)`），"
                "且不要重复引用同一张图片。"
            )
            logger.info(f"image_prompt prepared with {len(available_images)} images")
            prompt = prompt + image_prompt

        # 增加一条对话轮次计数
        self.current_chat_turns += 1

        # 发送 user 提示到历史
        await self.append_chat_history({"role": "user", "content": prompt})

        # 先请求模型生成初稿/或带工具调用的响应
        response = await self.model.chat(
            history=self.chat_history,
            tools=writer_tools,
            tool_choice="auto",
            agent_name=self.__class__.__name__,
            sub_title=sub_title,
        )

        footnotes: List[str] = []

        # 解析 helper：从 message 对象提取 content 和 tool_calls（安全）
        assistant_msg_obj = response.choices[0].message
        assistant_content = getattr(assistant_msg_obj, "content", "") or ""
        assistant_tool_calls = getattr(assistant_msg_obj, "tool_calls", None)

        # 如果模型发起了工具调用（例如 search_papers）
        if assistant_tool_calls:
            logger.info("检测到工具调用 in WriterAgent")
            # 规范化追加 assistant 消息（避免 model_dump 导致结构异常）
            safe_tool_calls = []
            try:
                for tc in assistant_tool_calls:
                    tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
                    fn_name = getattr(getattr(tc, "function", None), "name", None) or (
                        tc.get("function", {}).get("name") if isinstance(tc, dict) else None
                    )
                    fn_args = getattr(getattr(tc, "function", None), "arguments", None) or (
                        tc.get("function", {}).get("arguments") if isinstance(tc, dict) else None
                    )
                    safe_tool_calls.append({"id": tc_id, "function": {"name": fn_name, "arguments": fn_args}})
            except Exception:
                safe_tool_calls = None

            # 将 assistant 的文本内容与精简 tool_calls 写入历史
            if safe_tool_calls is not None:
                await self.append_chat_history({"role": "assistant", "content": assistant_content, "tool_calls": safe_tool_calls})
            else:
                await self.append_chat_history({"role": "assistant", "content": assistant_content})

            # 处理第一个工具调用（当前仅处理 search_papers）
            tool_call = assistant_tool_calls[0]
            tool_id = getattr(tool_call, "id", None)
            fn_name = getattr(getattr(tool_call, "function", None), "name", None)

            if fn_name == "search_papers":
                logger.info("WriterAgent 调用 search_papers")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"写作手调用{fn_name}工具"),
                )

                # 解析 query
                try:
                    query = json.loads(tool_call.function.arguments)["query"]
                except Exception as e:
                    query = None
                    logger.exception("解析 search_papers 参数失败")
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(content=f"解析 search_papers 参数失败: {e}", type="error"),
                    )
                    # 追加一个 tool 响应并返回错误
                    await self.append_chat_history(
                        {"role": "tool", "content": f"解析 search_papers 参数失败: {e}", "tool_call_id": tool_id, "name": "search_papers"}
                    )
                    return WriterResponse(response_content=f"解析 search_papers 参数失败: {e}", footnotes=footnotes)

                # publish query to frontend if needed
                await redis_manager.publish_message(
                    self.task_id,
                    WriterMessage(input={"query": query}),
                )

                # 调用 scholar 搜索文献
                try:
                    papers = await self.scholar.search_papers(query)
                except Exception as e:
                    error_msg = f"搜索文献失败: {str(e)}"
                    logger.error(error_msg)
                    await self.append_chat_history(
                        {"role": "tool", "content": error_msg, "tool_call_id": tool_id, "name": "search_papers"}
                    )
                    return WriterResponse(response_content=error_msg, footnotes=footnotes)

                # 将搜索结果格式化写入 tool 响应
                papers_str = self.scholar.papers_to_str(papers)
                logger.info(f"搜索文献结果长度: {len(papers_str)}")
                await self.append_chat_history(
                    {
                        "role": "tool",
                        "content": papers_str,
                        "tool_call_id": tool_id,
                        "name": "search_papers",
                    }
                )

                # 可以把部分元信息收集到 footnotes（例如标题+作者）
                try:
                    for p in papers[:5]:
                        # scholar 返回的 paper 对象结构可能不同，做容错读取
                        title = getattr(p, "title", None) or p.get("title") if isinstance(p, dict) else None
                        authors = getattr(p, "authors", None) or p.get("authors") if isinstance(p, dict) else None
                        footnotes.append(f"{title} — {authors}")
                except Exception:
                    pass

                # 继续对话，让模型在工具结果上下文中生成最终文本
                next_response = await self.model.chat(
                    history=self.chat_history,
                    tools=writer_tools,
                    tool_choice="auto",
                    agent_name=self.__class__.__name__,
                    sub_title=sub_title,
                )
                assistant_msg_obj = next_response.choices[0].message
                assistant_content = getattr(assistant_msg_obj, "content", "") or ""
                # 追加 assistant 最终文本到历史
                await self.append_chat_history({"role": "assistant", "content": assistant_content})
            else:
                # 未知工具：记录并返回 assistant_content（已写入历史）
                logger.warning(f"WriterAgent 收到未知工具调用: {fn_name}, 仅记录不执行")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"写作手收到未知工具调用: {fn_name}", type="warning"),
                )
                # 将当前 assistant_content 作为初稿继续后续校验
        else:
            # 无工具调用，直接追加 assistant 内容
            await self.append_chat_history({"role": "assistant", "content": assistant_content})

        # 至此，assistant_content 中应为模型最终生成的文本（字符串）
        response_content = assistant_content or ""

        # 进行图片引用校验：确保引用的图片都在 available_images，且只引用一次，且路径前缀合法
        # 重试机制：若不合规，向模型发起修正请求，最多重试 2 次
        max_fix_attempts = 2
        attempt = 0
        while attempt <= max_fix_attempts:
            img_paths = self._extract_image_paths(response_content)
            invalids, duplicates = self._validate_image_paths(img_paths)

            if not invalids and not duplicates:
                # 合法，返回结果
                logger.info("WriterAgent: 图片引用校验通过")
                break

            # 否则构造修正提示交给模型
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
            logger.warning(f"图片引用校验未通过（尝试 {attempt}/{max_fix_attempts}）:\n{error_msg}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"写作校验：图片引用问题，{error_msg}", type="error"),
            )

            # 要求模型修正：给出可用图片清单并要求替换/去重或使用占位
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

            # 追加 user 修正请求并再次调用模型
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

            # loop to re-validate

        # 最终返回（不论是否修正成功，都返回最后版本；若仍不合规，前端或调用方可根据 redis 日志处理）
        return WriterResponse(response_content=response_content, footnotes=footnotes)

    def _extract_image_paths(self, text: str) -> List[str]:
        """从 markdown 文本中提取所有图片链接的路径（原始字符串）"""
        if not text:
            return []
        matches = self._img_regex.findall(text)
        # 清理空格并保持原样
        return [m.strip() for m in matches if m and isinstance(m, str)]

    def _validate_image_paths(self, img_paths: List[str]) -> Tuple[List[str], List[str]]:
        """
        校验图片路径集合，返回 (invalid_paths, duplicated_paths)
        invalid_paths: 不在 available_images 或前缀不允许
        duplicated_paths: 在文本中重复出现的路径（出现次数 >1）
        """
        invalids = []
        duplicates = []
        if not img_paths:
            return invalids, duplicates

        # 计数出现次数
        counts = {}
        for p in img_paths:
            counts[p] = counts.get(p, 0) + 1

        # 找重复
        for p, c in counts.items():
            if c > 1:
                duplicates.append(p)

        # 校验合法性：在 available_images 且前缀允许（或 quesN）
        allowed_set = set(self.available_images or [])
        for p in counts.keys():
            if p not in allowed_set:
                invalids.append(p)
                continue
            # 前缀检查
            ok_prefix = False
            if p.startswith("eda/figures/") or p.startswith("sensitivity_analysis/figures/"):
                ok_prefix = True
            else:
                # quesN/figures/ 前缀，N 为正整数
                if re.match(r"^ques\d+/figures/", p):
                    ok_prefix = True
            if not ok_prefix:
                invalids.append(p)

        return invalids, duplicates
