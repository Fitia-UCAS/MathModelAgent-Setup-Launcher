# app/core/agents/coder_agent.py

import json
import re
from typing import Any, Mapping, Sequence, Union

from app.core.agents.agent import Agent
from app.config.setting import settings
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager
from app.schemas.response import SystemMessage, InterpreterMessage
from app.tools.base_interpreter import BaseCodeInterpreter
from app.core.llm.llm import LLM
from app.schemas.A2A import CoderToWriter
from app.core.prompts import CODER_PROMPT
from app.utils.common_utils import get_current_files
from app.core.prompts import get_reflection_prompt
from app.core.functions import coder_tools
from app.tools.text_sanitizer import TextSanitizer as TS
from app.tools.json_fixer import JsonFixer  # 修复/解析非规范 JSON arguments


# 1 全局设置
# 1.1 是否强制要求工具参数必须是严格 JSON：{"code": "<python>"}
# 1.2 轻度清洗：只移除控制字符、剥掉最外层围栏，不改写代码语义
# 1.3 首轮（或尚未执行过）强制要求 LLM 直接调 execute_code（避免把代码写进 content）
STRICT_TOOL_ARGS = True
LIGHT_CLEANING = True
FORCE_TOOL_ON_FIRST_TRY = True


# 2 解析与取值辅助
# 2.1 类型别名与单例
Key = Union[str, int]
JSON_FIXER = JsonFixer()  # 复用单例避免频繁创建


def _dig(obj: Any, *keys: Key, default=None):
    """
    2.2 兼容对象/字典/列表的嵌套取值
    2.2.1 使用示例：_dig(tc, "function", "name") 或 _dig(doc, "items", 0, "id")
    2.2.2 访问规则：
           - str key：优先 getattr，其次 Mapping.get
           - int key：序列下标访问
    2.2.3 取值失败则返回 default
    """
    cur = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(k, int):
            try:
                if isinstance(cur, Sequence) and not isinstance(cur, (str, bytes, bytearray)):
                    cur = cur[k]
                    continue
            except Exception:
                return default
        else:
            try:
                cur = getattr(cur, k)
                continue
            except Exception:
                pass
            try:
                if isinstance(cur, Mapping):
                    cur = cur.get(k, default)
                    continue
            except Exception:
                return default
            return default
    return cur if cur is not None else default


def _to_str_for_parsing(args_raw: Any) -> str:
    """
    2.3 将 tool.arguments 规整为字符串：
    2.3.1 优先 json.dumps（移除 repr 噪音）
    2.3.2 失败退回 str()
    """
    if isinstance(args_raw, str):
        return args_raw
    try:
        return json.dumps(args_raw, ensure_ascii=False)
    except Exception:
        return str(args_raw)


def _parse_arguments_to_dict(args_raw: Any) -> Mapping:
    """
    2.4 将 tool.arguments 解析为 dict：
    2.4.1 已是映射/模型对象 → 直接取
    2.4.2 字符串/其它 → 预清洗 → JsonFixer 修复解析
    2.4.3 失败返回 {}
    """
    try:
        if hasattr(args_raw, "model_dump") and callable(getattr(args_raw, "model_dump")):
            return args_raw.model_dump()
        if hasattr(args_raw, "dict") and callable(getattr(args_raw, "dict")):
            return args_raw.dict()
        if isinstance(args_raw, Mapping):
            return args_raw
    except Exception:
        pass

    try:
        raw_str = _to_str_for_parsing(args_raw)
        raw_str = TS.preclean_tool_wrappers(raw_str)
        raw_str = TS.clean_control_chars(raw_str, keep_whitespace=True)

        # 1) 直接 json.loads（优先）
        try:
            return json.loads(raw_str)
        except Exception:
            pass

        # 2) JSON_FIXER 的“arguments样式”解析
        try:
            parsed = JSON_FIXER.parse_arguments_like_openai(raw_str)
            if isinstance(parsed, Mapping):
                return parsed
        except Exception:
            pass

        # 3) JSON_FIXER 通用修复
        try:
            parsed = JSON_FIXER.fix_and_parse(raw_str, expect="dict")
            if isinstance(parsed, Mapping):
                return parsed
        except Exception:
            pass

        # 4) 抽第一个 JSON 块再试
        try:
            blob = TS.extract_first_json_block(raw_str, strip_fences_first=True)
            if blob:
                return json.loads(blob)
        except Exception:
            pass
    except Exception:
        pass

    return {}


