import logging
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx

from app.chat import create_conversation, load_messages, require_conversation
from app.config import Settings
from app.mastery import local_today, review_moment
from app.memory import teaching_facts

logger = logging.getLogger("eduagent.daily")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

_DAILY = _RULES["daily"]
DUE_LIMIT = int(_DAILY["due_limit"])
WEAK_LIMIT = int(_DAILY["weak_limit"])
WEAK_SCORE_BELOW = Decimal(str(_DAILY["weak_score_below"]))
WEAK_EVIDENCE_DAYS = int(_DAILY["weak_evidence_days"])
DUE_PHRASES = tuple(str(item) for item in _DAILY["due_phrases"])
DUE_PROMPT = str(_DAILY["due_prompt"]).strip()
BRIEFING_PROMPT = str(_DAILY["briefing_prompt"]).strip()


@dataclass(frozen=True)
class DueMistake:
    id: UUID
    stem: str
    error_type: str | None
    due_on: date
    knowledge_points: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeakPoint:
    name: str
    score: Decimal


@dataclass(frozen=True)
class DailySelection:
    """到期错题优先。不足上限时才补一个薄弱点。两边都空就跳过。"""

    mistakes: tuple[DueMistake, ...]
    weak_point: WeakPoint | None

    @property
    def is_empty(self) -> bool:
        return not self.mistakes and self.weak_point is None


def looks_like_due(text: str) -> bool:
    """问今天该复习什么才选题。点名某一题仍走原来的检索。"""
    return any(phrase in text for phrase in DUE_PHRASES)


async def prepare_due_note(pool: asyncpg.Pool, user_id: UUID, user_text: str) -> str | None:
    """问今天复习什么时，把选题交给家教。为空也要说不安排，避免另编题目。"""
    if not looks_like_due(user_text.strip()):
        return None
    selection = await select_daily(pool, user_id)
    logger.info(
        "今日选题：到期 %s 道，薄弱点 %s",
        len(selection.mistakes),
        selection.weak_point.name if selection.weak_point else "无",
    )
    return f"{DUE_PROMPT}\n{_body(selection)}"


@dataclass(frozen=True)
class OpenedDialog:
    conversation_id: UUID | None
    messages: list[dict[str, str]]


async def open_briefing(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    conversation_id: UUID | None,
) -> OpenedDialog:
    """打开对话前写简报。选题为空就不发，还没回复的上一条直接换掉。"""
    if conversation_id is not None:
        await require_conversation(pool, conversation_id)
    selection = await select_daily(pool, user_id)
    if selection.is_empty:
        if conversation_id is not None:
            await _drop_pending_briefing(pool, conversation_id)
            messages = await load_messages(pool, conversation_id)
        else:
            messages = []
        logger.info("打开前简报：选题为空，不发")
        return OpenedDialog(conversation_id, messages)
    try:
        text = await _compose_briefing(pool, client, settings, user_id, selection)
    except Exception:
        logger.exception("简报文案没有写成")
        messages = [] if conversation_id is None else await load_messages(pool, conversation_id)
        return OpenedDialog(conversation_id, messages)
    if conversation_id is None:
        conversation_id = await create_conversation(pool)
    replaced = await _store_briefing(pool, conversation_id, text)
    logger.info("打开前简报：%s", "替换未回复的一条" if replaced else "新写一条")
    return OpenedDialog(conversation_id, await load_messages(pool, conversation_id))


async def select_daily(
    pool: asyncpg.Pool,
    user_id: UUID,
    today: date | None = None,
) -> DailySelection:
    """最多取到期最早的若干道。到期不足时再补一个近期薄弱点。"""
    day = today or local_today()
    # 明天零点之前都算今天或更早，过期越久越靠前。
    due_before = review_moment(day + timedelta(days=1))
    due_count = await pool.fetchval(
        """
        SELECT count(*)::int
        FROM mistakes
        WHERE user_id = $1
          AND next_review_at IS NOT NULL
          AND next_review_at < $2
        """,
        user_id,
        due_before,
    )
    rows = await pool.fetch(
        """
        SELECT
            m.id,
            m.stem,
            m.error_type,
            (m.next_review_at AT TIME ZONE 'Asia/Shanghai')::date AS due_on,
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
          AND m.next_review_at IS NOT NULL
          AND m.next_review_at < $2
        GROUP BY m.id
        ORDER BY m.next_review_at, m.created_at, m.id
        LIMIT $3
        """,
        user_id,
        due_before,
        DUE_LIMIT,
    )
    mistakes = tuple(
        DueMistake(
            id=row["id"],
            stem=row["stem"] or "",
            error_type=row["error_type"],
            due_on=row["due_on"],
            knowledge_points=tuple(row["knowledge_points"]),
        )
        for row in rows
    )
    # 已经有够多到期错题时不再补。薄弱点避开这些到期题挂着的知识点。
    weak_point = None
    if due_count < DUE_LIMIT and WEAK_LIMIT > 0:
        weak_point = await _weak_point(pool, user_id, day, due_before)
    return DailySelection(mistakes=mistakes, weak_point=weak_point)


