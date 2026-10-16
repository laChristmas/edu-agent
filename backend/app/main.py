import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator, model_validator

from app.check import consider_revision, mark_revision, open_check, preview_reason, run_check
from app.chat import (
    TurnInProgress,
    append_message,
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
from app.daily import open_briefing
from app.db import LOCAL_USER_ID, create_pool, ensure_schema, pgvector_version
from app.extract import extract_turn
from app.jobs import prepare_weekly, reclaim_weekly_plans, serve_weekly_plans
from app.images import (
    PHOTO_AUTO_NOTE,
    PHOTO_GRADE,
    PHOTO_MISTAKE_NOTE,
    PHOTO_OTHER,
    PHOTO_PROBLEM,
    PHOTO_STEPS,
    PHOTO_UNREAD,
    PHOTO_WITH_TEXT,
    attach_image,
    open_image,
    read_image_bytes,
    require_pending_image,
    save_image,
)
from app.memory import active_facts, confirm_facts, list_profile, teaching_facts
from app.mistakes import PhotoReading, list_mistakes, read_photo, record_turn
from app.search import prepare_mistake_lookup
from app.web import prepare_web_lookup

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
    await reclaim_weekly_plans(app.state.pool)
    stop_jobs = asyncio.Event()
    job_worker = asyncio.create_task(
        serve_weekly_plans(app.state.pool, app.state.http, settings, stop_jobs)
    )
    try:
        yield
    finally:
        stop_jobs.set()
        job_worker.cancel()
        try:
            await job_worker
        except asyncio.CancelledError:
            pass
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


def _photo_fields(reading: PhotoReading) -> str:
    return (
        f"题干：{reading.stem or '（无）'}\n"
        f"解题过程：{reading.steps or '（无）'}\n"
        f"解析：{reading.description or '（无）'}"
    )


def _photo_note(reading: PhotoReading | None, *, photo_only: bool) -> str:
    """开口前带上照片解析。只发照片时按种类回复，说了记下才按错题讲。"""
    if reading is None:
        return PHOTO_UNREAD
    parsed = reading.mistake
    if parsed is not None:
        points = "、".join(parsed.knowledge_points) or "（无）"
        lead = PHOTO_AUTO_NOTE if reading.auto else PHOTO_MISTAKE_NOTE
        return (
            f"{lead}\n"
            f"题干：{parsed.stem}\n"
            f"学生答案：{parsed.student_answer or '（无）'}\n"
            f"参考答案：{parsed.reference_answer or '（无）'}\n"
            f"错因：{parsed.error_type or '（无）'}\n"
            f"学科：{parsed.subject or '（无）'}\n"
            f"知识点：{points}\n"
            f"{_photo_fields(reading)}"
        )
    lead = {
        "题目": PHOTO_PROBLEM,
        "题目和解题过程": PHOTO_GRADE,
        "解题过程": PHOTO_STEPS,
        "其他": PHOTO_OTHER,
    } if photo_only else {}
    return f"{lead.get(reading.kind, PHOTO_WITH_TEXT)}\n{_photo_fields(reading)}"


def _with_photo(messages: list[dict], media_type: str, data: bytes) -> list[dict]:
    """解析之后仍把原图交给家教，避免整理时丢掉图上的细节。"""
    if not messages or messages[-1].get("role") != "user":
        return messages
    text = messages[-1].get("content")
    if not isinstance(text, str):
        return messages
    encoded = base64.b64encode(data).decode("ascii")
    updated = list(messages)
    updated[-1] = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
            {"type": "text", "text": text},
        ],
    }
    return updated


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


class BriefingIn(BaseModel):
    conversation_id: UUID | None = None


class ChatIn(BaseModel):
    content: str = ""
    conversation_id: UUID | None = None
    image_id: UUID | None = None

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def content_or_image(self) -> "ChatIn":
        # 只发照片时没有文字。占位留在这句话里。题干只在记成错题时另写，不把照片换成题目。
        if not self.content and self.image_id is None:
            raise ValueError("内容不能为空")
        if not self.content:
            self.content = "（一张照片）"
        return self


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


