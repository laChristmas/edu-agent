import json
import logging
import tomllib
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx

from app.config import Settings
from app.memory import KEY_ORDER, invalidate_fact, upsert_fact

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
        facts, forgets = await _request_changes(pool, client, settings, user_id, user_text, assistant_text)
        written: set[tuple[str, str]] = set()
        for fact in facts:
            if await _store_fact(pool, user_id, user_message_id, fact):
                written.add((str(fact.get("dimension") or "").strip(), str(fact.get("key") or "").strip()))
        # 同一轮又改口又被误放进忘记时，保留刚写下的新值。
        for item in forgets:
            dimension = str(item.get("dimension") or "").strip()
            key = str(item.get("key") or "").strip()
            if (dimension, key) in written:
                continue
            await _forget_fact(pool, user_id, dimension, key)
    except Exception:
        logger.exception("画像抽取失败")


async def _request_changes(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    user_id: UUID,
    user_text: str,
    assistant_text: str,
) -> tuple[list[dict], list[dict]]:
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
    if not isinstance(payload, dict):
        return [], []
    facts = payload.get("facts")
    forgets = payload.get("forgets")
    fact_list = [fact for fact in facts if isinstance(fact, dict)] if isinstance(facts, list) else []
    forget_list = [item for item in forgets if isinstance(item, dict)] if isinstance(forgets, list) else []
    return fact_list, forget_list


def _json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.removesuffix("```").strip()
    return text


async def _forget_fact(pool: asyncpg.Pool, user_id: UUID, dimension: str, key: str) -> None:
    """学生要求忘掉某一项时，只把仍有效的已有键标成失效。"""
    if not dimension or not key:
        return
    current = await pool.fetchval(
        """
        SELECT id FROM profile_facts
        WHERE user_id = $1 AND dimension = $2 AND fact_key = $3 AND status = '有效'
        """,
        user_id,
        dimension,
        key,
    )
    if current is None:
        logger.info("没有可忘记的画像 %s/%s", dimension, key)
        return
    await invalidate_fact(pool, user_id, dimension, key)


async def _store_fact(pool: asyncpg.Pool, user_id: UUID, user_message_id: UUID, fact: dict) -> bool:
    dimension = str(fact.get("dimension") or "").strip()
    key = str(fact.get("key") or "").strip()
    value = str(fact.get("value") or "").strip()
    if not dimension or not key or not value:
        return False
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
        return False
    try:
        written = await upsert_fact(
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
        return False
    return written.applied
