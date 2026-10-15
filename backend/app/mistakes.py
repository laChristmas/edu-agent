import base64
import json
import logging
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
import httpx

from app.config import Settings
from app.images import PHOTO_KINDS, VISION_PROMPT

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
EMBEDDING_DIMENSIONS = int(_MISTAKES["embedding_dimensions"])
_MASTERY = _RULES["mastery"]
# 第一次因错题出现时落到薄弱线以下。再错一次按 mistake_drop 下调，不低于 0。
INITIAL_SCORE = Decimal(str(_MASTERY["initial_score"]))
MISTAKE_DROP = Decimal(str(_MASTERY["mistake_drop"]))
_TIMEZONE = str(_RULES["profile"]["default_timezone"])


@dataclass(frozen=True)
class ParsedMistake:
    stem: str
    student_answer: str | None
    reference_answer: str | None
    error_type: str | None
    subject: str | None
    knowledge_points: tuple[str, ...]


@dataclass(frozen=True)
class PhotoReading:
    """看过照片后的内容。auto 为真表示因过程错误且题目适合年级而记下。"""

    kind: str
    stem: str | None
    steps: str | None
    description: str | None
    mistake: ParsedMistake | None
    auto: bool = False


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


async def read_photo(
    client: httpx.AsyncClient,
    settings: Settings,
    pool: asyncpg.Pool,
    *,
    conversation_id: UUID,
    user_message_id: UUID,
    user_text: str,
    image: bytes,
    media_type: str,
    grade: str | None = None,
) -> PhotoReading:
    """先解析照片里有什么。过程错了且题目适合当前年级时，才收成错题。"""
    allowed = "、".join(ERROR_TYPE_LIST)
    earlier = await _earlier_text(pool, conversation_id, user_message_id)
    prior = earlier.strip() or "（无）"
    user_prompt = (
        f"允许的错因：{allowed}\n"
        f"当前年级：{grade or '（无）'}\n\n"
        f"先前对话：\n{prior}\n\n"
        f"学生原话：{user_text.strip()}"
    )
    encoded = base64.b64encode(image).decode("ascii")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": VISION_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        },
                        {"type": "text", "text": user_prompt},
                    ],
                },
            ],
            # 只要结构，不要先写一段思考把 JSON 冲掉。
            "enable_thinking": False,
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"照片识题返回 {response.status_code}: {detail}")
    message = response.json()["choices"][0]["message"]
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("照片识题没有返回文字")
    payload = json.loads(_json_text(content))
    if not isinstance(payload, dict):
        return PhotoReading("其他", None, None, None, None, False)
    return _reading_from(payload, user_text=user_text, grade=grade)


_AUTO_KINDS = frozenset({"题目和解题过程", "解题过程"})
_RECORD_PHRASES = ("记下", "记进错题", "记入错题", "错题本")


def _asked_to_record(user_text: str) -> bool:
    text = user_text.strip()
    if not text or text == "（一张照片）":
        return False
    return any(phrase in text for phrase in _RECORD_PHRASES)


