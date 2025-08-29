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
import json
from app.core.prompts import get_reflection_prompt, get_completion_check_prompt
from app.core.functions import coder_tools
from icecream import ic


# 代码手
class CoderAgent(Agent):  # 同样继承自Agent类
    def __init__(
        self,
        task_id: str,
        model: LLM,
        work_dir: str,  # 工作目录
        max_chat_turns: int = settings.MAX_CHAT_TURNS,  # 最大聊天次数
        max_retries: int = settings.MAX_RETRIES,  # 最大反思次数
        code_interpreter: BaseCodeInterpreter = None,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.work_dir = work_dir
        self.max_retries = max_retries
        self.is_first_run = True
        self.system_prompt = CODER_PROMPT
        self.code_interpreter = code_interpreter

    async def run(self, prompt: str, subtask_title: str) -> CoderToWriter:
        logger.info(f"{self.__class__.__name__}:开始:执行子任务: {subtask_title}")
        # 标记当前子任务区段，便于 interpreter 管理输出文件/图片
        self.code_interpreter.add_section(subtask_title)

        retry_count = 0
        last_error_message = ""
        executed_tool_calls = False  # 是否至少执行过一次 execute_code
        merged_prompt = None  # 首轮合并提示（如果有）
        assistant_content = ""  # 兜底：循环外返回时使用

        # 如果是第一次运行，则添加系统提示；并把“文件列表 + 子任务提示”合并为一条 user 消息
        if self.is_first_run:
            logger.info("首次运行，添加系统提示和数据集文件信息")
            self.is_first_run = False

            # 1) system 消息
            await self.append_chat_history({"role": "system", "content": self.system_prompt})

            # 2) 合并后的首条 user 消息（避免连续 user）
            files_info = f"当前文件夹下的数据集文件{get_current_files(self.work_dir, 'data')}"
            merged_prompt = f"{files_info}\n\n{subtask_title}：\n{prompt}"
            logger.info(f"添加首轮合并子任务提示: {merged_prompt}")
            await self.append_chat_history({"role": "user", "content": merged_prompt})
        else:
            # 非首次运行，正常追加一条 user 提示
            logger.info(f"添加子任务提示: {prompt}")
            await self.append_chat_history({"role": "user", "content": prompt})

        # 早期保护：若已超出最大轮次则直接报错
        if self.current_chat_turns >= self.max_chat_turns:
            logger.error(f"超过最大聊天次数: {self.max_chat_turns}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content="超过最大聊天次数", type="error"),
            )
            raise Exception(f"Reached maximum number of chat turns ({self.max_chat_turns}). Task incomplete.")

        # 主循环：通过模型交互 + 工具调用完成任务
        while retry_count < self.max_retries and self.current_chat_turns < self.max_chat_turns:
            self.current_chat_turns += 1
            logger.info(f"当前对话轮次: {self.current_chat_turns}")

            response = await self.model.chat(
                history=self.chat_history,
                tools=coder_tools,
                tool_choice="auto",
                agent_name=self.__class__.__name__,
            )

            # 规范化 assistant 消息对象
            assistant_msg_obj = response.choices[0].message
            assistant_content = getattr(assistant_msg_obj, "content", "") or ""
            assistant_tool_calls = getattr(assistant_msg_obj, "tool_calls", None)

            # 有工具调用（常见路径）
            if assistant_tool_calls:
                logger.info("检测到工具调用")
                # 先把 assistant 内容规范化写入历史
                await self.append_chat_history(
                    {"role": "assistant", "content": assistant_content, "tool_calls": assistant_tool_calls}
                )

                # 取第一个工具调用进行处理（当前仅支持 execute_code）
                tool_call = assistant_tool_calls[0]
                tool_id = getattr(tool_call, "id", None)
                fn_name = getattr(tool_call.function, "name", None)

                if fn_name == "execute_code":
                    executed_tool_calls = True
                    logger.info(f"调用工具: {fn_name}")
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(content=f"代码手调用{fn_name}工具"),
                    )

                    # 解析代码参数
                    try:
                        code_args = json.loads(tool_call.function.arguments)
                        code = code_args.get("code", "")
                    except Exception as e:
                        code = ""
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

                    await redis_manager.publish_message(
                        self.task_id,
                        InterpreterMessage(
                            input={"code": code},
                        ),
                    )

                    # 执行工具调用（实际运行代码）
                    logger.info("执行工具调用")
                    try:
                        text_to_gpt, error_occurred, error_message = await self.code_interpreter.execute_code(code)
                    except Exception as e:
                        text_to_gpt, error_occurred, error_message = "", True, f"执行工具时异常: {e}"

                    # 将 tool 响应写回历史
                    if error_occurred:
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "execute_code",
                                "content": error_message,
                            }
                        )

                        logger.warning(f"代码执行错误: {error_message}")
                        retry_count += 1
                        logger.info(f"当前尝试次:{retry_count} / {self.max_retries}")
                        last_error_message = error_message
                        reflection_prompt = get_reflection_prompt(error_message, code)

                        await redis_manager.publish_message(
                            self.task_id,
                            SystemMessage(content="代码手反思纠正错误", type="error"),
                        )

                        # 追加 user 反思提示让模型修正（前一条是 tool 响应，顺序合法）
                        await self.append_chat_history({"role": "user", "content": reflection_prompt})
                        # 继续下一轮
                        continue
                    else:
                        # 成功执行的 tool 响应写回历史
                        text_to_gpt_str = (
                            "\n".join(text_to_gpt) if isinstance(text_to_gpt, (list, tuple)) else str(text_to_gpt)
                        )
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "execute_code",
                                "content": text_to_gpt_str,
                            }
                        )

                        # 成功执行后，让模型进行完成度自检（使用 get_completion_check_prompt）
                        prompt_for_check = merged_prompt if merged_prompt is not None else prompt
                        completion_prompt = get_completion_check_prompt(prompt_for_check, text_to_gpt_str)
                        await self.append_chat_history({"role": "user", "content": completion_prompt})

                        # 进入下一轮，由模型决定是否继续调用工具或直接总结结束
                        continue
                else:
                    # 未知工具，写日志并尝试继续（或可扩展支持更多工具）
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

            else:
                # 没有 tool_calls 的 assistant 响应 —— 不要马上判定完成
                logger.info("收到 assistant 没有 tool_calls 的响应，进入完成性判定逻辑")

                # 先把 assistant 内容规范化写入历史
                await self.append_chat_history({"role": "assistant", "content": assistant_content})

                # 如果从未执行过任何 execute_code，则强制要求模型先执行代码
                if not executed_tool_calls:
                    logger.info("尚未执行过 execute_code，要求模型实际调用工具再总结（避免未经执行就报告完成）")
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(
                            content=f"代码手尚未运行代码，请调用 execute_code 并执行用于 {subtask_title} 的代码", type="info"
                        ),
                    )

                    run_code_request = (
                        "注意：你此前仅以文字说明了计划，但没有实际执行任何代码。"
                        " 现在请立刻调用 `execute_code` 工具并提供要执行的 Python 代码（确保生成本子任务需要的文件/图像/报告），"
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

                    # 继续下一轮，等待模型发出 tool_calls
                    continue
                else:
                    # 已至少执行过一次工具，而这次 assistant 没有发起工具调用，可视为模型在做总结
                    logger.info("已执行过工具，本次 assistant 无 tool_calls，被视为任务完成")
                    return CoderToWriter(
                        coder_response=assistant_content,
                        created_images=await self.code_interpreter.get_created_images(subtask_title),
                    )

        # —— while 循环结束后的安全检查 —— #
        if retry_count >= self.max_retries:
            logger.error(f"超过最大尝试次数: {self.max_retries}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content="超过最大尝试次数", type="error"),
            )
            return f"Failed to complete task after {self.max_retries} attempts. Last error: {last_error_message}"

        if self.current_chat_turns >= self.max_chat_turns:
            logger.error(f"超过最大对话轮次: {self.max_chat_turns}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content="超过最大对话轮次", type="error"),
            )
            return f"Reached maximum number of chat turns ({self.max_chat_turns}). Task incomplete."

        # 循环正常结束（兜底返回最后一次 assistant 内容）
        logger.info(f"{self.__class__.__name__}:完成:执行子任务: {subtask_title}")
        return CoderToWriter(
            coder_response=assistant_content,
            created_images=await self.code_interpreter.get_created_images(subtask_title),
        )
