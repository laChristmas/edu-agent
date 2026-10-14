import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

_MASTERY = _RULES["mastery"]
INITIAL_SCORE = Decimal(str(_MASTERY["initial_score"]))
FORGOT_DROP = Decimal(str(_MASTERY["forgot_drop"]))
HARD_RAISE = Decimal(str(_MASTERY["hard_raise"]))
GOOD_RAISE = Decimal(str(_MASTERY["good_raise"]))
EASY_RAISE = Decimal(str(_MASTERY["easy_raise"]))
INITIAL_EASE = Decimal(str(_MASTERY["initial_ease"]))
MIN_EASE = Decimal(str(_MASTERY["min_ease"]))
SECOND_INTERVAL = int(_MASTERY["second_interval_days"])
EASY_BONUS = Decimal(str(_MASTERY["easy_bonus"]))
HARD_EXTRA = int(_MASTERY["hard_extra_days"])
FORGOT_INTERVAL = int(_MASTERY["forgot_interval_days"])
QUALITY = {str(key): int(value) for key, value in _MASTERY["quality"].items()}
REVIEW_STATUS = {str(key): str(value) for key, value in _MASTERY["review_status"].items()}
_SCORE_DELTA = {
    "又忘了": -FORGOT_DROP,
    "勉强会": HARD_RAISE,
    "会了": GOOD_RAISE,
    "很熟": EASY_RAISE,
}
_TIMEZONE = str(_RULES["profile"]["default_timezone"])


@dataclass(frozen=True)
class ReviewUpdate:
    score: Decimal
    interval_days: int
    ease: Decimal
    next_review_on: date
    status: str


def apply_review(
    *,
    score: Decimal | None,
    interval_days: int | None,
    ease: Decimal | None,
    outcome: str,
    today: date,
) -> ReviewUpdate:
    """四种结果给出不同的下次间隔。会了和很熟用同一套 SM-2，很熟再乘一档。"""
    if outcome not in QUALITY:
        raise ValueError("未知的复习结果")
    base = INITIAL_SCORE if score is None else Decimal(score)
    updated_score = _clamp_score(base + _SCORE_DELTA[outcome])
    previous_ease = INITIAL_EASE if ease is None else Decimal(ease)
    current = 1 if interval_days is None else int(interval_days)
    interval, updated_ease = _next_interval(current, previous_ease, outcome)
    return ReviewUpdate(
        score=updated_score,
        interval_days=interval,
        ease=updated_ease,
        next_review_on=today + timedelta(days=interval),
        status=REVIEW_STATUS[outcome],
    )


def local_today() -> date:
    return datetime.now(_zone()).date()


def review_moment(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_zone())


async def apply_review_rows(
    connection: asyncpg.Connection,
    *,
    user_id: UUID,
    mistake_id: UUID,
    outcome: str,
    today: date,
) -> ReviewUpdate | None:
    """按这道错题挂着的知识点更新掌握度。没有知识点时仍给出间隔，供错题的下次日期使用。"""
    rows = await connection.fetch(
        """
        SELECT k.id AS point_id, k.name, ma.id AS mastery_id, ma.score, ma.interval_days, ma.ease
        FROM mistake_points mp
        JOIN knowledge_points k
            ON k.id = mp.knowledge_point_id AND k.user_id = mp.user_id
        LEFT JOIN mastery ma
            ON ma.knowledge_point_id = k.id AND ma.user_id = mp.user_id
        WHERE mp.mistake_id = $1 AND mp.user_id = $2
        ORDER BY k.name
        """,
        mistake_id,
        user_id,
    )
    now = datetime.now(_zone())
    chosen: ReviewUpdate | None = None
    if not rows:
        return apply_review(
            score=None,
            interval_days=1,
            ease=None,
            outcome=outcome,
            today=today,
        )
    for row in rows:
        update = apply_review(
            score=row["score"],
            interval_days=row["interval_days"],
            ease=row["ease"],
            outcome=outcome,
            today=today,
        )
        if chosen is None or update.next_review_on < chosen.next_review_on:
            chosen = update
        if row["mastery_id"] is None:
            await connection.execute(
                """
                INSERT INTO mastery (
                    id, user_id, knowledge_point_id, score, interval_days, ease,
                    next_review_on, evidence_count, last_evidence_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, $8)
                """,
                uuid4(),
                user_id,
                row["point_id"],
                update.score,
                update.interval_days,
                update.ease,
                update.next_review_on,
                now,
            )
            continue
        await connection.execute(
            """
            UPDATE mastery
            SET score = $2,
                interval_days = $3,
                ease = $4,
                next_review_on = $5,
                evidence_count = evidence_count + 1,
                last_evidence_at = $6,
                updated_at = $6
            WHERE id = $1
            """,
            row["mastery_id"],
            update.score,
            update.interval_days,
            update.ease,
            update.next_review_on,
            now,
        )
    return chosen


def _next_interval(current: int, ease: Decimal, outcome: str) -> tuple[int, Decimal]:
    # 先用旧的难易系数决定这一次的间隔，再按答题质量更新系数。
    updated_ease = _next_ease(ease, QUALITY[outcome])
    if outcome == "又忘了":
        return FORGOT_INTERVAL, updated_ease
    if outcome == "勉强会":
        return max(1, current + HARD_EXTRA), updated_ease
    interval = SECOND_INTERVAL if current <= 1 else _round_days(Decimal(current) * ease)
    if outcome == "很熟":
        interval = _round_days(Decimal(interval) * EASY_BONUS)
    return max(1, interval), updated_ease


def _next_ease(ease: Decimal, quality: int) -> Decimal:
    gap = Decimal(5 - quality)
    delta = Decimal("0.1") - gap * (Decimal("0.08") + gap * Decimal("0.02"))
    updated = ease + delta
    if updated < MIN_EASE:
        updated = MIN_EASE
    return updated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _clamp_score(score: Decimal) -> Decimal:
    if score < 0:
        score = Decimal("0")
    if score > 1:
        score = Decimal("1")
    return score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_days(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _zone():
    # 上海不实行夏令时，固定东八区，不必再装时区数据。
    if _TIMEZONE == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    return ZoneInfo(_TIMEZONE)
