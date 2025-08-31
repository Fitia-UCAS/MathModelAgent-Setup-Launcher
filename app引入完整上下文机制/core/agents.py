import json
from app.core.llm import LLM
from app.core.prompts import (
    get_completion_check_prompt,
    get_reflection_prompt,
    get_writer_prompt,
    CODER_PROMPT,
    MODELER_PROMPT,
)
from app.core.functions import tools
from app.models.model import CoderToWriter
from app.models.user_output import UserOutput
from app.utils.enums import CompTemplate, FormatOutPut
from app.utils.log_util import logger
from app.config.setting import settings
from app.utils.common_utils import get_current_files
from app.utils.redis_manager import redis_manager
from app.schemas.response import SystemMessage
from app.tools.base_interpreter import BaseCodeInterpreter


class Agent:
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 30,
        user_output: UserOutput = None,
        max_memory: int = 20,
    ) -> None:
        self.task_id = task_id
        self.model = model
        self.chat_history: list[dict] = []
        self.max_chat_turns = max_chat_turns
        self.current_chat_turns = 0
        self.user_output = user_output
        self.max_memory = max_memory

    async def run(self, prompt: str, system_prompt: str, sub_title: str) -> str:
        try:
            logger.info(f"{self.__class__.__name__}:开始:执行对话")
            self.current_chat_turns = 0
            self.append_chat_history({"role": "system", "content": system_prompt})
            self.append_chat_history({"role": "user", "content": prompt})

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

    def append_chat_history(self, msg: dict) -> None:
        self.clear_memory()
        self.chat_history.append(msg)

    def clear_memory(self):
        logger.info(f"{self.__class__.__name__}:清除记忆")
        if len(self.chat_history) <= self.max_memory:
            return
        self.chat_history = self.chat_history[:2] + self.chat_history[-5:]


class ModelerAgent(Agent):
    def __init__(
        self,
        model: LLM,
        max_chat_turns: int = 30,
    ) -> None:
        super().__init__(model, max_chat_turns)
        self.system_prompt = MODELER_PROMPT


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

    async def run(self, prompt: str, subtask_title: str) -> CoderToWriter:
        logger.info(f"{self.__class__.__name__}:开始:执行子任务: {subtask_title}")
        self.code_interpreter.add_section(subtask_title)

        if self.is_first_run:
            logger.info("首次运行，添加系统提示和数据集文件信息")
            self.is_first_run = False
            self.append_chat_history({"role": "system", "content": self.system_prompt})
            self.append_chat_history(
                {
                    "role": "user",
                    "content": f"当前文件夹下的数据集文件{get_current_files(self.work_dir, 'data')}",
                }
            )

        logger.info(f"添加子任务提示: {prompt}")
        self.append_chat_history({"role": "user", "content": prompt})

        retry_count = 0
        last_error_message = ""
        consecutive_error_count = 0

        while self.current_chat_turns < self.max_chat_turns:
            self.current_chat_turns += 1
            logger.info(f"当前对话轮次: {self.current_chat_turns}/{self.max_chat_turns}")

            response = await self.model.chat(
                history=self.chat_history,
                tools=tools,
                tool_choice="auto",
                agent_name=self.__class__.__name__,
            )

            response_message = response.choices[0].message
            
            # Construct the assistant message dictionary carefully
            assistant_message = {"role": "assistant"}
            if response_message.content:
                assistant_message["content"] = response_message.content
            if hasattr(response_message, "tool_calls") and response_message.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in response_message.tool_calls
                ]
            
            self.append_chat_history(assistant_message)

            if not assistant_message.get("tool_calls"):
                logger.info("模型没有要求调用工具，任务完成。")
                return CoderToWriter(
                    status="success",
                    summary=response_message.content or "任务已完成，但模型未提供最终总结。",
                    code_response="",
                    code_execution_result="",
                    created_images=await self.code_interpreter.get_created_images(subtask_title)
                )

            logger.info("检测到工具调用，开始执行。")
            tool_messages_to_append = []

            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "execute_code":
                    logger.info(f"调用工具: {tool_call.function.name}")
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(content=f"代码手调用{tool_call.function.name}工具"),
                    )
                    
                    code = ""
                    try:
                        code = json.loads(tool_call.function.arguments)["code"]
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.error(f"从tool_call解析代码失败: {e}")
                        response_content = f"Error parsing arguments: {e}"
                    else:
                        logger.info("执行代码...")
                        (
                            text_to_gpt,
                            error_occurred,
                            error_message,
                        ) = await self.code_interpreter.execute_code(code)

                        if error_occurred:
                            logger.warning(f"代码执行错误: {error_message}")
                            response_content = error_message
                            retry_count += 1
                            if retry_count >= self.max_retries:
                                logger.error(f"任务 '{subtask_title}' 已达到最大重试次数。")
                                return CoderToWriter(status="failed", summary=f"任务因达到最大重试次数而失败: {error_message}", code_response=code, code_execution_result=error_message, created_images=[])

                            if error_message == last_error_message:
                                consecutive_error_count += 1
                            else:
                                consecutive_error_count = 1
                            last_error_message = error_message

                            if consecutive_error_count >= 2:
                                logger.error(f"在同一个错误上连续失败2次，任务 '{subtask_title}' 熔断。")
                                return CoderToWriter(status="failed", summary=f"任务因无法解决的错误而失败: {error_message}", code_response=code, code_execution_result=error_message, created_images=[])
                        else:
                            response_content = text_to_gpt
                    
                    tool_messages_to_append.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": response_content,
                    })

            for msg in tool_messages_to_append:
                self.append_chat_history(msg)

        logger.warning(f"{self.__class__.__name__}:任务失败: {subtask_title} - 已达到最大对话轮次。")
        return CoderToWriter(status="failed", summary="任务因达到最大循环次数而失败。", code_response="", code_execution_result="", created_images=[])


