# app/utils/common_utils.py

# 1 导入依赖
import os
import datetime
import hashlib
import tomllib
import re
import pypandoc
from pathlib import Path
from app.schemas.enums import CompTemplate
from app.utils.log_util import logger
from app.config.setting import settings
from icecream import ic

# 2 基础路径定义
# 2.1 工程根目录：以当前文件为基准，向上两级
BASE_DIR = Path(__file__).resolve().parents[2]
# 2.2 工作区基目录：<repo_root>/project/work_dir
WORK_BASE = BASE_DIR / "project" / "work_dir"
WORK_BASE.mkdir(parents=True, exist_ok=True)


# 3 工具函数
# 3.1 生成任务 ID（时间戳 + 随机 hash）
def create_task_id() -> str:
    """
    将 task_id 改为 001、002、003... 的三位顺序编号。
    逻辑：
      1) 扫描 WORK_BASE（通常为 backend/project/work_dir）下已有的子目录名
      2) 目录名为纯数字的视为有效，取最大值 + 1
      3) 起始值为 001
    """
    from pathlib import Path
    import re

    # WORK_BASE 在文件前面已定义：WORK_BASE = BASE_DIR / "project" / "work_dir"
    work_base: Path = WORK_BASE
    work_base.mkdir(parents=True, exist_ok=True)

    max_n = 0
    for p in work_base.iterdir():
        if p.is_dir():
            m = re.fullmatch(r"(\d{1,})", p.name)
            if m:
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except ValueError:
                    pass

    next_id = f"{max_n + 1:03d}"  # 三位补零
    return next_id


# 3.2 创建并返回任务工作目录
def create_work_dir(task_id: str) -> str:
    try:
        work_dir = WORK_BASE / task_id
        work_dir.mkdir(parents=True, exist_ok=True)
        return str(work_dir.resolve())
    except Exception as e:
        logger.error(f"创建工作目录失败: {str(e)}")
        raise


# 3.3 获取任务工作目录
def get_work_dir(task_id: str) -> str:
    work_dir = WORK_BASE / task_id
    if work_dir.exists():
        return str(work_dir.resolve())
    else:
        logger.error(f"工作目录不存在: {work_dir}")
        raise FileNotFoundError(f"工作目录不存在: {work_dir}")


# 3.4 获取配置模板
def get_config_template(comp_template: CompTemplate = CompTemplate.CHINA) -> dict:
    if comp_template == CompTemplate.CHINA:
        toml_path = BASE_DIR / "app" / "config" / "md_template.toml"
        return load_toml(str(toml_path))


# 3.5 加载 TOML 文件
def load_toml(path: str) -> dict:
    p = Path(path)
    with p.open("rb") as f:
        return tomllib.load(f)


# 3.6 加载 Markdown 文件
def load_markdown(path: str) -> str:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        return f.read()


# 3.7 列出目录下文件
def get_current_files(folder_path: str | Path, type: str = "all") -> list[str]:
    folder = Path(folder_path)
    if not folder.exists():
        logger.error(f"目录不存在: {folder}")
        raise FileNotFoundError(f"目录不存在: {folder}")

    files = [p.name for p in folder.iterdir() if p.is_file()]
    if type == "all":
        return files
    elif type == "md":
        return [file for file in files if file.endswith(".md")]
    elif type == "ipynb":
        return [file for file in files if file.endswith(".ipynb")]
    elif type == "data":
        return [file for file in files if file.endswith((".xlsx", ".csv"))]
    elif type == "image":
        return [file for file in files if file.endswith((".png", ".jpg", ".jpeg"))]
    else:
        return files


# 3.8 转换图片链接为静态 URL
def transform_link(task_id: str, content: str):
    content = re.sub(
        r"!\[(.*?)\]\((.*?\.(?:png|jpg|jpeg|gif|bmp|webp))\)",
        lambda match: f"![{match.group(1)}]({settings.SERVER_HOST}/static/{task_id}/{match.group(2)})",
        content,
    )
    return content


# 3.9 将 Markdown 转为 DOCX
def md_2_docx(task_id: str):
    work_dir = get_work_dir(task_id)
    md_path = Path(work_dir) / "res.md"
    docx_path = Path(work_dir) / "res.docx"

    if not md_path.exists():
        logger.error(f"要转换的 md 文件不存在: {md_path}")
        raise FileNotFoundError(f"要转换的 md 文件不存在: {md_path}")

    extra_args = [
        "--resource-path",
        str(work_dir),
        "--mathml",
        "--standalone",
    ]

    pypandoc.convert_file(
        source_file=str(md_path),
        to="docx",
        outputfile=str(docx_path),
        format="markdown+tex_math_dollars",
        extra_args=extra_args,
    )
    print(f"转换完成: {docx_path}")
    logger.info(f"转换完成: {docx_path}")


# 3.10 拆分正文与脚注
def split_footnotes(text: str) -> tuple[str, list[tuple[str, str]]]:
    main_text = re.sub(r"\n\[\^\d+\]:.*?(?=\n\[\^|\n\n|\Z)", "", text, flags=re.DOTALL).strip()
    footnotes = re.findall(r"\[\^(\d+)\]:\s*(.+?)(?=\n\[\^|\n\n|\Z)", text, re.DOTALL)
    logger.info(f"main_text:{main_text} \n footnotes:{footnotes}")
    return main_text, footnotes
