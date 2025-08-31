import nbformat
from nbformat import v4 as nbf
import ansi2html
import os


class NotebookSerializer:
    def __init__(self, work_dir=None, notebook_name="notebook.ipynb"):
        self.nb = nbf.new_notebook()
        self.notebook_path = None
        self.initialized = True
        self.segmentation_output_content = {}
        self.current_segmentation: str = ""
        self.init_notebook(work_dir, notebook_name)

    def init_notebook(self, work_dir=None, notebook_name="notebook.ipynb"):
        if work_dir:
            base, ext = os.path.splitext(notebook_name)
            if ext.lower() != ".ipynb":
                notebook_name += ".ipynb"
            self.notebook_path = os.path.join(work_dir, notebook_name)

    def ansi_to_html(self, ansi_text):
        converter = ansi2html.Ansi2HTMLConverter()
        html_text = converter.convert(ansi_text)
        return html_text

    def write_to_notebook(self):
        if self.notebook_path:
            with open(self.notebook_path, "w", encoding="utf-8") as f:
                f.write(nbformat.writes(self.nb))

    def add_code_cell_to_notebook(self, code):
        code_cell = nbf.new_code_cell(source=code)
        self.nb["cells"].append(code_cell)
        self.write_to_notebook()

    def add_code_cell_output_to_notebook(self, output):
        html_content = self.ansi_to_html(output)
        if self.current_segmentation:
            if self.current_segmentation not in self.segmentation_output_content:
                self.segmentation_output_content[self.current_segmentation] = ""
            self.segmentation_output_content[self.current_segmentation] += html_content
        cell_output = nbf.new_output(output_type="display_data", data={"text/html": html_content})
        self.nb["cells"][-1]["outputs"].append(cell_output)
        self.write_to_notebook()

    def add_code_cell_error_to_notebook(self, error):
        nbf_error_output = nbf.new_output(
            output_type="error",
            ename="Error",
            evalue="Error message",
            traceback=[error],
        )
        self.nb["cells"][-1]["outputs"].append(nbf_error_output)
        self.write_to_notebook()

    def add_image_to_notebook(self, image, mime_type):
        image_output = nbf.new_output(output_type="display_data", data={mime_type: image})
        self.nb["cells"][-1]["outputs"].append(image_output)
        self.write_to_notebook()

    def add_markdown_to_notebook(self, content, title=None):
        if title:
            content = "##### " + title + ":\n" + content
        markdown_cell = nbf.new_markdown_cell(content)
        self.nb["cells"].append(markdown_cell)
        self.write_to_notebook()

    def add_markdown_segmentation_to_notebook(self, content, segmentation):
        self.current_segmentation = segmentation
        self.segmentation_output_content[segmentation] = ""
        self.add_markdown_to_notebook(content, segmentation)

    def get_notebook_output_content(self, segmentation):
        return self.segmentation_output_content[segmentation]
