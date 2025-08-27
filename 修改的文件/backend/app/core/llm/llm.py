import re
import json
import asyncio
import random
from app.utils.common_utils import transform_link, split_footnotes
from app.utils.log_util import logger
from app.schemas.response import (
    CoderMessage,
    WriterMessage,
    ModelerMessage,
    SystemMessage,
    CoordinatorMessage,
)
from app.services.redis_manager import redis_manager
from litellm import acompletion, token_counter
import litellm
from app.schemas.enums import AgentType
from app.utils.track import agent_metrics
from icecream import ic

# ====== 全局：请求与重试配置（可按需调大/调小）======
REQUEST_TIMEOUT = 300.0  # 单次请求整体超时（秒）
HTTPX_TIMEOUTS = {
    "connect": 300.0,
    "read": 120.0,
    "write": 60.0,
    "pool": 120.0,
}
DEFAULT_MAX_RETRIES = 8
BACKOFF_BASE = 0.8  # 指数退避基数，实际 backoff = base * (2**attempt) + jitter

# ====== 上下文长度保护（给 DeepSeek/GPT 等留余量）======
# 模型标称最大 131072，这里保守限制在 120000 左右，避免触发 400
CONTEXT_TOKEN_HARD_LIMIT = 120_000

litellm.callbacks = [agent_metrics]


class LLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        task_id: str,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.chat_count = 0
        self.max_tokens: int | None = None  # 添加最大token数限制（用于限制输出tokens）
        self.task_id = task_id

    async def chat(
        self,
        history: list = None,
        tools: list = None,
        tool_choice: str = None,
        max_retries: int = DEFAULT_MAX_RETRIES,  # 添加最大重试次数
        retry_delay: float = BACKOFF_BASE,  # 初始重试延迟（指数退避基数）
        top_p: float | None = None,  # 添加top_p参数
        agent_name: AgentType | str = AgentType.SYSTEM,  # ← 放宽为枚举或字符串
        sub_title: str | None = None,
    ) -> str:
        logger.info(f"subtitle是:{sub_title}")

        # 1) 验证 & 修复工具调用完整性
        if history:
            history = self._validate_and_fix_tool_calls(history)

        # 2) 截断上下文（按 token 限制，保留系统消息 + 最近对话尾部）
        if history:
            history = self._truncate_history_by_tokens(history, CONTEXT_TOKEN_HARD_LIMIT)

        kwargs = {
            "api_key": self.api_key,
            "model": self.model,
            "messages": history,
            "stream": False,
            "top_p": top_p,
            # 重要：metadata 用可序列化的字符串（枚举用 .name，字符串直接用）
            "metadata": {"agent_name": getattr(agent_name, "name", str(agent_name))},
            "request_timeout": REQUEST_TIMEOUT,  # 整体请求超时
            "client_args": {"timeout": HTTPX_TIMEOUTS},  # httpx 细粒度超时
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        if self.base_url:
            kwargs["base_url"] = self.base_url

        # TODO: stream 输出（如需流式，注意接收流时的容错与取消处理）
        for attempt in range(max_retries):
            try:
                response = await acompletion(**kwargs)
                logger.info(f"API返回: {response}")

                # 基本校验
                if not response or not hasattr(response, "choices"):
                    raise ValueError("无效的API响应")

                self.chat_count += 1
                await self.send_message(response, agent_name, sub_title)
                return response

            except asyncio.CancelledError:
                logger.warning("请求被上层取消（CancelledError），不重试。")
                raise

            # —— 不重试的 4xx 逻辑错误/配置错误 ——
            except (
                litellm.BadRequestError,
                litellm.AuthenticationError,
                litellm.NotFoundError,
            ) as e:
                msg = str(e)
                if "maximum context length" in msg or "context length" in msg or "ContextWindowExceeded" in msg:
                    logger.error("非重试错误：上下文超限，请确保在进入 acompletion 前已充分截断。")
                else:
                    logger.error(f"非重试错误：{e}")
                raise

            # —— 可重试的错误：网络/限流/超时/5xx/偶发解析 ——
            except (
                litellm.RateLimitError,
                litellm.Timeout,
                litellm.APIConnectionError,
                litellm.InternalServerError,
                json.JSONDecodeError,
            ) as e:
                logger.error(f"第 {attempt + 1}/{max_retries} 次重试: {e}")
                if attempt >= max_retries - 1:
                    logger.debug(f"请求参数: {kwargs}")
                    raise
                delay = retry_delay * (2**attempt) + random.random() * 0.3
                await asyncio.sleep(delay)

            # —— 兜底：未知异常，少量重试 ——
            except Exception as e:
                logger.error(f"第 {attempt + 1}/{max_retries} 次重试（未知异常）: {e}")
                if attempt >= max_retries - 1:
                    logger.debug(f"请求参数: {kwargs}")
                    raise
                delay = retry_delay * (2**attempt) + random.random() * 0.3
                await asyncio.sleep(delay)

    def _validate_and_fix_tool_calls(self, history: list) -> list:
        """验证并修复工具调用完整性"""
        if not history:
            return history

        ic(f"🔍 开始验证工具调用，历史消息数量: {len(history)}")

        # 查找所有未匹配的tool_calls
        fixed_history = []
        i = 0

        while i < len(history):
            msg = history[i]

            # 如果是包含tool_calls的消息
            if isinstance(msg, dict) and "tool_calls" in msg and msg["tool_calls"]:
                ic(f"📞 发现tool_calls消息在位置 {i}")

                # 检查每个tool_call是否都有对应的response，分别处理
                valid_tool_calls = []
                invalid_tool_calls = []

                for tool_call in msg["tool_calls"]:
                    tool_call_id = tool_call.get("id")
                    ic(f"  检查tool_call_id: {tool_call_id}")

                    if tool_call_id:
                        # 查找对应的tool响应
                        found_response = False
                        for j in range(i + 1, len(history)):
                            if history[j].get("role") == "tool" and history[j].get("tool_call_id") == tool_call_id:
                                ic(f"  ✅ 找到匹配响应在位置 {j}")
                                found_response = True
                                break

                        if found_response:
                            valid_tool_calls.append(tool_call)
                        else:
                            ic(f"  ❌ 未找到匹配响应: {tool_call_id}")
                            invalid_tool_calls.append(tool_call)

                # 根据检查结果处理消息
                if valid_tool_calls:
                    # 有有效的tool_calls，保留它们
                    fixed_msg = msg.copy()
                    fixed_msg["tool_calls"] = valid_tool_calls
                    fixed_history.append(fixed_msg)
                    ic(f"  🔧 保留 {len(valid_tool_calls)} 个有效tool_calls，移除 {len(invalid_tool_calls)} 个无效的")
                else:
                    # 没有有效的tool_calls，移除tool_calls但可能保留其他内容
                    cleaned_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                    if cleaned_msg.get("content"):
                        fixed_history.append(cleaned_msg)
                        ic(f"  🔧 移除所有tool_calls，保留消息内容")
                    else:
                        ic(f"  🗑️ 完全移除空的tool_calls消息")

            # 如果是tool响应消息，检查是否是孤立的
            elif isinstance(msg, dict) and msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id")
                ic(f"🔧 检查tool响应消息: {tool_call_id}")

                # 查找对应的tool_calls
                found_call = False
                for j in range(len(fixed_history)):
                    if fixed_history[j].get("tool_calls") and any(
                        tc.get("id") == tool_call_id for tc in fixed_history[j]["tool_calls"]
                    ):
                        found_call = True
                        break

                if found_call:
                    fixed_history.append(msg)
                    ic(f"  ✅ 保留有效的tool响应")
                else:
                    ic(f"  🗑️ 移除孤立的tool响应: {tool_call_id}")

            else:
                # 普通消息，直接保留
                fixed_history.append(msg)

            i += 1

        if len(fixed_history) != len(history):
            ic(f"🔧 修复完成: {len(history)} -> {len(fixed_history)} 条消息")
        else:
            ic(f"✅ 验证通过，无需修复")

        return fixed_history

    def _truncate_history_by_tokens(self, history: list, token_limit: int) -> list:
        """
        按 token 数量裁剪 messages（保留首条 system + 尾部若干条）。
        为避免破坏工具消息配对，采用“取对话尾部连续片段”的策略，再做一次完整性校验。
        """
        if not history:
            return history

        # 计算 token 的辅助函数
        def msg_tokens(msg: dict) -> int:
            # 仅对 content 计数（role/tool_calls 元数据不计）
            content = msg.get("content") or ""
            try:
                return token_counter(content, self.model)
            except Exception:
                # 兜底估算（大致 3~4 字符 ~ 1 token）
                return max(1, len(content) // 3)

        # 首条可能是 system，尽量保留
        system_msg = None
        start_idx = 0
        if history[0].get("role") == "system":
            system_msg = history[0]
            start_idx = 1

        # 先尝试全量计数
        total = (msg_tokens(system_msg) if system_msg else 0) + sum(msg_tokens(m) for m in history[start_idx:])
        if total <= token_limit:
            return history

        # 从尾部向前累积，直到达到上限
        kept = []
        running = msg_tokens(system_msg) if system_msg else 0

        for i in range(len(history) - 1, start_idx - 1, -1):
            t = msg_tokens(history[i])
            if running + t > token_limit:
                break
            kept.append(history[i])
            running += t

        kept.reverse()
        new_history = [system_msg] + kept if system_msg else kept

        # 再次做工具调用完整性修复，避免产生孤立 tool 消息
        new_history = self._validate_and_fix_tool_calls(new_history)
        return new_history

    async def send_message(self, response, agent_name, sub_title=None):
        logger.info(f"subtitle是:{sub_title}")
        raw_content = response.choices[0].message.content or ""

        # 允许上游传字符串（如 "JsonFixerHeavy"），在此归一化为 AgentType
        if isinstance(agent_name, str):
            key = agent_name.lower().replace(" ", "")
            mapping = {
                "coordinatoragent": AgentType.COORDINATOR,
                "modeleragent": AgentType.MODELER,
                "writeragent": AgentType.WRITER,
                "coderagent": AgentType.CODER,
                "jsonfixer": AgentType.MODELER,
                "jsonfixerheavy": AgentType.MODELER,
            }
            agent_name = mapping.get(key, None) or (
                AgentType.COORDINATOR
                if "coord" in key
                else (
                    AgentType.MODELER
                    if ("model" in key or "jsonfixer" in key)
                    else (
                        AgentType.WRITER if "writer" in key else AgentType.CODER if "coder" in key else AgentType.SYSTEM
                    )
                )
            )

        # ------- 仅对 Coordinator / Modeler 做 JSON 规范化（右侧面板要吃干净 JSON）-------
        def _cleanup_fences(s: str) -> str:
            return (s or "").replace("```json", "").replace("```", "").strip()

        def _cleanup_ctrl(s: str) -> str:
            return re.sub(r"[\x00-\x1F\x7F]", "", s or "")

        def _extract_first_json_block(s: str) -> str:
            if not s:
                return ""
            start = s.find("{")
            if start == -1:
                return ""
            stack, in_str, esc = [], False, False
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
                            return s[start : i + 1]
            return ""

        def _normalize_json_for_right_panel(text: str):
            # 返回 (ok, normalized_text)
            cleaned = _cleanup_ctrl(_cleanup_fences(text))
            blk = _extract_first_json_block(cleaned)
            if not blk:
                return False, text
            try:
                obj = json.loads(blk)
                return True, json.dumps(obj, ensure_ascii=False)
            except Exception:
                return False, text

        content_to_send = raw_content
        if agent_name in (AgentType.COORDINATOR, AgentType.MODELER):
            ok, normalized = _normalize_json_for_right_panel(raw_content)
            if ok:
                content_to_send = normalized
            else:
                logger.warning("send_message: 未能从原文中提取合法 JSON，按原文发布。")

        # ------- 构造并发布对应消息 -------
        match agent_name:
            case AgentType.CODER:
                agent_msg: CoderMessage = CoderMessage(content=content_to_send)
            case AgentType.WRITER:
                # 处理 Markdown 图片/脚注
                c, _ = split_footnotes(content_to_send)
                c = transform_link(self.task_id, c)
                agent_msg: WriterMessage = WriterMessage(content=c, sub_title=sub_title)
            case AgentType.MODELER:
                agent_msg: ModelerMessage = ModelerMessage(content=content_to_send)
            case AgentType.COORDINATOR:
                agent_msg: CoordinatorMessage = CoordinatorMessage(content=content_to_send)
            case AgentType.SYSTEM:
                agent_msg: SystemMessage = SystemMessage(content=content_to_send)
            case _:
                agent_msg: SystemMessage = SystemMessage(content=content_to_send)

        await redis_manager.publish_message(self.task_id, agent_msg)


async def simple_chat(model: LLM, history: list) -> str:
    """
    重量版 simple_chat：
    1) 先修复工具消息完整性（避免孤立 tool / 未匹配的 tool_call）
    2) 在总 token 超限时，采用：保留 system + 尾部完整对话片段 + 中段自动摘要
    3) 迭代压缩，直到 <= CONTEXT_TOKEN_HARD_LIMIT 后再发起最终补全
    """

    # ========== 工具函数 ==========
    def quick_count(msg):
        content = (msg or {}).get("content") or ""
        try:
            return token_counter(content, model.model)
        except Exception:
            return max(1, len(content) // 3)  # 兜底估算

    def tokens_of(messages):
        if not messages:
            return 0
        return sum(quick_count(m) for m in messages if isinstance(m, dict))

    def pair_safe_tail(messages):
        """
        获取对话尾部的“配对安全”连续片段（避免把 tool_call / tool 响应对拆开）。
        策略：从尾部向前取，遇到有 tool_calls 的消息，就确保其对应的 tool 响应在片段中；反之亦然。
        这里用简化策略：先取一段尾部，再调用模型已有的 _validate_and_fix_tool_calls 修复。
        """
        # 先直接给出一个“足量尾部”（以消息数为尺度，后续再做 token 限制）
        MAX_TAIL_MSGS = 30
        start = max(0, len(messages) - MAX_TAIL_MSGS)
        tail = messages[start:]
        # 修复尾部片段的工具关系
        return model._validate_and_fix_tool_calls(tail)

    async def summarize_chunk(chunk_msgs):
        """
        使用模型对中段进行摘要压缩（附带 system 提示），输出尽量简短但保留关键信息。
        """
        sys_prompt = {
            "role": "system",
            "content": (
                "你是一个对话摘要器。请将以下对话压缩为一段简洁的中文总结，"
                "保留任务目标、关键约束、重要结论和已完成步骤，去除无关细节。"
                "输出不超过 300~500 字。"
            ),
        }
        user_prompt = {
            "role": "user",
            "content": "\n".join(
                f"{m.get('role')}: { (m.get('content') or '')[:2000] }" for m in chunk_msgs if isinstance(m, dict)
            ),
        }

        kwargs = {
            "api_key": model.api_key,
            "model": model.model,
            "messages": [sys_prompt, user_prompt],
            "stream": False,
            "request_timeout": REQUEST_TIMEOUT,
            "client_args": {"timeout": HTTPX_TIMEOUTS},
        }
        if model.base_url:
            kwargs["base_url"] = model.base_url

        resp = await acompletion(**kwargs)
        return resp.choices[0].message.content.strip()

    # ========== 预处理：工具完整性修复 ==========
    history = history or []
    history = model._validate_and_fix_tool_calls(history)

    # 拆出 system（若存在则保留）
    sys_msg = history[0] if (history and history[0].get("role") == "system") else None
    start_idx = 1 if sys_msg else 0
    body = history[start_idx:]

    # 快速通过：未超限直接请求
    total_tokens = (quick_count(sys_msg) if sys_msg else 0) + tokens_of(body)
    if total_tokens <= CONTEXT_TOKEN_HARD_LIMIT:
        kwargs = {
            "api_key": model.api_key,
            "model": model.model,
            "messages": history,
            "stream": False,
            "request_timeout": REQUEST_TIMEOUT,
            "client_args": {"timeout": HTTPX_TIMEOUTS},
        }
        if model.base_url:
            kwargs["base_url"] = model.base_url
        resp = await acompletion(**kwargs)
        return resp.choices[0].message.content

    # ========== 重量压缩流程 ==========
    # 目标：system + [摘要后的中段1条] + 安全尾部片段  ——> 迭代到 <= 预算
    # 步骤：
    # 1) 先拿一个“配对安全的尾部片段” tail
    # 2) 其余作为 head（需要摘要）
    # 3) 用 summarize_chunk(head) 得到一条简短 assistant 消息
    # 4) 组合 new_history = [sys?] + [summary_msg] + tail -> 若仍超限，缩短 tail 再摘要（或二次摘要）
    MAX_SUMMARY_ROUNDS = 3
    for round_idx in range(MAX_SUMMARY_ROUNDS):
        tail = pair_safe_tail(body)
        # 预算要留给：system + summary(≈500字) + tail
        # 先估计 summary 预算：用 quick_count 的粗略值，给 1500 tokens 余量较稳妥
        SUMMARY_BUDGET_HINT = 1500

        # 二分/线性缩短 tail，直到“system + 预估summary + tail”不超过上限（粗筛）
        def tail_tokens(t):
            return tokens_of(t)

        keep = len(tail)
        while keep > 0:
            candidate_tail = tail[-keep:]
            rough_total = (quick_count(sys_msg) if sys_msg else 0) + SUMMARY_BUDGET_HINT + tail_tokens(candidate_tail)
            if rough_total <= CONTEXT_TOKEN_HARD_LIMIT:
                tail = model._validate_and_fix_tool_calls(candidate_tail)
                break
            keep //= 2
        else:
            # 实在太满，尾部清空，仅保留摘要
            tail = []

        # 需要摘要的 head = body 去掉 tail 的前缀部分
        cut_at = len(body) - len(tail)
        head = body[: max(cut_at, 0)]

        # 做一次摘要（若 head 为空则用空摘要）
        summary_text = ""
        if head:
            try:
                summary_text = await summarize_chunk(head)
            except Exception as e:
                logger.error(f"摘要失败，回退使用简短占位：{e}")
                summary_text = "（对话中段摘要：包含若干步骤、错误修复与中间结论，已省略细节以节省上下文。）"
        summary_msg = {"role": "assistant", "content": f"[历史对话总结] {summary_text}"}

        new_history = ([sys_msg] if sys_msg else []) + [summary_msg] + tail
        new_history = model._validate_and_fix_tool_calls(new_history)

        # 精确检查 token
        exact_total = tokens_of(new_history)
        if exact_total <= CONTEXT_TOKEN_HARD_LIMIT:
            # 达标，最终请求
            kwargs = {
                "api_key": model.api_key,
                "model": model.model,
                "messages": new_history,
                "stream": False,
                "request_timeout": REQUEST_TIMEOUT,
                "client_args": {"timeout": HTTPX_TIMEOUTS},
            }
            if model.base_url:
                kwargs["base_url"] = model.base_url
            resp = await acompletion(**kwargs)
            return resp.choices[0].message.content

        # 还超，上再来一轮：进一步缩尾部或二次摘要
        body = head + tail  # 继续以“更短的可压缩体”作为下一轮输入

    # 多轮仍超限：退而求其次 —— 仅保留 system + 极短摘要
    try:
        minimal_summary = await summarize_chunk(body[:50])  # 仅采样前50条做一个极短摘要
    except Exception:
        minimal_summary = "（超长上下文，已压缩为极短摘要。）"
    final_history = ([sys_msg] if sys_msg else []) + [
        {"role": "assistant", "content": f"[历史对话极简总结] {minimal_summary}"}
    ]

    kwargs = {
        "api_key": model.api_key,
        "model": model.model,
        "messages": final_history,
        "stream": False,
        "request_timeout": REQUEST_TIMEOUT,
        "client_args": {"timeout": HTTPX_TIMEOUTS},
    }
    if model.base_url:
        kwargs["base_url"] = model.base_url
    resp = await acompletion(**kwargs)
    return resp.choices[0].message.content
