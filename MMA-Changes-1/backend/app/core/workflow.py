# app/core/workflow.py

import json

from app.core.agents import WriterAgent, CoderAgent, CoordinatorAgent, ModelerAgent
from app.schemas.request import Problem
from app.schemas.response import SystemMessage
from app.utils.log_util import logger
from app.utils.common_utils import create_work_dir, get_config_template
from app.models.user_output import UserOutput
from app.config.setting import settings
from app.tools.interpreter_factory import create_interpreter
from app.services.redis_manager import redis_manager
from app.tools.notebook_serializer import NotebookSerializer
from app.core.flows import Flows
from app.core.llm.llm_factory import LLMFactory

# 仅返回相对工作目录的图片路径（不包含 task_id 前缀）
from app.tools.png_paths import (
    collect_png_paths_by_task,
    rewrite_image_paths_by_basename,
    validate_markdown_image_refs,
)


# 1 工具函数
def _normalize_writer_response_to_str(resp) -> str:
    """
    1.1 统一将 writer_agent.run(...) 的返回转换为字符串
    1.2 支持类型：str / 带 response_content|content|text|markdown 的对象 / pydantic BaseModel(v1|v2) / dict / list / 其它
    1.3 返回：字符串（失败时空串）
    """
    if isinstance(resp, str):
        return resp

    # 1.2.1 常见文本属性
    for attr in ("response_content", "content", "text", "markdown"):
        try:
            v = getattr(resp, attr, None)
            if isinstance(v, str):
                return v
        except Exception:
            pass

    # 1.2.2 pydantic BaseModel（兼容 v1/v2）
    try:
        from pydantic import BaseModel  # 项目中已依赖 pydantic

        if isinstance(resp, BaseModel):
            d = None
            try:
                d = resp.dict()  # v1
            except Exception:
                try:
                    d = resp.model_dump()  # v2
                except Exception:
                    d = None
            if isinstance(d, dict):
                for k in ("response_content", "content", "text", "markdown"):
                    v = d.get(k)
                    if isinstance(v, str):
                        return v
                try:
                    return json.dumps(d, ensure_ascii=False)
                except Exception:
                    return str(resp)
    except Exception:
        pass

    # 1.2.3 dict / list
    if isinstance(resp, (dict, list)):
        try:
            return json.dumps(resp, ensure_ascii=False)
        except Exception:
            return str(resp)

    # 1.2.4 兜底
    try:
        return str(resp)
    except Exception:
        return ""


# 2 基类
class WorkFlow:
    """2.1 工作流基类（子类实现 execute）"""

    def __init__(self):
        pass

    def execute(self) -> str:
        # 2.2 占位：子类覆盖
        # RichPrinter.workflow_start()
        # RichPrinter.workflow_end()
        pass


