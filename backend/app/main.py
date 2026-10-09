from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import create_pool, pgvector_version


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动时建立数据库连接，退出时关闭。连接失败则进程起不来。"""
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="EduAgent", lifespan=lifespan)


@app.get("/health")
async def health():
    """报告数据库、pgvector 和模型配置。只说明密钥是否已填，不回传密钥本身。"""
    settings = app.state.settings
    return {
        "status": "ok",
        "database": "ok",
        "pgvector": await pgvector_version(app.state.pool),
        "models": {
            "tutor": settings.tutor_model,
            "extractor": settings.extractor_model,
            "base_url": settings.llm_base_url,
            "api_key_configured": bool(settings.llm_api_key),
        },
    }
