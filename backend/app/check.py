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
REVISE_PROMPT = str(_RULES["revise_prompt"]).strip()
REASONS = {"计算", "检查"}
VERDICTS = {"通过", "有误", "看不出"}


class CheckOpinion:
    """留给家教决定的复核意见。revision 只在家教同意改口时才有。"""

    def __init__(self, check_id: UUID, reason: str, verdict: str, step: str | None, note: str) -> None:
        self.check_id = check_id
        self.reason = reason
        self.verdict = verdict
        self.step = step
        self.note = note


async def preview_reason(
    client: httpx.AsyncClient,
    settings: Settings,
    user_text: str,
) -> str | None:
    """先判断要不要复核。可以和讲解同时进行，不挡住已经开始说的话。"""
    question = user_text.strip()
    if not question:
        return None
    if _asked(question):
        return "检查"
    return await _decide(client, settings, question)


async def open_check(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    reason: str,
    user_id: UUID,
    conversation_id: UUID,
    user_message_id: UUID,
    assistant_message_id: UUID,
    user_text: str,
    assistant_text: str,
) -> CheckOpinion | None:
    """按已经判定的原因复核，并把意见留下。失败返回空，不改已经说出的话。"""
    try:
        logger.warning("解答复核启动 %s", reason)
        verdict, step, note = await _review(client, settings, user_text.strip(), assistant_text.strip())
        check_id = await _store(
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
        return CheckOpinion(check_id, reason, verdict, step, note)
    except Exception:
        logger.exception("解答复核失败")
        return None


async def consider_revision(
    client: httpx.AsyncClient,
    settings: Settings,
    opinion: CheckOpinion,
) -> str | None:
    """把意见交回家教。同意有误才返回要补上的那一句，否则不改口。"""
    user_prompt = (
        f"结论：{opinion.verdict}\n"
        f"步骤：{opinion.step or '（无）'}\n"
        f"说明：{opinion.note}"
    )
    payload = await _json_call(client, settings, REVISE_PROMPT, user_prompt, model=settings.tutor_model)
    if payload.get("revise") is not True:
        logger.warning("复核交回 %s 不改口", opinion.verdict)
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        logger.warning("复核交回 %s 不改口", opinion.verdict)
        return None
    logger.warning("复核交回 %s 改口", opinion.verdict)
    return text[:400]


async def mark_revision(pool: asyncpg.Pool, check_id: UUID, revision: str) -> None:
    """记下家教同意后追加的那一句。"""
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE solution_checks SET revision = $2 WHERE id = $1",
            check_id,
            revision,
        )


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
    """连接已经结束时补做复核。意见留下，但来不及再改口。"""
    try:
        question = user_text.strip()
        answer = assistant_text.strip()
        if not question or not answer:
            return
        reason = await preview_reason(client, settings, question)
        if reason is None:
            logger.warning("解答复核跳过")
            return
        await open_check(
            pool,
            client,
            settings,
            reason=reason,
            user_id=user_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            user_text=question,
            assistant_text=answer,
        )
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
    """只看题目和老师已经写出的关键步骤。改不改口由家教另决。"""
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
    *,
    model: str | None = None,
) -> dict:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": model or settings.extractor_model,
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
) -> UUID:
    check_id = uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO solution_checks (
                id, user_id, conversation_id, user_message_id, assistant_message_id,
                reason, verdict, step, note
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            check_id,
            user_id,
            conversation_id,
            user_message_id,
            assistant_message_id,
            reason,
            verdict,
            step,
            note,
        )
    return check_id


def _json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.removesuffix("```").strip()
    return text
