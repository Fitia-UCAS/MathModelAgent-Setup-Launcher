# app/tools/local_interpreter.py

# 1 导入依赖
from app.tools.base_interpreter import BaseCodeInterpreter
from app.tools.notebook_serializer import NotebookSerializer
import jupyter_client
from jupyter_client import KernelManager
from app.utils.log_util import logger
import os
from app.services.redis_manager import redis_manager
from app.schemas.response import (
    OutputItem,
    ResultModel,
    StdErrModel,
    SystemMessage,
)


# 2 LocalCodeInterpreter
# 2.1 目的：本地执行 Python 代码（基于 Jupyter kernel），不做预处理或清洗
class LocalCodeInterpreter(BaseCodeInterpreter):
    def __init__(
        self,
        task_id: str,
        work_dir: str,
        notebook_serializer: NotebookSerializer,
    ):
        super().__init__(task_id, work_dir, notebook_serializer)
        self.km: KernelManager | None = None
        self.kc = None
        self.interrupt_signal = False

    # 2.2 启动内核（优先级：环境变量 kernel_name > python_exe > 默认 python3）
    def _start_kernel_with_env(self):
        kernel_name = (os.environ.get("MMA_KERNEL_NAME") or "").strip()
        python_exe = (os.environ.get("MMA_PYTHON_EXE") or "").strip()

        if kernel_name:
            logger.info(f"使用指定内核名称启动 Jupyter Kernel: {kernel_name}")
            self.km, self.kc = jupyter_client.manager.start_new_kernel(kernel_name=kernel_name)
            return

        if python_exe:
            logger.info(f"使用指定 Python 解释器启动 ipykernel: {python_exe}")
            km = KernelManager()
            km.kernel_cmd = [python_exe, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
            km.start_kernel()
            kc = km.client()
            kc.start_channels()
            self.km, self.kc = km, kc
            return

        logger.info("未设置 MMA_KERNEL_NAME / MMA_PYTHON_EXE，使用默认 kernel_name='python3'")
        self.km, self.kc = jupyter_client.manager.start_new_kernel(kernel_name="python3")

    # 2.3 初始化：启动内核并执行预置初始化代码
    async def initialize(self):
        logger.info("初始化本地内核（可绑定到自定义环境）")
        self._start_kernel_with_env()
        self._pre_execute_code()

    # 2.4 预执行代码：设置工作目录、绘图后端和目录结构
    def _pre_execute_code(self):
        init_lines = [
            "import os, sys, platform",
            f"work_dir = {repr(os.path.abspath(self.work_dir))}",
            "os.makedirs(work_dir, exist_ok=True)",
            "os.chdir(work_dir)",
            "print('当前工作目录:', os.getcwd())",
            "print('当前 Python 可执行文件:', sys.executable)",
            "print('Python 版本:', platform.python_version())",
            "import matplotlib as mpl",
            "mpl.use('Agg')",
            "import matplotlib.pyplot as plt",
            "try:",
            "    import seaborn as sns",
            "    sns.set_style('whitegrid')",
            "    sns.set_context('paper', font_scale=1.2)",
            "except Exception as _e:",
            "    print('[初始化] seaborn 不可用，跳过风格设置:', _e)",
            "plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'DejaVu Sans']",
            "plt.rcParams['axes.unicode_minus'] = False",
            "plt.rcParams['font.family'] = 'sans-serif'",
            "mpl.rcParams['font.size'] = 12",
            "mpl.rcParams['axes.labelsize'] = 12",
            "mpl.rcParams['xtick.labelsize'] = 10",
            "mpl.rcParams['ytick.labelsize'] = 10",
            "_q_count = 6",
            "print('\\n[题目数] 强制使用题目数 =', _q_count, '(fixed)')",
            "_sections = {'eda': ['datasets','figures','reports']}",
            "for i in range(1, _q_count + 1):",
            "    _sections[f'ques{i}'] = ['datasets','figures','reports']",
            "_sections['sensitivity_analysis'] = ['datasets','figures','reports']",
            "created = []",
            "for sec, subs in _sections.items():",
            "    for sub in subs:",
            "        p = os.path.join(work_dir, sec, sub)",
            "        os.makedirs(p, exist_ok=True)",
            "        created.append(os.path.relpath(p, work_dir))",
            "print('\\n[目录初始化] 已确保存在以下路径：')",
            "for p in created: print(' -', p)",
            "print('[目录初始化] 共计:', len(created), '个子目录\\n')",
        ]
        self.execute_code_("\n".join(init_lines))

    # 2.5 执行代码：发送至 kernel，收集输出并推送给前端
    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        code_for_exec = code
        try:
            self.notebook_serializer.add_code_cell_to_notebook(code_for_exec)
        except Exception:
            logger.exception("将代码写入 notebook 记录时出错，但继续执行。")

        text_to_gpt: list[str] = []
        content_to_display: list[OutputItem] = []
        error_occurred = False
        error_message = ""

        await redis_manager.publish_message(self.task_id, SystemMessage(content="开始执行代码", type="info"))
        logger.info("开始在本地执行代码...")
        try:
            execution = self.execute_code_(code_for_exec)
        except Exception as e:
            err = f"本地执行异常: {e}"
            logger.exception(err)
            await redis_manager.publish_message(self.task_id, SystemMessage(content=err, type="error"))
            return err, True, err

        logger.info("代码执行完成，开始处理结果...")
        await redis_manager.publish_message(self.task_id, SystemMessage(content="代码执行完成", type="info"))

        for mark, out_str in execution:
            if mark in ("stdout", "execute_result_text", "display_text", "execute_result_html", "display_html"):
                text_to_gpt.append(self._truncate_text(f"[{mark}]\n{out_str}"))
                content_to_display.append(ResultModel(type="result", format="text", msg=out_str))
                try:
                    self.notebook_serializer.add_code_cell_output_to_notebook(out_str)
                except Exception:
                    logger.exception("记录 cell output 到 notebook 失败")
            elif mark in ("execute_result_png", "execute_result_jpeg", "display_png", "display_jpeg"):
                text_to_gpt.append(f"[{mark} 图片已生成，内容为 base64，未展示]")
                try:
                    if "png" in mark:
                        self.notebook_serializer.add_image_to_notebook(out_str, "image/png")
                        content_to_display.append(ResultModel(type="result", format="png", msg=out_str))
                    else:
                        self.notebook_serializer.add_image_to_notebook(out_str, "image/jpeg")
                        content_to_display.append(ResultModel(type="result", format="jpeg", msg=out_str))
                except Exception:
                    logger.exception("保存图片到 notebook 失败")
                    content_to_display.append(ResultModel(type="result", format="text", msg="[图片数据]"))
            elif mark == "error":
                error_occurred = True
                error_message = self.delete_color_control_char(out_str)
                error_message = self._truncate_text(error_message)
                logger.error(f"执行错误: {error_message}")
                text_to_gpt.append(error_message)
                try:
                    self.notebook_serializer.add_code_cell_error_to_notebook(out_str)
                except Exception:
                    logger.exception("记录错误到 notebook 失败")
                content_to_display.append(StdErrModel(msg=out_str))

        combined_text = "\n".join(text_to_gpt)
        await self._push_to_websocket(content_to_display)
        return combined_text, error_occurred, error_message

    # 2.6 内部执行：发送代码给 kernel，收集 iopub 消息并规整为 (mark, output) 列表
    def execute_code_(self, code) -> list[tuple[str, str]]:
        msg_id = self.kc.execute(code)
        logger.info("执行代码（送入 kernel）")
        logger.info(f"执行内容（截断显示）: {code[:10000]}")
        msg_list = []
        while True:
            try:
                iopub_msg = self.kc.get_iopub_msg(timeout=1)
                msg_list.append(iopub_msg)
                if iopub_msg["msg_type"] == "status" and iopub_msg["content"].get("execution_state") == "idle":
                    break
            except Exception:
                if self.interrupt_signal:
                    try:
                        self.km.interrupt_kernel()
                    except Exception:
                        logger.exception("中断内核时发生错误")
                    self.interrupt_signal = False
                continue

        all_output: list[tuple[str, str]] = []
        for iopub_msg in msg_list:
            try:
                msg_type = iopub_msg.get("msg_type")
                if msg_type == "stream":
                    if iopub_msg["content"].get("name") == "stdout":
                        all_output.append(("stdout", iopub_msg["content"]["text"]))
                elif msg_type == "execute_result":
                    data = iopub_msg["content"].get("data", {})
                    if "text/plain" in data:
                        all_output.append(("execute_result_text", data["text/plain"]))
                    if "text/html" in data:
                        all_output.append(("execute_result_html", data["text/html"]))
                    if "image/png" in data:
                        all_output.append(("execute_result_png", data["image/png"]))
                    if "image/jpeg" in data:
                        all_output.append(("execute_result_jpeg", data["image/jpeg"]))
                elif msg_type == "display_data":
                    data = iopub_msg["content"].get("data", {})
                    if "text/plain" in data:
                        all_output.append(("display_text", data["text/plain"]))
                    if "text/html" in data:
                        all_output.append(("display_html", data["text/html"]))
                    if "image/png" in data:
                        all_output.append(("display_png", data["image/png"]))
                    if "image/jpeg" in data:
                        all_output.append(("display_jpeg", data["image/jpeg"]))
                elif msg_type == "error":
                    if "traceback" in iopub_msg["content"]:
                        output = "\n".join(iopub_msg["content"]["traceback"])
                        cleaned_output = self.delete_color_control_char(output)
                        all_output.append(("error", cleaned_output))
            except Exception:
                logger.exception("解析 iopub_msg 时发生异常，跳过该消息")
        return all_output

    # 2.7 获取新创建的图片文件列表（递归扫描工作目录）
    async def get_created_images(self, section: str) -> list[str]:
        current_images = set()
        for root, _, files in os.walk(self.work_dir):
            for file in files:
                if file.lower().endswith((".png", ".jpg", ".jpeg")):
                    rel = os.path.relpath(os.path.join(root, file), self.work_dir)
                    current_images.add(rel)

        new_images = current_images - self.last_created_images
        self.last_created_images = current_images
        logger.info(f"新创建的图片列表: {new_images}")
        return sorted(list(new_images))

    # 2.8 清理：关闭 kernel client 与 kernel manager
    async def cleanup(self):
        if self.kc:
            try:
                self.kc.shutdown()
            except Exception:
                pass
        if self.km:
            logger.info("关闭内核")
            try:
                self.km.shutdown_kernel()
            except Exception:
                pass

    # 2.9 发送中断信号（将在下一次执行循环时触发内核中断）
    def send_interrupt_signal(self):
        self.interrupt_signal = True

    # 2.10 重启内核并重新创建工作目录
    def restart_jupyter_kernel(self):
        try:
            if self.kc:
                self.kc.shutdown()
        except Exception:
            pass
        self._start_kernel_with_env()
        self.interrupt_signal = False
        self._create_work_dir()

    # 2.11 确保工作目录存在
    def _create_work_dir(self):
        os.makedirs(self.work_dir, exist_ok=True)