# 3 具体实现：数学建模工作流
class MathModelWorkFlow(WorkFlow):
    """
    3.1 概述
        3.1.1 编排：题面解析 → 建模 → 代码执行 → 写作 → 结果保存
        3.1.2 角色：Coordinator / Modeler / Coder / Writer
        3.1.3 约束：图片引用全局去重，最终报告落盘
    """

    task_id: str
    work_dir: str
    ques_count: int = 0
    questions: dict[str, str | int] = {}

    # 3.2 全局“已使用图片”集合（确保全篇每图只用一次）
    _used_images: set[str]

    async def execute(self, problem: Problem):
        """
        3.3 执行入口（步骤总览）
            3.3.1 初始化与准备
            3.3.2 协调器：题面结构化
            3.3.3 建模器：生成建模方案
            3.3.4 准备代码沙盒与解释器
            3.3.5 按 flows 求解并写作（逐章循环）
            3.3.6 写作汇总阶段
            3.3.7 收尾与保存
        """

        # 3.3.1 初始化与准备
        self.task_id = problem.task_id
        self.work_dir = create_work_dir(self.task_id)
        self._used_images = set()

        llm_factory = LLMFactory(self.task_id)
        coordinator_llm, modeler_llm, coder_llm, writer_llm = llm_factory.get_all_llms()

        coordinator_agent = CoordinatorAgent(self.task_id, coordinator_llm)

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="识别用户意图和拆解问题 ing..."),
        )

        # 3.3.2 协调器：题面结构化
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

        # 3.3.3 建模器：生成建模方案
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

        # 3.3.4 准备代码沙盒与解释器
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

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="创建完成"),
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="初始化代码手"),
        )

        # 3.3.5 构造 CoderAgent 与 WriterAgent
        coder_agent = CoderAgent(
            task_id=problem.task_id,
            model=coder_llm,
            work_dir=self.work_dir,
            max_chat_turns=settings.MAX_CHAT_TURNS,
            max_retries=settings.MAX_RETRIES,
            code_interpreter=code_interpreter,
        )

        # 3.3.5.1 禁用外部检索：显式传 None
        writer_agent = WriterAgent(
            task_id=problem.task_id,
            model=writer_llm,
            comp_template=problem.comp_template,
            format_output=problem.format_output,
            scholar=None,
        )

        flows = Flows(self.questions)

        # 3.3.5.2 生成求解步骤并逐章求解
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

            # 3.3.5.2.1 生成写作提示（传入代码产出）
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

            # 3.3.5.2.2 图片：扫描 → 章节过滤 → 全局去重
            all_images = collect_png_paths_by_task(self.task_id) or []
            available_images = self._filter_images_for_section(key, all_images)

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

            # 3.3.5.2.3 统一转换为字符串 → 规范路径 → 校验引用 → 记账
            md_text = _normalize_writer_response_to_str(writer_response)
            if not md_text:
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手写作 {key} 返回空内容，已以空字符串继续处理", type="warning"),
                )
                md_text = ""

            writer_response_fixed = rewrite_image_paths_by_basename(md_text, available_images)
            valid_refs, invalid_refs = validate_markdown_image_refs(writer_response_fixed, available_images)

            if invalid_refs:
                # 3.3.5.2.3.1 非法引用占位替换并提示
                for bad in invalid_refs:
                    writer_response_fixed = writer_response_fixed.replace(f"]({bad})", "](#)")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手 {key} 存在未找到的图片引用：{invalid_refs}", type="warning"),
                )

            # 3.3.5.2.3.2 全局记账：避免后续章节重复引用
            for p in valid_refs:
                self._used_images.add(p)

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手完成 {key} 部分"),
            )

            user_output.set_res(key, writer_response_fixed)

        # 3.3.5.3 关闭沙盒并记录中间产物
        try:
            await code_interpreter.cleanup()
        except Exception as e:
            logger.warning(f"清理沙盒出现问题：{e}")
        finally:
            logger.info(user_output.get_res())

        # 3.3.6 写作汇总阶段（封面/重述/分析/假设/符号/评价）
        write_flows = flows.get_write_flows(user_output, config_template, problem.ques_all)
        for key, value in write_flows.items():
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手开始写 {key} 部分"),
            )

            # 3.3.6.1 图片：扫描 → 章节过滤 → 全局去重
            all_images = collect_png_paths_by_task(self.task_id) or []
            available_images = self._filter_images_for_section(key, all_images)

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

            # 3.3.6.2 统一转换为字符串
            md_text = _normalize_writer_response_to_str(writer_response)
            if not md_text:
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手写作 {key} 返回空内容，已以空字符串继续处理", type="warning"),
                )
                md_text = ""

            # 3.3.6.3 写作后：修正路径 + 校验 + 记账
            writer_response_fixed = rewrite_image_paths_by_basename(md_text, available_images)
            valid_refs, invalid_refs = validate_markdown_image_refs(writer_response_fixed, available_images)
            if invalid_refs:
                for bad in invalid_refs:
                    writer_response_fixed = writer_response_fixed.replace(f"]({bad})", "](#)")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手 {key} 存在未找到的图片引用：{invalid_refs}", type="warning"),
                )
            for p in valid_refs:
                self._used_images.add(p)

            user_output.set_res(key, writer_response_fixed)

        # 3.3.7 收尾与保存
        logger.info(user_output.get_res())
        user_output.save_result()

    # 4 辅助：按章节过滤 + 全局去重
    def _filter_images_for_section(self, key: str, all_images: list[str]) -> list[str]:
        """
        4.1 规则
            4.1.1 eda                  → eda/figures/...
            4.1.2 quesN                → quesN/figures/...
            4.1.3 sensitivity_analysis → sensitivity_analysis/figures/...
            4.1.4 返回前做全局去重（避免跨章节重复引用）
        """
        if key == "eda":
            base = [p for p in all_images if p.startswith("eda/figures/")]
        elif key.startswith("ques"):
            base = [p for p in all_images if p.startswith(f"{key}/figures/")]
        elif key == "sensitivity_analysis":
            base = [p for p in all_images if p.startswith("sensitivity_analysis/figures/")]
        else:
            base = []

        # 4.1.4 全局去重
        return [p for p in base if p not in getattr(self, "_used_images", set())]
