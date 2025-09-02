# app/router/files_router.py

from fastapi import APIRouter
from app.utils.common_utils import get_current_files, get_work_dir
import os
import subprocess
from icecream import ic
from fastapi import HTTPException

# 1 路由初始化
# 1.1 创建模块级 APIRouter（由主应用 include_router 挂载）
router = APIRouter()


# 2 文件列表
# 2.1 GET /files ：返回指定 task 的工作目录下文件清单
@router.get("/files")
async def get_files(task_id: str):
    work_dir = get_work_dir(task_id)
    files = get_current_files(work_dir, "all")
    return {"files": files}


# 3 打开工作目录
# 3.1 GET /open_folder ：在宿主机上用系统文件管理器打开工作目录
@router.get("/open_folder")
async def open_folder(task_id: str):
    ic(task_id)  # 3.1.1 调试输出 task_id
    work_dir = get_work_dir(task_id)  # 3.1.2 解析工作目录

    # 3.1.3 按操作系统调用对应命令
    if os.name == "nt":  # Windows
        subprocess.run(["explorer", work_dir])
    elif os.name == "posix":  # macOS / Linux（此处默认 open；如需兼容 Linux 可改为 xdg-open）
        subprocess.run(["open", work_dir])
    else:
        raise HTTPException(status_code=500, detail=f"不支持的操作系统: {os.name}")

    # 3.1.4 返回结果
    return {"message": "打开工作目录成功", "work_dir": work_dir}
