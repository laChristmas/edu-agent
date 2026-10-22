import logging
import tomllib
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx

from app.config import Settings

logger = logging.getLogger("eduagent.summary")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _CONTEXT = tomllib.load(config_file)["context"]

COMPRESS_ROUNDS = int(_CONTEXT["compress_rounds"])
SUMMARY_PROMPT = str(_CONTEXT["summary_prompt"]).strip()
SUMMARY_NOTE = str(_CONTEXT["summary_note"]).strip()
# 模型被要求写 500 字。这里只挡住异常的长输出，避免下一轮提示被撑开。
_MAX_SUMMARY_CHARS = 2000


def next_fold(messages: list[dict], covered_id: UUID | None, size: int) -> list[dict] | None:
    """取出覆盖点之后最先满一轮数的完整对话。当前这句还没有回复时不放进去。"""
    if size < 1:
        return None
    start = 0
    if covered_id is not None:
        found = False
        for index, message in enumerate(messages):
            if message["id"] == covered_id:
                start = index + 1
                found = True
                break
        # 覆盖点对不上时不重压整段，避免把已经摘要过的原文再压一遍。
        if not found:
            return None
    tail = messages[start:]
    user_indexes = [index for index, message in enumerate(tail) if message["role"] == "user"]
    complete_ends: list[int] = []
    for number, index in enumerate(user_indexes):
        end = user_indexes[number + 1] if number + 1 < len(user_indexes) else len(tail)
        chunk = tail[index:end]
        if any(item["role"] == "assistant" and str(item["content"]).strip() for item in chunk):
            complete_ends.append(end)
    if len(complete_ends) < size:
        return None
    return tail[: complete_ends[size - 1]]


async def prompt_history(
    pool: asyncpg.Pool,
    conversation_id: UUID,
) -> tuple[str | None, list[dict[str, str]]]:
    """已压缩的部分换成摘要，还没满一轮数的原文照样附上。"""
    summary, covered_id, messages = await _load(pool, conversation_id)
    tail = _after(messages, covered_id)
    note = f"{SUMMARY_NOTE}\n{summary}" if summary else None
    return note, [{"role": item["role"], "content": item["content"]} for item in tail]


async def compress_context(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    conversation_id: UUID,
) -> None:
    """满十轮就压一次。再满十轮时，把旧摘要和这十轮合成新摘要。"""
    while True:
        summary, covered_id, messages = await _load(pool, conversation_id)
        fold = next_fold(messages, covered_id, COMPRESS_ROUNDS)
        if not fold:
            return
        text = await _summarize(client, settings, summary, fold)
        if not text:
            raise RuntimeError("对话摘要是空的")
        await _store(pool, conversation_id, text, fold[-1]["id"], covered_id)
        logger.warning("对话已压缩 %s 轮", COMPRESS_ROUNDS)


async def _load(
    pool: asyncpg.Pool,
    conversation_id: UUID,
) -> tuple[str | None, UUID | None, list[dict]]:
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT summary, summary_message_id FROM conversations
            WHERE id = $1
            """,
            conversation_id,
        )
        messages = await connection.fetch(
            """
            SELECT id, role, content FROM messages
            WHERE conversation_id = $1
            ORDER BY created_at, id
            """,
            conversation_id,
        )
    summary = str(row["summary"]).strip() if row and row["summary"] else None
    covered_id = row["summary_message_id"] if row else None
    loaded = [
        {"id": item["id"], "role": item["role"], "content": item["content"]}
        for item in messages
    ]
    return summary, covered_id, loaded


def _after(messages: list[dict], covered_id: UUID | None) -> list[dict]:
    if covered_id is None:
        return messages
    for index, message in enumerate(messages):
        if message["id"] == covered_id:
            return messages[index + 1 :]
    return messages


async def _store(
    pool: asyncpg.Pool,
    conversation_id: UUID,
    summary: str,
    message_id: UUID,
    previous_id: UUID | None,
) -> None:
    async with pool.acquire() as connection:
        async with connection.transaction():
            current = await connection.fetchval(
                """
                SELECT summary_message_id FROM conversations
                WHERE id = $1
                FOR UPDATE
                """,
                conversation_id,
            )
            # 另一轮已经压过更后面的消息时，不再用这份旧摘要盖回去。
            if current != previous_id:
                return
            await connection.execute(
                """
                UPDATE conversations
                SET summary = $2, summary_message_id = $3
                WHERE id = $1
                """,
                conversation_id,
                summary,
                message_id,
            )


async def _summarize(
    client: httpx.AsyncClient,
    settings: Settings,
    summary: str | None,
    fold: list[dict],
) -> str:
    earlier = summary or "（无）"
    lines = "\n".join(f"{_speaker(item['role'])}：{item['content']}" for item in fold)
    user_prompt = f"更早的摘要：\n{earlier}\n\n这十轮：\n{lines}"
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            # 摘要只要正文。思考过程会把要留下的原话挤掉。
            "enable_thinking": False,
            "messages": [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"对话摘要返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        return ""
    return content.strip()[:_MAX_SUMMARY_CHARS]


def _speaker(role: str) -> str:
    if role == "user":
        return "学生"
    if role == "assistant":
        return "老师"
    return role