def _reading_from(payload: dict, *, user_text: str, grade: str | None) -> PhotoReading:
    """代码再核对一遍。模型把做对的或不适合年级的题收成错题时，这里丢掉。"""
    kind = _clean(payload.get("kind")) or "其他"
    if kind not in PHOTO_KINDS:
        kind = "其他"
    stem = _clean(payload.get("stem"))
    steps = _clean(payload.get("steps"))
    description = _clean(payload.get("description"))
    model_mistake = _normalize(payload.get("mistake"))
    # 学生明确要记下时，沿用原话里的错题，不再用年级挡掉。
    if _asked_to_record(user_text) and model_mistake is not None:
        return PhotoReading(kind, stem, steps, description, model_mistake, False)
    fits = payload.get("fits_grade") is True
    wrong = payload.get("work_correct") is False
    if kind in _AUTO_KINDS and grade and fits and wrong and (stem or (model_mistake and model_mistake.stem)):
        mistake = model_mistake
        if mistake is None and stem:
            mistake = ParsedMistake(stem, steps, None, None, None, ())
        if mistake is not None:
            return PhotoReading(kind, stem or mistake.stem, steps, description, mistake, True)
    return PhotoReading(kind, stem, steps, description, None, False)


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
    """先把错题和明天的复习日写上，并下调掌握度。题干向量在提交之后另补。"""
    if source not in SOURCES:
        source = DEFAULT_SOURCE
    mistake_id = uuid4()
    tomorrow = _local_today() + timedelta(days=1)
    reviewed_at = _review_at(tomorrow)
    now = datetime.now(reviewed_at.tzinfo)
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO mistakes (
                    id, user_id, raw_text, stem, student_answer, reference_answer,
                    error_type, subject, source, source_message_id, next_review_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                reviewed_at,
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
                await _lower_mastery(connection, user_id, point_id, tomorrow, now)
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
    saw_photo: bool = False,
    photo_reading: PhotoReading | None = None,
) -> None:
    """回复落库后，把口述、粘贴、补记和照片记错题收成同一条写入。失败只记日志。"""
    try:
        if saw_photo and photo_reading is not None:
            # 开口前已经看过。这里只落库，不再把照片又判成一道题。
            parsed = photo_reading.mistake
        else:
            # 没照片，或照片没读成。没读成时只看原话，不把照片本身当题目。
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
        raw_text = user_text
        if photo_reading is not None and photo_reading.auto:
            # 只发照片时原话是占位。错题里留下题目和过程，方便以后找回。
            raw_text = parsed.stem
            if photo_reading.steps:
                raw_text = f"{raw_text}\n{photo_reading.steps}"
        mistake_id = await save_mistake(
            pool,
            user_id=user_id,
            raw_text=raw_text,
            parsed=parsed,
            source=mistake_source(raw_text),
            source_message_id=user_message_id,
        )
    except Exception:
        logger.exception("错题录入失败")
        return
    # 向量失败时错题和明天的复习日已经留下。检索还没做，缺向量时以后用关键词。
    try:
        await fill_embedding(pool, client, settings, mistake_id, parsed.stem)
    except Exception:
        logger.exception("错题向量补写失败")


async def fill_embedding(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    mistake_id: UUID,
    stem: str,
) -> None:
    """补写题干向量。调用方应在错题行提交之后再调用，避免向量接口拖住入库。"""
    vector = await _embed(client, settings, stem)
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(f"题干向量长度是 {len(vector)}，配置是 {EMBEDDING_DIMENSIONS}")
    literal = "[" + ",".join(format(float(item), ".8f") for item in vector) + "]"
    await pool.execute(
        """
        UPDATE mistakes
        SET embedding = $1::vector
        WHERE id = $2 AND embedding IS NULL
        """,
        literal,
        mistake_id,
    )


async def _embed(client: httpx.AsyncClient, settings: Settings, text: str) -> list:
    url = settings.llm_base_url.rstrip("/") + "/embeddings"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.embedding_model,
            "input": text,
            "dimensions": EMBEDDING_DIMENSIONS,
            "encoding_format": "float",
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"向量接口返回 {response.status_code}: {detail}")
    data = response.json().get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("向量接口没有返回数组")
    embedding = data[0].get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise RuntimeError("向量接口没有返回数组")
    return embedding


def _zone():
    # 上海不实行夏令时，固定东八区，不必再装时区数据。
    if _TIMEZONE == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    return ZoneInfo(_TIMEZONE)


def _local_today() -> date:
    return datetime.now(_zone()).date()


def _review_at(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_zone())


async def _lower_mastery(
    connection: asyncpg.Connection,
    user_id: UUID,
    point_id: UUID,
    tomorrow: date,
    now: datetime,
) -> None:
    """新错题把分数下调，并把这个知识点的下次复习放到明天。"""
    await connection.execute(
        """
        INSERT INTO mastery (
            id, user_id, knowledge_point_id, score, interval_days,
            next_review_on, evidence_count, last_evidence_at, updated_at
        )
        VALUES ($1, $2, $3, $4, 1, $5, 1, $6, $6)
        ON CONFLICT (user_id, knowledge_point_id) DO UPDATE
        SET score = CASE
                WHEN mastery.score IS NULL THEN EXCLUDED.score
                ELSE GREATEST(0, mastery.score - $7)
            END,
            interval_days = 1,
            next_review_on = EXCLUDED.next_review_on,
            evidence_count = mastery.evidence_count + 1,
            last_evidence_at = EXCLUDED.last_evidence_at,
            updated_at = EXCLUDED.updated_at
        """,
        uuid4(),
        user_id,
        point_id,
        INITIAL_SCORE,
        tomorrow,
        now,
        MISTAKE_DROP,
    )


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
