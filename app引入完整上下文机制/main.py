from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
import os
from app.routers import modeling, ws
from app.utils.log_util import logger
from app.config.setting import settings
from fastapi.staticfiles import StaticFiles


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting MathModelAgent")

    PROJECT_FOLDER = "./project"
    os.makedirs(PROJECT_FOLDER, exist_ok=True)

    logger.info("CORS_ALLOW_ORIGINS:", settings.CORS_ALLOW_ORIGINS)

    yield
    logger.info("Stopping MathModelAgent")


app = FastAPI(
    title="MathModelAgent",
    description="Agents for MathModel",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(modeling.router)
app.include_router(ws.router)


# 跨域 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],  # 暴露所有响应头
)

app.mount(
    "/static",  # 这是访问时的前缀
    StaticFiles(directory="project/work_dir"),  # 这是本地文件夹路径
    name="static",
)
