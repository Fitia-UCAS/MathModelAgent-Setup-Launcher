# main.py

# 1. 标准库与三方库导入
from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
import os

# 2. 项目内模块导入
from app.routers import modeling_router, ws_router, common_router, files_router
from app.utils.log_util import logger
from app.config.setting import settings
from fastapi.staticfiles import StaticFiles
from app.utils.cli import get_ascii_banner, center_cli_str


# 3. 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(get_ascii_banner())
    print(center_cli_str("GitHub:https://github.com/jihe520/MathModelAgent"))
    logger.info("Starting MathModelAgent")

    PROJECT_FOLDER = "./project"
    os.makedirs(PROJECT_FOLDER, exist_ok=True)

    yield
    logger.info("Stopping MathModelAgent")


# 4. 应用实例
app = FastAPI(
    title="MathModelAgent",
    description="Agents for MathModel",
    version="0.1.0",
    lifespan=lifespan,
)

# 5. 路由注册
app.include_router(modeling_router.router)
app.include_router(ws_router.router)
app.include_router(common_router.router)
app.include_router(files_router.router)

# 6. 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],  # 暴露所有响应头
)

# 7. 静态文件挂载
app.mount(
    "/static",  # 访问前缀
    StaticFiles(directory="project/work_dir"),  # 本地目录
    name="static",
)
