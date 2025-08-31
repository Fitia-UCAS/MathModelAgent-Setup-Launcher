from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from app.config.setting import settings
from app.core.workflow import MathModelWorkFlow
from app.utils.enums import CompTemplate, FormatOutPut
from app.utils.log_util import logger
from app.utils.redis_manager import redis_manager
from app.schemas.request import Problem
from app.schemas.response import SystemMessage
from app.utils.common_utils import (
    create_task_id,
    create_work_dir,
    get_config_template,
    get_current_files,
    create_task_fingerprint,
)
import os
import asyncio
import json
from datetime import datetime
from fastapi import HTTPException
from app.utils.common_utils import md_2_docx, get_work_dir
import subprocess
from icecream import ic
from pydantic import BaseModel
from app.utils.track import track_cost_callback
import shutil

router = APIRouter()

CACHE_DIR = "project/cache"
os.makedirs(CACHE_DIR, exist_ok=True)


@router.get("/")
async def root():
    return {"message": "Hello World"}


@router.get("/config")
async def config():
    return {
        "environment": settings.ENV,
        "deepseek_model": settings.DEEPSEEK_MODEL,
        "deepseek_base_url": settings.DEEPSEEK_BASE_URL,
        "max_chat_turns": settings.MAX_CHAT_TURNS,
        "max_retries": settings.MAX_RETRIES,
        "CORS_ALLOW_ORIGINS": settings.CORS_ALLOW_ORIGINS,
    }


class ExampleRequest(BaseModel):
    example_id: str
    source: str


@router.post("/example")
async def exampleModeling(
    example_request: ExampleRequest,
    background_tasks: BackgroundTasks,
):
    task_id = create_task_id()
    work_dir = create_work_dir(task_id)
    example_dir = os.path.join("app", "example", "example", example_request.source)
    ic(example_dir)
    with open(os.path.join(example_dir, "questions.txt"), "r", encoding="utf-8") as f:
        ques_all = f.read()

    current_files = get_current_files(example_dir, "data")
    for file in current_files:
        src_file = os.path.join(example_dir, file)
        dst_file = os.path.join(work_dir, file)
        with open(src_file, "rb") as src, open(dst_file, "wb") as dst:
            dst.write(src.read())

    # --- Start of State Management Logic for /example ---
    task_status = {
        "status": "processing",
        "current_step": "initializing",
        "completed_steps": [],
        "error_message": None,
        "last_updated": datetime.utcnow().isoformat(),
        "ques_all": ques_all,  # 保存原始问题
        "comp_template": CompTemplate.CHINA.value, # 示例任务默认使用CHINA模板
        "format_output": FormatOutPut.Markdown.value, # 示例任务默认使用Markdown格式
    }
    await redis_manager.set(f"status:{task_id}", json.dumps(task_status))
    # --- End of State Management Logic for /example ---

    logger.info(f"Adding background task for task_id: {task_id}")
    background_tasks.add_task(
        run_modeling_task_async,
        task_id,
        ques_all,
        CompTemplate.CHINA,
        FormatOutPut.Markdown,
        task_fingerprint=None,
        resume_from_step=None,
    )
    return {"task_id": task_id, "status": "processing"}


@router.post("/modeling")
async def modeling(
    background_tasks: BackgroundTasks,
    ques_all: str = Form(...),
    comp_template: CompTemplate = Form(...),
    format_output: FormatOutPut = Form(...),
    files: list[UploadFile] = File(default=None),
):
    task_fingerprint = await create_task_fingerprint(ques_all, files or [])
    cache_key = f"cache:task_result:{task_fingerprint}"
    cached_file_path = await redis_manager.get(cache_key)

    task_id = create_task_id()
    work_dir = create_work_dir(task_id)

    if cached_file_path and os.path.exists(cached_file_path):
        logger.info(f"Task-level cache hit for fingerprint: {task_fingerprint}")
        try:
            shutil.unpack_archive(cached_file_path, work_dir)
            logger.info(f"Successfully unpacked cache from {cached_file_path} to {work_dir}")
            return {"task_id": task_id, "status": "completed_from_cache"}
        except Exception as e:
            logger.error(f"Failed to unpack cache file {cached_file_path}: {e}. Proceeding with normal execution.")

    logger.info(f"Task-level cache miss for fingerprint: {task_fingerprint}. Starting new task.")

    if files:
        logger.info(f"开始处理上传的文件，工作目录: {work_dir}")
        for file in files:
            try:
                await file.seek(0)
                data_file_path = os.path.join(work_dir, file.filename)
                with open(data_file_path, "wb") as f:
                    f.write(await file.read())
                logger.info(f"成功保存文件: {data_file_path}")
            except Exception as e:
                logger.error(f"保存文件 {file.filename} 失败: {str(e)}")
                raise HTTPException(status_code=500, detail=f"保存文件 {file.filename} 失败: {str(e)}")

    task_status = {
        "status": "processing",
        "current_step": "initializing",
        "completed_steps": [],
        "error_message": None,
        "last_updated": datetime.utcnow().isoformat(),
        "ques_all": ques_all,
        "comp_template": comp_template.value,
        "format_output": format_output.value,
    }
    await redis_manager.set(f"status:{task_id}", json.dumps(task_status))

    logger.info(f"Adding background task for task_id: {task_id}")
    background_tasks.add_task(
        run_modeling_task_async,
        task_id,
        ques_all,
        comp_template,
        format_output,
        task_fingerprint,
        resume_from_step=None,
    )
    return {"task_id": task_id, "status": "processing"}


