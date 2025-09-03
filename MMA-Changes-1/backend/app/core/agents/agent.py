# app/core/agents/agent.py

# 1 依赖
import json
from app.core.llm.llm import LLM, simple_chat
from app.utils.log_util import logger
from icecream import ic
from litellm import token_counter

# 2 文本与 JSON 工具
from app.tools.text_sanitizer import TextSanitizer as TS
from app.tools.json_fixer import JsonFixer

# 3 全局与策略
# 3.1 软阈值（在硬上限前主动清理，避免 ContextWindowExceeded）
SOFT_TOKEN_LIMIT = 100_000

# 3.2 严格参数 + 轻清洗
STRICT_JSON_ONLY = True  # 仅接受严格 JSON（dict）；解析失败直接报错或返回 None
LIGHT_CLEANING = True  # 只移除控制字符与最外层围栏，不改写语义


class Agent:
    """
    4 基类说明
    4.1 统一管理对话历史（system/user/assistant/tool），并提供稳健的 append_chat_history
    4.2 内置“重量版”记忆清理：在超出阈值时，用 simple_chat 生成历史摘要，再拼接尾部上下文
    4.3 提供 fix_and_parse_json：按“严格参数 + 轻清洗”解析 JSON 文本
    """

    # 5 初始化
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 600,
        max_memory: int = 100,
    ) -> None:
        self.task_id = task_id
        self.model = model
        self.chat_history: list[dict] = []
        self.max_chat_turns = max_chat_turns
        self.current_chat_turns = 0
        self.max_memory = max_memory
        self._inited = False  # 仅首次注入 system

    # 6 历史入库前的统一清洗
    @staticmethod
    def sanitize_text_for_history(text: str) -> str:
        """
        6.1 去除控制字符（保留 \t \n \r）
        6.2 去除 ANSI 颜色控制序列
        """
        if text is None:
            return ""
        text = TS.clean_control_chars(text, keep_whitespace=True)
        text = TS.strip_ansi(text)
        return text

    # 7 JSON 解析（严格参数 + 轻清洗）
    async def fix_and_parse_json(self, raw_text: str):
        """
        7.1 预处理：按需做“轻清洗”（控制字符、围栏）
        7.2 调用 JsonFixer.fix_and_parse：
            a) STRICT_JSON_ONLY=True → llm=None（禁用重建，仅本地解析/最小修复）
            b) STRICT_JSON_ONLY=False → llm=self.model（允许一次重建）
        7.3 返回 (obj, stage)，obj 失败为 None，stage 为修复阶段标记
        """
        prepared = raw_text or ""
        if LIGHT_CLEANING:
            prepared = TS.clean_control_chars(prepared, keep_whitespace=True)
            prepared = TS.strip_fences_outer_or_all(prepared)

        obj, stage = await JsonFixer.fix_and_parse(
            raw=prepared,
            llm=None if STRICT_JSON_ONLY else self.model,
            agent_name=f"{self.__class__.__name__}.JsonFixer",
        )
        return obj, stage

    # 8 默认 run（子类可覆盖）
    async def run(self, prompt: str, system_prompt: str, sub_title: str) -> str:
        """
        8.1 首次注入 system
        8.2 追加用户消息
        8.3 调用模型并返回 assistant 文本
        """
        try:
            logger.info(f"{self.__class__.__name__}:开始:执行对话")
            self.current_chat_turns = 0

            if not self._inited:
                await self.append_chat_history({"role": "system", "content": system_prompt})
                self._inited = True

            await self.append_chat_history({"role": "user", "content": prompt})

            response = await self.model.chat(
                history=self.chat_history,
                agent_name=self.__class__.__name__,
                sub_title=sub_title,
            )
            response_content = getattr(response.choices[0].message, "content", "") or ""
            response_content = self.sanitize_text_for_history(response_content)

            self.chat_history.append({"role": "assistant", "content": response_content})
            logger.info(f"{self.__class__.__name__}:完成:执行对话")
            return response_content
        except Exception as e:
            error_msg = f"执行过程中遇到错误: {str(e)}"
            logger.error(f"Agent执行失败: {str(e)}")
            return error_msg

    # 9 追加历史（核心入口）
    async def append_chat_history(self, msg: dict) -> None:
        """
        9.1 入参规范化：对象 → dict；保证 role 存在、content 为 str
        9.2 tool 消息空内容时，尝试从常见字段抽取文本
        9.3 标准化 tool_calls（仅梳理结构，不 stringify arguments）
        9.4 入库前统一清洗；合并相邻 user；必要时触发内存清理
        """
        # 9.1.1 规范化入参
        if not isinstance(msg, dict):
            try:
                content = getattr(msg, "content", "") or ""
                role = getattr(msg, "role", "assistant")
                tool_calls = getattr(msg, "tool_calls", None)
                msg = {"role": role, "content": content, "tool_calls": tool_calls}
            except Exception:
                msg = {"role": "assistant", "content": repr(msg)}
        msg["role"] = msg.get("role", "assistant") or "assistant"

        # 9.1.2 content 兜底为字符串
        raw_content = msg.get("content", "")
        if raw_content is None:
            raw_content = ""

        # 9.2 tool 消息的可读内容抽取
        if msg["role"] == "tool" and (not raw_content or str(raw_content).strip() == ""):
            extracted_parts = []

            out = msg.get("output") or msg.get("outputs") or msg.get("result") or msg.get("results")
            if out is not None:
                if isinstance(out, (list, tuple)):
                    for item in out:
                        if isinstance(item, dict):
                            for k in ("msg", "message", "text", "result", "content"):
                                v = item.get(k)
                                if v:
                                    extracted_parts.append(str(v))
                                    break
                            else:
                                try:
                                    extracted_parts.append(json.dumps(item, ensure_ascii=False))
                                except Exception:
                                    extracted_parts.append(str(item))
                        else:
                            extracted_parts.append(str(item))
                elif isinstance(out, dict):
                    for k in ("msg", "message", "text", "result", "content"):
                        v = out.get(k)
                        if v:
                            extracted_parts.append(str(v))
                            break
                    else:
                        try:
                            extracted_parts.append(json.dumps(out, ensure_ascii=False))
                        except Exception:
                            extracted_parts.append(str(out))
                else:
                    extracted_parts.append(str(out))

            if not extracted_parts:
                for k in ("text", "stdout", "stderr", "data", "value"):
                    v = msg.get(k)
                    if v:
                        if isinstance(v, (list, dict)):
                            try:
                                extracted_parts.append(json.dumps(v, ensure_ascii=False))
                            except Exception:
                                extracted_parts.append(str(v))
                        else:
                            extracted_parts.append(str(v))

            if not extracted_parts:
                tc = msg.get("tool_result") or msg.get("tool_response") or msg.get("tool_outputs")
                if tc is not None:
                    try:
                        extracted_parts.append(json.dumps(tc, ensure_ascii=False))
                    except Exception:
                        extracted_parts.append(str(tc))

            if extracted_parts:
                seen, parts = set(), []
                for p in extracted_parts:
                    s = (p or "").strip()
                    if s and s not in seen:
                        seen.add(s)
                        parts.append(s)
                raw_content = "\n".join(parts)

        # 9.1.3 最终保证字符串
        try:
            final_content = raw_content if isinstance(raw_content, str) else str(raw_content)
        except Exception:
            final_content = ""

        # 9.3 入库前统一清洗
        final_content = self.sanitize_text_for_history(final_content)
        msg["content"] = final_content

        # 9.4 相邻 user 合并
        ic(f"添加消息: role={msg.get('role')}, 当前历史长度={len(self.chat_history)}")
        last = self.chat_history[-1] if self.chat_history else None
        if last and last.get("role") == "user" and msg.get("role") == "user":
            last["content"] = f"""{last.get("content") or ""}\n\n{msg.get("content") or ""}"""
            ic("相邻 user 合并，避免连续 user 触发 400")
            return

        # 9.5 标准化 tool_calls（不 stringify）
        if "tool_calls" in msg and isinstance(msg["tool_calls"], (list, tuple)):
            try:
                lightweight = []
                for tc in msg["tool_calls"]:
                    tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
                    fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
                    fn_name = (
                        getattr(fn, "name", None)
                        if fn is not None
                        else (fn.get("name") if isinstance(fn, dict) else None)
                    )
                    fn_args = (
                        getattr(fn, "arguments", None)
                        if fn is not None
                        else (fn.get("arguments") if isinstance(fn, dict) else None)
                    )
                    if fn_args is None:
                        fn_args = ""
                    lightweight.append(
                        {"id": tc_id, "type": "function", "function": {"name": fn_name, "arguments": fn_args}}
                    )
                msg["tool_calls"] = lightweight
            except Exception:
                msg.pop("tool_calls", None)

        # 9.6 tool 消息尽量回填 tool_call_id
        if msg.get("role") == "tool" and "tool_call_id" not in msg:
            msg["tool_call_id"] = msg.get("tool_call_id") or msg.get("id") or msg.get("tool_id") or None

        # 9.7 追加到历史并规范“system 后第一条是 user”
        self.chat_history.append(msg)
        ic(f"添加后历史长度={len(self.chat_history)}")
        self.chat_history = self._ensure_first_after_system_user(self.chat_history)

        # 9.8 触发内存清理（跳过 tool）
        def _approx_total_tokens(messages):
            total = 0
            for m in messages:
                if not isinstance(m, dict):
                    continue
                c = m.get("content") or ""
                try:
                    total += token_counter(self.model.model, c)
                except Exception:
                    total += max(1, len(c) // 3)
            return total

        if msg.get("role") != "tool":
            ic("触发内存清理判定")
            try:
                total_tokens = _approx_total_tokens(self.chat_history)
            except Exception:
                total_tokens = SOFT_TOKEN_LIMIT - 1

            if total_tokens > SOFT_TOKEN_LIMIT:
                ic(f"超过软阈值 {SOFT_TOKEN_LIMIT} tokens，执行 clear_memory()")
                await self.clear_memory()
            elif len(self.chat_history) > self.max_memory:
                ic("超过消息条数兜底阈值，执行 clear_memory()")
                await self.clear_memory()
        else:
            ic("跳过内存清理(tool 消息)")

    # 10 内存清理（重量版）
    async def clear_memory(self):
        """
        10.1 计算需要总结的区间：保留尾部安全片段
        10.2 调用 simple_chat 生成“历史对话总结”
        10.3 重构历史：system + 摘要(user) + 尾部片段
        """
        ic(f"检查内存清理: 当前={len(self.chat_history)}, 最大(条数兜底)={self.max_memory}")
        ic("开始内存清理")
        logger.info(f"{self.__class__.__name__}:开始清除记忆，当前记录数：{len(self.chat_history)}")

        try:
            system_msg = (
                self.chat_history[0] if self.chat_history and self.chat_history[0].get("role") == "system" else None
            )

            preserve_start_idx = self._find_safe_preserve_point()
            ic(f"保留起始索引: {preserve_start_idx}")

            start_idx = 1 if system_msg else 0
            end_idx = preserve_start_idx
            ic(f"总结范围: {start_idx} -> {end_idx}")

            if end_idx > start_idx:
                summarize_history = []
                if system_msg:
                    summarize_history.append(system_msg)

                summarize_history.append(
                    {
                        "role": "user",
                        "content": f"""请简洁总结以下对话的关键内容和重要结论，保留重要上下文信息：

{self._format_history_for_summary(self.chat_history[start_idx:end_idx])}""",
                    }
                )

                summary = await simple_chat(self.model, summarize_history)

                new_history = []
                if system_msg:
                    new_history.append(system_msg)

                new_history.append(
                    {
                        "role": "user",
                        "content": f"""[历史对话总结-仅供上下文，无需回复]
{summary}""",
                    }
                )
                new_history.extend(self.chat_history[preserve_start_idx:])

                new_history = self._ensure_first_after_system_user(new_history)

                self.chat_history = new_history
                ic(f"内存清理完成，新历史长度: {len(self.chat_history)}")
                logger.info(f"{self.__class__.__name__}:记忆清除完成，压缩至：{len(self.chat_history)}条记录")
            else:
                logger.info(f"{self.__class__.__name__}:无需清除记忆，记录数量合理")

        except Exception as e:
            logger.error(f"记忆清除失败，使用简单切片策略: {str(e)}")
            self.chat_history = self._get_safe_fallback_history()

    # 11 切割点选择
    def _find_safe_preserve_point(self) -> int:
        """
        11.1 至少保留最后 10 条
        11.2 从后向前找“不会产生孤立 tool 的位置”
        """
        min_preserve = min(10, len(self.chat_history))
        preserve_start = len(self.chat_history) - min_preserve
        ic(f"寻找安全保留点: 历史长度={len(self.chat_history)}, 最少保留={min_preserve}, 开始位置={preserve_start}")

        for i in range(preserve_start, -1, -1):
            is_safe = self._is_safe_cut_point(i)
            ic(f"检查位置 {i}: 安全={is_safe}")
            if is_safe:
                ic(f"找到安全保留点: {i}")
                return i

        fallback = len(self.chat_history) - 1
        ic(f"未找到安全点，使用备用位置: {fallback}")
        return fallback

    def _is_safe_cut_point(self, start_idx: int) -> bool:
        """
        11.3 切割安全性判断：切割后不得出现“tool 无对应 tool_call”的孤立情况
        """
        if start_idx >= len(self.chat_history):
            ic(f"切割点 {start_idx} >= 历史长度，安全")
            return True

        for i in range(start_idx, len(self.chat_history)):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id")
                ic(f"发现工具响应消息在位置 {i}, tool_call_id={tool_call_id}")

                if tool_call_id:
                    found_tool_call = False
                    for j in range(start_idx, i):
                        prev_msg = self.chat_history[j]
                        if isinstance(prev_msg, dict) and prev_msg.get("tool_calls"):
                            for tool_call in prev_msg["tool_calls"]:
                                tid = (
                                    tool_call.get("id")
                                    if isinstance(tool_call, dict)
                                    else getattr(tool_call, "id", None)
                                )
                                if tid == tool_call_id:
                                    found_tool_call = True
                                    ic(f"找到对应的tool_call在位置 {j}")
                                    break
                            if found_tool_call:
                                break
                    if not found_tool_call:
                        ic(f"❌ 工具响应 {tool_call_id} 没有找到对应的tool_call，切割点不安全")
                        return False

        ic(f"切割点 {start_idx} 安全")
        return True

    # 12 回退历史（简化但不破坏配对）
    def _get_safe_fallback_history(self) -> list:
        """
        12.1 优先保留首条 system
        12.2 尝试保留最后 1~4 条的安全窗口
        12.3 全不安全时，至少保留最后一条非 tool
        """
        if not self.chat_history:
            return []

        safe_history = []
        if self.chat_history and self.chat_history[0].get("role") == "system":
            safe_history.append(self.chat_history[0])

        for preserve_count in range(1, min(4, len(self.chat_history)) + 1):
            start_idx = len(self.chat_history) - preserve_count
            if self._is_safe_cut_point(start_idx):
                safe_history.extend(self.chat_history[start_idx:])
                return safe_history

        for i in range(len(self.chat_history) - 1, -1, -1):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("role") != "tool":
                safe_history.append(msg)
                break

        return safe_history

    # 13 辅助：查找未匹配的 tool_call（调试用）
    def _find_last_unmatched_tool_call(self) -> int | None:
        """
        13.1 逆序查找包含 tool_calls 的消息
        13.2 对每个 call 检查是否存在后续 tool 响应
        """
        ic("开始查找未匹配的tool_call")

        for i in range(len(self.chat_history) - 1, -1, -1):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("tool_calls"):
                ic(f"在位置 {i} 发现tool_calls消息")
                for tool_call in msg["tool_calls"]:
                    tool_call_id = (
                        tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", None)
                    )
                    ic(f"检查tool_call_id: {tool_call_id}")

                    if tool_call_id:
                        response_found = False
                        for j in range(i + 1, len(self.chat_history)):
                            response_msg = self.chat_history[j]
                            if (
                                isinstance(response_msg, dict)
                                and response_msg.get("role") in ("tool", "function")
                                and response_msg.get("tool_call_id") == tool_call_id
                            ):
                                ic(f"找到匹配的工具响应在位置 {j}")
                                response_found = True
                                break
                        if not response_found:
                            ic(f"❌ 发现未匹配的tool_call在位置 {i}, id={tool_call_id}")
                            return i

        ic("没有发现未匹配的tool_call")
        return None

    # 14 摘要用格式化
    def _format_history_for_summary(self, history: list[dict]) -> str:
        """
        14.1 截断每条内容至 2000 字符
        14.2 按 role: content 逐行输出
        """
        formatted = []
        for msg in history:
            role = msg.get("role")
            content = msg.get("content") or ""
            content = content[:2000] + "..." if len(content) > 2000 else content
            formatted.append(f"{role}: {content}")
        return "\n".join(formatted)

    # 15 保证“system 后第一条是 user”
    def _ensure_first_after_system_user(self, history: list) -> list:
        """
        15.1 任意数量的 system 之后，第一条非 system 必须是 user（除非 assistant 正在发起 tool_calls）
        15.2 若首条非 system 是 assistant 且以“[历史对话总结”开头，则就地改为 user
        15.3 其他情况在其前插入最小 user 承接语
        """
        if not history:
            return [{"role": "user", "content": """[空对话启动] 继续。"""}]

        i = 0
        while i < len(history) and isinstance(history[i], dict) and history[i].get("role") == "system":
            i += 1

        if i >= len(history):
            return history + [{"role": "user", "content": """[承接上文上下文] 继续。"""}]

        first_msg = history[i]
        if first_msg.get("role") != "user":
            if first_msg.get("role") == "assistant" and first_msg.get("tool_calls"):
                return history
            content = (first_msg.get("content") or "").strip()
            if first_msg.get("role") == "assistant" and content.startswith("[历史对话总结"):
                first_msg["role"] = "user"
                history[i] = first_msg
            else:
                history = history[:i] + [{"role": "user", "content": """[承接上文上下文] 继续。"""}] + history[i:]
        return history
