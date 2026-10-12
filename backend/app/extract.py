import json
import logging
import tomllib
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx

from app.config import Settings
from app.memory import KEY_ORDER, upsert_fact

logger = logging.getLogger("eduagent.extract")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    EXTRACTOR_PROMPT = str(tomllib.load(config_file)["extractor_prompt"]).strip()

_OPEN_DIMENSIONS = ("偏好", "兴趣", "学情")


async def extract_turn(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    user_message_id: UUID,
    user_text: str,
    assistant_text: str,
) -> None:
    """回复落库后抽取这一轮的画像。失败只记日志，不反过来影响已经发出的讲解。"""
    try:
        facts = await _request_facts(pool, client, settings, user_id, user_text, assistant_text)
        for fact in facts:
            await _store_fact(pool, user_id, user_message_id, fact)
    except Exception:
        logger.exception("画像抽取失败")


async def _request_facts(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    user_id: UUID,
    user_text: str,
    assistant_text: str,
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT dimension, fact_key, value FROM profile_facts
        WHERE user_id = $1 AND status = '有效'
        ORDER BY dimension, fact_key
        """,
        user_id,
    )
    existing = "\n".join(f"- {row['dimension']}/{row['fact_key']}={row['value']}" for row in rows) or "（无）"
    identity = "、".join(KEY_ORDER["身份"])
    goals = "、".join(KEY_ORDER["目标"])
    user_prompt = (
        f"身份允许的键：{identity}\n"
        f"目标允许的键：{goals}\n"
        f"开放维度：{'、'.join(_OPEN_DIMENSIONS)}。这些维度已有的键要沿用。\n"
        f"已有事实：\n{existing}\n\n"
        f"学生：{user_text}\n"
        f"老师：{assistant_text}"
    )
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": EXTRACTOR_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"抽取接口返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(_json_text(content))
    facts = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        return []
    return [fact for fact in facts if isinstance(fact, dict)]


def _json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.removesuffix("```").strip()
    return text


async def _store_fact(pool: asyncpg.Pool, user_id: UUID, user_message_id: UUID, fact: dict) -> None:
    dimension = str(fact.get("dimension") or "").strip()
    key = str(fact.get("key") or "").strip()
    value = str(fact.get("value") or "").strip()
    if not dimension or not key or not value:
        return
    explicit = fact.get("explicit")
    confidence = 0.9 if explicit is True or explicit == "true" else 0.6
    current = await pool.fetchval(
        """
        SELECT value FROM profile_facts
        WHERE user_id = $1 AND dimension = $2 AND fact_key = $3 AND status = '有效'
        """,
        user_id,
        dimension,
        key,
    )
    if current == value:
        return
    try:
        await upsert_fact(
            pool,
            user_id=user_id,
            dimension=dimension,
            key=key,
            value=value,
            confidence=confidence,
            source_message_id=user_message_id,
        )
    except ValueError:
        logger.info("跳过无效画像 %s/%s", dimension, key)
