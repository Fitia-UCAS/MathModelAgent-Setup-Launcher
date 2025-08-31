from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from mcm_coder.workflow import MathModelWorkFlow
from mcm_coder.utils.log_util import create_logger
from mcm_coder.utils.common_utils import create_task_id, create_work_dir
import os

router = APIRouter()


@router.post("/solve")
async def solve_problem(
    ques_file: UploadFile = File(...),
    data_files: list[UploadFile] = File(default=None),
) -> FileResponse:
    """处理数学建模问题并生成解决方案 Notebook。"""
    # 生成任务 ID 和工作目录
    task_id = create_task_id()
    work_dir = create_work_dir(task_id)

    # 初始化任务日志
    logger = create_logger(work_dir, "task")
    logger.info(f"生成任务 ID: {task_id}，工作目录: {work_dir}")

    # 保存题目文件
    try:
        ques_file_path = os.path.join(work_dir, ques_file.filename)
        logger.info(f"保存题目文件: {ques_file.filename} -> {ques_file_path}")
        if not ques_file.filename:
            logger.error("题目文件名为空")
            raise HTTPException(status_code=400, detail="题目文件名不能为空")
        content = await ques_file.read()
        if not content:
            logger.error("题目文件内容为空")
            raise HTTPException(status_code=400, detail="题目文件内容为空")
        with open(ques_file_path, "wb") as f:
            f.write(content)
        logger.info(f"成功保存题目文件: {ques_file_path}")
    except Exception as e:
        logger.error(f"保存题目文件失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"保存题目文件失败: {str(e)}")

    # 保存数据文件
    if data_files:
        logger.info(f"开始处理上传的数据文件，工作目录: {work_dir}")
        for file in data_files:
            try:
                data_file_path = os.path.join(work_dir, file.filename)
                logger.info(f"保存数据文件: {file.filename} -> {data_file_path}")
                if not file.filename:
                    logger.warning("跳过空文件名")
                    continue
                content = await file.read()
                if not content:
                    logger.warning(f"文件 {file.filename} 内容为空")
                    continue
                with open(data_file_path, "wb") as f:
                    f.write(content)
                logger.info(f"成功保存数据文件: {data_file_path}")
            except Exception as e:
                logger.error(f"保存数据文件 {file.filename} 失败: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"保存数据文件 {file.filename} 失败: {str(e)}"
                )
    else:
        logger.info("未提供数据文件，将假设题目文件中包含所有必要信息")

    # 读取题目内容
    try:
        with open(ques_file_path, "r", encoding="utf-8") as f:
            ques_all = f.read()
        logger.info(f"成功读取题目内容: {ques_file_path}")
    except Exception as e:
        logger.error(f"读取题目文件失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"读取题目文件失败: {str(e)}")

    # 执行代码生成，生成 Notebook
    logger.info(f"执行数学建模工作流，task_id: {task_id}")
    try:
        workflow = MathModelWorkFlow()
        notebook_path, _, _ = await workflow.execute(
            task_id, ques_all, work_dir, iteration=1, work_dir=work_dir
        )
        logger.info(f"任务 {task_id} 处理完成，Notebook 保存至: {notebook_path}")
        return FileResponse(
            notebook_path, media_type="application/x-ipynb+json", filename="solution.ipynb"
        )
    except Exception as e:
        logger.error(f"任务 {task_id} 处理失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"任务处理失败: {str(e)}")