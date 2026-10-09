import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import HTTPException

from app.config import Settings
from app.db import LOCAL_USER_ID

# 同一会话的进行中标记。一个 API 进程内即可，不跨进程加锁。
_active_turns: set[UUID] = set()
_turns_guard = asyncio.Lock()


class TurnInProgress(Exception):
    """上一轮流式回复还没结束。"""


async def begin_turn(conversation_id: UUID) -> None:
    async with _turns_guard:
        if conversation_id in _active_turns:
            raise TurnInProgress
        _active_turns.add(conversation_id)


async def end_turn(conversation_id: UUID) -> None:
    async with _turns_guard:
        _active_turns.discard(conversation_id)


async def create_conversation(pool: asyncpg.Pool) -> UUID:
    conversation_id = uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO conversations (id, user_id) VALUES ($1, $2)",
            conversation_id,
            LOCAL_USER_ID,
        )
    return conversation_id


async def require_conversation(pool: asyncpg.Pool, conversation_id: UUID) -> None:
    async with pool.acquire() as connection:
        exists = await connection.fetchval(
            """
            SELECT 1 FROM conversations
            WHERE id = $1 AND user_id = $2
            """,
            conversation_id,
            LOCAL_USER_ID,
        )
    if exists is None:
        raise HTTPException(status_code=404, detail="会话不存在")


async def save_message(
    pool: asyncpg.Pool,
    conversation_id: UUID,
    role: str,
    content: str,
) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO messages (id, conversation_id, role, content)
            VALUES ($1, $2, $3, $4)
            """,
            uuid4(),
            conversation_id,
            role,
            content,
        )


async def load_messages(pool: asyncpg.Pool, conversation_id: UUID) -> list[dict[str, str]]:
    """按时间读出本会话已落库的消息。截断最近 12 轮在送给模型时做。"""
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT role, content FROM messages
            WHERE conversation_id = $1
            ORDER BY created_at, id
            """,
            conversation_id,
        )
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def stream_tutor(
    client: httpx.AsyncClient,
    settings: Settings,
    messages: list[dict[str, str]],
) -> AsyncIterator[str]:
    """按 OpenAI 兼容接口逐段取出模型增量。流式期间不占着数据库事务。"""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    async with client.stream(
        "POST",
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.tutor_model,
            "messages": messages,
            "stream": True,
        },
    ) as response:
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", errors="replace")
            raise HTTPException(status_code=502, detail=detail[:500])
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            payload = json.loads(data)
            error = payload.get("error")
            if error:
                message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise HTTPException(status_code=502, detail=message[:500])
            # 有的实现会先发一条空 choices，或在末尾只带 usage。
            choices = payload.get("choices") or []
            if not choices:
                continue
            delta = (choices[0].get("delta") or {}).get("content")
            if delta:
                yield delta


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
