# app/core/llm/llm.py

import json
import codecs
import string
import asyncio
import random
import uuid
from typing import Any, List, Dict

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

# 引入集中式清洗/正则工具
from app.tools.text_sanitizer import TextSanitizer as TS
from app.tools.json_fixer import JsonFixer

# ====== 全局：请求与重试配置（可按需调大/调小）======
REQUEST_TIMEOUT = 300.0  # 单次请求整体超时（秒）
HTTPX_TIMEOUTS = {
    "connect": 120,
    "read": 60,
    "write": 120,
    "pool": 60,
}
DEFAULT_MAX_RETRIES = 100
BACKOFF_BASE = 0.8  # 指数退避基数，实际 backoff = base * (2**attempt) + jitter

# ====== 上下文长度保护（给 DeepSeek/GPT 等留余量）======
# 模型标称最大 131072，这里保守限制在 120000 左右，避免触发 400
CONTEXT_TOKEN_HARD_LIMIT = 120_000

litellm.callbacks = [agent_metrics]

# ========= 最后一跳消息清洗（确保 messages 可被 OpenAI/DeepSeek 正确反序列化） =========
# 仅保留顶层允许的字段：role/content/name/tool_calls/tool_call_id
_ALLOWED_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}


def _json_dumps_safe(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def _extract_tool_text(msg: Dict[str, Any]) -> str:
    """尽量从 tool 消息的各类字段中提炼可读文本"""
    extracted = []

    # 常见：output/outputs/result/results
    out = msg.get("output") or msg.get("outputs") or msg.get("result") or msg.get("results")
    if out is not None:
        if isinstance(out, (list, tuple)):
            for it in out:
                if isinstance(it, dict):
                    for k in ("msg", "message", "text", "result", "content"):
                        v = it.get(k)
                        if v:
                            extracted.append(str(v))
                            break
                    else:
                        extracted.append(_json_dumps_safe(it))
                else:
                    extracted.append(str(it))
        elif isinstance(out, dict):
            for k in ("msg", "message", "text", "result", "content"):
                v = out.get(k)
                if v:
                    extracted.append(str(v))
                    break
            else:
                extracted.append(_json_dumps_safe(out))
        else:
            extracted.append(str(out))

    # 备选：text/stdout/stderr/data/value
    for k in ("text", "stdout", "stderr", "data", "value"):
        v = msg.get(k)
        if v:
            if isinstance(v, (list, dict)):
                extracted.append(_json_dumps_safe(v))
            else:
                extracted.append(str(v))

    # 备选：tool_result/tool_response/tool_outputs
    tc = msg.get("tool_result") or msg.get("tool_response") or msg.get("tool_outputs")
    if tc is not None:
        extracted.append(_json_dumps_safe(tc))

    # 去重+拼接
    parts, seen = [], set()
    for s in (x.strip() for x in extracted if isinstance(x, str)):
        if s and s not in seen:
            seen.add(s)
            parts.append(s)
    return "\n".join(parts)


def _looks_like_literal_escapes(s: str) -> bool:
    """
    委托给 TextSanitizer 的实现，保持行为一致。
    """
    return TS.looks_like_literal_escapes(s)


def _stringify_tool_calls(tc_list: Any) -> Any:
    """把 assistant 消息里的 tool_calls.arguments 强制转成字符串，并兜底 function.name / type / id"""
    if not isinstance(tc_list, (list, tuple)):
        return tc_list
    cleaned = []
    for tc in tc_list:
        if not isinstance(tc, dict):
            cleaned.append(tc)
            continue

        tc = dict(tc)

        # type 兜底
        if tc.get("type") != "function":
            tc["type"] = "function"

        # id 兜底（若上游不给，我们自己给，后续 tool 消息用同一个 id）
        if not isinstance(tc.get("id"), str) or not tc.get("id"):
            tc["id"] = f"call_{uuid.uuid4().hex[:12]}"

        # function 兜底
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            fn = {"name": "unknown", "arguments": _json_dumps_safe(fn)}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            name = "unknown"
        args = fn.get("arguments")
        if isinstance(args, (dict, list)):
            args = _json_dumps_safe(args)
        if args is None:
            args = ""

        tc["function"] = {"name": name, "arguments": args}
        cleaned.append(tc)
    return cleaned


def sanitize_messages_for_openai(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    最后一跳强制清洗（OpenAI/DeepSeek 兼容）改良版：保留原有行为。
    1) 不会无脑转义或移除换行
    2) 如果 content 是 list-of-lines，保留并合并为真实换行
    3) 若 content 为字符串且仅包含字面 '\\n'（没有真实 '\n'），则保守地尝试一次反转义为真实换行
    4) 其它行为与原版一致：规范角色、处理 assistant.tool_calls、绑定 tool_call_id、丢弃孤儿消息等
    """
    result: List[Dict[str, Any]] = []
    if not history:
        return result

    pending_tool_ids: List[str] = []  # assistant.tool_calls 产生的待消费 id 队列

    for idx, orig in enumerate(history):
        base = {} if not isinstance(orig, dict) else dict(orig)

        # 先裁剪到允许字段（保留原 base 用于抽取文本）
        m = {k: v for k, v in base.items() if k in _ALLOWED_KEYS}

        # -------- 角色规范化 --------
        role = m.get("role") or base.get("role") or "assistant"
        if role == "function":
            role = "tool"
            if not isinstance(m.get("name"), str) or not m.get("name"):
                m["name"] = base.get("name") or "tool"
        elif role == "tool":
            pass
        elif role not in ("system", "user", "assistant"):
            logger.warning(f"[sanitize] unexpected role={role} at idx={idx}, fallback to 'assistant'")
            role = "assistant"
        m["role"] = role

        # -------- assistant.tool_calls 处理 --------
        if role == "assistant" and base.get("tool_calls"):
            tool_calls = _stringify_tool_calls(base.get("tool_calls"))
            m["tool_calls"] = tool_calls
            for tc in tool_calls or []:
                tc_id = (tc or {}).get("id")
                if isinstance(tc_id, str) and tc_id:
                    pending_tool_ids.append(tc_id)

        # -------- content 规范化（所有角色）--------
        content = None
        # Prefer explicit content from m (already filtered); fallback to base variety
        if "content" in m:
            content = m.get("content")
        else:
            # try to find likely textual fields in original object
            if isinstance(base, dict):
                # keep the same priority as _extract_tool_text but don't over-dumps strings
                for k in ("content", "text", "result", "message", "msg"):
                    if k in base and base.get(k) is not None:
                        content = base.get(k)
                        break

        # Normalize content to a single string while preserving existing newlines:
        normalized_content = ""
        if content is None:
            normalized_content = ""
        elif isinstance(content, list):
            # If already list-of-lines, join preserving explicit line breaks.
            # Allow items to be either strings or objects (non-strings -> json dumps)
            parts = []
            for it in content:
                if isinstance(it, str):
                    parts.append(it)
                else:
                    parts.append(_json_dumps_safe(it))
            normalized_content = "\n".join(parts)
        elif isinstance(content, dict):
            # dict -> json string (no ascii escape)
            normalized_content = _json_dumps_safe(content)
        else:
            # it's a scalar (likely string or number)
            try:
                normalized_content = str(content)
            except Exception:
                normalized_content = ""

        # 保守反转义：仅当内容看起来是“字面转义的 ASCII 文本”时再处理；
        # 且对 tool 消息一律跳过（避免破坏代码/二进制）
        if role != "tool" and _looks_like_literal_escapes(normalized_content):
            try:
                # 只做一次 unicode_escape 解码（不再先 .encode('utf-8')）
                candidate = codecs.decode(normalized_content, "unicode_escape")
                # 要求至少出现真实换行，避免把正常文本搞坏
                if "\n" in candidate or "\r\n" in candidate or "\t" in candidate:
                    normalized_content = candidate
                else:
                    normalized_content = (
                        normalized_content.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
                    )
            except Exception:
                normalized_content = (
                    normalized_content.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
                )

        # Now we have normalized_content with real '\n' where appropriate. Do NOT strip or remove newlines.
        # 对“tool（含遗留 function）消息”尝试从其它字段提取可读文本（仅在 content 为空时）
        if role == "tool" and (not normalized_content or not normalized_content.strip()):
            extracted = _extract_tool_text(base) if isinstance(base, dict) else ""
            if extracted:
                # _extract_tool_text returns joined parts with "\n" already
                normalized_content = extracted

        # 对“assistant 且包含 tool_calls”的消息，允许没有 content（多数模型就是空 content）
        if not (role == "assistant" and m.get("tool_calls")):
            # 其它角色一律写回 content
            m["content"] = normalized_content

        # -------- 为 tool 消息确保 tool_call_id 配对 --------
        if role == "tool":
            tcid = m.get("tool_call_id")
            if not isinstance(tcid, str) or not tcid:
                if pending_tool_ids:
                    assigned = pending_tool_ids.pop(0)
                    m["tool_call_id"] = assigned
                    logger.debug(f"[sanitize] tool msg auto-bound tool_call_id={assigned} at idx={idx}")
                else:
                    # 没有可匹配的 id，属于孤儿工具响应：直接丢弃，避免非法消息
                    logger.warning(f"[sanitize] dropping orphan tool message at idx={idx} (no matching tool_call_id)")
                    continue

        # -------- 剔除 None 值，避免严格校验问题 --------
        for k in list(m.keys()):
            if m[k] is None:
                del m[k]

        # -------- 丢弃纯空消息（除 system 外）--------
        is_meaningless = (
            (role != "system")
            and (not m.get("content", "") or not m.get("content", "").strip())
            and (not m.get("tool_calls"))
            and (role != "tool" or not (m.get("name") or m.get("tool_call_id")))
        )
        if is_meaningless:
            logger.debug(f"[sanitize] drop empty message at idx={idx}, role={role}")
            continue

        # 记录 debug（保持原来的调试输出）
        if (m.get("content", "") or "") == "":
            logger.debug(f"[sanitize] empty content kept at idx={idx}, role={role}")

        result.append(m)

    # 调试：打印前几条，确认没有 None / 异常
    try:
        for i, mm in enumerate(result[:4]):
            logger.debug(
                f"[sanitize] #{i}: role={mm.get('role')}, "
                f"type(content)={type(mm.get('content'))}, "
                f"len(content)={len(mm.get('content') or '')}"
            )
    except Exception:
        pass

    return result


# =======================================================================================


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
        self.max_tokens: int | None = None
        self.task_id = task_id

    async def chat(
        self,
        history: list = None,
        tools: list = None,
        tool_choice: str = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = BACKOFF_BASE,
        top_p: float | None = None,
        agent_name: AgentType | str = AgentType.SYSTEM,
        sub_title: str | None = None,
        publish: bool = True,  # 关键新增：是否发布到 Redis/WebSocket
    ) -> object:  # 返回 ModelResponse
        logger.info(f"subtitle是:{sub_title}")

        # 1) 工具配对修复 + 截断 + system后首条user
        if history:
            history = self._validate_and_fix_tool_calls(history)
            history = self._truncate_history_by_tokens(history, CONTEXT_TOKEN_HARD_LIMIT)
            history = self._ensure_first_after_system_user(history)

        # 2) 最后一跳清洗
        safe_messages = sanitize_messages_for_openai(history or [])

        # 3) 组装请求参数
        kwargs = {
            "api_key": self.api_key,
            "model": self.model,
            "messages": safe_messages,
            "stream": False,
            "top_p": top_p,
            "metadata": {"agent_name": getattr(agent_name, "name", str(agent_name))},
            "request_timeout": REQUEST_TIMEOUT,
            "client_args": {"timeout": HTTPX_TIMEOUTS},
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if self.base_url:
            kwargs["base_url"] = self.base_url

        # 4) 调用 + 重试
        for attempt in range(max_retries):
            try:
                response = await acompletion(**kwargs)
                logger.info(f"API返回: {response}")

                if not response or not hasattr(response, "choices"):
                    raise ValueError("无效的API响应")

                # 仅在 publish=True 时，才入库并广播
                if publish:
                    self.chat_count += 1
                    await self.send_message(response, agent_name, sub_title)

                return response

            except asyncio.CancelledError:
                logger.warning("请求被上层取消（CancelledError），不重试。")
                raise
            except (litellm.BadRequestError, litellm.AuthenticationError, litellm.NotFoundError) as e:
                msg = str(e)
                if "maximum context length" in msg or "context length" in msg or "ContextWindowExceeded" in msg:
                    logger.error("非重试错误：上下文超限，请确保在进入 acompletion 前已充分截断。")
                else:
                    logger.error(f"非重试错误：{e}")
                raise
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
            except Exception as e:
                logger.error(f"第 {attempt + 1}/{max_retries} 次重试（未知异常）: {e}")
                if attempt >= max_retries - 1:
                    logger.debug(f"请求参数: {kwargs}")
                    raise
                delay = retry_delay * (2**attempt) + random.random() * 0.3
                await asyncio.sleep(delay)

    def _validate_and_fix_tool_calls(self, history: list) -> list:
        """
        验证并修复工具调用完整性（OpenAI 新规范）：
        1) 合法角色只允许：system / user / assistant / tool
        2) assistant 消息里的 tool_calls[*].id 必须与后续某条 role='tool' 的消息的 tool_call_id 匹配
        3) 若发现历史遗留的 role='function'，在此阶段就地改为 role='tool'
        4) 未匹配到的“孤儿 tool 消息”丢弃；assistant 中未被消费的 tool_calls 也会被移除
        """
        if not history:
            return history

        ic(f"🔍 开始验证工具调用，历史消息数量: {len(history)}")

        fixed_history = []
        i = 0

        def _is_tool_resp(m: dict) -> bool:
            # 兼容历史：把 'function' 视为 'tool' 并在写入时改回 'tool'
            return isinstance(m, dict) and m.get("role") in ("tool", "function")

        while i < len(history):
            msg = history[i]

            # 1) assistant 带 tool_calls 的消息：逐一检查是否有后续响应（tool）
            if isinstance(msg, dict) and msg.get("tool_calls"):
                ic(f"📞 发现tool_calls消息在位置 {i}")
                valid_tool_calls, invalid_tool_calls = [], []

                for tc in msg["tool_calls"]:
                    tool_call_id = (tc or {}).get("id")
                    ic(f"  检查tool_call_id: {tool_call_id}")
                    if not tool_call_id:
                        invalid_tool_calls.append(tc)
                        continue

                    found_response = False
                    for j in range(i + 1, len(history)):
                        m2 = history[j]
                        if _is_tool_resp(m2):
                            # 若是遗留 'function'，仅用于判断，稍后写回统一改 'tool'
                            m2_id = m2.get("tool_call_id")
                            if m2_id == tool_call_id:
                                ic(f"  ✅ 找到匹配响应在位置 {j}")
                                found_response = True
                                break

                    if found_response:
                        valid_tool_calls.append(tc)
                    else:
                        ic(f"  ❌ 未找到匹配响应: {tool_call_id}")
                        invalid_tool_calls.append(tc)

                if valid_tool_calls:
                    fixed_msg = msg.copy()
                    fixed_msg["tool_calls"] = valid_tool_calls
                    fixed_history.append(fixed_msg)
                    ic(f"  🔧 保留 {len(valid_tool_calls)} 个有效tool_calls，移除 {len(invalid_tool_calls)} 个无效的")
                else:
                    # 没有有效 tool_call：如果还有文本，就保留文本；否则丢弃整条
                    cleaned_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                    content = (cleaned_msg.get("content") or "").strip()
                    if content:
                        fixed_history.append(cleaned_msg)
                        ic(f"  🔧 移除所有tool_calls，保留消息内容")
                    else:
                        ic(f"  🗑️ 完全移除空的tool_calls消息")

            # 2) tool/function 响应：确认是否与上游 tool_calls 配对；无配对则丢弃
            elif _is_tool_resp(msg):
                role = msg.get("role")
                tool_call_id = msg.get("tool_call_id")
                ic(f"🔧 检查工具响应消息: role={role}, tool_call_id={tool_call_id}")

                # 在 fixed_history 中回溯查找是否存在匹配的 assistant.tool_calls
                found_call = False
                for k in range(len(fixed_history) - 1, -1, -1):
                    prev = fixed_history[k]
                    if isinstance(prev, dict) and prev.get("tool_calls"):
                        if any((tc or {}).get("id") == tool_call_id for tc in prev["tool_calls"]):
                            found_call = True
                            break

                if found_call:
                    # 统一将遗留的 'function' 改为 'tool'，与 OpenAI 规范一致
                    if role == "function":
                        msg = dict(msg)
                        msg["role"] = "tool"
                    fixed_history.append(msg)
                    ic(f"  ✅ 保留有效的工具响应（role=tool）")
                else:
                    ic(f"  🗑️ 移除孤立的工具响应: {tool_call_id}")

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
        raw_content = getattr(response.choices[0].message, "content", "") or ""

        # 字符串 -> AgentType 的归一化（保持你现有逻辑）
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

        # ------- 对 Coordinator / Modeler 做严格 JSON 规范化（右侧面板要吃干净 JSON） -------
        content_to_send = raw_content

        if agent_name in (AgentType.COORDINATOR, AgentType.MODELER):
            stripped = TS.strip_fences_outer_or_all(raw_content)
            try:
                # 关键：把 llm=self 交给 JsonFixer，由它内部用 publish=False 调 self.chat
                obj, stage = await JsonFixer.fix_and_parse(
                    stripped,
                    llm=self,
                    agent_name=f"{getattr(agent_name, 'name', str(agent_name))}.JsonFixer",
                )
            except Exception as e:
                logger.exception(f"JsonFixer 调用失败: {e}")
                err_obj = {"error": "jsonfixer_exception", "exc": str(e)}
                content_to_send = json.dumps(err_obj, ensure_ascii=False)
            else:
                if isinstance(obj, dict):
                    # 成功：发布纯 JSON 字符串（仅一层序列化），不要再包 ```json 围栏
                    content_to_send = json.dumps(obj, ensure_ascii=False)
                else:
                    # 解析失败：发布结构化错误对象（避免把脏原文再次传回引起循环）
                    preview = (stripped[:600] + "…") if len(stripped) > 600 else stripped
                    err_obj = {"error": "json_unparseable", "stage": stage, "raw_preview": preview}
                    content_to_send = json.dumps(err_obj, ensure_ascii=False)
                    logger.warning(f"send_message: JSON 解析失败 stage={stage}; 已发布错误对象供上游处理.")

        # 发布给前端（保持原有分支）
        match agent_name:
            case AgentType.CODER:
                agent_msg: CoderMessage = CoderMessage(content=content_to_send)
            case AgentType.WRITER:
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

    def _ensure_first_after_system_user(self, history: list) -> list:
        """
        保证：任意数量的 system 之后，第一条非 system 必须是 user。
        1) 若首条非 system 是 assistant 且内容像“历史对话总结…”，则就地改成 user；
        2) 否则在其前面插入一条简短的 user 承接消息；
        3) 若全是 system（或空），也插入一条最小 user 启动语。
        """
        if not history:
            return [{"role": "user", "content": "[空对话启动] 继续。"}]

        # 找到首个非 system 的索引
        i = 0
        while i < len(history) and isinstance(history[i], dict) and history[i].get("role") == "system":
            i += 1

        # 情况A：全是 system
        if i >= len(history):
            return history + [{"role": "user", "content": "[承接上文上下文] 继续。"}]

        # 情况B：首个非 system 不是 user
        first = history[i] if isinstance(history[i], dict) else {}
        role = first.get("role")
        if role != "user":
            content = (first.get("content") or "").strip()
            # 如果像我们的“历史对话总结…”，直接就地改成 user 更自然
            if role == "assistant" and content.startswith("[历史对话总结"):
                first["role"] = "user"
                history[i] = first
            else:
                # 否则在其前面插入一条最小 user 承接消息
                history = history[:i] + [{"role": "user", "content": "[承接上文上下文] 继续。"}] + history[i:]

        return history


async def simple_chat(model: LLM, history: list) -> str:
    """
    重量版 simple_chat：
    1) 先修复工具消息完整性（避免孤立 tool / 未匹配的 tool_call）
    2) 在总 token 超限时，采用：保留 system + 尾部完整对话片段 + 中段自动摘要
    3) 迭代压缩，直到 <= CONTEXT_TOKEN_HARD_LIMIT 后再发起最终补全
    """

    def quick_count(msg):
        content = (msg or {}).get("content") or ""
        try:
            return token_counter(content, model.model)
        except Exception:
            return max(1, len(content) // 3)

    def tokens_of(messages):
        if not messages:
            return 0
        return sum(quick_count(m) for m in messages if isinstance(m, dict))

    def pair_safe_tail(messages):
        MAX_TAIL_MSGS = 100
        start = max(0, len(messages) - MAX_TAIL_MSGS)
        tail = messages[start:]
        return model._validate_and_fix_tool_calls(tail)

    async def summarize_chunk(chunk_msgs):
        sys_prompt = {
            "role": "system",
            "content": (
                "你是一个对话摘要器。请将以下对话压缩为一段简洁的中文总结，"
                "保留任务目标、关键约束、重要结论和已完成步骤，去除无关细节。"
                "输出不超过 300~600 字。"
            ),
        }
        user_prompt = {
            "role": "user",
            "content": "\n".join(
                f"{m.get('role')}: { (m.get('content') or '')[:2000] }" for m in chunk_msgs if isinstance(m, dict)
            ),
        }
        msgs = sanitize_messages_for_openai([sys_prompt, user_prompt])
        kwargs = {
            "api_key": model.api_key,
            "model": model.model,
            "messages": msgs,
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
        # **保证 system 后第一条是 user**
        ready = [sys_msg] + body if sys_msg else body
        ready = model._ensure_first_after_system_user(ready)
        msgs = sanitize_messages_for_openai(ready)

        kwargs = {
            "api_key": model.api_key,
            "model": model.model,
            "messages": msgs,
            "stream": False,
            "request_timeout": REQUEST_TIMEOUT,
            "client_args": {"timeout": HTTPX_TIMEOUTS},
        }
        if model.base_url:
            kwargs["base_url"] = model.base_url
        resp = await acompletion(**kwargs)
        return resp.choices[0].message.content

    # ========== 重量压缩流程 ==========
    MAX_SUMMARY_ROUNDS = 3
    for round_idx in range(MAX_SUMMARY_ROUNDS):
        tail = pair_safe_tail(body)
        SUMMARY_BUDGET_HINT = 1500

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
            tail = []

        cut_at = len(body) - len(tail)
        head = body[: max(cut_at, 0)]

        summary_text = ""
        if head:
            try:
                summary_text = await summarize_chunk(head)
            except Exception as e:
                logger.error(f"摘要失败，回退使用简短占位：{e}")
                summary_text = "（对话中段摘要：包含若干步骤、错误修复与中间结论，已省略细节以节省上下文。）"

        # **关键修改：把“历史总结”作为 user 消息喂给模型，仅作上下文**
        summary_msg = {"role": "user", "content": f"[历史对话总结-仅供上下文，无需回复]\n{summary_text}"}

        new_history = ([sys_msg] if sys_msg else []) + [summary_msg] + tail
        new_history = model._validate_and_fix_tool_calls(new_history)

        # **再次保证 system 后第一条是 user**
        new_history = model._ensure_first_after_system_user(new_history)
        exact_total = tokens_of(new_history)

        if exact_total <= CONTEXT_TOKEN_HARD_LIMIT:
            msgs = sanitize_messages_for_openai(new_history)
            kwargs = {
                "api_key": model.api_key,
                "model": model.model,
                "messages": msgs,
                "stream": False,
                "request_timeout": REQUEST_TIMEOUT,
                "client_args": {"timeout": HTTPX_TIMEOUTS},
            }
            if model.base_url:
                kwargs["base_url"] = model.base_url
            resp = await acompletion(**kwargs)
            return resp.choices[0].message.content

        body = head + tail  # 下一轮继续压缩

    # 多轮仍超限：退而求其次 —— 仅保留 system + 极短摘要（仍为 user）
    try:
        minimal_summary = await summarize_chunk(body[:200])
    except Exception:
        minimal_summary = "（超长上下文，已压缩为极短摘要。）"

    final_history = ([sys_msg] if sys_msg else []) + [
        {"role": "user", "content": f"[历史对话极简总结-仅供上下文，无需回复]\n{minimal_summary}"}
    ]
    final_history = model._ensure_first_after_system_user(final_history)
    msgs = sanitize_messages_for_openai(final_history)

    kwargs = {
        "api_key": model.api_key,
        "model": model.model,
        "messages": msgs,
        "stream": False,
        "request_timeout": REQUEST_TIMEOUT,
        "client_args": {"timeout": HTTPX_TIMEOUTS},
    }
    if model.base_url:
        kwargs["base_url"] = model.base_url
    resp = await acompletion(**kwargs)
    return resp.choices[0].message.content