class WriterAgent(Agent):
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 10,
        comp_template: CompTemplate = CompTemplate,
        format_output: FormatOutPut = FormatOutPut.Markdown,
        user_output: UserOutput = None,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns, user_output)
        self.format_out_put = format_output
        self.comp_template = comp_template
        self.system_prompt = get_writer_prompt(format_output)
        self.available_images: list[str] = []

    async def run(
        self,
        prompt: str,
        available_images: list[str] = None,
        sub_title: str = None,
    ) -> str:
        logger.info(f"subtitle是:{sub_title}")

        if available_images:
            self.available_images = available_images
            image_list = ",".join(available_images)
            image_prompt = f"\n\n重要指令：在写作时，你必须只引用以下列表中提供的图片链接：\n{image_list}\n严禁引用任何不在此列表中的图片。"
            prompt += image_prompt
        else:
            no_image_prompt = "\n\n重要指令：在此步骤中，没有生成任何图片。因此，你的回复中严禁包含任何图片引用、图片链接或Markdown格式的图片（例如 `![alt text](path/to/image.png)`）。"
            prompt += no_image_prompt

        try:
            logger.info(f"{self.__class__.__name__}:开始:执行对话")
            self.current_chat_turns = 0
            self.append_chat_history({"role": "system", "content": self.system_prompt})
            self.append_chat_history({"role": "user", "content": prompt})

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

    async def summarize(self) -> str:
        """
        总结对话内容
        """
        try:
            self.append_chat_history(
                {"role": "user", "content": "请简单总结以上完成什么任务取得什么结果:"}
            )
            response = await self.model.chat(
                history=self.chat_history, agent_name=self.__class__.__name__
            )
            self.append_chat_history(
                {"role": "assistant", "content": response.choices[0].message.content}
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"总结生成失败: {str(e)}")
            return "由于网络原因无法生成详细总结，但已完成主要任务处理。"