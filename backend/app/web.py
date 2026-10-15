import json
import logging
import tomllib
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger("eduagent.web")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

_WEB = _RULES["web"]
DECIDE_PROMPT = str(_WEB["decide_prompt"]).strip()
DIGEST_PROMPT = str(_WEB["digest_prompt"]).strip()
NOTE_PROMPT = str(_WEB["note_prompt"]).strip()
KINDS = {str(item) for item in _WEB["kinds"]}
MAX_RESULTS = int(_WEB["max_results"])
MAX_DIGEST_CHARS = int(_WEB["max_digest_chars"])
# 只发照片时这句话是占位，不代表学生在问一件会过时的事。
_PHOTO_ONLY = "（一张照片）"


async def prepare_web_lookup(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    user_text: str,
) -> str | None:
    """会过时的事实才去检索，并把材料交给家教。普通讲题返回空。"""
    text = user_text.strip()
    if not text or text == _PHOTO_ONLY:
        return None
    query = await _search_query(client, settings, text)
    if query is None:
        return None
    digest, sources = await _search_web(client, settings, query)
    if not digest and not sources:
        return None
    lines = [NOTE_PROMPT, "", f"问：{query}"]
    if digest:
        lines.extend(["", "摘录：", digest])
    if sources:
        lines.append("")
        lines.append("条目：")
        lines.extend(f"{index}. {title} {url}" for index, title, url in enumerate(sources, start=1))
    return "\n".join(lines)


async def _search_query(client: httpx.AsyncClient, settings: Settings, user_text: str) -> str | None:
    """先判断要不要查。判断失败就抛出，由这一轮照常讲。"""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            # 判断只要 JSON。思考过程会把结果挤掉。
            "enable_thinking": False,
            "messages": [
                {"role": "system", "content": DECIDE_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "response_format": {"type": "json_object"},
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"检索判断返回 {response.status_code}: {detail}")
    content = response.json()["choices"][0]["message"]["content"]
    payload = json.loads(_json_text(content))
    if not isinstance(payload, dict):
        return None
    # 只认配置里的三类。模型多标的种类不拿去检索。
    kind = str(payload.get("kind") or "").strip()
    if kind not in KINDS:
        return None
    query = str(payload.get("query") or "").strip() or user_text
    return query[:200]


async def _search_web(
    client: httpx.AsyncClient,
    settings: Settings,
    query: str,
) -> tuple[str, list[tuple[str, str]]]:
    """这个模型走文本生成接口会报地址错误。联网搜索沿用现在的兼容接口，并强制检索。"""
    logger.warning("网页检索 %s", query)
    response = await client.post(
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.extractor_model,
            "enable_thinking": False,
            "enable_search": True,
            "search_options": {
                "forced_search": True,
                "enable_source": True,
            },
            "messages": [
                {"role": "system", "content": DIGEST_PROMPT},
                {"role": "user", "content": query},
            ],
        },
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"网页检索返回 {response.status_code}: {detail}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("网页检索没有返回结果")
    if payload.get("code"):
        message = str(payload.get("message") or payload["code"])
        raise RuntimeError(f"网页检索失败: {message[:500]}")
    digest = _digest(payload)[:MAX_DIGEST_CHARS].strip()
    sources = _sources(payload)
    logger.warning("网页检索完成 摘录%s 条目%s", "有" if digest else "无", len(sources))
    return digest, sources


def _digest(output: dict) -> str:
    choices = output.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _sources(output: dict) -> list[tuple[str, str]]:
    info = output.get("search_info") or {}
    rows = info.get("search_results") or []
    found: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        found.append((title, url))
        if len(found) >= MAX_RESULTS:
            break
    return found


def _json_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.removesuffix("```").strip()
    return text
