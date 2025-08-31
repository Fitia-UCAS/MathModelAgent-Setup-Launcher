# interpreter_factory.py
from typing import Literal
from app.tools.e2b_interpreter import E2BCodeInterpreter
from app.tools.local_interpreter import LocalCodeInterpreter
from app.utils.notebook_serializer import NotebookSerializer
from app.config.setting import settings
from app.utils.log_util import logger


async def create_interpreter(
    kind: Literal["remote", "local"] = "local",
    *,
    task_id: str,
    work_dir: str,
    notebook_serializer: NotebookSerializer,
    timeout=3000,
):
    if not settings.E2B_API_KEY:
        logger.info("默认使用本地解释器")
        kind = "local"
    else:
        logger.info("使用远程解释器")
        kind = "remote"

    if kind == "remote":
        interp: E2BCodeInterpreter = await E2BCodeInterpreter.create(
            task_id=task_id,
            work_dir=work_dir,
            notebook_serializer=notebook_serializer,
        )
        await interp.initialize(timeout=timeout)
        logger.info(f"工厂正在返回一个类型为: {type(interp)} 的实例")
        return interp
    elif kind == "local":
        interp: LocalCodeInterpreter = LocalCodeInterpreter(
            task_id=task_id,
            work_dir=work_dir,
            notebook_serializer=notebook_serializer,
        )
        await interp.initialize()
        logger.info(f"工厂正在返回一个类型为: {type(interp)} 的实例")
        return interp
    else:
        raise ValueError(f"未知 interpreter 类型：{kind}")
