# app/tools/notebook_serializer.py

# 1 导入依赖
import os
import nbformat
from nbformat import v4 as nbf
import ansi2html
from app.tools.text_sanitizer import TextSanitizer as TS


# 2 NotebookSerializer
# 2.1 目的：将代码、输出、错误、图片、Markdown 等执行过程写入 .ipynb 文件
class NotebookSerializer:
    def __init__(self, work_dir=None, notebook_name="notebook.ipynb"):
        self.nb = nbf.new_notebook()
        self.notebook_path = None
        self.initialized = True
        self.segmentation_output_content: dict[str, str] = {}
        self.current_segmentation: str = ""
        self.init_notebook(work_dir, notebook_name)

    # 2.2 初始化 notebook 文件路径
    def init_notebook(self, work_dir=None, notebook_name="notebook.ipynb"):
        if work_dir:
            base, ext = os.path.splitext(notebook_name)
            if ext.lower() != ".ipynb":
                notebook_name += ".ipynb"
            self.notebook_path = os.path.join(work_dir, notebook_name)
            parent = os.path.dirname(self.notebook_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

    # 2.3 ANSI 转 HTML（用于日志/终端输出美化）
    def ansi_to_html(self, ansi_text: str) -> str:
        converter = ansi2html.Ansi2HTMLConverter()
        return converter.convert(ansi_text or "")

    # 2.4 写入 notebook 文件
    def write_to_notebook(self):
        if self.notebook_path:
            with open(self.notebook_path, "w", encoding="utf-8") as f:
                f.write(nbformat.writes(self.nb))

    # 2.5 确保最后一个单元是 code cell
    def _ensure_last_code_cell(self):
        if not self.nb["cells"] or self.nb["cells"][-1].get("cell_type") != "code":
            placeholder = "# auto-created cell for outputs"
            code_clean = TS.normalize_for_execution(placeholder)
            code_cell = nbf.new_code_cell(source=code_clean)
            self.nb["cells"].append(code_cell)
            self.write_to_notebook()

    # 2.6 添加代码单元
    def add_code_cell_to_notebook(self, code: str):
        code_clean = TS.normalize_for_execution(code or "")
        if not code_clean.strip():
            code_clean = "# (empty)"
        code_cell = nbf.new_code_cell(source=code_clean)
        self.nb["cells"].append(code_cell)
        self.write_to_notebook()

    # 2.7 添加输出（HTML 化后存入）
    def add_code_cell_output_to_notebook(self, output: str):
        safe_text = TS.strip_ansi(TS.clean_control_chars(output or "", keep_whitespace=True))
        html_content = self.ansi_to_html(safe_text)

        if self.current_segmentation:
            self.segmentation_output_content.setdefault(self.current_segmentation, "")
            self.segmentation_output_content[self.current_segmentation] += html_content

        self._ensure_last_code_cell()
        cell_output = nbf.new_output(output_type="display_data", data={"text/html": html_content})
        self.nb["cells"][-1].setdefault("outputs", []).append(cell_output)
        self.write_to_notebook()

    # 2.8 添加错误输出
    def add_code_cell_error_to_notebook(self, error: str):
        safe_err = TS.strip_ansi(TS.clean_control_chars(error or "", keep_whitespace=True))
        self._ensure_last_code_cell()
        nbf_error_output = nbf.new_output(
            output_type="error",
            ename="ExecutionError",
            evalue=safe_err.splitlines()[0] if safe_err else "Error",
            traceback=[safe_err] if safe_err else [],
        )
        self.nb["cells"][-1].setdefault("outputs", []).append(nbf_error_output)
        self.write_to_notebook()

    # 2.9 添加图片输出（base64 编码）
    def add_image_to_notebook(self, image_b64: str, mime_type: str):
        self._ensure_last_code_cell()
        image_output = nbf.new_output(output_type="display_data", data={mime_type: image_b64})
        self.nb["cells"][-1].setdefault("outputs", []).append(image_output)
        self.write_to_notebook()

    # 2.10 添加 Markdown 单元
    def add_markdown_to_notebook(self, content: str, title: str | None = None):
        md = content or ""
        if title:
            md = "##### " + str(title) + ":\n" + md
        markdown_cell = nbf.new_markdown_cell(md)
        self.nb["cells"].append(markdown_cell)
        self.write_to_notebook()

    # 2.11 添加分段 Markdown 并初始化对应 HTML 缓存
    def add_markdown_segmentation_to_notebook(self, content: str, segmentation: str):
        self.current_segmentation = segmentation or ""
        if self.current_segmentation:
            self.segmentation_output_content[self.current_segmentation] = ""
        self.add_markdown_to_notebook(content, segmentation)

    # 2.12 获取指定分段的 HTML 输出
    def get_notebook_output_content(self, segmentation: str) -> str:
        return self.segmentation_output_content.get(segmentation or "", "")
