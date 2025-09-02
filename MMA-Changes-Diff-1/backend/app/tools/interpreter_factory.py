# app/tools/interpreter_factory.py

# 1 目的：根据配置与请求创建并初始化合适的代码解释器（remote 或 local）
# 2 行为概述：优先根据 settings.E2B_API_KEY 决定默认类型；创建实例并调用 initialize
# 3 注意：返回的解释器实例须已完成 initialize，调用方可直接使用

from typing import Literal
from app.tools.e2b_interpreter import E2BCodeInterpreter
from app.tools.local_interpreter import LocalCodeInterpreter
from app.tools.notebook_serializer import NotebookSerializer
from app.config.setting import settings
from app.utils.log_util import logger


async def create_interpreter(
    kind: Literal["remote", "local"] = "local",
    *,
    task_id: str,
    work_dir: str,
    notebook_serializer: NotebookSerializer,
    timeout=36000,
):
    # 1.1 若未配置远程 API key，则强制使用本地解释器
    if not settings.E2B_API_KEY:
        logger.info("默认使用本地解释器")
        kind = "local"
    else:
        logger.info("使用远程解释器")
        kind = "remote"

    # 1.2 远程解释器：创建并初始化（可能抛出异常）
    if kind == "remote":
        interp: E2BCodeInterpreter = await E2BCodeInterpreter.create(
            task_id=task_id,
            work_dir=work_dir,
            notebook_serializer=notebook_serializer,
        )
        await interp.initialize(timeout=timeout)
        return interp

    # 1.3 本地解释器：构造并初始化
    elif kind == "local":
        interp: LocalCodeInterpreter = LocalCodeInterpreter(
            task_id=task_id,
            work_dir=work_dir,
            notebook_serializer=notebook_serializer,
        )
        await interp.initialize()
        return interp

    # 1.4 非法参数处理
    else:
        raise ValueError(f"未知 interpreter 类型：{kind}")