def _safe_get_code_from_arguments(args_raw: Any) -> str:
    """
    2.5 从 tool.arguments 中提取 code：
    """
    # --- 快路径：字符串 JSON ---
    if isinstance(args_raw, str):
        s = args_raw.strip()
        # 典型兼容：OpenAI/LiteLLM 会给到纯 JSON 字符串
        try:
            obj = json.loads(s)
            if isinstance(obj, Mapping) and "code" in obj:
                val = obj.get("code", "")
                return val if isinstance(val, str) else str(val)
        except Exception:
            pass  # 继续走通用路径

    # --- 直接的 Mapping/模型对象 ---
    try:
        if hasattr(args_raw, "code"):
            val = getattr(args_raw, "code")
            return val if isinstance(val, str) else str(val)
        if isinstance(args_raw, Mapping) and "code" in args_raw:
            val = args_raw.get("code", "")
            return val if isinstance(val, str) else str(val)
    except Exception:
        pass

    # --- 通用路径：尽量 parse 成 dict 再取 ---
    try:
        args_dict = _parse_arguments_to_dict(args_raw)
        if isinstance(args_dict, Mapping) and "code" in args_dict:
            val = args_dict.get("code", "")
            return val if isinstance(val, str) else str(val)
    except Exception:
        pass

    # --- 正则兜底（只做提取，不放宽约束）---
    try:
        raw_str = _to_str_for_parsing(args_raw)
        raw_str = TS.preclean_tool_wrappers(raw_str)
        raw_str = TS.clean_control_chars(raw_str, keep_whitespace=True)
        m = _CODE_FIELD_RE.search(raw_str)
        if m:
            val = m.group("code")
            try:
                val = json.loads(f'"{val}"')
            except Exception:
                try:
                    val = bytes(val, "utf-8").decode("unicode_escape")
                except Exception:
                    pass
            return val
    except Exception:
        pass

    return ""


# 2.6 从文本解析 JSON 与 code（兜底）
# 2.6.1 简单 "code":"..."" 抽取正则（仅在 JSON 失败时兜底）
_CODE_FIELD_RE = re.compile(r'"code"\s*:\s*"(?P<code>.*?)"', re.DOTALL)


