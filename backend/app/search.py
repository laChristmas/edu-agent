import json
import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
import httpx

from app.config import Settings
from app.mistakes import ERROR_TYPE_LIST, ERROR_TYPES

logger = logging.getLogger("eduagent.search")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

_SEARCH = _RULES["search"]
SEARCH_PROMPT = str(_SEARCH["search_prompt"]).strip()
REPLY_PROMPT = str(_SEARCH["reply_prompt"]).strip()
RETEACH_DEFAULT = str(_SEARCH["reteach_default"]).strip()
_RETEACH = {str(key): str(value).strip() for key, value in _SEARCH["reteach"].items()}
TIME_LABELS = [str(item) for item in _SEARCH["time_labels"]]
_TIMEZONE = str(_RULES["profile"]["default_timezone"])
# 上周在方案里就是近 7 天，不按自然周切。模型若照原话写，这里收成配置里的词。
_TIME_ALIASES = {
    "上周": "近7天",
    "上星期": "近7天",
    "最近一周": "近7天",
    "最近七天": "近7天",
    "最近7天": "近7天",
    "近七天": "近7天",
    "这周": "本周",
    "这个星期": "本周",
    "这个月": "本月",
}
_CN_DIGIT = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class MistakeFilters:
    """问句收成的条件。since 和 until 含当天，由时间词在查询时算出来。"""

    subject: str | None = None
    knowledge_points: tuple[str, ...] = ()
    error_type: str | None = None
    time_label: str | None = None
    since: date | None = None
    until: date | None = None
    keywords: tuple[str, ...] = ()

    def narrows(self) -> bool:
        return bool(self.subject or self.knowledge_points or self.error_type or self.since or self.until)


async def parse_search_filters(
    client: httpx.AsyncClient,
    settings: Settings,
    user_text: str,
    today: date | None = None,
) -> MistakeFilters:
    """把找错题的问句收成学科、知识点、错因和时间。普通讲题得到空条件。"""
    day = today or _local_today()
    allowed_errors = "、".join(ERROR_TYPE_LIST)
    allowed_times = "、".join(TIME_LABELS)
    user_prompt = (
        f"允许的错因：{allowed_errors}\n"
        f"允许的时间：{allowed_times}\n\n"
        f"学生：{user_text.strip()}"
    )
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": SEARCH_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"错题过滤返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(_json_text(content))
    raw = payload.get("filters") if isinstance(payload, dict) else None
    return _normalize(raw, day)


async def narrow_mistakes(
    pool: asyncpg.Pool,
    user_id: UUID,
    filters: MistakeFilters,
) -> list[dict]:
    """用学科、知识点、错因和时间收窄这个用户的错题。没有条件时不把整本都取出来。"""
    if not filters.narrows():
        return []
    patterns = [_contains_pattern(name) for name in filters.knowledge_points]
    rows = await pool.fetch(
        """
        SELECT
            m.id,
            m.stem,
            m.subject,
            m.error_type,
            (m.created_at AT TIME ZONE 'Asia/Shanghai')::date AS created_on,
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
          AND ($2::text IS NULL OR m.subject = $2)
          AND ($3::text IS NULL OR m.error_type = $3)
          AND ($4::date IS NULL OR (m.created_at AT TIME ZONE 'Asia/Shanghai')::date >= $4)
          AND ($5::date IS NULL OR (m.created_at AT TIME ZONE 'Asia/Shanghai')::date <= $5)
          AND (
                cardinality($6::text[]) = 0
                OR EXISTS (
                    SELECT 1
                    FROM mistake_points wanted_mp
                    JOIN knowledge_points wanted_k
                        ON wanted_k.id = wanted_mp.knowledge_point_id
                       AND wanted_k.user_id = wanted_mp.user_id
                    WHERE wanted_mp.mistake_id = m.id
                      AND wanted_mp.user_id = m.user_id
                      AND (
                            wanted_k.name = ANY($6::text[])
                            OR EXISTS (
                                SELECT 1 FROM unnest($7::text[]) AS pat(value)
                                WHERE wanted_k.name LIKE pat.value ESCAPE '\\'
                            )
                      )
                )
          )
        GROUP BY m.id
        ORDER BY m.created_at DESC, m.id
        """,
        user_id,
        filters.subject,
        filters.error_type,
        filters.since,
        filters.until,
        list(filters.knowledge_points),
        patterns,
    )
    return [
        {
            "id": row["id"],
            "stem": row["stem"],
            "subject": row["subject"],
            "error_type": row["error_type"],
            "created_on": row["created_on"],
            "knowledge_points": list(row["knowledge_points"]),
        }
        for row in rows
    ]


