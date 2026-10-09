from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.chat import (
    TurnInProgress,
    begin_turn,
    create_conversation,
    end_turn,
    load_messages,
    require_conversation,
    save_message,
    sse,
    stream_tutor,
)
from app.config import get_settings
from app.db import create_pool, ensure_schema, pgvector_version


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动时建立数据库连接，退出时关闭。连接失败则进程起不来。"""
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    await ensure_schema(app.state.pool)
    # 读超时放宽，流式回复可能持续较久。
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=180.0))
    try:
        yield
    finally:
        await app.state.http.aclose()
        await app.state.pool.close()


app = FastAPI(title="EduAgent", lifespan=lifespan)


class ChatIn(BaseModel):
    content: str
    conversation_id: UUID | None = None

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("内容不能为空")
        return text


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


@app.post("/chat")
async def chat(body: ChatIn):
    """把用户消息落库后逐字流式返回。同一会话上一轮未结束时不开始下一轮。"""
    settings = app.state.settings
    if not settings.llm_api_key or not settings.llm_base_url:
        raise HTTPException(status_code=503, detail="模型尚未配置")

    pool = app.state.pool
    if body.conversation_id is None:
        conversation_id = await create_conversation(pool)
    else:
        conversation_id = body.conversation_id
        await require_conversation(pool, conversation_id)

    try:
        await begin_turn(conversation_id)
    except TurnInProgress:
        raise HTTPException(status_code=409, detail="上一轮尚未结束") from None

    async def events():
        parts: list[str] = []
        assistant_saved = False
        try:
            await save_message(pool, conversation_id, "user", body.content)
            messages = await load_messages(pool, conversation_id)
            yield sse({"type": "conversation", "id": str(conversation_id)})
            async for delta in stream_tutor(app.state.http, settings, messages):
                parts.append(delta)
                yield sse({"type": "delta", "text": delta})
            if parts:
                await save_message(pool, conversation_id, "assistant", "".join(parts))
                assistant_saved = True
            yield sse({"type": "done"})
        finally:
            # 客户端断开或模型中途失败时，仍留下已经生成的文字，并放开这一轮。
            try:
                if parts and not assistant_saved:
                    await save_message(pool, conversation_id, "assistant", "".join(parts))
            finally:
                await end_turn(conversation_id)

    return StreamingResponse(events(), media_type="text/event-stream")