def _maybe_json_to_dict(text: str) -> Mapping:
    """
    2.6.2 尝试把文本里的 JSON 解析成 dict：
    2.6.2.1 去掉 ```json / json 前缀 / 外层围栏 等“包壳”
    2.6.2.2 优先抽取首个 JSON 块，再用 JsonFixer 解析
    2.6.2.3 失败返回 {}
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    t = TS.preclean_tool_wrappers(text)
    blob = TS.extract_first_json_block(t, strip_fences_first=True) or t
    try:
        parsed = JSON_FIXER.fix_and_parse(blob, expect="dict")
        return parsed if isinstance(parsed, Mapping) else {}
    except Exception:
        return {}


def _safe_get_code_from_any(arguments_obj: Any, fallback_text: str = "") -> str:
    """
    2.7 统一获取 code（arguments 优先，content 兜底）：
    2.7.1 先从 tool.arguments 中严格提取
    2.7.2 若为空/缺失：从 assistant content 尝试解析 {"code":"..."} 或 ```json 包裹
    2.7.3 若仍失败：剥离围栏，把整段当“裸代码”兜底
    2.7.4 最后做轻清洗：仅移除控制字符/最外层围栏
    """
    # 2.7.1 arguments 优先
    code = _safe_get_code_from_arguments(arguments_obj)
    if isinstance(code, str) and code.strip():
        return TS.clean_control_chars(TS.strip_fences_outer_or_all(code), keep_whitespace=True)

    # 2.7.2 从 assistant 文本回退
    t = str(fallback_text or "")
    if not t.strip():
        return ""

    d = _maybe_json_to_dict(t)
    if isinstance(d, Mapping) and "code" in d and isinstance(d["code"], str) and d["code"].strip():
        return TS.clean_control_chars(TS.strip_fences_outer_or_all(d["code"]), keep_whitespace=True)

    m = _CODE_FIELD_RE.search(t)
    if m:
        val = m.group("code")
        try:
            val = bytes(val, "utf-8").decode("unicode_escape")  # 反转义 \n \t 等
        except Exception:
            pass
        return TS.clean_control_chars(TS.strip_fences_outer_or_all(val), keep_whitespace=True)

    # 2.7.3 兜底：把整段当“裸代码”
    t2 = TS.strip_fences_outer_or_all(TS.preclean_tool_wrappers(t))
    if t2 and any(s in t2 for s in ("# %%", "import ", "from ", "plt.", "pd.read_", "np.", "def ", "class ")):
        return TS.clean_control_chars(t2, keep_whitespace=True)

    return ""


# 3 非 Python 代码守门（粗判）
# 3.1 常用关键字正则提示
_PY_HINT_RE = re.compile(
    r"\b(import|from|def|class|for|while|if|elif|else|try|except|with|return|print|plt\.|np\.|pd\.|fit\(|read_csv\(|range\(|open\()",
    re.I,
)


def _looks_like_python(code: str) -> bool:
    """
    3.2 判断是否像可执行 Python：
    3.2.1 剥离最外层围栏后判断
    3.2.2 以 { / [ 开头（JSON/列表）判为非 Python
    3.2.3 未命中关键字且无弱信号（:、=、.plot 等）判为非 Python
    """
    if not isinstance(code, str):
        return False
    snippet = TS.strip_fences_outer_or_all(code or "").strip()

    if re.match(r"^\s*[{[]", snippet):
        return False

    if not _PY_HINT_RE.search(snippet):
        weak_sig = any(s in snippet for s in (":\n", ":\r", "=\n", "=\r", "():", ".plot(", ".read_csv("))
        if not weak_sig:
            return False

    return True


# 4 Agent 实现
# 4.1 初始化与构造器
class CoderAgent(Agent):
    def __init__(
        self,
        task_id: str,
        model: LLM,
        work_dir: str,
        max_chat_turns: int = settings.MAX_CHAT_TURNS,
        max_retries: int = settings.MAX_RETRIES,
        code_interpreter: BaseCodeInterpreter = None,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.work_dir = work_dir
        self.max_retries = max_retries
        self.is_first_run = True
        self.system_prompt = CODER_PROMPT
        self.code_interpreter = code_interpreter

    # 4.2 主运行逻辑
    async def run(self, prompt: str, subtask_title: str) -> CoderToWriter:
        logger.info(f"{self.__class__.__name__}:开始:执行子任务: {subtask_title}")
        # 4.2.1 标记本子任务输出分区
        self.code_interpreter.add_section(subtask_title)

        retry_count = 0
        last_error_message = ""
        executed_tool_calls = False  # 仅当“成功执行代码”后置位
        code_executed_successfully = False  # 新增：严格表示“至少一次执行成功”
        merged_prompt = None
        assistant_content = ""

        # 4.2.2 首轮上下文准备
        if self.is_first_run:
            logger.info("首次运行，添加系统提示和数据集文件信息")
            self.is_first_run = False
            await self.append_chat_history({"role": "system", "content": self.system_prompt})

            files_info = f"当前文件夹下的数据集文件{get_current_files(self.work_dir, 'data')}"
            merged_prompt = f"{files_info}\n\n{subtask_title}：\n{prompt}"
            logger.info(f"添加首轮合并子任务提示: {merged_prompt}")
            await self.append_chat_history({"role": "user", "content": merged_prompt})
        else:
            logger.info(f"添加子任务提示: {prompt}")
            await self.append_chat_history({"role": "user", "content": prompt})

        # 4.2.3 轮次保护
        if self.current_chat_turns >= self.max_chat_turns:
            logger.error(f"超过最大聊天次数: {self.max_chat_turns}")
            await redis_manager.publish_message(self.task_id, SystemMessage(content="超过最大聊天次数", type="error"))
            raise Exception(f"Reached maximum number of chat turns ({self.max_chat_turns}). Task incomplete.")

        # 4.2.4 主循环
        while retry_count < self.max_retries and self.current_chat_turns < self.max_chat_turns:
            self.current_chat_turns += 1
            logger.info(f"当前对话轮次: {self.current_chat_turns}")

            # 4.2.4.0 首轮强制工具调用：避免 LLM 把代码写进 content
            if FORCE_TOOL_ON_FIRST_TRY and not code_executed_successfully:
                tool_choice_param = {"type": "function", "function": {"name": "execute_code"}}
            else:
                tool_choice_param = "auto"

            # 修改run方法中的response处理（从response = await self.model.chat(开始）
            response = await self.model.chat(
                history=self.chat_history,
                tools=coder_tools,
                tool_choice=tool_choice_param,
                agent_name=self.__class__.__name__,
            )

            assistant_msg_obj = response.choices[0].message
            assistant_content_raw = (
                getattr(assistant_msg_obj, "content", "") or "(assistant returned empty content - continuing)"
            )  # Fallback for empty content
            assistant_tool_calls = getattr(assistant_msg_obj, "tool_calls", None)
            logger.info(f"Received assistant content length: {len(assistant_content_raw)}")  # Log for verification

            # 4.2.4.1 轻清洗 assistant 文本
            if LIGHT_CLEANING:
                assistant_content_clean = TS.clean_control_chars(assistant_content_raw, keep_whitespace=True)
                assistant_content_clean = TS.normalize_common_glitches(assistant_content_clean)
                assistant_content_clean = TS.strip_fences_outer_or_all(assistant_content_clean)
            else:
                assistant_content_clean = assistant_content_raw

            # 4.2.4.2 有工具调用路径
            if assistant_tool_calls:
                logger.info("检测到工具调用")
                assistant_payload = {"role": "assistant", "tool_calls": assistant_tool_calls}
                if isinstance(assistant_content_clean, str) and assistant_content_clean.strip():
                    assistant_payload["content"] = assistant_content_clean
                await self.append_chat_history(assistant_payload)

                # 4.2.4.2.1 找第一个 execute_code
                tool_call = None
                for tc in assistant_tool_calls:
                    fn_name = _dig(tc, "function", "name")
                    if fn_name == "execute_code":
                        tool_call = tc
                        break

                if tool_call is None:
                    first_tc = assistant_tool_calls[0]
                    tool_id = _dig(first_tc, "id")
                    fn_name = _dig(first_tc, "function", "name") or "unknown"
                    logger.warning(f"未发现 execute_code 调用（收到 {len(assistant_tool_calls)} 个工具），跳过处理。")
                    await self.append_chat_history(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "name": fn_name,
                            "content": "未检测到可执行的 execute_code 调用，未执行。",
                        }
                    )
                    retry_count += 1
                    continue

                # 4.2.4.2.2 execute_code 路径
                tool_id = _dig(tool_call, "id")
                fn_name = _dig(tool_call, "function", "name")

                if fn_name == "execute_code":
                    logger.info(f"调用工具: {fn_name}")
                    await redis_manager.publish_message(self.task_id, SystemMessage(content=f"代码手调用{fn_name}工具"))

                    # 4.2.4.2.3 解析 code 参数（arguments 优先，必要时从 content 兜底）
                    try:
                        logger.info(
                            f"[DEV] execute_code raw arguments: {repr(_dig(tool_call, 'function', 'arguments'))[:2000]}"
                        )
                        raw_args = _dig(tool_call, "function", "arguments")
                        raw_code = _safe_get_code_from_any(raw_args, fallback_text=assistant_content_clean)
                        logger.info(f"[DEV] extracted code length: {len(raw_code)}")

                        if not isinstance(raw_code, str):
                            raw_code = str(raw_code or "")
                    except Exception as e:
                        raw_code = ""
                        logger.exception("解析 tool.arguments 失败")
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "execute_code",
                                "content": f"解析工具参数失败: {e}",
                            }
                        )
                        retry_count += 1
                        last_error_message = f"解析工具参数失败: {e}"
                        continue

                    # 4.2.4.2.4 空 code / 缺 code 字段
                    if not raw_code.strip():
                        logger.warning("代码为空，跳过工具调用")
                        await redis_manager.publish_message(
                            self.task_id,
                            SystemMessage(content="任务跳过：代码为空，未执行", type="warning"),
                        )
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "execute_code",
                                "content": (
                                    "参数校验失败：`arguments.code` 缺失或为空。"
                                    '请严格以 JSON 重新调用：{"code": "<仅 Python 代码，不要 Markdown/JSON/说明文字>"}'
                                ),
                            }
                        )
                        await self.append_chat_history(
                            {
                                "role": "user",
                                "content": (
                                    "请重新调用 execute_code，并只返回严格 JSON："
                                    '{"code": "<Python 代码>"}（不要围栏/不要说明文字）。'
                                ),
                            }
                        )
                        retry_count += 1
                        last_error_message = "empty_code"
                        continue

                    # 4.2.4.2.5 非 Python 粗判
                    probe = TS.strip_fences_outer_or_all(raw_code or "").strip()
                    if not _looks_like_python(probe):
                        msg = (
                            "未检测到可执行的 Python 代码：看起来像 JSON/Markdown/报告文本，已跳过执行。"
                            "请重新调用 execute_code，并仅提供纯 Python 代码（不要围栏、不要自然语言、不要 JSON 包壳）。"
                        )
                        await self.append_chat_history(
                            {"role": "tool", "tool_call_id": tool_id, "name": "execute_code", "content": msg}
                        )
                        await self.append_chat_history(
                            {
                                "role": "user",
                                "content": """请仅返回可直接运行的 Python 代码文本，不要任何 Markdown/JSON/说明文字。调用示例：{"code": "print('ok')"}""",
                            }
                        )
                        retry_count += 1
                        last_error_message = "非 Python 代码被拦截"
                        continue

                    # 4.2.4.2.6 轻清洗 code（不改写语义）
                    if LIGHT_CLEANING:
                        try:
                            code = TS.strip_fences_outer_or_all(raw_code or "")
                            code = TS.clean_control_chars(code, keep_whitespace=True)
                        except Exception as e:
                            logger.exception(f"轻清洗失败，使用原始代码继续执行: {e}")
                            code = raw_code
                    else:
                        code = raw_code

                    # 4.2.4.2.7 展示将要执行的代码并执行
                    await redis_manager.publish_message(self.task_id, InterpreterMessage(input={"code": code}))

                    logger.info("执行工具调用")
                    try:
                        text_to_gpt, error_occurred, error_message = await self.code_interpreter.execute_code(code)
                        # Handle None or empty outputs explicitly
                        if text_to_gpt is None:
                            text_to_gpt = []
                        if isinstance(text_to_gpt, (list, tuple)) and not text_to_gpt:
                            text_to_gpt = ["(no captured output from execution)"]
                        if error_message is None:
                            error_message = ""
                    except Exception as e:
                        text_to_gpt, error_occurred, error_message = (
                            ["(execution exception)"],
                            True,
                            f"执行工具时异常: {e}",
                        )

                    # 4.2.4.2.8 处理执行结果
                    text_to_gpt_str = (
                        "\n".join(str(item) for item in text_to_gpt)
                        if isinstance(text_to_gpt, (list, tuple))
                        else str(text_to_gpt)
                    )
                    if not text_to_gpt_str.strip():
                        text_to_gpt_str = "(tool returned no text or output)"  # Stronger fallback
                    logger.info(
                        f"Captured tool output length: {len(text_to_gpt_str)} | Preview: {text_to_gpt_str[:200]}"
                    )  # Debug log
                    if error_occurred:
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "execute_code",
                                "content": error_message or "(execution error)",
                            }
                        )
                        logger.warning(f"代码执行错误: {error_message}")
                        retry_count += 1
                        logger.info(f"当前尝试次:{retry_count} / {self.max_retries}")
                        last_error_message = error_message
                        reflection_prompt = get_reflection_prompt(error_message, code)
                        await redis_manager.publish_message(
                            self.task_id, SystemMessage(content="代码手反思纠正错误", type="error")
                        )
                        await self.append_chat_history({"role": "user", "content": reflection_prompt})
                        continue
                    else:
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "execute_code",
                                "content": text_to_gpt_str,
                            }
                        )
                        executed_tool_calls = True
                        code_executed_successfully = True
                        continue

                else:
                    logger.warning(f"收到未知工具调用: {fn_name}，跳过处理。")
                    await self.append_chat_history(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "name": fn_name or "unknown",
                            "content": "收到未知工具调用，未执行。",
                        }
                    )
                    retry_count += 1
                    continue

            # 4.2.4.3 无工具调用路径
            else:
                logger.info("收到 assistant 没有 tool_calls 的响应，进入完成性判定逻辑")
                await self.append_chat_history({"role": "assistant", "content": assistant_content_clean})
                assistant_content = assistant_content_clean

                # 4.2.4.3.1 若尚未“成功执行”过代码，强制引导继续走工具
                if not code_executed_successfully:
                    # —— 兜底：如果 content 里真的带了 {"code": "..."} 或围栏/裸代码，直接执行（不等模型再发工具调用）
                    fallback_code = _safe_get_code_from_any({}, fallback_text=assistant_content_clean)
                    if fallback_code.strip() and _looks_like_python(fallback_code):
                        if LIGHT_CLEANING:
                            try:
                                fallback_code = TS.strip_fences_outer_or_all(fallback_code or "")
                                fallback_code = TS.clean_control_chars(fallback_code, keep_whitespace=True)
                            except Exception:
                                pass
                        await redis_manager.publish_message(
                            self.task_id, InterpreterMessage(input={"code": fallback_code})
                        )
                        logger.info("兜底：从 content 中还原代码并直接执行")
                        try:
                            text_to_gpt, error_occurred, error_message = await self.code_interpreter.execute_code(
                                fallback_code
                            )
                        except Exception as e:
                            text_to_gpt, error_occurred, error_message = "", True, f"执行工具时异常: {e}"

                        text_to_gpt_str = (
                            "\n".join(text_to_gpt) if isinstance(text_to_gpt, (list, tuple)) else str(text_to_gpt)
                        )
                        if not text_to_gpt_str.strip():
                            text_to_gpt_str = "(tool returned no text)"  # 兜底非空content
                        if error_occurred:
                            # ★ 改动点：不再写入未配对的 tool 消息，改为 assistant 文本
                            await self.append_chat_history(
                                {
                                    "role": "assistant",
                                    "content": f"[执行失败] {error_message or '(execution error)'}",
                                }
                            )
                            logger.warning(f"兜底执行错误: {error_message}")
                            retry_count += 1
                            last_error_message = error_message
                            reflection_prompt = get_reflection_prompt(error_message, fallback_code)
                            await redis_manager.publish_message(
                                self.task_id, SystemMessage(content="代码手反思纠正错误", type="error")
                            )
                            await self.append_chat_history({"role": "user", "content": reflection_prompt})
                            continue
                        else:
                            # ★ 改动点：不再写入未配对的 tool 消息，改为 assistant 文本
                            await self.append_chat_history(
                                {
                                    "role": "assistant",
                                    "content": text_to_gpt_str,
                                }
                            )
                            executed_tool_calls = True
                            code_executed_successfully = True
                            continue

                    # —— 没有可用兜底：明确要求再次走工具调用
                    logger.info("尚未成功执行过代码，要求模型实际调用工具再总结")
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(
                            content=f"代码手尚未运行代码，请调用 execute_code 并执行用于 {subtask_title} 的代码",
                            type="info",
                        ),
                    )
                    run_code_request = (
                        "注意：你此前仅以文字说明了计划，但没有实际执行任何代码。"
                        "现在请调用 execute_code 并提供要执行的 Python 代码（确保生成本子任务需要的文件/图像/报告），"
                        "不要直接总结为“任务完成”，必须先运行并在工具响应中返回执行结果。"
                    )
                    await self.append_chat_history({"role": "user", "content": run_code_request})

                    retry_count += 1
                    logger.info(f"要求模型执行代码后的重试计数: {retry_count}/{self.max_retries}")

                    if retry_count >= self.max_retries:
                        logger.error("模型多次未实际执行工具，达到最大重试次数")
                        await redis_manager.publish_message(
                            self.task_id,
                            SystemMessage(content="模型未实际执行代码，达到最大重试次数，任务失败", type="error"),
                        )
                        raise Exception(f"Model refused to execute code after {self.max_retries} attempts.")
                    continue
                else:
                    # 4.2.4.3.2 已“成功执行”过工具，本轮无 tool_calls → 视为任务完成
                    logger.info("已成功执行过工具，本次 assistant 无 tool_calls，被视为任务完成")
                    return CoderToWriter(
                        coder_response=assistant_content_clean,
                        created_images=await self.code_interpreter.get_created_images(subtask_title),
                    )

        # 4.2.5 循环收尾与返回
        if retry_count >= self.max_retries:
            logger.error(f"超过最大尝试次数: {self.max_retries}")
            await redis_manager.publish_message(self.task_id, SystemMessage(content="超过最大尝试次数", type="error"))
            return f"Failed to complete task after {self.max_retries} attempts. Last error: {last_error_message}"

        if self.current_chat_turns >= self.max_chat_turns:
            logger.error(f"超过最大对话轮次: {self.max_chat_turns}")
            await redis_manager.publish_message(self.task_id, SystemMessage(content="超过最大对话轮次", type="error"))
            return f"Reached maximum number of chat turns ({self.max_chat_turns}). Task incomplete."

        logger.info(f"{self.__class__.__name__}:完成:执行子任务: {subtask_title}")
        return CoderToWriter(
            coder_response=assistant_content,
            created_images=await self.code_interpreter.get_created_images(subtask_title),
        )