async def prepare_mistake_lookup(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    conversation_id: UUID,
    user_text: str,
) -> str | None:
    """多题只准备短列表。点名或只命中一道时按错因准备重讲。普通讲题返回空。"""
    text = user_text.strip()
    if not _looks_like_lookup(text):
        return None
    picked = _named_index(text)
    if picked is not None:
        listed = await _load_list(pool, conversation_id, user_id)
        if listed:
            position = len(listed) if picked < 0 else picked
            if position < 1 or position > len(listed):
                return _reply(f"刚才的列表只有 {len(listed)} 道，没有第 {position} 道。")
            detail = await _load_mistake(pool, user_id, listed[position - 1])
            if detail is None:
                return _reply("点到的那道错题已经不在了。")
            logger.info("按错因重讲：%s", detail["error_type"] or "未记下")
            return _reply(_open_body(detail))
    filters = await parse_search_filters(client, settings, text)
    if not filters.narrows():
        return None
    rows = await narrow_mistakes(pool, user_id, filters)
    if not rows:
        return _reply("没有命中已记下的错题。")
    if len(rows) == 1:
        detail = await _load_mistake(pool, user_id, rows[0]["id"])
        if detail is None:
            return _reply("没有命中已记下的错题。")
        await _remember(pool, conversation_id, user_id, [detail["id"]])
        logger.info("按错因重讲：%s", detail["error_type"] or "未记下")
        return _reply(_open_body(detail))
    await _remember(pool, conversation_id, user_id, [row["id"] for row in rows])
    return _reply(_list_body(rows))


def _looks_like_lookup(text: str) -> bool:
    """点名常说「那道」或「第几道」。条件若是空的，调用方会照常讲。"""
    if "错题" in text:
        return True
    if _named_index(text) is not None:
        return True
    return re.search(r"那[道题]", text) is not None


def _named_index(text: str) -> int | None:
    """点名从 1 起。返回 -1 表示最后一道。"""
    match = re.search(r"第\s*([0-9]{1,2}|[一二三四五六七八九]|十)\s*[道题条个]", text)
    if match:
        token = match.group(1)
        number = int(token) if token.isdigit() else _CN_DIGIT[token]
        if number >= 1:
            return number
    if "最后一道" in text or "最后一题" in text:
        return -1
    return None


def _reply(body: str) -> str:
    return f"{REPLY_PROMPT}\n{body}"


def _list_body(rows: list[dict]) -> str:
    lines = ["命中了多道，按这个顺序只报时间、错因和题干首句。"]
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {_day_label(row['created_on'])}，错因{_error_label(row['error_type'])}，"
            f"题干：{_first_sentence(row['stem'])}"
        )
    return "\n".join(lines)


def _open_body(detail: dict) -> str:
    reference = detail["reference_answer"] or "（没有记下）"
    student = detail["student_answer"] or "（没有记下）"
    points = detail.get("point_names") or "（没有）"
    mastery = detail.get("mastery_text") or "（没有）"
    return "\n".join(
        [
            "按下面的讲法重讲这一道。",
            f"讲法：{_reteach_line(detail['error_type'])}",
            f"时间：{_day_label(detail['created_on'])}",
            f"题干：{detail['stem'] or '（没有题干）'}",
            f"学生答案：{student}",
            f"参考答案：{reference}",
            f"错因：{_error_label(detail['error_type'])}",
            f"知识点：{points}",
            f"掌握度：{mastery}",
        ]
    )


def _reteach_line(error_type: str | None) -> str:
    if error_type and error_type in _RETEACH:
        return _RETEACH[error_type]
    return RETEACH_DEFAULT


def _first_sentence(stem: str | None) -> str:
    text = (stem or "").strip()
    if not text:
        return "（没有题干）"
    sentence = re.split(r"[。！？\n]", text, maxsplit=1)[0].strip() or text
    if len(sentence) > 80:
        return sentence[:80] + "…"
    return sentence


