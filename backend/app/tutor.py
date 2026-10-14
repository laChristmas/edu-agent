import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"

with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

RECENT_ROUNDS = int(_RULES["recent_rounds"])
TUTOR_PROMPT = str(_RULES["tutor_prompt"]).strip()
SNAPSHOT_PROMPT = str(_RULES["snapshot_prompt"]).strip()
ARCHIVE_PROMPT = str(_RULES["archive_prompt"]).strip()
GRADE_ASK_PROMPT = str(_RULES["grade_ask_prompt"]).strip()
GRADE_KNOWN_PROMPT = str(_RULES["grade_known_prompt"]).strip()


def recent_rounds(messages: list[dict[str, str]], limit: int = RECENT_ROUNDS) -> list[dict[str, str]]:
    """保留最近若干轮。一轮从一条用户消息算起，包含它后面的助手回复。"""
    user_indexes = [index for index, message in enumerate(messages) if message["role"] == "user"]
    if len(user_indexes) <= limit:
        return messages
    return messages[user_indexes[-limit] :]


def _fact_lines(facts: list[dict[str, str]]) -> str:
    if not facts:
        return "（无）"
    return "\n".join(f"{fact['dimension']}：{fact['key']}={fact['value']}" for fact in facts)


def tutor_messages(
    history: list[dict[str, str]],
    facts: list[dict[str, str]] | None = None,
    archive: list[dict[str, str]] | None = None,
    *,
    ask_grade: bool = False,
    mistake_note: str | None = None,
) -> list[dict[str, str]]:
    """家教提示放在最前，并附上讲法画像和可口述的档案。这段提示不写入会话表。"""
    parts = [TUTOR_PROMPT]
    if facts:
        parts.append(f"{SNAPSHOT_PROMPT}\n{_fact_lines(facts)}")
    if archive is not None:
        parts.append(f"{ARCHIVE_PROMPT}\n{_fact_lines(archive)}")
    parts.append(GRADE_ASK_PROMPT if ask_grade else GRADE_KNOWN_PROMPT)
    # 检索结果放在最后，避免家教按默认讲法把多道题展开。
    if mistake_note:
        parts.append(mistake_note)
    return [{"role": "system", "content": "\n\n".join(parts)}, *recent_rounds(history)]
