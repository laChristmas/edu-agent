import calendar
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"

with _CONFIG_PATH.open("rb") as config_file:
    _PROFILE = tomllib.load(config_file)["profile"]

# 五个维度都保留。身份和目标的键在配置里收死，偏好、兴趣、学情可以新建键。
DIMENSIONS = frozenset({"身份", "目标", "兴趣", "偏好", "学情"})
KEY_ORDER = {name: list(keys) for name, keys in _PROFILE["keys"].items()}
FIXED_KEYS = {name: frozenset(keys) for name, keys in KEY_ORDER.items()}
SECTION_ORDER = ("身份", "目标", "偏好", "兴趣", "学情")
MANUAL_CONFIDENCE = 0.9
MIN_CONFIDENCE = Decimal(str(_PROFILE["min_confidence"]))
SNAPSHOT_PER_DIMENSION = int(_PROFILE["snapshot_per_dimension"])
EXAM_GROUPS = list(_PROFILE["exam_groups"])
DEFAULT_TIMEZONE = str(_PROFILE["default_timezone"])
_TIME_KEYS = {group["time_key"] for group in EXAM_GROUPS}

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass(frozen=True)
class FactWrite:
    applied: bool
    fact_id: UUID | None = None


def parse_exam_date(text: str, today: date) -> date | None:
    """把考试时间说法解析成日期。解析不了就返回空，这种考试不自动结束。"""
    year_day = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if year_day:
        return _safe_date(int(year_day.group(1)), int(year_day.group(2)), int(year_day.group(3)))

    year_month = re.search(r"(\d{4})年(\d{1,2})月", text)
    if year_month:
        year, month = int(year_month.group(1)), int(year_month.group(2))
        if not 1 <= month <= 12:
            return None
        return date(year, month, calendar.monthrange(year, month)[1])

    month_day = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if month_day:
        return _upcoming_day(today, int(month_day.group(1)), int(month_day.group(2)))

    month_only = re.search(r"(\d{1,2})月", text)
    if month_only:
        month = int(month_only.group(1))
        if not 1 <= month <= 12:
            return None
        last_day = calendar.monthrange(today.year, month)[1]
        return _upcoming_day(today, month, last_day)

    if "大后天" in text:
        return today + timedelta(days=3)
    if "后天" in text:
        return today + timedelta(days=2)
    if "明天" in text:
        return today + timedelta(days=1)
    if "今天" in text:
        return today

    named = re.search(r"(下个?周|这周|本周|周|星期|礼拜)([一二三四五六日天])", text)
    if named is None:
        return None
    weekday = _WEEKDAY[named.group(2)]
    prefix = named.group(1)
    monday = today - timedelta(days=today.weekday())
    if prefix.startswith("下"):
        return monday + timedelta(days=7 + weekday)
    if prefix in {"这周", "本周"}:
        return monday + timedelta(days=weekday)
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _upcoming_day(today: date, month: int, day: int) -> date | None:
    """没有年份时，取今天或之后最近的那一天。已经过去的月份放到明年。"""
    if not 1 <= month <= 12:
        return None
    this_year = _safe_date(today.year, month, day)
    if this_year is not None and this_year >= today:
        return this_year
    return _safe_date(today.year + 1, month, day)


def _local_today() -> date:
    # 上海不实行夏令时，固定东八区，不必再装时区数据。
    if DEFAULT_TIMEZONE == "Asia/Shanghai":
        zone = timezone(timedelta(hours=8))
    else:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(zone).date()


async def upsert_fact(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    dimension: str,
    key: str,
    value: str,
    confidence: float,
    source_message_id: UUID | None = None,
) -> FactWrite:
    """写入一条画像事实。同键已有事实时改插新行，并把旧行标成失效。

    身份和目标只能用配置里的键。置信度低于当前有效行时不覆盖。
    考试时间能解析出日期时写入 ends_on，供到点后标成已结束。
    """
    dimension = dimension.strip()
    fact_key = key.strip()
    fact_value = value.strip()
    if dimension not in DIMENSIONS:
        raise ValueError("未知的画像维度")
    allowed = FIXED_KEYS.get(dimension)
    if allowed is not None and fact_key not in allowed:
        raise ValueError("这个维度不能使用该键")
    if not fact_key or not fact_value:
        raise ValueError("画像的键和值不能为空")
    score = Decimal(str(confidence))
    if score < 0 or score > 1:
        raise ValueError("置信度必须在 0 到 1 之间")
    ends_on = parse_exam_date(fact_value, _local_today()) if fact_key in _TIME_KEYS else None

    async with pool.acquire() as connection:
        async with connection.transaction():
            current = await connection.fetchrow(
                """
                SELECT id, confidence FROM profile_facts
                WHERE user_id = $1 AND dimension = $2 AND fact_key = $3 AND status = '有效'
                FOR UPDATE
                """,
                user_id,
                dimension,
                fact_key,
            )
            if current is not None and score < current["confidence"]:
                return FactWrite(applied=False)
            if current is not None:
                await connection.execute(
                    """
                    UPDATE profile_facts
                    SET status = '失效', superseded_at = now()
                    WHERE id = $1
                    """,
                    current["id"],
                )
            fact_id = uuid4()
            await connection.execute(
                """
                INSERT INTO profile_facts (
                    id, user_id, dimension, fact_key, value,
                    confidence, source_message_id, status, ends_on
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, '有效', $8)
                """,
                fact_id,
                user_id,
                dimension,
                fact_key,
                fact_value,
                score,
                source_message_id,
                ends_on,
            )
            return FactWrite(applied=True, fact_id=fact_id)


