import json
import logging
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
