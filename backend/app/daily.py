import logging
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import asyncpg

from app.mastery import local_today, review_moment

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


@dataclass(frozen=True)
class DueMistake:
    id: UUID
    stem: str
    error_type: str | None
    due_on: date


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
            id,
            stem,
            error_type,
            (next_review_at AT TIME ZONE 'Asia/Shanghai')::date AS due_on
        FROM mistakes
        WHERE user_id = $1
          AND next_review_at IS NOT NULL
          AND next_review_at < $2
        ORDER BY next_review_at, created_at, id
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
            lines.append(
                f"{index}. {_day_label(item.due_on)}到期，错因{_error_label(item.error_type)}，"
                f"{_first_sentence(item.stem)}"
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
