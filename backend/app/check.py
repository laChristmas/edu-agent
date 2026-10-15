import json
import logging
import tomllib
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx

from app.config import Settings

logger = logging.getLogger("eduagent.check")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)["check"]

ASK_PHRASES = tuple(str(item) for item in _RULES["ask_phrases"])
DECIDE_PROMPT = str(_RULES["decide_prompt"]).strip()
REVIEW_PROMPT = str(_RULES["review_prompt"]).strip()
REASONS = {"计算", "检查"}
VERDICTS = {"通过", "有误", "看不出"}


async def run_check(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_id: UUID,
    conversation_id: UUID,
    user_message_id: UUID,
    assistant_message_id: UUID,
    user_text: str,
    assistant_text: str,
) -> None:
    """回复发出后再决定要不要复核。计算题或学生要求检查才启动，失败不影响已经说出的话。"""
    try:
        question = user_text.strip()
        answer = assistant_text.strip()
        if not question or not answer:
            return
        if _asked(question):
            reason = "检查"
        else:
            reason = await _decide(client, settings, question)
        if reason is None:
            logger.warning("解答复核跳过")
            return
        logger.warning("解答复核启动 %s", reason)
        verdict, step, note = await _review(client, settings, question, answer)
        await _store(
            pool,
            user_id=user_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            reason=reason,
            verdict=verdict,
            step=step,
            note=note,
        )
        logger.warning("解答复核完成 %s %s", reason, verdict)
    except Exception:
        logger.exception("解答复核失败")


def _asked(text: str) -> bool:
    return any(phrase in text for phrase in ASK_PHRASES)


async def _decide(client: httpx.AsyncClient, settings: Settings, user_text: str) -> str | None:
    """不是计算、也没要求检查时返回空。判断失败就抛出，由这一轮当作没启动。"""
    payload = await _json_call(client, settings, DECIDE_PROMPT, user_text)
    if not isinstance(payload, dict) or payload.get("start") is not True:
        return None
    reason = str(payload.get("reason") or "").strip()
    if reason not in REASONS:
        return None
    return reason


async def _review(
    client: httpx.AsyncClient,
    settings: Settings,
    user_text: str,
    assistant_text: str,
) -> tuple[str, str | None, str]:
    """只看题目和老师已经写出的关键步骤。意见留下，不在这一轮改口。"""
    payload = await _json_call(
        client,
        settings,
        REVIEW_PROMPT,
        f"题目：{user_text}\n\n老师的回复：{assistant_text}",
    )
    if not isinstance(payload, dict):
        raise RuntimeError("解答复核没有返回结果")
    verdict = str(payload.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        verdict = "看不出"
    step = str(payload.get("step") or "").strip() or None
    note = str(payload.get("note") or "").strip() or "没有给出意见"
    return verdict, step, note[:400]


async def _json_call(
    client: httpx.AsyncClient,
    settings: Settings,
    prompt: str,
    user_text: str,
) -> dict:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            # 判断只要 JSON。思考过程会把结果挤掉。
            "enable_thinking": False,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            "response_format": {"type": "json_object"},
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"解答复核返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(_json_text(content))
    if not isinstance(payload, dict):
        raise RuntimeError("解答复核没有返回结果")
    return payload


async def _store(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    conversation_id: UUID,
    user_message_id: UUID,
    assistant_message_id: UUID,
    reason: str,
    verdict: str,
    step: str | None,
    note: str,
) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO solution_checks (
                id, user_id, conversation_id, user_message_id, assistant_message_id,
                reason, verdict, step, note
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid4(),
            user_id,
            conversation_id,
            user_message_id,
            assistant_message_id,
            reason,
            verdict,
            step,
            note,
        )


def _json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.removesuffix("```").strip()
    return text
