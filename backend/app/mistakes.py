import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx

from app.config import Settings

logger = logging.getLogger("eduagent.mistakes")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

_MISTAKES = _RULES["mistakes"]
MISTAKE_PROMPT = str(_MISTAKES["mistake_prompt"]).strip()
ERROR_TYPE_LIST = [str(item) for item in _MISTAKES["error_types"]]
ERROR_TYPES = frozenset(ERROR_TYPE_LIST)
SOURCES = frozenset(str(item) for item in _MISTAKES["sources"])
DEFAULT_SOURCE = str(_MISTAKES["default_source"])


@dataclass(frozen=True)
class ParsedMistake:
    stem: str
    student_answer: str | None
    reference_answer: str | None
    error_type: str | None
    subject: str | None
    knowledge_points: tuple[str, ...]


async def parse_mistake(
    client: httpx.AsyncClient,
    settings: Settings,
    user_text: str,
    assistant_text: str = "",
    earlier: str = "",
) -> ParsedMistake | None:
    """抽出错题结构。普通提问返回空。写入由 save_mistake 负责。"""
    allowed = "、".join(ERROR_TYPE_LIST)
    prior = earlier.strip() or "（无）"
    teacher = assistant_text.strip() or "（无）"
    user_prompt = (
        f"允许的错因：{allowed}\n\n"
        f"先前对话：\n{prior}\n\n"
        f"学生：{user_text.strip()}\n"
        f"老师：{teacher}"
    )
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": MISTAKE_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"错题解析返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(_json_text(content))
    if not isinstance(payload, dict):
        return None
    return _normalize(payload.get("mistake"))


def _normalize(raw) -> ParsedMistake | None:
    """错因只收配置里的词。学生没给的答案留空，避免把老师后补的内容当成原答案。"""
    if not isinstance(raw, dict):
        return None
    stem = _clean(raw.get("stem"))
    if stem is None:
        return None
    error_type = _clean(raw.get("error_type"))
    if error_type not in ERROR_TYPES:
        error_type = None
    points: list[str] = []
    seen: set[str] = set()
    values = raw.get("knowledge_points")
    if isinstance(values, list):
        for value in values:
            name = _clean(value)
            if name is None or name in seen:
                continue
            seen.add(name)
            points.append(name)
    return ParsedMistake(
        stem=stem,
        student_answer=_clean(raw.get("student_answer")),
        reference_answer=_clean(raw.get("reference_answer")),
        error_type=error_type,
        subject=_clean(raw.get("subject")),
        knowledge_points=tuple(points),
    )


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.removesuffix("```").strip()
    return text


def mistake_source(user_text: str) -> str:
    """多行内容算粘贴，一句话算口述或讲完后的补记。两条都进入同一张错题表。"""
    lines = [line for line in user_text.splitlines() if line.strip()]
    source = "paste" if len(lines) > 1 else DEFAULT_SOURCE
    if source not in SOURCES:
        return DEFAULT_SOURCE
    return source


async def save_mistake(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    raw_text: str,
    parsed: ParsedMistake,
    source: str,
    source_message_id: UUID | None = None,
) -> UUID:
    """写入一道错题和它的知识点。向量和下次复习日留到后面再补。"""
    if source not in SOURCES:
        source = DEFAULT_SOURCE
    mistake_id = uuid4()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO mistakes (
                    id, user_id, raw_text, stem, student_answer, reference_answer,
                    error_type, subject, source, source_message_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                mistake_id,
                user_id,
                raw_text.strip(),
                parsed.stem,
                parsed.student_answer,
                parsed.reference_answer,
                parsed.error_type,
                parsed.subject,
                source,
                source_message_id,
            )
            for name in parsed.knowledge_points:
                point_id = await _ensure_point(connection, user_id, name)
                await connection.execute(
                    """
                    INSERT INTO mistake_points (mistake_id, knowledge_point_id, user_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    """,
                    mistake_id,
                    point_id,
                    user_id,
                )
    return mistake_id


async def record_turn(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    conversation_id: UUID,
    user_message_id: UUID,
    user_text: str,
    assistant_text: str,
) -> None:
    """回复落库后，把口述、粘贴和补记收成同一条写入。失败只记日志。"""
    try:
        earlier = await _earlier_text(pool, conversation_id, user_message_id)
        parsed = await parse_mistake(
            client,
            settings,
            user_text,
            assistant_text,
            earlier,
        )
        if parsed is None:
            return
        await save_mistake(
            pool,
            user_id=user_id,
            raw_text=user_text,
            parsed=parsed,
            source=mistake_source(user_text),
            source_message_id=user_message_id,
        )
    except Exception:
        logger.exception("错题录入失败")


async def _ensure_point(connection: asyncpg.Connection, user_id: UUID, name: str) -> UUID:
    point_id = await connection.fetchval(
        """
        INSERT INTO knowledge_points (id, user_id, name)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, name) DO NOTHING
        RETURNING id
        """,
        uuid4(),
        user_id,
        name,
    )
    if point_id is not None:
        return point_id
    return await connection.fetchval(
        "SELECT id FROM knowledge_points WHERE user_id = $1 AND name = $2",
        user_id,
        name,
    )


async def _earlier_text(pool: asyncpg.Pool, conversation_id: UUID, user_message_id: UUID) -> str:
    """补记时带上这一句之前的几轮，让题干能对上刚讲过的题。"""
    rows = await pool.fetch(
        """
        SELECT role, content FROM messages
        WHERE conversation_id = $1
          AND created_at < (SELECT created_at FROM messages WHERE id = $2)
        ORDER BY created_at DESC
        LIMIT 6
        """,
        conversation_id,
        user_message_id,
    )
    lines = []
    for row in reversed(rows):
        speaker = "学生" if row["role"] == "user" else "老师"
        text = str(row["content"]).strip()
        if len(text) > 500:
            text = text[:500]
        lines.append(f"{speaker}：{text}")
    return "\n".join(lines)


async def list_mistakes(pool: asyncpg.Pool, user_id: UUID) -> list[dict]:
    """读出这个用户的错题，带上知识点。这里只给页面查看，不改分数。"""
    rows = await pool.fetch(
        """
        SELECT
            m.id,
            m.raw_text,
            m.stem,
            m.student_answer,
            m.reference_answer,
            m.error_type,
            m.subject,
            m.status,
            m.source,
            m.created_at,
            COALESCE(
                array_agg(k.name ORDER BY k.name) FILTER (WHERE k.name IS NOT NULL),
                ARRAY[]::text[]
            ) AS knowledge_points
        FROM mistakes m
        LEFT JOIN mistake_points mp
            ON mp.mistake_id = m.id AND mp.user_id = m.user_id
        LEFT JOIN knowledge_points k
            ON k.id = mp.knowledge_point_id AND k.user_id = m.user_id
        WHERE m.user_id = $1
        GROUP BY m.id
        ORDER BY m.created_at DESC
        """,
        user_id,
    )
    return [
        {
            "id": str(row["id"]),
            "raw_text": row["raw_text"],
            "stem": row["stem"],
            "student_answer": row["student_answer"],
            "reference_answer": row["reference_answer"],
            "error_type": row["error_type"],
            "subject": row["subject"],
            "status": row["status"],
            "source": row["source"],
            "knowledge_points": list(row["knowledge_points"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
