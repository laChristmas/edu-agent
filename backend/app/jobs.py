import asyncio
import json
import logging
import tomllib
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx

from app.config import Settings
from app.mastery import local_today, review_moment
from app.memory import active_facts

logger = logging.getLogger("eduagent.jobs")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)["jobs"]

ASK_PHRASES = tuple(str(item) for item in _RULES["ask_phrases"])
ACK_PROMPT = str(_RULES["ack_prompt"]).strip()
BUSY_PROMPT = str(_RULES["busy_prompt"]).strip()
PLAN_PROMPT = str(_RULES["plan_prompt"]).strip()
FAIL_SENTENCE = str(_RULES["fail_sentence"]).strip()
_FORBIDDEN = ("任务", "后台", "队列", "复核")
_WEEK_DAYS = 7
_MISTAKE_LIMIT = 5


def _wants_weekly(text: str) -> bool:
    return any(phrase in text for phrase in ASK_PHRASES)


async def prepare_weekly(
    pool: asyncpg.Pool,
    *,
    user_id: UUID,
    conversation_id: UUID,
    user_text: str,
) -> tuple[str | None, bool]:
    """要周计划时先落一行任务。这一轮只让家教说稍后补一句，不在这里排完。"""
    if not _wants_weekly(user_text.strip()):
        return None, False
    existing = await pool.fetchval(
        """
        SELECT id FROM jobs
        WHERE user_id = $1 AND kind = '周计划' AND status IN ('待做', '进行中')
        LIMIT 1
        """,
        user_id,
    )
    if existing is not None:
        logger.warning("周计划已在排")
        return BUSY_PROMPT, False
    await pool.execute(
        """
        INSERT INTO jobs (id, user_id, conversation_id, kind, status, step, payload)
        VALUES ($1, $2, $3, '周计划', '待做', 0, '{}'::jsonb)
        """,
        uuid4(),
        user_id,
        conversation_id,
    )
    logger.warning("周计划入队")
    return ACK_PROMPT, True


async def reclaim_weekly_plans(pool: asyncpg.Pool) -> None:
    """单进程重启后，进行中的周计划都是上一进程留下的。步数和材料留着，只放开领取。"""
    await pool.execute("UPDATE jobs SET claimed_at = NULL WHERE status = '进行中'")


