# app/tools/e2b_interpreter.py

# 1 导入与依赖
# 1.1 标准库与第三方
import os
import json

# 1.2 第三方沙箱与项目内类型
from e2b_code_interpreter import AsyncSandbox
from app.schemas.response import (
    ErrorModel,
    OutputItem,
    ResultModel,
    StdErrModel,
    StdOutModel,
    SystemMessage,
)
from app.services.redis_manager import redis_manager
from app.tools.notebook_serializer import NotebookSerializer
from app.utils.log_util import logger
from app.config.setting import settings

# 1.3 基类
from app.tools.base_interpreter import BaseCodeInterpreter


# 2 E2BCodeInterpreter 实现
# 2.1 目的：基于 e2b_code_interpreter 的解释器实现，负责初始化沙箱、上传/下载文件、执行代码并推送结果
class E2BCodeInterpreter(BaseCodeInterpreter):
    # 2.1.1 初始化：设置必要属性（包含 created_images 集合用于追踪新生成图片）
    def __init__(
        self,
        task_id: str,
        work_dir: str,
        notebook_serializer: NotebookSerializer,
    ):
        super().__init__(task_id, work_dir, notebook_serializer)
        self.sbx = None
        self.created_images = set()

    # 2.2 工厂方法：创建实例（不启动沙箱）
    @classmethod
    async def create(
        cls,
        task_id: str,
        work_dir: str,
        notebook_serializer: NotebookSerializer,
    ) -> "E2BCodeInterpreter":
        # 2.2.1 仅构造对象，初始化实际资源在 initialize 中完成
        instance = cls(task_id, work_dir, notebook_serializer)
        return instance

    # 2.3 初始化沙箱环境并上传工作目录文件
    async def initialize(self, timeout: int = 36000):
        # 2.3.1 启动 AsyncSandbox，执行预置初始化代码并上传文件
        try:
            self.sbx = await AsyncSandbox.create(api_key=settings.E2B_API_KEY, timeout=timeout)
            logger.info("沙箱环境初始化成功")
            await self._pre_execute_code()
            await self._upload_all_files()
        except Exception as e:
            logger.error(f"初始化沙箱环境失败: {str(e)}")
            raise

    # 2.4 将工作目录中的 CSV/XLSX 文件上传到沙箱（/home/user/ 下）
    async def _upload_all_files(self):
        # 2.4.1 检查目录、列举文件并逐个上传（出现错误则记录并抛出）
        try:
            logger.info(f"开始上传文件，工作目录: {self.work_dir}")
            if not os.path.exists(self.work_dir):
                logger.error(f"工作目录不存在: {self.work_dir}")
                raise FileNotFoundError(f"工作目录不存在: {self.work_dir}")

            files = [f for f in os.listdir(self.work_dir) if f.endswith((".csv", ".xlsx"))]
            logger.info(f"工作目录中的文件列表: {files}")

            for file in files:
                file_path = os.path.join(self.work_dir, file)
                if os.path.isfile(file_path):
                    try:
                        with open(file_path, "rb") as f:
                            content = f.read()
                            # 使用沙箱的 files.write 接口上传二进制内容
                            await self.sbx.files.write(f"/home/user/{file}", content)
                            logger.info(f"成功上传文件到沙箱: {file}")
                    except Exception as e:
                        logger.error(f"上传文件 {file} 失败: {str(e)}")
                        raise

        except Exception as e:
            logger.error(f"文件上传过程失败: {str(e)}")
            raise

    # 2.5 预执行代码（在内核/沙箱就绪后执行的初始化脚本）
    async def _pre_execute_code(self):
        init_code = (
            "import matplotlib.pyplot as plt\n"
            # 可以在此处添加字体/渲染相关设置（注释掉以避免环境特异性问题）
            # "plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial Unicode MS']\n"
            # "plt.rcParams['axes.unicode_minus'] = False\n"
            # "plt.rcParams['font.family'] = 'sans-serif'\n"
        )
        await self.execute_code(init_code)

    # 2.6 执行代码主入口：在沙箱中执行并收集结果（文本/错误/各种 repr）
    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        # 2.6.1 前置检查：确保沙箱已初始化
        if not self.sbx:
            raise RuntimeError("沙箱环境未初始化")

        logger.info(f"执行代码: {code}")
        self.notebook_serializer.add_code_cell_to_notebook(code)

        text_to_gpt: list[str] = []
        content_to_display: list[OutputItem] | None = []
        error_occurred: bool = False
        error_message: str = ""

        # 2.6.2 推送开始执行的系统消息
        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="开始执行代码"),
        )
        logger.info("开始在沙箱中执行代码...")
        execution = await self.sbx.run_code(code)  # 返回 Execution 对象
        logger.info("代码执行完成，开始处理结果...")

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="代码执行完成"),
        )

        # 2.6.3 处理执行错误（execution.error 结构）
        if execution.error:
            error_occurred = True
            error_message = f"Error: {execution.error.name}: {execution.error.value}\n{execution.error.traceback}"
            error_message = self._truncate_text(error_message)
            logger.error(f"执行错误: {error_message}")
            text_to_gpt.append(self.delete_color_control_char(error_message))
            content_to_display.append(
                ErrorModel(
                    name=execution.error.name,
                    value=execution.error.value,
                    traceback=execution.error.traceback,
                )
            )

        # 2.6.4 处理标准输出 / 标准错误日志
        if execution.logs:
            if execution.logs.stdout:
                stdout_str = "\n".join(execution.logs.stdout)
                stdout_str = self._truncate_text(stdout_str)
                logger.info(f"标准输出: {stdout_str}")
                text_to_gpt.append(stdout_str)
                content_to_display.append(StdOutModel(msg="\n".join(execution.logs.stdout)))
                self.notebook_serializer.add_code_cell_output_to_notebook(stdout_str)

            if execution.logs.stderr:
                stderr_str = "\n".join(execution.logs.stderr)
                stderr_str = self._truncate_text(stderr_str)
                logger.warning(f"标准错误: {stderr_str}")
                text_to_gpt.append(stderr_str)
                content_to_display.append(StdErrModel(msg="\n".join(execution.logs.stderr)))

        # 2.6.5 处理 execution.results 中的各种展示能力（text/html/png/...）
        if execution.results:
            for result in execution.results:
                # 文本表示（__str__）
                if str(result):
                    content_to_display.append(ResultModel(type="result", format="text", msg=str(result)))
                # HTML 表示
                try:
                    html = result._repr_html_() if hasattr(result, "_repr_html_") else None
                    if html:
                        content_to_display.append(ResultModel(type="result", format="html", msg=html))
                except Exception:
                    pass
                # Markdown 表示
                try:
                    md = result._repr_markdown_() if hasattr(result, "_repr_markdown_") else None
                    if md:
                        content_to_display.append(ResultModel(type="result", format="markdown", msg=md))
                except Exception:
                    pass
                # PNG / JPEG / SVG / PDF / LaTeX / JSON / JavaScript 表示（分别尝试）
                try:
                    if hasattr(result, "_repr_png_") and result._repr_png_():
                        content_to_display.append(ResultModel(type="result", format="png", msg=result._repr_png_()))
                except Exception:
                    pass
                try:
                    if hasattr(result, "_repr_jpeg_") and result._repr_jpeg_():
                        content_to_display.append(ResultModel(type="result", format="jpeg", msg=result._repr_jpeg_()))
                except Exception:
                    pass
                try:
                    if hasattr(result, "_repr_svg_") and result._repr_svg_():
                        content_to_display.append(ResultModel(type="result", format="svg", msg=result._repr_svg_()))
                except Exception:
                    pass
                try:
                    if hasattr(result, "_repr_pdf_") and result._repr_pdf_():
                        content_to_display.append(ResultModel(type="result", format="pdf", msg=result._repr_pdf_()))
                except Exception:
                    pass
                try:
                    if hasattr(result, "_repr_latex_") and result._repr_latex_():
                        content_to_display.append(ResultModel(type="result", format="latex", msg=result._repr_latex_()))
                except Exception:
                    pass
                try:
                    if hasattr(result, "_repr_json_") and result._repr_json_():
                        content_to_display.append(
                            ResultModel(
                                type="result",
                                format="json",
                                msg=json.dumps(result._repr_json_(), ensure_ascii=False),
                            )
                        )
                except Exception:
                    pass
                try:
                    if hasattr(result, "_repr_javascript_") and result._repr_javascript_():
                        content_to_display.append(
                            ResultModel(
                                type="result",
                                format="javascript",
                                msg=result._repr_javascript_(),
                            )
                        )
                except Exception:
                    pass

        # 2.6.6 将可读的结果/文本拼接为发送给 GPT 的摘要（同时对长文本做截断或占位提示）
        for item in content_to_display:
            if isinstance(item, dict):
                if item.get("type") in ["stdout", "stderr", "error"]:
                    text_to_gpt.append(self._truncate_text(item.get("content") or item.get("value") or ""))
            elif isinstance(item, ResultModel):
                if item.format in ["text", "html", "markdown", "json"]:
                    text_to_gpt.append(self._truncate_text(f"[{item.format}]\n{item.msg}"))
                elif item.format in ["png", "jpeg", "svg", "pdf"]:
                    text_to_gpt.append(f"[{item.format} 图片已生成，内容为 base64，未展示]")

        logger.info(f"text_to_gpt: {text_to_gpt}")
        combined_text = "\n".join(text_to_gpt)

        # 2.6.7 执行完成后尝试下载沙箱中新生成/更新的文件（不阻塞主流程）
        try:
            await self.download_all_files_from_sandbox()
            logger.info("文件同步完成")
        except Exception as e:
            logger.error(f"文件同步失败: {str(e)}")

        # 2.6.8 将展示内容推送到 WebSocket（前端渲染）
        await self._push_to_websocket(content_to_display)

        return (
            combined_text,
            error_occurred,
            error_message,
        )

    # 2.7 获取指定 section 新创建的图片列表（从沙箱列文件并比较差异）
    async def get_created_images(self, section: str) -> list[str]:
        # 2.7.1 若沙箱未初始化则返回空列表
        if not self.sbx:
            logger.warning("沙箱环境未初始化")
            return []

        try:
            files = await self.sbx.files.list("./")
            for file in files:
                if file.path.endswith(".png") or file.path.endswith(".jpg") or file.path.endswith(".jpeg"):
                    self.add_section(section)
                    self.section_output[section]["images"].append(file.name)

            # 2.7.2 计算本次新图片（差集），并更新 created_images 集合
            current_set = set(self.section_output[section]["images"])
            new_images = list(current_set - self.created_images)
            self.created_images.update(current_set)
            logger.info(f"{section}-获取创建的图片列表: {new_images}")
            return new_images
        except Exception as e:
            logger.error(f"获取创建的图片列表失败: {str(e)}")
            return []

    # 2.8 清理资源：尝试下载文件并停止沙箱进程
    async def cleanup(self):
        # 2.8.1 若沙箱存在且正在运行，先尝试同步文件再关闭
        try:
            if self.sbx:
                if await self.sbx.is_running():
                    try:
                        await self.download_all_files_from_sandbox()
                    except Exception as e:
                        logger.error(f"下载文件失败: {str(e)}")
                    finally:
                        await self.sbx.kill()
                        logger.info("成功关闭沙箱环境")
                else:
                    logger.warning("沙箱已经关闭，跳过清理步骤")
        except Exception as e:
            logger.error(f"清理沙箱环境失败: {str(e)}")
            # 清理失败不抛出，以避免影响上层流程

    # 2.9 从沙箱下载所有文件并与本地工作目录同步（覆盖同名文件）
    async def download_all_files_from_sandbox(self) -> None:
        # 2.9.1 列出 /home/user 下的文件并比较本地目录，下载新文件或覆盖已有文件
        try:
            sandbox_files = await self.sbx.files.list("/home/user")
            sandbox_files_dict = {f.name: f for f in sandbox_files}

            local_files = set()
            if os.path.exists(self.work_dir):
                local_files = set(os.listdir(self.work_dir))

            for file in sandbox_files:
                try:
                    # 排除常见 shell 配置文件
                    if file.name in [".bash_logout", ".bashrc", ".profile"]:
                        continue

                    local_path = os.path.join(self.work_dir, file.name)
                    should_download = True

                    # 这里保留简单策略：同名文件也进行覆盖（可根据需要改为时间戳/哈希比较）
                    if should_download:
                        content = await self.sbx.files.read(file.path, format="bytes")
                        os.makedirs(self.work_dir, exist_ok=True)
                        with open(local_path, "wb") as f:
                            f.write(content)
                        logger.info(f"同步文件: {file.name}")

                except Exception as e:
                    logger.error(f"同步文件 {file.name} 失败: {str(e)}")
                    continue

            logger.info("文件同步完成")

        except Exception as e:
            logger.error(f"文件同步失败: {str(e)}")