async def _weak_point(
    pool: asyncpg.Pool,
    user_id: UUID,
    today: date,
    due_before: datetime,
) -> WeakPoint | None:
    # 近 14 天含今天，从第 14 天的零点算起。
    evidence_since = review_moment(today - timedelta(days=WEAK_EVIDENCE_DAYS - 1))
    row = await pool.fetchrow(
        """
        SELECT k.name, ma.score
        FROM mastery ma
        JOIN knowledge_points k
            ON k.id = ma.knowledge_point_id AND k.user_id = ma.user_id
        WHERE ma.user_id = $1
          AND ma.score IS NOT NULL
          AND ma.score < $2
          AND ma.last_evidence_at IS NOT NULL
          AND ma.last_evidence_at >= $3
          AND NOT EXISTS (
                SELECT 1
                FROM mistake_points mp
                JOIN mistakes m
                    ON m.id = mp.mistake_id AND m.user_id = mp.user_id
                WHERE mp.knowledge_point_id = ma.knowledge_point_id
                  AND mp.user_id = ma.user_id
                  AND m.next_review_at IS NOT NULL
                  AND m.next_review_at < $4
          )
        ORDER BY ma.score, ma.last_evidence_at DESC, k.name
        LIMIT 1
        """,
        user_id,
        WEAK_SCORE_BELOW,
        evidence_since,
        due_before,
    )
    if row is None:
        return None
    return WeakPoint(name=row["name"], score=Decimal(row["score"]))


def _body(selection: DailySelection) -> str:
    if selection.is_empty:
        return "今天没有到期错题，也没有近期薄弱点。"
    lines = ["到期错题："]
    if selection.mistakes:
        for index, item in enumerate(selection.mistakes, start=1):
            points = "、".join(item.knowledge_points)
            point_label = f"，知识点{points}" if points else ""
            lines.append(
                f"{index}. {_day_label(item.due_on)}到期，错因{_error_label(item.error_type)}"
                f"{point_label}，{_first_sentence(item.stem)}"
            )
    else:
        lines.append("无")
    if selection.weak_point is None:
        lines.append("薄弱点：无")
    else:
        lines.append(f"薄弱点：{selection.weak_point.name}")
    return "\n".join(lines)


def _first_sentence(stem: str) -> str:
    text = stem.strip()
    if not text:
        return "（没有题干）"
    sentence = text.split("。", 1)[0].split("\n", 1)[0].strip() or text
    if len(sentence) > 80:
        return sentence[:80] + "…"
    return sentence


def _error_label(error_type: str | None) -> str:
    return error_type or "未记下"


def _day_label(day: date) -> str:
    return f"{day.month}月{day.day}日"


async def _compose_briefing(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    user_id: UUID,
    selection: DailySelection,
) -> str:
    """简报用抽取那一档模型。文案失败就不要写成一条长讲解。"""
    facts = await teaching_facts(pool, user_id)
    interests = [f"{fact['key']}={fact['value']}" for fact in facts if fact["dimension"] == "兴趣"]
    interest_line = "、".join(interests) if interests else "无"
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": BRIEFING_PROMPT},
                {"role": "user", "content": f"{_body(selection)}\n兴趣：{interest_line}"},
            ],
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"简报接口返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    text = str(content or "").strip()
    if not text:
        raise RuntimeError("简报是空的")
    return text


async def _store_briefing(pool: asyncpg.Pool, conversation_id: UUID, text: str) -> bool:
    """最后一条若仍是没被回复的简报，就改写它。已经接过话就另写一条。"""
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT id FROM conversations WHERE id = $1 FOR UPDATE",
                conversation_id,
            )
            last_id = await _pending_briefing_id(connection, conversation_id)
            if last_id is not None:
                await connection.execute(
                    "UPDATE messages SET content = $2 WHERE id = $1",
                    last_id,
                    text,
                )
                return True
            await connection.execute(
                """
                INSERT INTO messages (id, conversation_id, role, content, kind)
                VALUES ($1, $2, 'assistant', $3, 'briefing')
                """,
                uuid4(),
                conversation_id,
                text,
            )
    return False


async def _drop_pending_briefing(pool: asyncpg.Pool, conversation_id: UUID) -> None:
    """这次没有选题时，不把上一条还没回复的简报留在开场。"""
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT id FROM conversations WHERE id = $1 FOR UPDATE",
                conversation_id,
            )
            last_id = await _pending_briefing_id(connection, conversation_id)
            if last_id is None:
                return
            await connection.execute("DELETE FROM messages WHERE id = $1 AND kind = 'briefing'", last_id)


async def _pending_briefing_id(connection: asyncpg.Connection, conversation_id: UUID) -> UUID | None:
    row = await connection.fetchrow(
        """
        SELECT id, kind FROM messages
        WHERE conversation_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        conversation_id,
    )
    if row is None or row["kind"] != "briefing":
        return None
    return row["id"]