@app.post("/briefing")
async def briefing(body: BriefingIn):
    """打开对话前写一条简报。选题为空就不发，还没回复的上一条会被换掉。"""
    settings = app.state.settings
    if not settings.llm_api_key or not settings.llm_base_url:
        raise HTTPException(status_code=503, detail="模型尚未配置")
    opened = await open_briefing(
        app.state.pool,
        app.state.http,
        settings,
        user_id=LOCAL_USER_ID,
        conversation_id=body.conversation_id,
    )
    return {
        "id": None if opened.conversation_id is None else str(opened.conversation_id),
        "messages": opened.messages,
    }


@app.post("/images")
async def upload_image(
    file: UploadFile = File(),
    conversation_id: UUID | None = Form(None),
):
    """收下照片并挂到用户和会话。是不是错题要等这句发出去再看。"""
    data = await file.read()
    image_id, saved_conversation, media_type = await save_image(
        app.state.pool,
        conversation_id=conversation_id,
        data=data,
    )
    return {
        "id": str(image_id),
        "conversation_id": str(saved_conversation),
        "media_type": media_type,
    }


@app.get("/images/{image_id}")
async def read_image(image_id: UUID):
    """按编号取回这个用户的照片。"""
    path, media_type = await open_image(app.state.pool, image_id)
    return FileResponse(path, media_type=media_type)


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
    if body.image_id is not None:
        await require_pending_image(pool, body.image_id, conversation_id)

    try:
        await begin_turn(conversation_id)
    except TurnInProgress:
        raise HTTPException(status_code=409, detail="上一轮尚未结束") from None

    async def events():
        parts: list[str] = []
        assistant_saved = False
        handed = False
        user_message_id = None
        assistant_message_id = None
        decide_task: asyncio.Task[str | None] | None = None
        # 交接一旦排上，就不再依赖这条流。页面中途断开时，意见仍然要留下。
        handoff_task: asyncio.Task[str | None] | None = None
        photo = None
        photo_reading: PhotoReading | None = None

        async def _handoff() -> str | None:
            """讲解落库后把意见交回家教。同意改口就先写入回复，连接还在时再推给页面。"""
            if decide_task is None or assistant_message_id is None or user_message_id is None:
                return None
            reason = await decide_task
            if reason is None:
                logger.warning("解答复核跳过")
                return None
            opinion = await open_check(
                pool,
                app.state.http,
                settings,
                reason=reason,
                user_id=LOCAL_USER_ID,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                user_text=body.content,
                assistant_text="".join(parts),
            )
            if opinion is None:
                return None
            spoken = await consider_revision(app.state.http, settings, opinion)
            if not spoken:
                return None
            extra = f"\n\n{spoken}"
            await append_message(pool, assistant_message_id, extra)
            await mark_revision(pool, opinion.check_id, spoken)
            return extra

        async def _finish_turn() -> None:
            nonlocal assistant_message_id, assistant_saved
            if parts and not assistant_saved:
                assistant_message_id = await save_message(pool, conversation_id, "assistant", "".join(parts))
                assistant_saved = True
            if (
                handoff_task is None
                and not handed
                and user_message_id is not None
                and assistant_message_id is not None
            ):
                # 讲解已经落库，但交接还没排上。先补做复核，避免断线把意见丢掉。
                _spawn(
                    run_check(
                        pool,
                        app.state.http,
                        settings,
                        user_id=LOCAL_USER_ID,
                        conversation_id=conversation_id,
                        user_message_id=user_message_id,
                        assistant_message_id=assistant_message_id,
                        user_text=body.content,
                        assistant_text="".join(parts),
                    )
                )
            await end_turn(conversation_id)
            # 画像和错题在回复落库之后另起任务，不占这条流式连接。
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
                        saw_photo=body.image_id is not None,
                        photo_reading=photo_reading,
                    )
                )

        try:
            user_message_id = await save_message(pool, conversation_id, "user", body.content)
            job_note, job_queued = await prepare_weekly(
                pool,
                user_id=LOCAL_USER_ID,
                conversation_id=conversation_id,
                user_text=body.content,
            )
            # 要不要复核只看学生这句话，可以和后面的讲解一起进行。
            decide_task = asyncio.create_task(preview_reason(app.state.http, settings, body.content))
            if body.image_id is not None:
                await attach_image(pool, body.image_id, conversation_id, user_message_id)
                # 先解析照片内容。过程错了且题目适合当前年级时才记入错题。
                try:
                    photo = await read_image_bytes(pool, body.image_id)
                    archive_for_grade = await active_facts(pool, LOCAL_USER_ID)
                    grade = next(
                        (
                            fact["value"]
                            for fact in archive_for_grade
                            if fact["dimension"] == "身份" and fact["key"] == "年级" and fact["value"].strip()
                        ),
                        None,
                    )
                    photo_reading = await read_photo(
                        app.state.http,
                        settings,
                        pool,
                        conversation_id=conversation_id,
                        user_message_id=user_message_id,
                        user_text=body.content,
                        image=photo[0],
                        media_type=photo[1],
                        grade=grade,
                    )
                except Exception:
                    logger.exception("照片识题失败")
                    photo_reading = None
            yield sse({"type": "conversation", "id": str(conversation_id)})
            if job_queued:
                # 页面据此等后补的那一句。学生看不到这个事件。
                yield sse({"type": "later"})
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
            web_note = None
            try:
                # 会过时的事实才检索。检索失败时家教说明不确定。判断本身失败才照常讲，不挡住这一轮。
                web_note = await prepare_web_lookup(
                    app.state.http,
                    settings,
                    user_text=body.content,
                )
            except Exception:
                logger.exception("网页检索失败")
            if body.image_id is not None:
                photo_note = _photo_note(photo_reading, photo_only=body.content == "（一张照片）")
                mistake_note = f"{mistake_note}\n\n{photo_note}" if mistake_note else photo_note
            messages = tutor_messages(
                await load_messages(pool, conversation_id),
                facts,
                archive,
                ask_grade=ask_grade,
                mistake_note=mistake_note,
                web_note=web_note,
                job_note=job_note,
            )
            if photo is not None and photo_reading is not None:
                messages = _with_photo(messages, photo[1], photo[0])
            async for delta in stream_tutor(app.state.http, settings, messages):
                parts.append(delta)
                yield sse({"type": "delta", "text": delta})
            if parts:
                assistant_message_id = await save_message(pool, conversation_id, "assistant", "".join(parts))
                assistant_saved = True
                if user_message_id is not None and decide_task is not None:
                    # 先把交接排出这条流。学生那边断开时，取消的是等待，不是复核本身。
                    handoff_task = asyncio.create_task(_handoff())
                    app.state.tasks.add(handoff_task)
                    handoff_task.add_done_callback(app.state.tasks.discard)
                    try:
                        revision = await asyncio.shield(handoff_task)
                        handed = True
                        if revision:
                            parts.append(revision)
                            yield sse({"type": "delta", "text": revision})
                    except Exception:
                        logger.exception("复核交回失败")
                        handed = True
            yield sse({"type": "done"})
        finally:
            if handoff_task is None and decide_task is not None and not decide_task.done():
                decide_task.cancel()
            # 页面可能在字收齐后就断开。收尾独立出去，取消这一轮时复核仍然排得上。
            tail = asyncio.create_task(_finish_turn())
            try:
                await asyncio.shield(tail)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    while current.uncancel():
                        pass
                await tail

    return StreamingResponse(events(), media_type="text/event-stream")
