import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"

with _CONFIG_PATH.open("rb") as config_file:
    _RULES = tomllib.load(config_file)

RECENT_ROUNDS = int(_RULES["recent_rounds"])
TUTOR_PROMPT = str(_RULES["tutor_prompt"]).strip()
SNAPSHOT_PROMPT = str(_RULES["snapshot_prompt"]).strip()
ARCHIVE_PROMPT = str(_RULES["archive_prompt"]).strip()
IDENTITY_ASK_KEYS = tuple(str(item) for item in _RULES["identity_ask_keys"])
IDENTITY_ASK_ROUNDS = int(_RULES["identity_ask_rounds"])
IDENTITY_ASK_PROMPT = str(_RULES["identity_ask_prompt"]).strip()
IDENTITY_DONE_PROMPT = str(_RULES["identity_done_prompt"]).strip()
_DAILY = _RULES["daily"]
GREETING_ASK_PROMPT = str(_DAILY["greeting_ask_prompt"]).strip()
GREETING_KNOWN_PROMPT = str(_DAILY["greeting_known_prompt"]).strip()
GREETING_ASK_LINES = {str(key): str(value) for key, value in _DAILY["greeting_ask_lines"].items()}


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


# 用上一轮回复里的说法判断问过没有。档案还空着时，模型自己经常把同一句又问一遍。
_ASKED_MARKS = {
    "年级": ("几年级", "哪个年级"),
    "姓名": ("怎么称呼", "称呼你", "叫你什么", "你的名字"),
    "年龄": ("今年多大", "多大了", "几岁"),
    "性别": ("男生还是女生", "男孩还是女孩"),
    "身高": ("大概多高", "身高多少", "你的身高"),
    "体重": ("体重大概", "体重多少", "你的体重", "多少公斤", "多少斤"),
}
_ASK_HOW = {
    "年级": "问现在读几年级。",
    "姓名": "问可以怎么称呼。不要把年级当成名字，也不要用「本地用户」当名字。",
    "年龄": "问今年多大。",
    "性别": "轻轻问男生还是女生。不要写成填写性别。",
    "身高": "问大概多高。",
    "体重": "问体重大概多少。不要写成填表。",
}


def _already_asked(history: list[dict[str, str]], key: str) -> bool:
    marks = _ASKED_MARKS[key]
    for message in history:
        if message.get("role") != "assistant":
            continue
        text = str(message.get("content") or "")
        if any(mark in text for mark in marks):
            return True
    return False


def missing_identity(archive: list[dict[str, str]] | None, history: list[dict[str, str]]) -> list[str]:
    """按年级、姓名、年龄、性别、身高、体重的顺序，列出还没有、也还没问过的身份。"""
    known = {
        fact["key"]
        for fact in archive or []
        if fact.get("dimension") == "身份" and str(fact.get("value") or "").strip()
    }
    return [key for key in IDENTITY_ASK_KEYS if key not in known and not _already_asked(history, key)]


def identity_note(archive: list[dict[str, str]] | None, history: list[dict[str, str]]) -> str:
    """前五轮里每轮最多准备一件还没问过的身份。过了这几轮，或要问的都问过了，就不再问。"""
    rounds = sum(1 for message in history if message.get("role") == "user")
    pending = missing_identity(archive, history)
    if rounds < 1 or rounds > IDENTITY_ASK_ROUNDS or not pending:
        return IDENTITY_DONE_PROMPT
    item = pending[0]
    return IDENTITY_ASK_PROMPT.format(round=rounds, limit=IDENTITY_ASK_ROUNDS, item=item, how=_ASK_HOW[item])


def greeting_identity_note(archive: list[dict[str, str]] | None) -> str:
    """招呼里只问还缺的第一件。后面的对话看得到这句，就不会再问同一件。"""
    pending = missing_identity(archive, [])
    if not pending:
        return GREETING_KNOWN_PROMPT
    item = pending[0]
    return GREETING_ASK_PROMPT.format(item=item, how=_ASK_HOW[item])


def ensure_greeting_asks(text: str, archive: list[dict[str, str]] | None) -> str:
    """模型没把该问的那一句写上时补在末尾。已经问过就不再加。"""
    pending = missing_identity(archive, [])
    if not pending:
        return text
    item = pending[0]
    if _already_asked([{"role": "assistant", "content": text}], item):
        return text
    body = text.rstrip()
    if body and body[-1] not in "。！？":
        body += "。"
    return body + GREETING_ASK_LINES[item]


def tutor_messages(
    history: list[dict[str, str]],
    facts: list[dict[str, str]] | None = None,
    archive: list[dict[str, str]] | None = None,
    *,
    identity_note: str | None = None,
    mistake_note: str | None = None,
    web_note: str | None = None,
    job_note: str | None = None,
) -> list[dict[str, str]]:
    """家教提示放在最前，并附上讲法画像和可口述的档案。这段提示不写入会话表。"""
    parts = [TUTOR_PROMPT]
    if facts:
        parts.append(f"{SNAPSHOT_PROMPT}\n{_fact_lines(facts)}")
    if archive is not None:
        parts.append(f"{ARCHIVE_PROMPT}\n{_fact_lines(archive)}")
    # 检索结果放在最后，避免家教按默认讲法把多道题展开。
    if mistake_note:
        parts.append(mistake_note)
    # 没有时效问题时不附。材料只约束会过时的事实，不改讲题。
    if web_note:
        parts.append(web_note)
    # 周计划还在后台排。这一轮只说稍后补一句，不把还没排好的安排说出来。
    if job_note:
        parts.append(job_note)
    # 放在最后。问的时候不会被讲法盖住；不问的时候也不会被讲题带去再问一遍。
    if identity_note:
        parts.append(identity_note)
    return [{"role": "system", "content": "\n\n".join(parts)}, *recent_rounds(history)]