def _error_label(error_type: str | None) -> str:
    return error_type or "未记下"


def _day_label(day: date) -> str:
    return f"{day.month}月{day.day}日"


async def _load_list(pool: asyncpg.Pool, conversation_id: UUID, user_id: UUID) -> list[UUID]:
    rows = await pool.fetch(
        """
        SELECT mistake_id
        FROM mistake_list_items
        WHERE conversation_id = $1 AND user_id = $2
        ORDER BY position
        """,
        conversation_id,
        user_id,
    )
    return [row["mistake_id"] for row in rows]


async def _remember(
    pool: asyncpg.Pool,
    conversation_id: UUID,
    user_id: UUID,
    mistake_ids: list[UUID],
) -> None:
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "DELETE FROM mistake_list_items WHERE conversation_id = $1",
                conversation_id,
            )
            await connection.executemany(
                """
                INSERT INTO mistake_list_items (conversation_id, position, mistake_id, user_id)
                VALUES ($1, $2, $3, $4)
                """,
                [
                    (conversation_id, index, mistake_id, user_id)
                    for index, mistake_id in enumerate(mistake_ids, start=1)
                ],
            )


async def _load_mistake(pool: asyncpg.Pool, user_id: UUID, mistake_id: UUID) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT
            m.id,
            m.stem,
            m.student_answer,
            m.reference_answer,
            m.error_type,
            (m.created_at AT TIME ZONE 'Asia/Shanghai')::date AS created_on,
            COALESCE(
                (
                    SELECT string_agg(k.name, '、' ORDER BY k.name)
                    FROM mistake_points mp
                    JOIN knowledge_points k
                        ON k.id = mp.knowledge_point_id AND k.user_id = mp.user_id
                    WHERE mp.mistake_id = m.id AND mp.user_id = m.user_id
                ),
                ''
            ) AS point_names,
            COALESCE(
                (
                    SELECT string_agg(
                        k.name || ' ' || COALESCE(ma.score::text, '未记'),
                        '、' ORDER BY k.name
                    )
                    FROM mistake_points mp
                    JOIN knowledge_points k
                        ON k.id = mp.knowledge_point_id AND k.user_id = mp.user_id
                    LEFT JOIN mastery ma
                        ON ma.knowledge_point_id = k.id AND ma.user_id = mp.user_id
                    WHERE mp.mistake_id = m.id AND mp.user_id = m.user_id
                ),
                ''
            ) AS mastery_text
        FROM mistakes m
        WHERE m.id = $1 AND m.user_id = $2
        """,
        mistake_id,
        user_id,
    )
    if row is None:
        return None
    return dict(row)


def _normalize(raw, today: date) -> MistakeFilters:
    if not isinstance(raw, dict):
        return MistakeFilters()
    subject = _clean(raw.get("subject"))
    error_type = _clean(raw.get("error_type"))
    if error_type not in ERROR_TYPES:
        error_type = None
    points = _names(raw.get("knowledge_points"))
    keywords = _names(raw.get("keywords"))
    label = _TIME_ALIASES.get(_clean(raw.get("time")) or "", _clean(raw.get("time")))
    if label not in TIME_LABELS:
        label = None
    since, until = _time_range(label, today) if label else (None, None)
    return MistakeFilters(
        subject=subject,
        knowledge_points=points,
        error_type=error_type,
        time_label=label,
        since=since,
        until=until,
        keywords=keywords,
    )


def _time_range(label: str, today: date) -> tuple[date, date]:
    if label == "今天":
        return today, today
    if label == "昨天":
        day = today - timedelta(days=1)
        return day, day
    if label == "前天":
        day = today - timedelta(days=2)
        return day, day
    if label == "近7天":
        return today - timedelta(days=6), today
    if label == "本周":
        return today - timedelta(days=today.weekday()), today
    start = today.replace(day=1)
    return start, today


def _names(values) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _clean(value)
        if name is None or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return tuple(names)


def _contains_pattern(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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


def _zone():
    # 上海不实行夏令时，固定东八区，不必再装时区数据。
    if _TIMEZONE == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    return ZoneInfo(_TIMEZONE)


def _local_today() -> date:
    return datetime.now(_zone()).date()
