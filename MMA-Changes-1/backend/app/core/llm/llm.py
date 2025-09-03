# app/core/llm/llm.py

# 1 依赖
import json
import codecs
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

# 2 文本处理工具
from app.tools.text_sanitizer import TextSanitizer as TS
from app.tools.json_fixer import JsonFixer

# 3 全局配置
# 3.1 请求/重试
REQUEST_TIMEOUT = 600
HTTPX_TIMEOUTS = {"connect": 120, "read": 120, "write": 240, "pool": 120}
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE = 0.8

# 3.2 上下文保护
CONTEXT_TOKEN_HARD_LIMIT = 120_000

# 3.3 严格参数 + 轻清洗（面板 JSON 发布）
STRICT_JSON_ONLY = True  # 仅接受严格 JSON（dict），禁用 LLM 重建
LIGHT_CLEANING = True  # 只去控制字符与最外层围栏，不改写语义

litellm.callbacks = [agent_metrics]

# 4 最后一跳消息清洗
_ALLOWED_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}


def _json_dumps_safe(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


# === 放在 llm.py 顶部工具函数区域（_json_dumps_safe 之后）===
def _pretty_preview_messages(msgs: List[Dict[str, Any]], max_len: int = 2000) -> str:
    """
    将 messages 打印为易读的多行文本，截断 content，隐藏 tool arguments 的长串。
    仅用于调试日志。
    """
    lines = []
    for i, m in enumerate(msgs or []):
        if not isinstance(m, dict):
            lines.append(f"[{i}] <non-dict> {type(m).__name__}")
            continue
        role = m.get("role")
        tc = m.get("tool_calls")
        tc_info = ""
        if isinstance(tc, list) and tc:
            brief = []
            for t in tc:
                if not isinstance(t, dict):
                    continue
                fid = t.get("id")
                fn = (t.get("function") or {}).get("name")
                fargs = (t.get("function") or {}).get("arguments")
                alen = len(fargs) if isinstance(fargs, str) else -1
                brief.append(f"(id={fid}, fn={fn}, args_len={alen})")
            tc_info = f" tool_calls={brief}"
        tcid = m.get("tool_call_id")
        content = m.get("content")
        if isinstance(content, (dict, list)):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except Exception:
                content = str(content)
        cprev = content or ""
        if isinstance(cprev, str) and len(cprev) > max_len:
            cprev = cprev[:max_len] + "…"
        lines.append(f"[{i}] role={role}{tc_info}{(' tool_call_id='+tcid) if tcid else ''} | {repr(cprev)}")
    return "\n".join(lines)


# Replace _extract_tool_text function
def _extract_tool_text(msg: Dict[str, Any]) -> str:
    """4.1 从 tool 消息尽量提炼可读文本"""
    extracted = []

    out = msg.get("output") or msg.get("outputs") or msg.get("result") or msg.get("results")
    if out is None:  # Explicit null handling
        return "(tool output was null or empty)"

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

    for k in ("text", "stdout", "stderr", "data", "value"):
        v = msg.get(k)
        if v:
            extracted.append(_json_dumps_safe(v) if isinstance(v, (list, dict)) else str(v))

    tc = msg.get("tool_result") or msg.get("tool_response") or msg.get("tool_outputs")
    if tc is not None:
        extracted.append(_json_dumps_safe(tc))

    parts, seen = [], set()
    for s in (x.strip() for x in extracted if isinstance(x, str)):
        if s and s not in seen:
            seen.add(s)
            parts.append(s)
    text = "\n".join(parts)
    return text if text.strip() else "(tool returned no text)"  # Final fallback


def _looks_like_literal_escapes(s: str) -> bool:
    return TS.looks_like_literal_escapes(s)


def _stringify_tool_calls(tc_list: Any) -> Any:
    # 保留以防旧调用引用；不再对 tool_calls 做任何“规范化/重编码”
    return tc_list


def _flatten_tool_messages(
    messages: List[Dict[str, Any]], drop_assistant_tool_calls: bool = True
) -> List[Dict[str, Any]]:
    """
    将 tool 消息展平成 assistant 文本消息：
    - 把 tool 的文本（通过 _extract_tool_text）转成 assistant 的普通 content，插在对应的 tool_call 后面
    - 可选地删除 assistant.tool_calls，避免后端把它当作真实工具调用
    """
    out: List[Dict[str, Any]] = []
    pending_texts: Dict[str, List[str]] = {}

    for m in messages or []:
        role = m.get("role")

        if role == "assistant":
            # 在推入上一个 assistant 前，把它可能对应的 pending tool 文本折叠进去
            if drop_assistant_tool_calls and "tool_calls" in m:
                m = {k: v for k, v in m.items() if k != "tool_calls"}

            # 如果上轮有工具文本，前置合并
            merged = dict(m)
            add_parts: List[str] = []
            # 将上一轮积累的 tool 文本全部拼到这条 assistant 之后
            # （注意：严格匹配窗口可更复杂，这里采用简单累加）
            for _, texts in list(pending_texts.items()):
                if texts:
                    add_parts.append("\n".join(texts))
            pending_texts.clear()

            if add_parts:
                base = (merged.get("content") or "").strip()
                extra = "\n".join(add_parts).strip()
                merged["content"] = (base + ("\n\n" if base and extra else "") + extra) if (base or extra) else ""
            out.append(merged)

        elif role == "tool":
            tcid = m.get("tool_call_id")
            text = _extract_tool_text(m).strip()
            if not text:
                # 兜底，也许 tool.content 里有东西
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    text = c.strip()
                elif isinstance(c, (list, dict)):
                    try:
                        text = json.dumps(c, ensure_ascii=False)
                    except Exception:
                        pass
            if not text:
                text = "(tool returned no text)"
            pending_texts.setdefault(tcid or "", []).append(text)

        else:
            # user / system 原样保留，但如果有 pending 文本，也合并到前一条 assistant（若存在）
            if pending_texts and out and out[-1].get("role") == "assistant":
                parts = []
                for _, texts in list(pending_texts.items()):
                    if texts:
                        parts.append("\n".join(texts))
                if parts:
                    base = (out[-1].get("content") or "").strip()
                    extra = "\n".join(parts).strip()
                    out[-1]["content"] = (base + ("\n\n" if base and extra else "") + extra) if (base or extra) else ""
                pending_texts.clear()
            out.append(m)

    # 结束时若还有 pending 文本，且最后一条是 assistant，就并过去
    if pending_texts and out and out[-1].get("role") == "assistant":
        parts = []
        for _, texts in pending_texts.items():
            if texts:
                parts.append("\n".join(texts))
        if parts:
            base = (out[-1].get("content") or "").strip()
            extra = "\n".join(parts).strip()
            out[-1]["content"] = (base + ("\n\n" if base and extra else "") + extra) if (base or extra) else ""

    # 移除末尾的 tool（理论上不会再有）
    while out and out[-1].get("role") == "tool":
        out.pop()

    return out


def _preflight_validate_messages(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    发送前的最保守兜底：
    1) 删去任何非字典项
    2) 丢弃空 content（但 assistant+tool_calls 允许无 content）
    3) 确保首条非 system 是 user；若缺失则插入最小 user
    4) 丢弃最后的 tool
    5) 关键：为 role=tool 的消息，从 output/stdout 等字段提取文本，强制填充到 content
    """
    msgs = [m for m in (msgs or []) if isinstance(m, dict)]
    if not msgs:
        return [{"role": "user", "content": "[空对话启动] 继续。"}]

    # 首条非 system 必须是 user
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "system":
        i += 1
    if i >= len(msgs) or msgs[i].get("role") != "user":
        msgs = msgs[:i] + [{"role": "user", "content": "[承接上文上下文] 继续。"}] + msgs[i:]

    # 去掉末尾的 tool（先做一轮）
    while msgs and msgs[-1].get("role") == "tool":
        msgs.pop()

    cleaned: List[Dict[str, Any]] = []
    for m in msgs:
        role = m.get("role")
        m2: Dict[str, Any] = {"role": role}

        # 仅 assistant 复制 tool_calls
        if role == "assistant" and isinstance(m.get("tool_calls"), list) and m["tool_calls"]:
            m2["tool_calls"] = m["tool_calls"]

        # tool 消息：保留 tool_call_id，并且**强制构造非空 content**
        if role == "tool":
            tcid = m.get("tool_call_id")
            if isinstance(tcid, str) and tcid.strip():
                m2["tool_call_id"] = tcid
            # 关键：把 output/stdout/stderr/result 等字段抽到 content
            content_text = _extract_tool_text(m).strip()
            if not content_text:
                # 兜底：试试原 content 的 JSON 化
                c = m.get("content")
                if isinstance(c, (list, dict)):
                    try:
                        content_text = json.dumps(c, ensure_ascii=False)
                    except Exception:
                        pass
                elif isinstance(c, str):
                    content_text = c.strip()
            if not content_text:
                content_text = "(tool returned no text)"  # 必须是非空字符串
            m2["content"] = content_text

        else:
            # 其它角色：统一把 content 变成字符串
            c = m.get("content")
            if isinstance(c, str):
                if c.strip():
                    m2["content"] = c
            elif isinstance(c, (dict, list)):
                try:
                    s = json.dumps(c, ensure_ascii=False)
                    if s.strip():
                        m2["content"] = s
                except Exception:
                    pass

            # 没内容而且也没有 tool_calls（非 assistant）就跳过
            if role != "assistant" and "tool_calls" not in m2 and "content" not in m2:
                continue

        cleaned.append(m2)

    # 再次去掉末尾的 tool（双保险）
    while cleaned and cleaned[-1].get("role") == "tool":
        cleaned.pop()

    if not cleaned:
        return [{"role": "user", "content": "[空对话启动] 继续。"}]

    return cleaned


def sanitize_messages_for_openai(messages: List[Dict[str, Any]]):
    """
    统一规整 messages，尽可能符合最保守的 OpenAI 兼容格式：
    1) 仅允许 role in {system,user,assistant,tool}
    2) content 必须为非空字符串；assistant+tool_calls 可以无 content
    3) tool 必须能匹配到历史 assistant.tool_calls[*].id；且不携带 name
    4) 丢弃空/无意义消息；合并相邻同角色（user/assistant，纯文本且均不含 tool_calls）
    5) 确保最后一条不是 tool；若全是 system，补一条最小 user
    6) 新增：对 tool.content 的 None/空串做兜底；尝试从 outputs/result 等字段提取文本
    """
    allowed_roles = {"system", "user", "assistant", "tool"}
    result: List[Dict[str, Any]] = []
    assistant_call_ids: set[str] = set()

    def _nonempty_str(x) -> bool:
        return isinstance(x, str) and x.strip() != ""

    for _, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in allowed_roles:
            continue

        clean: Dict[str, Any] = {"role": role}

        # ------- 通用 content 规范化（除 tool 外的角色）-------
        if role != "tool" and "content" in msg and msg.get("content") is not None:
            c = msg.get("content")
            if isinstance(c, str):
                if _nonempty_str(c):
                    clean["content"] = c
            else:
                try:
                    c2 = json.dumps(c, ensure_ascii=False)
                    if _nonempty_str(c2):
                        clean["content"] = c2
                except Exception:
                    pass

        # ------- assistant 的 tool_calls 规范化 -------
        if role == "assistant":
            tcs = msg.get("tool_calls")
            if isinstance(tcs, list) and len(tcs) > 0:
                valid_calls = []
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    f = tc.get("function") or {}
                    name = f.get("name")
                    args = f.get("arguments")
                    if not (isinstance(name, str) and name.strip()):
                        continue
                    if args is None:
                        args_str = "{}"
                    elif isinstance(args, (dict, list)):
                        try:
                            args_str = json.dumps(args, ensure_ascii=False)
                        except Exception:
                            args_str = "{}"
                    elif isinstance(args, str):
                        args_str = args
                    else:
                        args_str = str(args)
                    tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                    assistant_call_ids.add(tc_id)
                    valid_calls.append(
                        {"id": tc_id, "type": "function", "function": {"name": name, "arguments": args_str}}
                    )
                if valid_calls:
                    clean["tool_calls"] = valid_calls
                    # 有 tool_calls 时允许无 content
                    if not _nonempty_str(clean.get("content", "")):
                        clean.pop("content", None)

        # ------- tool 的内容兜底 & 匹配 -------
        if role == "tool":
            tcid = msg.get("tool_call_id")
            if not isinstance(tcid, str) or not tcid.strip():
                # 没有匹配 id 的 tool 直接跳过
                continue
            clean["tool_call_id"] = tcid

            # *关键*：从多来源提取文本作为 content（避免空导致被丢弃）
            content = msg.get("content")
            text = ""
            if isinstance(content, str) and content.strip():
                text = content.strip()
            elif isinstance(content, (dict, list)):
                try:
                    text = json.dumps(content, ensure_ascii=False)
                except Exception:
                    text = ""
            if not text:
                # 尝试从其它字段提取
                text = _extract_tool_text(msg).strip()
            if not text:
                # 仍然为空，兜底为占位
                text = "(tool returned empty content)"

            clean["content"] = text  # tool 必须带非空字符串内容

        # ------- 丢弃无意义项 -------
        if role != "assistant" and "tool_calls" not in clean and not _nonempty_str(clean.get("content", "")):
            continue

        result.append(clean)

    # 合并相邻 user/assistant 纯文本
    merged: List[Dict[str, Any]] = []
    for m in result:
        if merged:
            last = merged[-1]
            if (
                last["role"] == m["role"]
                and last["role"] in ("user", "assistant")
                and not last.get("tool_calls")
                and not m.get("tool_calls")
            ):
                a = (last.get("content") or "").strip()
                b = (m.get("content") or "").strip()
                s = (a + ("\n\n" if a and b else "") + b).strip()
                if s:
                    last["content"] = s
                    continue
        merged.append(m)

    # 不能以 tool 结尾
    while merged and merged[-1]["role"] == "tool":
        merged.pop()

    if not merged:
        return [{"role": "user", "content": "[空对话启动] 继续。"}]
    if all(m.get("role") == "system" for m in merged):
        merged.append({"role": "user", "content": "[承接上文上下文] 继续。"})
    logger.info("🧹 sanitize_messages_for_openai =>\n" + _pretty_preview_messages(merged))
    return merged


# 修改 _audit_openai_messages 函数（完整替换原函数）
def _audit_openai_messages(messages: List[Dict[str, Any]]) -> tuple[bool, List[str]]:
    problems: List[str] = []
    allowed_roles = {"system", "user", "assistant", "tool"}

    if not isinstance(messages, list):
        return False, ["messages 不是 list"]

    pending_ids: set[str] = set()
    first_non_system_idx = None

    for idx, m in enumerate(messages):
        if not isinstance(m, dict):
            problems.append(f"[{idx}] 非 dict 项：{type(m).__name__}")
            continue
        role = m.get("role")
        if role not in allowed_roles:
            problems.append(f"[{idx}] 非法 role={role}")
            continue

        if role != "system" and first_non_system_idx is None:
            first_non_system_idx = idx

        content = m.get("content") or ""  # 强制字符串，null变空

        if role == "assistant":
            if pending_ids:
                problems.append(f"[{idx}] 上一轮 assistant.tool_calls 未见对应 tool 响应：{sorted(pending_ids)}")
                pending_ids.clear()

            tcs = m.get("tool_calls")
            has_tcs = isinstance(tcs, list) and tcs  # 非空 tool_calls
            if tcs is not None:
                if not isinstance(tcs, list):
                    problems.append(f"[{idx}] assistant.tool_calls 不是 list")
                else:
                    for j, tc in enumerate(tcs):
                        if not isinstance(tc, dict):
                            problems.append(f"[{idx}] tool_calls[{j}] 不是 dict")
                            continue
                        if tc.get("type") != "function":
                            problems.append(f"[{idx}] tool_calls[{j}].type 必须为 'function'")
                        func = tc.get("function") or {}
                        name = func.get("name")
                        args = func.get("arguments")
                        if not isinstance(name, str) or not name.strip():
                            problems.append(f"[{idx}] tool_calls[{j}].function.name 缺失/非法")
                        if not isinstance(args, str):
                            problems.append(f"[{idx}] tool_calls[{j}].function.arguments 必须是字符串（JSON）")
                        tcid = tc.get("id")
                        if not isinstance(tcid, str) or not tcid.strip():
                            problems.append(f"[{idx}] tool_calls[{j}].id 缺失/非法")
                        else:
                            pending_ids.add(tcid)
            # 关键放宽：有 tool_calls 时允许 content 为空
            if not has_tcs and not (isinstance(content, str) and len(content.strip()) >= 1):
                problems.append(
                    f"[{idx}] assistant 没有 tool_calls 时 content 必须是非空字符串 (当前len={len(content)})"
                )

        elif role == "tool":
            tcid = m.get("tool_call_id")
            if not isinstance(tcid, str) or not tcid.strip():
                problems.append(f"[{idx}] tool.tool_call_id 缺失/非法")
            else:
                if tcid not in pending_ids:
                    problems.append(f"[{idx}] 未找到对应的 assistant.tool_calls id={tcid}")
                else:
                    pending_ids.discard(tcid)
            if not (isinstance(content, str) and len(content.strip()) >= 1):
                problems.append(f"[{idx}] tool.content 必须是非空字符串 (当前len={len(content)})")
            if "name" in m:
                problems.append(f"[{idx}] tool 不应携带 name 字段")

        else:  # user/system
            if not (isinstance(content, str) and content.strip()):
                problems.append(f"[{idx}] {role}.content 必须是非空字符串")

    if first_non_system_idx is None:
        problems.append("全是 system；缺少 user 启动消息")
    else:
        if messages[first_non_system_idx].get("role") != "user":
            problems.append(
                f"第一条非 system 必须是 user（当前 idx={first_non_system_idx}, role={messages[first_non_system_idx].get('role')})"
            )

    if messages and messages[-1].get("role") == "tool":
        problems.append("最后一条消息不能是 tool")

    if pending_ids:
        problems.append(f"存在未匹配的 tool 响应：{sorted(pending_ids)}")

    if problems:
        logger.error("审计问题详情：\n" + "\n".join(problems))
        logger.error("失败messages预览：\n" + _pretty_preview_messages(messages))

    return (len(problems) == 0), problems


# 5 LLM 封装
class LLM:
    # 5.1 初始化
    def __init__(self, api_key: str, model: str, base_url: str, task_id: str):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.chat_count = 0
        self.max_tokens: int | None = None
        self.task_id = task_id

    # 5.2 主入口：发送对话（含工具消息修复/裁剪/审计/可选发布）
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
        publish: bool = True,
    ) -> object:
        # 5.2.1 记录副标题
        logger.info(f"subtitle是:{sub_title}")

        # 5.2.2 历史规整（工具配对 / token 截断 / 首条非 system → user）
        if history:
            history = self._validate_and_fix_tool_calls(history)
            history = self._truncate_history_by_tokens(history, CONTEXT_TOKEN_HARD_LIMIT)
            history = self._ensure_first_after_system_user(history)

        # 5.2.3 预清洗 + 预验
        safe_messages_before_preflight = sanitize_messages_for_openai(history or [])
        logger.info("🧾 sanitize 预览：\n" + _pretty_preview_messages(safe_messages_before_preflight))

        safe_messages_after_preflight = _preflight_validate_messages(safe_messages_before_preflight)
        logger.info("🧾 preflight 预览：\n" + _pretty_preview_messages(safe_messages_after_preflight))

        safe_messages = safe_messages_after_preflight

        # 5.2.4 tool 支持能力探测与“展平策略”
        contains_tool_role = any(m.get("role") == "tool" for m in safe_messages)
        contains_assistant_tool_calls = any(
            (m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list) and m["tool_calls"])
            for m in safe_messages
        )
        if (contains_tool_role or contains_assistant_tool_calls) and not tools:
            logger.warning("⚠️ 历史包含 tool / tool_calls，但本次 chat 未传 tools，部分后端会拒收。将视配置尝试展平。")

        supports_tool_role = True
        try:
            import os as _os

            supports_tool_role = _os.getenv("MMA_SUPPORTS_TOOL_ROLE", "1") == "1"
        except Exception:
            pass

        need_flatten = (contains_tool_role or contains_assistant_tool_calls) and (
            (not tools) or (not supports_tool_role)
        )
        if need_flatten:
            safe_messages = _flatten_tool_messages(
                safe_messages,
                drop_assistant_tool_calls=(not supports_tool_role or not tools),
            )
            logger.info("🔧 已展平 tool 消息为 assistant 文本，以兼容不支持 tool-role 的后端或本轮未传 tools 的情况。")
            logger.info("🧾 flatten 预览：\n" + _pretty_preview_messages(safe_messages))

        # 5.2.5 严格审计（必要时二次展平修复）
        ok, probs = _audit_openai_messages(safe_messages)
        if not ok:
            logger.error("🚫 OpenAI 消息审计未通过，具体问题如下：")
            for p in probs:
                logger.error(" - " + p)
            logger.error("🧾 清洗后 messages 预览：\n" + _pretty_preview_messages(safe_messages))
            logger.warning("🔧 审计失败，尝试自动展平tool消息并重试审计。")
            flattened = _flatten_tool_messages(safe_messages, drop_assistant_tool_calls=True)
            ok_flatten, probs_flatten = _audit_openai_messages(flattened)
            if ok_flatten:
                safe_messages = flattened
                logger.info("✅ 展平后审计通过，继续。")
            else:
                logger.error("🚫 展平后仍失败，放弃。")
                raise ValueError('"messages" failed strict audit before acompletion')
        else:
            logger.info("✅ OpenAI 消息审计通过。")
            logger.info("🧾 清洗后 messages 预览：\n" + _pretty_preview_messages(safe_messages))

        # 5.2.6 组装补全参数
        kwargs = {
            "api_key": self.api_key,
            "model": self.model,
            "messages": safe_messages,
            "stream": False,
            "metadata": {"agent_name": getattr(agent_name, "name", str(agent_name))},
            "request_timeout": REQUEST_TIMEOUT,
            "client_args": {"timeout": HTTPX_TIMEOUTS},
        }
        kwargs["top_p"] = top_p if top_p is not None else None
        if tools and supports_tool_role:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if self.base_url:
            kwargs["base_url"] = self.base_url

        # 5.2.7 打码打印（避免泄露敏感字段）
        def _redact(d: dict) -> dict:
            safe = dict(d)
            if "api_key" in safe:
                safe["api_key"] = "***"
            if "client_args" in safe:
                safe["client_args"] = {"timeout": "(configured)"}
            if "messages" in safe:
                safe["messages"] = f"[{len(safe['messages'])} messages]"
            return safe

        # 5.2.8 调用与重试（注意：不再改写空 content 为占位文本）
        for attempt in range(max_retries):
            try:
                response = await acompletion(**kwargs)
                if not response or not hasattr(response, "choices"):
                    raise ValueError("无效的API响应")

                # 5.2.8.1 不做“空 content → 占位字符串”的改写；是否发布交给 send_message 决定
                if publish:
                    published = await self.send_message(response, agent_name, sub_title)
                    if published:
                        self.chat_count += 1
                return response

            except asyncio.CancelledError:
                logger.warning("请求被上层取消（CancelledError），不重试。")
                raise
            except (litellm.BadRequestError, litellm.AuthenticationError, litellm.NotFoundError) as e:
                msg = str(e)
                if "context" in msg.lower():
                    logger.error("上下文超限，请在进入 acompletion 前已充分截断。")
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
                    logger.info(f"请求参数: {_redact(kwargs)}")
                    raise
                delay = retry_delay * (2**attempt) + random.random() * 0.3
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"第 {attempt + 1}/{max_retries} 次重试（未知异常）: {e}")
                if attempt >= max_retries - 1:
                    logger.info(f"请求参数: {_redact(kwargs)}")
                    raise
                delay = retry_delay * (2**attempt) + random.random() * 0.3
                await asyncio.sleep(delay)

    # 5.3 工具调用配对/清洗
    def _validate_and_fix_tool_calls(self, history: list) -> list:
        """
        5.3 修复工具调用完整性：
        1) 角色合法化；2) assistant.tool_calls 与后续 tool 配对；
        3) 遗留 function → tool；4) 孤儿 tool 丢弃。
        """
        if not history:
            return history

        ic(f"🔍 开始验证工具调用，历史消息数量: {len(history)}")
        fixed_history = []
        i = 0

        def _is_tool_resp(m: dict) -> bool:
            return isinstance(m, dict) and m.get("role") in ("tool", "function")

        while i < len(history):
            msg = history[i]

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
                            if m2.get("tool_call_id") == tool_call_id:
                                ic(f"  ✅ 找到匹配响应在位置 {j}")
                                found_response = True
                                break

                    (valid_tool_calls if found_response else invalid_tool_calls).append(tc)

                if valid_tool_calls:
                    fixed_msg = msg.copy()
                    fixed_msg["tool_calls"] = valid_tool_calls
                    fixed_history.append(fixed_msg)
                    ic(f"  🔧 保留 {len(valid_tool_calls)} 个有效tool_calls，移除 {len(invalid_tool_calls)} 个无效的")
                else:
                    cleaned_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                    content = (cleaned_msg.get("content") or "").strip()
                    if content:
                        fixed_history.append(cleaned_msg)
                        ic("  🔧 移除所有tool_calls，保留消息内容")
                    else:
                        ic("  🗑️ 完全移除空的tool_calls消息")

            elif _is_tool_resp(msg):
                role = msg.get("role")
                tool_call_id = msg.get("tool_call_id")
                ic(f"🔧 检查工具响应消息: role={role}, tool_call_id={tool_call_id}")

                found_call = False
                for k in range(len(fixed_history) - 1, -1, -1):
                    prev = fixed_history[k]
                    if isinstance(prev, dict) and prev.get("tool_calls"):
                        if any((tc or {}).get("id") == tool_call_id for tc in prev["tool_calls"]):
                            found_call = True
                            break

                if found_call:
                    if role == "function":
                        msg = dict(msg)
                        msg["role"] = "tool"
                    fixed_history.append(msg)
                    ic("  ✅ 保留有效的工具响应（role=tool）")
                else:
                    ic(f"  🗑️ 移除孤立的工具响应: {tool_call_id}")

            else:
                fixed_history.append(msg)

            i += 1

        if len(fixed_history) != len(history):
            ic(f"🔧 修复完成: {len(history)} -> {len(fixed_history)} 条消息")
        else:
            ic("✅ 验证通过，无需修复")

        return fixed_history

    # 5.4 历史按 token 截断（保留首个 system + 尾段）
    def _truncate_history_by_tokens(self, history: list, token_limit: int) -> list:
        """
        5.4 基于 token 的裁剪：保留首条 system + 尾部连续片段（再次做工具配对修复）。
        """
        if not history:
            return history

        def msg_tokens(msg: dict) -> int:
            content = msg.get("content") or ""
            try:
                return token_counter(self.model, content)
            except Exception:
                return max(1, len(content) // 3)

        system_msg = None
        start_idx = 0
        if history[0].get("role") == "system":
            system_msg = history[0]
            start_idx = 1

        total = (msg_tokens(system_msg) if system_msg else 0) + sum(msg_tokens(m) for m in history[start_idx:])
        if total <= token_limit:
            return history

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
        new_history = self._validate_and_fix_tool_calls(new_history)
        return new_history

    # 5.5 发布到右侧面板（含 JSON 严格化与“仅 tool_calls 空文本轮次跳过发布”）
    async def send_message(self, response, agent_name, sub_title=None) -> bool:
        # 5.5.1 读取本轮模型消息
        logger.info(f"subtitle是:{sub_title}")
        msg = response.choices[0].message
        raw_content = getattr(msg, "content", "") or ""

        # 5.5.2 仅 tool_calls 且无文本 → 跳过发布（返回 False，不计入 chat_count）
        has_tool_calls = bool(getattr(msg, "tool_calls", None))
        if has_tool_calls and not raw_content.strip():
            logger.info("跳过发布：assistant 仅含 tool_calls 且无文本（等待工具输出/下一轮总结）")
            return False

        content_to_send = raw_content

        # 5.5.3 归一 AgentType
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
            agent_name = mapping.get(key) or (
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

        # 5.5.4 Coordinator / Modeler：严格 JSON 发布（配合 STRICT_JSON_ONLY + LIGHT_CLEANING）
        if agent_name in (AgentType.COORDINATOR, AgentType.MODELER):
            prepared = raw_content
            if LIGHT_CLEANING:
                prepared = TS.clean_control_chars(prepared, keep_whitespace=True)
                prepared = TS.strip_fences_outer_or_all(prepared)

            try:
                obj, stage = await JsonFixer.fix_and_parse(
                    raw=prepared,
                    llm=None if STRICT_JSON_ONLY else self,
                    agent_name=f"{getattr(agent_name, 'name', str(agent_name))}.JsonFixer",
                )
                logger.info(f"[send_message] JsonFixer stage: {stage}")
            except Exception as e:
                logger.exception(f"JsonFixer 调用失败: {e}")
                err_obj = {"error": "jsonfixer_exception", "exc": str(e)}
                content_to_send = json.dumps(err_obj, ensure_ascii=False)
            else:
                if isinstance(obj, dict):
                    content_to_send = json.dumps(obj, ensure_ascii=False)
                else:
                    preview = (prepared[:2000] + "…") if len(prepared) > 2000 else prepared
                    err_obj = {"error": "json_unparseable", "stage": stage, "raw_preview": preview}
                    content_to_send = json.dumps(err_obj, ensure_ascii=False)
                    logger.warning(f"send_message: JSON 解析失败 stage={stage}; 已发布错误对象供上游处理.")

        # 5.5.5 类型分派并发布
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
        return True  # 成功发布

    # 5.6 保证首条非 system 为 user（对话承接）
    def _ensure_first_after_system_user(self, history: list) -> list:
        """
        5.6 保证：任意数量的 system 之后，第一条非 system 必须是 user。
        A) 若首条非 system 是 assistant 且内容像“历史对话总结…”，就地改为 user；
        B) 否则在其前插入最小 user 承接语；
        C) 若全是 system 或空，也插入一条最小 user。
        """
        if not history:
            return [{"role": "user", "content": "[空对话启动] 继续。"}]

        i = 0
        while i < len(history) and isinstance(history[i], dict) and history[i].get("role") == "system":
            i += 1

        if i >= len(history):
            return history + [{"role": "user", "content": "[承接上文上下文] 继续。"}]

        first = history[i] if isinstance(history[i], dict) else {}
        role = first.get("role")
        if role != "user":
            content = (first.get("content") or "").strip()
            if role == "assistant" and content.startswith("[历史对话总结"):
                first["role"] = "user"
                history[i] = first
            else:
                history = history[:i] + [{"role": "user", "content": "[承接上文上下文] 继续。"}] + history[i:]

        return history


# 6 简单聊天（含上下文压缩）
async def simple_chat(model: LLM, history: list) -> str:
    """
    6.1 先做工具配对修复；
    6.2 若超限：保留 system + 尾部片段，前段做摘要（多轮递减）；
    6.3 始终保证 system 后第一条是 user，再发起补全。
    """

    # 6.1 计数工具（兼容不同 token_counter 签名）
    def quick_count(msg):
        content = (msg or {}).get("content") or ""
        try:
            return token_counter(model=model.model, text=content)
        except Exception:
            try:
                return token_counter(model.model, content)
            except Exception:
                return max(1, len(content) // 3)

    def tokens_of(messages):
        if not messages:
            return 0
        return sum(quick_count(m) for m in messages if isinstance(m, dict))

    def pair_safe_tail(messages):
        MAX_TAIL_MSGS = 300
        start = max(0, len(messages) - MAX_TAIL_MSGS)
        tail = messages[start:]
        return model._validate_and_fix_tool_calls(tail)

    async def summarize_chunk(chunk_msgs, retries: int = 2):
        sys_prompt = {
            "role": "system",
            "content": (
                "你是一个对话摘要器。请将以下对话压缩为一段简洁的中文总结，"
                "保留任务目标、关键约束、重要结论和已完成步骤，去除无关细节。输出不超过 600 字。"
            ),
        }
        user_prompt = {
            "role": "user",
            "content": "\n".join(
                f"{m.get('role')}: {(m.get('content') or '')[:2000]}" for m in chunk_msgs if isinstance(m, dict)
            ),
        }
        msgs = sanitize_messages_for_openai([sys_prompt, user_prompt])
        for attempt in range(retries + 1):
            try:
                kwargs2 = {
                    "api_key": model.api_key,
                    "model": model.model,
                    "messages": msgs,
                    "stream": False,
                    "request_timeout": REQUEST_TIMEOUT,
                    "client_args": {"timeout": HTTPX_TIMEOUTS},
                }
                if model.base_url:
                    kwargs2["base_url"] = model.base_url
                resp = await acompletion(**kwargs2)
                return resp.choices[0].message.content.strip()
            except Exception as e:
                if attempt >= retries:
                    raise
                await asyncio.sleep(0.6 * (2**attempt))  # 0.6s, 1.2s

    # 6.2 工具配对与系统消息抽取
    history = history or []
    history = model._validate_and_fix_tool_calls(history)
    sys_msg = history[0] if (history and history[0].get("role") == "system") else None
    start_idx = 1 if sys_msg else 0
    body = history[start_idx:]

    total_tokens = (quick_count(sys_msg) if sys_msg else 0) + tokens_of(body)
    if total_tokens <= CONTEXT_TOKEN_HARD_LIMIT:
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

    # 6.3 超限：多轮“头部摘要 + 尾部保留”
    MAX_SUMMARY_ROUNDS = 3
    for _ in range(MAX_SUMMARY_ROUNDS):
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

        try:
            summary_text = await summarize_chunk(head) if head else ""
        except Exception as e:
            logger.error(f"摘要失败，回退使用占位：{e}")
            summary_text = "（对话中段摘要：包含若干步骤与中间结论，已省略细节。）"

        summary_msg = {"role": "user", "content": f"[历史对话总结-仅供上下文，无需回复]\n{summary_text}"}
        new_history = ([sys_msg] if sys_msg else []) + [summary_msg] + tail
        new_history = model._validate_and_fix_tool_calls(new_history)
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

    # 6.4 兜底：极简摘要 + 最短上下文
    try:
        minimal_summary = await summarize_chunk(body[:2000])
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