def _arrange_facts(rows, limit: int | None) -> list[dict[str, str]]:
    """按维度和固定键顺序排好。limit 只限制讲法快照，口述档案不截断。"""
    chosen: dict[str, list] = {dimension: [] for dimension in SECTION_ORDER}
    for row in rows:
        bucket = chosen.get(row["dimension"])
        if bucket is None or (limit is not None and len(bucket) >= limit):
            continue
        bucket.append(row)

    facts: list[dict[str, str]] = []
    for dimension in SECTION_ORDER:
        order = KEY_ORDER.get(dimension, [])

        def sort_key(row, order=order):
            key = row["fact_key"]
            if key in order:
                return (0, order.index(key))
            return (1, key)

        for row in sorted(chosen[dimension], key=sort_key):
            facts.append({"dimension": dimension, "key": row["fact_key"], "value": row["value"]})
    return facts


async def teaching_facts(pool: asyncpg.Pool, user_id: UUID) -> list[dict[str, str]]:
    """取出可以改讲法的画像。失效、已结束，以及置信度不够的行都不进入讲法。"""
    rows = await pool.fetch(
        """
        SELECT dimension, fact_key, value
        FROM profile_facts
        WHERE user_id = $1 AND status = '有效' AND confidence >= $2
        ORDER BY created_at DESC
        """,
        user_id,
        MIN_CONFIDENCE,
    )
    return _arrange_facts(rows, SNAPSHOT_PER_DIMENSION)


async def active_facts(pool: asyncpg.Pool, user_id: UUID) -> list[dict[str, str]]:
    """取出全部仍有效的事实，供学生询问档案时口述。低置信度的也在这里，但不拿来改讲法。"""
    rows = await pool.fetch(
        """
        SELECT dimension, fact_key, value
        FROM profile_facts
        WHERE user_id = $1 AND status = '有效'
        ORDER BY created_at DESC
        """,
        user_id,
    )
    return _arrange_facts(rows, None)


async def list_profile(pool: asyncpg.Pool, user_id: UUID) -> list[dict]:
    """读出仍有效的画像，并按固定键补齐空栏，方便用户逐项确认。"""
    rows = await pool.fetch(
        """
        SELECT dimension, fact_key, value FROM profile_facts
        WHERE user_id = $1 AND status = '有效'
        ORDER BY fact_key
        """,
        user_id,
    )
    stored: dict[str, dict[str, str]] = {}
    for row in rows:
        stored.setdefault(row["dimension"], {})[row["fact_key"]] = row["value"]

    sections = []
    for dimension in SECTION_ORDER:
        known = stored.get(dimension, {})
        if dimension in KEY_ORDER:
            fields = [{"key": key, "value": known.get(key, "")} for key in KEY_ORDER[dimension]]
            fixed = True
        else:
            fields = [{"key": key, "value": value} for key, value in known.items()]
            fixed = False
        sections.append({"dimension": dimension, "fixed": fixed, "fields": fields})
    return sections


async def invalidate_fact(pool: asyncpg.Pool, user_id: UUID, dimension: str, key: str) -> None:
    """用户清空某一项时，把当前有效行标成失效，不另插空值。"""
    async with pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE profile_facts
            SET status = '失效', superseded_at = now()
            WHERE user_id = $1 AND dimension = $2 AND fact_key = $3 AND status = '有效'
            """,
            user_id,
            dimension,
            key,
        )


async def confirm_facts(
    pool: asyncpg.Pool,
    user_id: UUID,
    facts: list[tuple[str, str, str]],
) -> None:
    """按用户在界面里确认的内容写入。没变的跳过，清空的标成失效。"""
    current = {
        (section["dimension"], field["key"]): field["value"]
        for section in await list_profile(pool, user_id)
        for field in section["fields"]
        if field["value"]
    }
    for dimension, key, value in facts:
        dimension = dimension.strip()
        fact_key = key.strip()
        fact_value = value.strip()
        if not fact_key:
            continue
        if not fact_value:
            if (dimension, fact_key) in current:
                await invalidate_fact(pool, user_id, dimension, fact_key)
            continue
        if current.get((dimension, fact_key)) == fact_value:
            continue
        await upsert_fact(
            pool,
            user_id=user_id,
            dimension=dimension,
            key=fact_key,
            value=fact_value,
            confidence=MANUAL_CONFIDENCE,
        )


async def close_finished_exams(
    pool: asyncpg.Pool,
    *,
    today: date | None = None,
    user_id: UUID | None = None,
) -> int:
    """把日期已过的考试组标成已结束。考试当天仍有效，次日才结束。

    不删除原行。每日服务以后用这些已结束的考试询问成绩。早晨任务到第 4 期再调用。
    """
    if today is None:
        today = _local_today()
    closed = 0
    async with pool.acquire() as connection:
        async with connection.transaction():
            for group in EXAM_GROUPS:
                rows = await connection.fetch(
                    """
                    SELECT DISTINCT user_id FROM profile_facts
                    WHERE dimension = '目标' AND fact_key = $1 AND status = '有效'
                      AND ends_on IS NOT NULL AND ends_on < $2
                      AND ($3::uuid IS NULL OR user_id = $3)
                    """,
                    group["time_key"],
                    today,
                    user_id,
                )
                for row in rows:
                    result = await connection.execute(
                        """
                        UPDATE profile_facts
                        SET status = '已结束', superseded_at = now()
                        WHERE user_id = $1 AND dimension = '目标'
                          AND fact_key = ANY($2::text[]) AND status = '有效'
                        """,
                        row["user_id"],
                        list(group["close_keys"]),
                    )
                    closed += int(result.split()[-1])
    return closed
