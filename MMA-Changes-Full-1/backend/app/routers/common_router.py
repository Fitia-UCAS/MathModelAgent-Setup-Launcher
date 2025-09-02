# app/router/common_router.py

from fastapi import APIRouter
from app.config.setting import settings
from app.utils.common_utils import get_config_template
from app.schemas.enums import CompTemplate

# 1 路由初始化
# 1.1 创建模块级 APIRouter 实例（由主应用 include_router 挂载）
router = APIRouter()


# 2 基础与健康检查
# 2.1 GET / ：简单连通性验证
@router.get("/")
async def root():
    return {"message": "Hello World"}


# 3 配置查询
# 3.1 GET /config ：返回关键运行时配置（用于前端展示/调试）
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


# 4 写作顺序
# 4.1 GET /writer_seque ：返回论文章节顺序（按模板定义的键顺序）
@router.get("/writer_seque")
async def get_writer_seque():
    config_template: dict = get_config_template(CompTemplate.CHINA)
    return list(config_template.keys())


# 5 任务追踪
# 5.1 GET /track?task_id=... ：占位接口（预留：查询指定任务的 token/进度等）
@router.get("/track")
async def track(task_id: str):
    # TODO: 实现任务追踪逻辑（从存储/监控系统读取统计信息并返回）
    pass