async def serve_weekly_plans(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """领取用 SKIP LOCKED，避免两个循环拿到同一行。停机时不把进行中改成失败。"""
    while not stop.is_set():
        try:
            job = await _claim(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("周计划领取失败")
            job = None
        if job is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                continue
            break
        try:
            await _advance(pool, client, settings, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("周计划失败")
            await _fail(pool, job)


async def _claim(pool: asyncpg.Pool) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        UPDATE jobs
        SET status = '进行中', claimed_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = '待做'
               OR (status = '进行中' AND claimed_at IS NULL)
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, user_id, conversation_id, step, payload, result, message_id, created_at
        """
    )


async def _advance(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    settings: Settings,
    job: asyncpg.Record,
) -> None:
    step = int(job["step"])
    payload = _payload(job["payload"])
    result = str(job["result"] or "").strip()
    if step > 0:
        logger.warning("周计划继续 第%s步", step)
    else:
        logger.warning("周计划开始")
    if step < 1:
        payload = await _gather(pool, job["user_id"])
        await _save(pool, job["id"], 1, payload, None)
        step = 1
    if step < 2:
        result = await _compose(client, settings, payload)
        await _save(pool, job["id"], 2, payload, result)
        step = 2
    if step < 3:
        if job["message_id"] is not None:
            await _finish(pool, job["id"], job["message_id"], result)
            return
        # 先说出的那句还没落库时不补。补在它后面，学生才是先听到「稍后说」，再听到安排。
        ready = await _ack_ready(pool, job["conversation_id"], job["created_at"])
        if not ready:
            await pool.execute(
                "UPDATE jobs SET claimed_at = NULL WHERE id = $1 AND status = '进行中'",
                job["id"],
            )
            await asyncio.sleep(0.5)
            return
        message_id = await _speak(pool, job["conversation_id"], result)
        await _finish(pool, job["id"], message_id, result)


async def _gather(pool: asyncpg.Pool, user_id: UUID) -> dict:
    """只收未来七天的到期错题和已经记下的考试，不在这里编安排。"""
    today = local_today()
    due_before = review_moment(today + timedelta(days=_WEEK_DAYS))
    rows = await pool.fetch(
        """
        SELECT
            m.stem,
            (m.next_review_at AT TIME ZONE 'Asia/Shanghai')::date AS due_on,
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
          AND m.next_review_at IS NOT NULL
          AND m.next_review_at < $2
        GROUP BY m.id
        ORDER BY m.next_review_at, m.created_at, m.id
        LIMIT $3
        """,
        user_id,
        due_before,
        _MISTAKE_LIMIT,
    )
    facts = await active_facts(pool, user_id)
    exam = next(
        (
            fact["value"]
            for fact in facts
            if fact["dimension"] == "目标" and fact["key"] == "近期考试" and fact["value"].strip()
        ),
        "",
    )
    mistakes = []
    for row in rows:
        points = [str(name) for name in row["knowledge_points"] if name]
        stem = (row["stem"] or "").strip().split("。", 1)[0].split("\n", 1)[0].strip()
        label = points[0] if points else (stem[:40] or "一道错题")
        due = row["due_on"]
        mistakes.append({"due": f"{due.month}月{due.day}日", "label": label})
    return {"exam": exam, "mistakes": mistakes}


async def _compose(client: httpx.AsyncClient, settings: Settings, payload: dict) -> str:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "enable_thinking": False,
            "messages": [
                {"role": "system", "content": PLAN_PROMPT},
                {"role": "user", "content": _material_text(payload)},
            ],
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"周计划接口返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    text = _one_sentence(str(content or ""))
    if not text or any(word in text for word in _FORBIDDEN):
        return _fallback(payload)
    return text


def _material_text(payload: dict) -> str:
    lines = ["到期错题："]
    mistakes = payload.get("mistakes") or []
    if mistakes:
        for item in mistakes:
            lines.append(f"{item['due']} {item['label']}")
    else:
        lines.append("无")
    exam = str(payload.get("exam") or "").strip()
    lines.append(f"近期考试：{exam or '无'}")
    return "\n".join(lines)


def _one_sentence(text: str) -> str:
    compact = "".join(text.split())
    for mark in ("。", "！", "？"):
        if mark in compact:
            compact = compact.split(mark, 1)[0] + mark
            break
    return compact[:180]


def _fallback(payload: dict) -> str:
    mistakes = payload.get("mistakes") or []
    exam = str(payload.get("exam") or "").strip()
    if mistakes and exam:
        item = mistakes[0]
        return f"这周先顾着{exam}，{item['due']}要复习的{item['label']}也一起过一遍。"
    if mistakes:
        item = mistakes[0]
        return f"这周我们先把{item['due']}要复习的{item['label']}过一遍。"
    if exam:
        return f"这周先准备{exam}就好，没有别的到期的题。"
    return "这周没有要复习的题。你想问什么，跟我说就好。"


async def _save(
    pool: asyncpg.Pool,
    job_id: UUID,
    step: int,
    payload: dict,
    result: str | None,
) -> None:
    await pool.execute(
        """
        UPDATE jobs
        SET step = $2, payload = $3::jsonb, result = COALESCE($4, result), claimed_at = now()
        WHERE id = $1 AND status = '进行中'
        """,
        job_id,
        step,
        json.dumps(payload, ensure_ascii=False),
        result,
    )


async def _ack_ready(pool: asyncpg.Pool, conversation_id: UUID, job_created_at) -> bool:
    found = await pool.fetchval(
        """
        SELECT id FROM messages
        WHERE conversation_id = $1
          AND role = 'assistant'
          AND created_at >= $2
        LIMIT 1
        """,
        conversation_id,
        job_created_at,
    )
    if found is not None:
        return True
    # 回话始终没落库时，安排也不能一直闷着。
    age = await pool.fetchval("SELECT now() - $1::timestamptz", job_created_at)
    return age is not None and age.total_seconds() >= 120


async def _speak(pool: asyncpg.Pool, conversation_id: UUID, text: str) -> UUID:
    message_id = uuid4()
    await pool.execute(
        """
        INSERT INTO messages (id, conversation_id, role, content)
        VALUES ($1, $2, 'assistant', $3)
        """,
        message_id,
        conversation_id,
        text,
    )
    return message_id


async def _finish(pool: asyncpg.Pool, job_id: UUID, message_id: UUID, result: str) -> None:
    await pool.execute(
        """
        UPDATE jobs
        SET status = '完成', step = 3, message_id = $2, result = $3, finished_at = now()
        WHERE id = $1
        """,
        job_id,
        message_id,
        result,
    )
    logger.warning("周计划完成")


async def _fail(pool: asyncpg.Pool, job: asyncpg.Record) -> None:
    logger.warning("周计划没排成")
    if job["message_id"] is not None:
        await pool.execute(
            "UPDATE jobs SET status = '失败', finished_at = now() WHERE id = $1 AND status <> '完成'",
            job["id"],
        )
        return
    message_id = await _speak(pool, job["conversation_id"], FAIL_SENTENCE)
    await pool.execute(
        """
        UPDATE jobs
        SET status = '失败', message_id = $2, result = $3, finished_at = now()
        WHERE id = $1 AND status <> '完成'
        """,
        job["id"],
        message_id,
        FAIL_SENTENCE,
    )


def _payload(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}
