import json
from app.core.llm.llm import LLM, simple_chat
from app.utils.log_util import logger
from icecream import ic
from litellm import token_counter  # 用于按 token 估算

# TODO: Memory 的管理
# TODO: 评估任务完成情况，rethinking

# 软阈值：优先在到达硬上限前做清理，避免 400/ContextWindowExceeded
SOFT_TOKEN_LIMIT = 100_000  # 可视需要调大/调小;理论最大值：≤131072（超过就一定报错）;建议范围：80k – 110k


class Agent:
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 500,  # 单个agent最大对话轮次
        max_memory: int = 50,  # 最大记忆轮次（条数兜底）
    ) -> None:
        self.task_id = task_id
        self.model = model
        self.chat_history: list[dict] = []  # 存储对话历史
        self.max_chat_turns = max_chat_turns  # 最大对话轮次
        self.current_chat_turns = 0  # 当前对话轮次计数器
        self.max_memory = max_memory  # 最大记忆轮次（条数兜底）
        self._inited = False  # 仅首次注入 system

    async def run(self, prompt: str, system_prompt: str, sub_title: str) -> str:
        """
        执行agent的对话并返回结果和总结

        Args:
            prompt: 输入的提示

        Returns:
            str: 模型的响应
        """
        try:
            logger.info(f"{self.__class__.__name__}:开始:执行对话")
            self.current_chat_turns = 0  # 重置对话轮次计数器

            # 只在首次运行注入 system，避免同一 system 多次堆叠
            if not self._inited:
                await self.append_chat_history({"role": "system", "content": system_prompt})
                self._inited = True

            await self.append_chat_history({"role": "user", "content": prompt})

            # 获取历史消息用于本次对话
            response = await self.model.chat(
                history=self.chat_history,
                agent_name=self.__class__.__name__,
                sub_title=sub_title,
            )
            response_content = response.choices[0].message.content
            self.chat_history.append({"role": "assistant", "content": response_content})
            logger.info(f"{self.__class__.__name__}:完成:执行对话")
            return response_content
        except Exception as e:
            error_msg = f"执行过程中遇到错误: {str(e)}"
            logger.error(f"Agent执行失败: {str(e)}")
            return error_msg

    async def append_chat_history(self, msg: dict) -> None:
        """
        稳健的消息追加函数 - 替换原有实现
        功能：
        1) 规范化入参（把对象/非 dict 情况转换为 dict）
        2) 强制保证 msg['content'] 为字符串（绝不为 None）
        3) 对 role == 'tool' 的消息，优先从 output/result/msg 字段抽取文本并填充 content
        4) 保持原有的合并相邻 user / tool_calls 标准化 / 内存清理逻辑
        """
        # —— 规范化入参 —— #
        if not isinstance(msg, dict):
            try:
                content = getattr(msg, "content", "") or ""
                role = getattr(msg, "role", "assistant")
                tool_calls = getattr(msg, "tool_calls", None)
                msg = {"role": role, "content": content, "tool_calls": tool_calls}
            except Exception:
                msg = {"role": "assistant", "content": repr(msg)}

        # 保证 role 字段存在且为字符串
        msg["role"] = msg.get("role", "assistant") or "assistant"

        # —— 重要：确保 content 字段永远为字符串（非 None） —— #
        # 若 content 本身为空或 None，稍后对 tool 消息会填充
        raw_content = msg.get("content", "")
        if raw_content is None:
            raw_content = ""

        # 如果是 tool 类型且 content 为空，尝试从常见字段中抽取文本：
        if msg["role"] == "tool" and (not raw_content or str(raw_content).strip() == ""):
            extracted_parts = []

            # 1) 优先查看 msg.get("output")（常见为 list）
            out = msg.get("output") or msg.get("outputs") or msg.get("result") or msg.get("results")
            if out is not None:
                # 如果是列表，逐项抽取
                if isinstance(out, (list, tuple)):
                    for item in out:
                        if isinstance(item, dict):
                            # 常见字段：msg, message, text, result, content
                            for k in ("msg", "message", "text", "result", "content"):
                                v = item.get(k)
                                if v:
                                    extracted_parts.append(str(v))
                                    break
                            else:
                                # fallback: dump whole dict small excerpt
                                try:
                                    extracted_parts.append(json.dumps(item, ensure_ascii=False))
                                except Exception:
                                    extracted_parts.append(str(item))
                        else:
                            extracted_parts.append(str(item))
                elif isinstance(out, dict):
                    # 抽取典型键
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

            # 2) 再尝试 tool-specific 字段
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

            # 3) 尝试从嵌套的 tool_call / tool_result 中抽取
            if not extracted_parts:
                tc = msg.get("tool_result") or msg.get("tool_response") or msg.get("tool_outputs")
                if tc is not None:
                    try:
                        extracted_parts.append(json.dumps(tc, ensure_ascii=False))
                    except Exception:
                        extracted_parts.append(str(tc))

            # 最终填充 content（去重并按行拼接）
            if extracted_parts:
                # 过滤空字符串并去重（但保留顺序）
                seen = set()
                parts = []
                for p in extracted_parts:
                    s = p.strip()
                    if not s:
                        continue
                    if s in seen:
                        continue
                    seen.add(s)
                    parts.append(s)
                raw_content = "\n".join(parts)

        # 最终确保为字符串（防止非 str 类型）
        try:
            if isinstance(raw_content, str):
                final_content = raw_content
            else:
                final_content = str(raw_content)
        except Exception:
            final_content = ""

        # 把标准化后的 content 放回 msg
        msg["content"] = final_content

        # —— 合并相邻 user —— #
        ic(f"添加消息: role={msg.get('role')}, 当前历史长度={len(self.chat_history)}")
        last = self.chat_history[-1] if self.chat_history else None
        if last and last.get("role") == "user" and msg.get("role") == "user":
            last["content"] = (last.get("content") or "") + "\n\n" + (msg.get("content") or "")
            ic("相邻 user 合并，避免连续 user 触发 400")
            return

        # —— 标准化 tool_calls —— #
        if "tool_calls" in msg and isinstance(msg["tool_calls"], (list, tuple)):
            try:
                lightweight = []
                for tc in msg["tool_calls"]:
                    # 兼容对象或字典
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

                    # arguments 必须是字符串
                    if isinstance(fn_args, (dict, list)):
                        try:
                            fn_args = json.dumps(fn_args, ensure_ascii=False)
                        except Exception:
                            fn_args = str(fn_args)
                    elif fn_args is None:
                        fn_args = ""

                    # 必须包含 type: "function"
                    lightweight.append(
                        {"id": tc_id, "type": "function", "function": {"name": fn_name, "arguments": fn_args}}
                    )
                msg["tool_calls"] = lightweight
            except Exception:
                # 出错就移除，避免发不出去
                msg.pop("tool_calls", None)

        # 为 tool 消息尽可能设置 tool_call_id（如果存在）
        if msg.get("role") == "tool" and "tool_call_id" not in msg:
            # 兼容多种命名
            msg["tool_call_id"] = msg.get("tool_call_id") or msg.get("id") or msg.get("tool_id") or None

        # 最终把消息追加
        self.chat_history.append(msg)
        ic(f"添加后历史长度={len(self.chat_history)}")
        self.chat_history = self._ensure_first_after_system_user(self.chat_history)

        # —— 触发内存清理 —— #
        def _approx_total_tokens(messages):
            total = 0
            for m in messages:
                if not isinstance(m, dict):
                    continue
                c = m.get("content") or ""
                try:
                    total += token_counter(c, self.model.model)
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
            ic("跳过内存清理(tool消息)")

    async def clear_memory(self):
        """当聊天历史超过限制时，使用 simple_chat 进行“重量版”总结压缩"""
        ic(f"检查内存清理: 当前={len(self.chat_history)}, 最大(条数兜底)={self.max_memory}")

        # 条数不超时也可能因 token 超限，这里不再提前 return

        ic("开始内存清理")
        logger.info(f"{self.__class__.__name__}:开始清除记忆，当前记录数：{len(self.chat_history)}")

        try:
            # 保留第一条系统消息
            system_msg = (
                self.chat_history[0] if self.chat_history and self.chat_history[0].get("role") == "system" else None
            )

            # 查找需要保留的消息范围 - 保留最后几条完整的对话和工具调用
            preserve_start_idx = self._find_safe_preserve_point()
            ic(f"保留起始索引: {preserve_start_idx}")

            # 需要总结的消息范围
            start_idx = 1 if system_msg else 0
            end_idx = preserve_start_idx
            ic(f"总结范围: {start_idx} -> {end_idx}")

            if end_idx > start_idx:
                # 构造摘要提示，仅传“需要总结的片段”（重量版 simple_chat 会做中段摘要+工具修复）
                summarize_history = []
                if system_msg:
                    summarize_history.append(system_msg)

                summarize_history.append(
                    {
                        "role": "user",  # 修改：使用 user 角色
                        "content": (
                            "请简洁总结以下对话的关键内容和重要结论，保留重要的上下文信息：\n\n"
                            f"{self._format_history_for_summary(self.chat_history[start_idx:end_idx])}"
                        ),
                    }
                )

                # 调用 simple_chat 进行总结（重量版已在 llm.py 中实现）
                summary = await simple_chat(self.model, summarize_history)

                # 重构聊天历史：系统消息 + 摘要 + 保留的消息（最后若干条）
                new_history = []
                if system_msg:
                    new_history.append(system_msg)

                new_history.append(
                    {"role": "user", "content": f"[历史对话总结-仅供上下文，无需回复]\n{summary}"}
                )  # 修改：使用 user 角色
                new_history.extend(self.chat_history[preserve_start_idx:])

                # 确保 system 后第一条是 user
                new_history = self._ensure_first_after_system_user(new_history)

                self.chat_history = new_history
                ic(f"内存清理完成，新历史长度: {len(self.chat_history)}")
                logger.info(f"{self.__class__.__name__}:记忆清除完成，压缩至：{len(self.chat_history)}条记录")
            else:
                logger.info(f"{self.__class__.__name__}:无需清除记忆，记录数量合理")

        except Exception as e:
            logger.error(f"记忆清除失败，使用简单切片策略: {str(e)}")
            # 如果总结失败，回退到安全的策略：保留系统消息和最后几条消息，确保工具调用完整性
            safe_history = self._get_safe_fallback_history()
            self.chat_history = safe_history

    def _find_safe_preserve_point(self) -> int:
        """找到安全的保留起始点，确保不会破坏工具调用序列"""
        # 最少保留最后10条消息，确保基本对话完整性（由原先3条提高到10条）
        min_preserve = min(10, len(self.chat_history))
        preserve_start = len(self.chat_history) - min_preserve
        ic(f"寻找安全保留点: 历史长度={len(self.chat_history)}, 最少保留={min_preserve}, 开始位置={preserve_start}")

        # 从后往前查找，确保不会在工具调用序列中间切断
        for i in range(preserve_start, -1, -1):
            if i >= len(self.chat_history):
                continue

            # 检查从这个位置开始是否是安全的（没有孤立的tool消息）
            is_safe = self._is_safe_cut_point(i)
            ic(f"检查位置 {i}: 安全={is_safe}")
            if is_safe:
                ic(f"找到安全保留点: {i}")
                return i

        # 如果找不到安全点，至少保留最后1条消息
        fallback = len(self.chat_history) - 1
        ic(f"未找到安全点，使用备用位置: {fallback}")
        return fallback

    def _is_safe_cut_point(self, start_idx: int) -> bool:
        """检查从指定位置开始切割是否安全（不会产生孤立的tool消息）"""
        if start_idx >= len(self.chat_history):
            ic(f"切割点 {start_idx} >= 历史长度，安全")
            return True

        # 检查切割后的消息序列是否有孤立的tool消息
        tool_messages = []
        for i in range(start_idx, len(self.chat_history)):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id")
                tool_messages.append((i, tool_call_id))
                ic(f"发现tool消息在位置 {i}, tool_call_id={tool_call_id}")

                # 向前查找对应的tool_calls消息
                if tool_call_id:
                    found_tool_call = False
                    for j in range(start_idx, i):
                        prev_msg = self.chat_history[j]
                        if isinstance(prev_msg, dict) and "tool_calls" in prev_msg and prev_msg["tool_calls"]:
                            for tool_call in prev_msg["tool_calls"]:
                                # 工具调用条目可能是字典或对象，尝试标准化比较
                                tid = None
                                if isinstance(tool_call, dict):
                                    tid = tool_call.get("id")
                                else:
                                    tid = getattr(tool_call, "id", None)
                                if tid == tool_call_id:
                                    found_tool_call = True
                                    ic(f"找到对应的tool_call在位置 {j}")
                                    break
                            if found_tool_call:
                                break

                    if not found_tool_call:
                        ic(f"❌ tool消息 {tool_call_id} 没有找到对应的tool_call，切割点不安全")
                        return False

        ic(f"切割点 {start_idx} 安全，检查了 {len(tool_messages)} 个tool消息")
        return True

    def _get_safe_fallback_history(self) -> list:
        """获取安全的后备历史记录，确保不会有孤立的tool消息"""
        if not self.chat_history:
            return []

        # 保留系统消息
        safe_history = []
        if self.chat_history and self.chat_history[0].get("role") == "system":
            safe_history.append(self.chat_history[0])

        # 从后往前查找安全的消息序列
        for preserve_count in range(1, min(4, len(self.chat_history)) + 1):
            start_idx = len(self.chat_history) - preserve_count
            if self._is_safe_cut_point(start_idx):
                safe_history.extend(self.chat_history[start_idx:])
                return safe_history

        # 如果都不安全，只保留最后一条非tool消息
        for i in range(len(self.chat_history) - 1, -1, -1):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("role") != "tool":
                safe_history.append(msg)
                break

        return safe_history

    def _find_last_unmatched_tool_call(self) -> int | None:
        """查找最后一个未匹配的tool call的索引"""
        ic("开始查找未匹配的tool_call")

        # 从后往前查找，寻找没有对应tool response的tool call
        for i in range(len(self.chat_history) - 1, -1, -1):
            msg = self.chat_history[i]

            # 检查是否是包含tool_calls的消息
            if isinstance(msg, dict) and "tool_calls" in msg and msg["tool_calls"]:
                ic(f"在位置 {i} 发现tool_calls消息")

                # 检查每个tool call是否都有对应的response
                for tool_call in msg["tool_calls"]:
                    tool_call_id = (
                        tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", None)
                    )
                    ic(f"检查tool_call_id: {tool_call_id}")

                    if tool_call_id:
                        # 在后续消息中查找对应的tool response
                        response_found = False
                        for j in range(i + 1, len(self.chat_history)):
                            response_msg = self.chat_history[j]
                            if (
                                isinstance(response_msg, dict)
                                and response_msg.get("role") == "tool"
                                and response_msg.get("tool_call_id") == tool_call_id
                            ):
                                ic(f"找到匹配的tool响应在位置 {j}")
                                response_found = True
                                break

                        if not response_found:
                            # 找到未匹配的tool call
                            ic(f"❌ 发现未匹配的tool_call在位置 {i}, id={tool_call_id}")
                            return i

        ic("没有发现未匹配的tool_call")
        return None

    def _format_history_for_summary(self, history: list[dict]) -> str:
        """格式化历史记录用于总结"""
        formatted = []
        for msg in history:
            role = msg.get("role")
            content = msg.get("content") or ""
            content = content[:500] + "..." if len(content) > 500 else content  # 限制长度
            formatted.append(f"{role}: {content}")
        return "\n".join(formatted)

    def _ensure_first_after_system_user(self, history: list) -> list:
        """
        保证：system 消息之后第一条是 user 消息。
        - 如果有相邻的 assistant/role 非 user，强制修改。
        """
        if not history:
            return [{"role": "user", "content": "[空对话启动] 继续。"}]

        # 找到第一个非 system 的消息
        i = 0
        while i < len(history) and isinstance(history[i], dict) and history[i].get("role") == "system":
            i += 1

        # 如果全是 system 消息，插入 user 消息
        if i >= len(history):
            return history + [{"role": "user", "content": "[承接上文上下文] 继续。"}]

        # 如果第一条非 system 不是 user
        first_msg = history[i]
        if first_msg.get("role") != "user":
            content = (first_msg.get("content") or "").strip()
            if first_msg.get("role") == "assistant" and content.startswith("[历史对话总结"):
                first_msg["role"] = "user"
                history[i] = first_msg
            else:
                history = history[:i] + [{"role": "user", "content": "[承接上文上下文] 继续。"}] + history[i:]

        return history
