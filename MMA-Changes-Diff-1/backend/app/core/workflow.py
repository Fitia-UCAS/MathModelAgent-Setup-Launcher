# app/core/workflow.py

from app.core.agents import WriterAgent, CoderAgent, CoordinatorAgent, ModelerAgent
from app.schemas.request import Problem
from app.schemas.response import SystemMessage
from app.tools.openalex_scholar import OpenAlexScholar
from app.utils.log_util import logger
from app.utils.common_utils import create_work_dir, get_config_template
from app.models.user_output import UserOutput
from app.config.setting import settings
from app.tools.interpreter_factory import create_interpreter
from app.services.redis_manager import redis_manager
from app.tools.notebook_serializer import NotebookSerializer
from app.core.flows import Flows
from app.core.llm.llm_factory import LLMFactory
from app.tools.png_paths import collect_png_paths_by_task  # 扫描 PNG 路径


class WorkFlow:
    def __init__(self):
        pass

    def execute(self) -> str:
        # RichPrinter.workflow_start()
        # RichPrinter.workflow_end()
        pass


class MathModelWorkFlow(WorkFlow):
    task_id: str  #
    work_dir: str  # workflow work dir
    ques_count: int = 0  # 问题数量
    questions: dict[str, str | int] = {}  # 问题

    async def execute(self, problem: Problem):
        self.task_id = problem.task_id
        self.work_dir = create_work_dir(self.task_id)

        llm_factory = LLMFactory(self.task_id)
        coordinator_llm, modeler_llm, coder_llm, writer_llm = llm_factory.get_all_llms()

        coordinator_agent = CoordinatorAgent(self.task_id, coordinator_llm)

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="识别用户意图和拆解问题 ing..."),
        )

        try:
            coordinator_response = await coordinator_agent.run(problem.ques_all)
            self.questions = coordinator_response.questions
            self.ques_count = coordinator_response.ques_count
        except Exception as e:
            logger.error(f"CoordinatorAgent 执行失败: {e}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"识别/拆解失败：{e}", type="error"),
            )
            raise

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="识别用户意图和拆解问题完成，任务转交给建模手"),
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="建模手开始建模 ing..."),
        )

        modeler_agent = ModelerAgent(self.task_id, modeler_llm)

        try:
            modeler_response = await modeler_agent.run(coordinator_response)
        except Exception as e:
            logger.error(f"ModelerAgent 执行失败: {e}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"建模失败：{e}", type="error"),
            )
            raise

        user_output = UserOutput(work_dir=self.work_dir, ques_count=self.ques_count)

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="正在创建代码沙盒环境"),
        )

        notebook_serializer = NotebookSerializer(work_dir=self.work_dir)
        try:
            code_interpreter = await create_interpreter(
                kind="local",
                task_id=self.task_id,
                work_dir=self.work_dir,
                notebook_serializer=notebook_serializer,
                timeout=36000,
            )
        except Exception as e:
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"创建沙盒失败：{e}", type="error"),
            )
            raise

        # ❶ 删掉这一整段（学术检索实例创建）
        # scholar = OpenAlexScholar(task_id=self.task_id, email=settings.OPENALEX_EMAIL)

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="创建完成"),
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="初始化代码手"),
        )

        coder_agent = CoderAgent(
            task_id=problem.task_id,
            model=coder_llm,
            work_dir=self.work_dir,
            max_chat_turns=settings.MAX_CHAT_TURNS,
            max_retries=settings.MAX_RETRIES,
            code_interpreter=code_interpreter,
        )

        # ❷ 创建 WriterAgent 时，不再注入 scholar（显式传 None，更直观）
        writer_agent = WriterAgent(
            task_id=problem.task_id,
            model=writer_llm,
            comp_template=problem.comp_template,
            format_output=problem.format_output,
            scholar=None,  # 禁用外部检索
        )

        flows = Flows(self.questions)

        # ============================ solution steps ============================
        solution_flows = flows.get_solution_flows(self.questions, modeler_response)
        config_template = get_config_template(problem.comp_template)

        for key, value in solution_flows.items():
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"代码手开始求解 {key}"),
            )

            try:
                coder_response = await coder_agent.run(prompt=value["coder_prompt"], subtask_title=key)
            except Exception as e:
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"代码手求解 {key} 失败：{e}", type="error"),
                )
                raise

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"代码手求解成功 {key}", type="success"),
            )

            writer_prompt = flows.get_writer_prompt(
                key,
                coder_response.code_response,
                code_interpreter,
                config_template,
            )

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手开始写 {key} 部分"),
            )

            # 扫描全部可用图片
            all_images_raw = collect_png_paths_by_task(self.task_id) or []

            # 规范化为相对路径（去掉可能的 task_id/ 前缀）
            prefix = f"{self.task_id}/"
            all_images = [p[len(prefix) :] if p.startswith(prefix) else p for p in all_images_raw]

            if key == "eda":
                available_images = [p for p in all_images if p.startswith("eda/figures/")]
            elif key.startswith("ques"):
                available_images = [p for p in all_images if p.startswith(f"{key}/figures/")]
            elif key == "sensitivity_analysis":
                available_images = [p for p in all_images if p.startswith("sensitivity_analysis/figures/")]
            else:
                available_images = []

            try:
                writer_response = await writer_agent.run(
                    writer_prompt,
                    available_images=available_images,
                    sub_title=key,
                )
            except Exception as e:
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手写作 {key} 失败：{e}", type="error"),
                )
                raise

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手完成 {key} 部分"),
            )

            user_output.set_res(key, writer_response)

        # 关闭沙盒
        try:
            await code_interpreter.cleanup()
        except Exception as e:
            logger.warning(f"清理沙盒出现问题：{e}")
        finally:
            logger.info(user_output.get_res())

        # ============================ write steps ============================
        write_flows = flows.get_write_flows(user_output, config_template, problem.ques_all)
        for key, value in write_flows.items():
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手开始写 {key} 部分"),
            )

            all_images = collect_png_paths_by_task(self.task_id) or []

            if key == "eda":
                available_images = [p for p in all_images if p.startswith("eda/figures/")]
            elif key.startswith("ques"):
                available_images = [p for p in all_images if p.startswith(f"{key}/figures/")]
            elif key == "sensitivity_analysis":
                available_images = [p for p in all_images if p.startswith("sensitivity_analysis/figures/")]
            else:
                available_images = []

            try:
                writer_response = await writer_agent.run(
                    prompt=value,
                    available_images=available_images,
                    sub_title=key,
                )
            except Exception as e:
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手写作 {key} 失败：{e}", type="error"),
                )
                raise

            user_output.set_res(key, writer_response)

        logger.info(user_output.get_res())
        user_output.save_result()
