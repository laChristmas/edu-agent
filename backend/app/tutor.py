import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"

with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

RECENT_ROUNDS = int(_RULES["recent_rounds"])
TUTOR_PROMPT = str(_RULES["tutor_prompt"]).strip()


def recent_rounds(messages: list[dict[str, str]], limit: int = RECENT_ROUNDS) -> list[dict[str, str]]:
    """保留最近若干轮。一轮从一条用户消息算起，包含它后面的助手回复。"""
    user_indexes = [index for index, message in enumerate(messages) if message["role"] == "user"]
    if len(user_indexes) <= limit:
        return messages
    return messages[user_indexes[-limit] :]


def tutor_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """家教提示放在最前，不写入会话表。"""
    return [{"role": "system", "content": TUTOR_PROMPT}, *recent_rounds(history)]
