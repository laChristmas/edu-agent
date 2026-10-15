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
UNCERTAIN_PROMPT = str(_WEB["uncertain_prompt"]).strip()
KINDS = {str(item) for item in _WEB["kinds"]}
CITE_KINDS = {str(item) for item in _WEB["cite_kinds"]}
SEARCH_STRATEGY = str(_WEB["search_strategy"]).strip()
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
    """会过时的事实才去检索。考纲和分数线没有可引用的来源时，让家教说明不确定。"""
    text = user_text.strip()
    if not text or text == _PHOTO_ONLY:
        return None
    decided = await _search_query(client, settings, text)
    if decided is None:
        return None
    kind, query = decided
    try:
        digest, sources = await _search_web(client, settings, query)
    except Exception:
        logger.exception("网页检索失败")
        return _uncertain_note(query)
    # 考纲和分数线必须能指出来源。只有摘录、没有出处，仍算没查到。
    if kind in CITE_KINDS and (not digest or not sources):
        logger.warning("网页检索没有可引用的来源")
        return _uncertain_note(query)
    if not digest and not sources:
        return _uncertain_note(query)
    return _result_note(query, digest, sources)


def _uncertain_note(query: str) -> str:
    return "\n".join([UNCERTAIN_PROMPT, "", f"问：{query}"])


def _result_note(query: str, digest: str, sources: list[tuple[str, str]]) -> str:
    lines = [NOTE_PROMPT, "", f"问：{query}"]
    if digest:
        lines.extend(["", "摘录：", digest])
    if sources:
        lines.append("")
        lines.append("来源：")
        lines.extend(f"{index}. {title} {url}" for index, (title, url) in enumerate(sources, start=1))
    return "\n".join(lines)


async def _search_query(
    client: httpx.AsyncClient,
    settings: Settings,
    user_text: str,
) -> tuple[str, str] | None:
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
    return kind, query[:200]


def _multimodal_url(base: str) -> str:
    """兼容接口不返回来源。这个模型走文本生成会报地址错误，所以改走多模态接口。"""
    root = base.rstrip("/")
    marker = "/compatible-mode/v1"
    if root.endswith(marker):
        root = root[: -len(marker)]
    elif root.endswith("/v1"):
        root = root[:-3]
    return root + "/api/v1/services/aigc/multimodal-generation/generation"


async def _search_web(
    client: httpx.AsyncClient,
    settings: Settings,
    query: str,
) -> tuple[str, list[tuple[str, str]]]:
    """多模态模型联网必须流式，否则接口不检索。来源在最后一段的 search_info 里。"""
    logger.warning("网页检索 %s", query)
    async with client.stream(
        "POST",
        _multimodal_url(settings.llm_base_url),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
            "X-DashScope-SSE": "enable",
        },
        json={
            "model": settings.extractor_model,
            "input": {
                "messages": [
                    {"role": "system", "content": [{"text": DIGEST_PROMPT}]},
                    {"role": "user", "content": [{"text": query}]},
                ]
            },
            "parameters": {
                # 思考过程会把摘录挤掉，来源也不在思考里。
                "enable_thinking": False,
                "enable_search": True,
                "search_options": {
                    "forced_search": True,
                    "enable_source": True,
                    "search_strategy": SEARCH_STRATEGY,
                },
                "result_format": "message",
                "incremental_output": True,
            },
        },
    ) as response:
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"网页检索返回 {response.status_code}: {detail}")
        digest, sources = await _read_search_stream(response)
    digest = digest[:MAX_DIGEST_CHARS].strip()
    logger.warning("网页检索完成 摘录%s 条目%s", "有" if digest else "无", len(sources))
    return digest, sources


async def _read_search_stream(response: httpx.Response) -> tuple[str, list[tuple[str, str]]]:
    """增量流要把文字拼起来。来源列表通常只在最后一段给全。"""
    parts: list[str] = []
    sources: list[tuple[str, str]] = []
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise RuntimeError("网页检索没有返回结果")
        if payload.get("code"):
            message = str(payload.get("message") or payload["code"])
            raise RuntimeError(f"网页检索失败: {message[:500]}")
        output = payload.get("output") or {}
        parts.extend(_content_parts(output))
        found = _sources(output)
        if found:
            sources = found
    return "".join(parts), sources


def _content_parts(output: dict) -> list[str]:
    choices = output.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("text")]
    return []


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
