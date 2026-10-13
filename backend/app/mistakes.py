import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx

from app.config import Settings

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

_MISTAKES = _RULES["mistakes"]
MISTAKE_PROMPT = str(_MISTAKES["mistake_prompt"]).strip()
ERROR_TYPE_LIST = [str(item) for item in _MISTAKES["error_types"]]
ERROR_TYPES = frozenset(ERROR_TYPE_LIST)


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
) -> ParsedMistake | None:
    """从这一轮原话抽出错题结构。不是记错题时返回空，不在这里写入数据库。"""
    allowed = "、".join(ERROR_TYPE_LIST)
    teacher = assistant_text.strip() or "（无）"
    user_prompt = f"允许的错因：{allowed}\n\n学生：{user_text.strip()}\n老师：{teacher}"
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
