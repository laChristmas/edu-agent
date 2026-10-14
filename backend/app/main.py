import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
from app.tutor import tutor_messages
from app.config import get_settings
from app.db import LOCAL_USER_ID, create_pool, ensure_schema, pgvector_version
from app.extract import extract_turn
from app.memory import active_facts, confirm_facts, list_profile, teaching_facts
from app.mistakes import list_mistakes, record_turn
from app.search import prepare_mistake_lookup

logger = logging.getLogger("eduagent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动时建立数据库连接，退出时关闭。连接失败则进程起不来。"""
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    app.state.tasks = set()
    await ensure_schema(app.state.pool)
    # 读超时放宽，流式回复可能持续较久。
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=180.0))
    try:
        yield
    finally:
        await app.state.http.aclose()
        await app.state.pool.close()


app = FastAPI(title="EduAgent", lifespan=lifespan)


def _spawn(coro) -> None:
    """把回复后的抽取放到当前事件循环里，不占用正在返回的流。"""
    task = asyncio.create_task(coro)
    app.state.tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        app.state.tasks.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.error("后台任务失败", exc_info=done.exception())

    task.add_done_callback(_finished)
# 聊天页在另一个端口，只放开本机开发地址。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ProfileFactIn(BaseModel):
    dimension: str
    key: str
    value: str


class ProfileIn(BaseModel):
    facts: list[ProfileFactIn]


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


@app.get("/profile")
async def profile():
    """返回本地用户仍有效的画像，以及已经记下的错题。"""
    pool = app.state.pool
    return {
        "sections": await list_profile(pool, LOCAL_USER_ID),
        "mistakes": await list_mistakes(pool, LOCAL_USER_ID),
    }


@app.post("/profile")
async def save_profile(body: ProfileIn):
    """用户在界面里确认后写入。原话按 0.9 记，清空的项标成失效。"""
    try:
        await confirm_facts(
            app.state.pool,
            LOCAL_USER_ID,
            [(fact.dimension, fact.key, fact.value) for fact in body.facts],
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return {"sections": await list_profile(app.state.pool, LOCAL_USER_ID)}


@app.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: UUID):
    """刷新页面时按会话编号取回已落库的消息。家教提示不在这些消息里。"""
    await require_conversation(app.state.pool, conversation_id)
    return {
        "id": str(conversation_id),
        "messages": await load_messages(app.state.pool, conversation_id),
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
        user_message_id = None
        try:
            user_message_id = await save_message(pool, conversation_id, "user", body.content)
            yield sse({"type": "conversation", "id": str(conversation_id)})
            facts = await teaching_facts(pool, LOCAL_USER_ID)
            archive = await active_facts(pool, LOCAL_USER_ID)
            ask_grade = not any(fact["dimension"] == "身份" and fact["key"] == "年级" for fact in archive)
            mistake_note = None
            try:
                # 像在找错题时才检索。失败就照常讲，不挡住这一轮。
                mistake_note = await prepare_mistake_lookup(
                    pool,
                    app.state.http,
                    settings,
                    user_id=LOCAL_USER_ID,
                    conversation_id=conversation_id,
                    user_text=body.content,
                    source_message_id=user_message_id,
                )
            except Exception:
                logger.exception("错题检索失败")
            messages = tutor_messages(
                await load_messages(pool, conversation_id),
                facts,
                archive,
                ask_grade=ask_grade,
                mistake_note=mistake_note,
            )
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
                # 画像和错题都在回复落库之后另起任务，不占这条流式连接。
                if user_message_id is not None and parts:
                    _spawn(
                        extract_turn(
                            pool,
                            app.state.http,
                            settings,
                            user_id=LOCAL_USER_ID,
                            user_message_id=user_message_id,
                            user_text=body.content,
                            assistant_text="".join(parts),
                        )
                    )
                if user_message_id is not None:
                    _spawn(
                        record_turn(
                            pool,
                            app.state.http,
                            settings,
                            user_id=LOCAL_USER_ID,
                            conversation_id=conversation_id,
                            user_message_id=user_message_id,
                            user_text=body.content,
                            assistant_text="".join(parts),
                        )
                    )

    return StreamingResponse(events(), media_type="text/event-stream")
