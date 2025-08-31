import os
import datetime
import hashlib
import tomllib
from app.utils.enums import CompTemplate
from app.utils.log_util import logger
import re
import pypandoc
from app.config.setting import settings
import shutil
from fastapi import UploadFile
from typing import List


async def create_task_fingerprint(ques_all: str, files: List[UploadFile]) -> str:
    """根据问题和文件内容创建唯一的任务指纹"""
    # 1. 对问题描述进行哈希
    ques_hash = hashlib.sha256(ques_all.encode()).hexdigest()

    # 2. 对每个文件内容进行哈希
    file_hashes = []
    for file in files:
        # 在读取文件内容之前，需要将文件指针移到开头
        await file.seek(0)
        content = await file.read()
        file_hashes.append(hashlib.sha256(content).hexdigest())
        # 读取后，再次将指针移到开头，以便后续操作可以重新读取文件
        await file.seek(0)

    # 3. 对文件哈希值进行排序，确保文件顺序不影响最终结果
    file_hashes.sort()

    # 4. 组合所有哈希值并生成最终的任务指纹
    combined_hash_str = ques_hash + "".join(file_hashes)
    fingerprint = hashlib.sha256(combined_hash_str.encode()).hexdigest()

    return fingerprint


def create_task_id() -> str:
    """生成任务ID"""
    # 生成时间戳和随机hash
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    random_hash = hashlib.md5(str(datetime.datetime.now()).encode()).hexdigest()[:8]
    return f"{timestamp}-{random_hash}"


def create_work_dir(task_id: str) -> str:
    # 设置主工作目录和子目录
    work_dir = os.path.join("project", "work_dir", task_id)

    try:
        # 创建目录，如果目录已存在也不会报错
        os.makedirs(work_dir, exist_ok=True)
        return work_dir
    except Exception as e:
        # 捕获并记录创建目录时的异常
        logger.error(f"创建工作目录失败: {str(e)}")
        raise


def get_work_dir(task_id: str) -> str:
    work_dir = os.path.join("project", "work_dir", task_id)
    if os.path.exists(work_dir):
        return work_dir
    else:
        logger.error(f"工作目录不存在: {work_dir}")
        raise FileNotFoundError(f"工作目录不存在: {work_dir}")


#  TODO: 是不是应该将 Prompt 写成一个 class
def get_config_template(comp_template: CompTemplate = CompTemplate.CHINA) -> dict:
    if comp_template == CompTemplate.CHINA:
        return load_toml(os.path.join("app", "config", "md_template.toml"))


def load_toml(path: str) -> dict:
    with open(path, "rb") as f:
        data = tomllib.load(f)
        if not isinstance(data, dict):
            logger.error(f"TOML文件 {path} 加载后不是一个有效的字典，返回空字典。")
            return {}
        return data


def load_markdown(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_current_files(folder_path: str, type: str = "all") -> list[str]:
    files = os.listdir(folder_path)
    if type == "all":
        return files

    elif type == "md":
        return [file for file in files if file.endswith(".md")]
    elif type == "ipynb":
        return [file for file in files if file.endswith(".ipynb")]
    elif type == "data":
        return [
            file for file in files if file.endswith(".xlsx") or file.endswith(".csv")
        ]
    elif type == "image":
        return [
            file for file in files if file.endswith(".png") or file.endswith(".jpg")
        ]


# 判断content是否包含图片 xx.png,对其处理为    ![filename](http://localhost:8000/static/20250428-200915-ebc154d4/filename.jpg)
def transform_link(task_id: str, content: str):
    content = re.sub(
        r"!\[(.*?)\]\((.*?\.(?:png|jpg|jpeg|gif|bmp|webp))\)",
        lambda match: f"![{match.group(1)}]({settings.SERVER_HOST}/static/{task_id}/{match.group(2)})",
        content,
    )
    return content


# TODO: fix 公式显示
def md_2_docx(task_id: str):
    work_dir = get_work_dir(task_id)
    md_path = os.path.join(work_dir, "res.md")
    docx_path = os.path.join(work_dir, "res.docx")

    extra_args = [
        "--resource-path",
        str(work_dir),
        "--mathml",  # MathML 格式公式
        "--standalone",
        # "--extract-media=" + str(md_dir / "generated_images")  # 按需启用
    ]

    pypandoc.convert_file(
        source_file=md_path,
        to="docx",
        outputfile=docx_path,
        format="markdown+tex_math_dollars",
        extra_args=extra_args,
    )
    print(f"转换完成: {docx_path}")
    logger.info(f"转换完成: {docx_path}")