@router.post("/resume/{task_id}")
async def resume_modeling(task_id: str, background_tasks: BackgroundTasks):
    """恢复一个失败的任务"""
    status_key = f"status:{task_id}"
    status_str = await redis_manager.get(status_key)

    if not status_str:
        raise HTTPException(status_code=404, detail="Task not found")

    status = json.loads(status_str)

    if status.get("status") != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Task status is '{status.get('status')}'. Only failed tasks can be resumed.",
        )

    ques_all = status.get("ques_all")
    comp_template = CompTemplate(status.get("comp_template"))
    format_output = FormatOutPut(status.get("format_output"))
    resume_from_step = status.get("current_step")

    status["status"] = "resuming"
    status["error_message"] = None
    status["last_updated"] = datetime.utcnow().isoformat()
    await redis_manager.set(status_key, json.dumps(status))

    logger.info(f"Resuming background task for task_id: {task_id} from step: {resume_from_step}")
    background_tasks.add_task(
        run_modeling_task_async,
        task_id,
        ques_all,
        comp_template,
        format_output,
        task_fingerprint=None,
        resume_from_step=resume_from_step,
    )

    return {"task_id": task_id, "status": "resuming"}


@router.get("/writer_seque")
async def get_writer_seque():
    config_template: dict = get_config_template(CompTemplate.CHINA)
    return list(config_template.keys())


@router.get("/open_folder")
async def open_folder(task_id: str):
    work_dir = get_work_dir(task_id)
    if os.name == "nt":
        subprocess.run(["explorer", work_dir])
    elif os.name == "posix":
        subprocess.run(["open", work_dir])
    else:
        raise HTTPException(status_code=500, detail=f"不支持的操作系统: {os.name}")
    return {"message": "打开工作目录成功", "work_dir": work_dir}


async def run_modeling_task_async(
    task_id: str,
    ques_all: str,
    comp_template: CompTemplate,
    format_output: FormatOutPut,
    task_fingerprint: str | None,
    resume_from_step: str | None,
):
    logger.info(f"run modeling task for task_id: {task_id}")

    try:
        problem = Problem(
            task_id=task_id,
            ques_all=ques_all,
            comp_template=comp_template,
            format_output=format_output,
        )

        status_key = f"status:{task_id}"
        if not resume_from_step:
            initial_status = json.loads(await redis_manager.get(status_key))
            initial_status.update({
                "status": "running",
                "current_step": "start_workflow",
                "last_updated": datetime.utcnow().isoformat(),
            })
            await redis_manager.set(status_key, json.dumps(initial_status))

        await redis_manager.publish_message(task_id, SystemMessage(content="任务开始处理"))
        await asyncio.sleep(1)

        await MathModelWorkFlow().execute(problem, resume_from_step)

        md_2_docx(task_id)

        if task_fingerprint:
            logger.info(f"Task successful. Creating cache for fingerprint: {task_fingerprint}")
            work_dir = get_work_dir(task_id)
            archive_path = os.path.join(CACHE_DIR, f"{task_fingerprint}")
            try:
                shutil.make_archive(archive_path, 'zip', work_dir)
                cache_key = f"cache:task_result:{task_fingerprint}"
                await redis_manager.set(cache_key, f"{archive_path}.zip", ex=86400)
                logger.info(f"Successfully created cache archive: {archive_path}.zip")
            except Exception as e:
                logger.error(f"Failed to create cache archive for task {task_id}: {e}")

        final_status = json.loads(await redis_manager.get(status_key))
        final_status.update({
            "status": "completed",
            "current_step": "finished",
            "last_updated": datetime.utcnow().isoformat(),
        })
        await redis_manager.set(status_key, json.dumps(final_status))

        await redis_manager.publish_message(task_id, SystemMessage(content="任务处理完成", type="success"))

    except Exception as e:
        logger.error(f"Task {task_id} failed: {e}")
        try:
            status_key = f"status:{task_id}"
            error_status = json.loads(await redis_manager.get(status_key))
            error_status.update({
                "status": "failed",
                "error_message": str(e),
                "last_updated": datetime.utcnow().isoformat(),
            })
            await redis_manager.set(status_key, json.dumps(error_status))
        except Exception as redis_e:
            logger.error(f"Failed to update Redis status for failed task {task_id}: {redis_e}")
        await redis_manager.publish_message(task_id, SystemMessage(content=f"任务处理失败: {e}", type="error"))


@router.get("/track")
async def track(task_id: str):
    pass
