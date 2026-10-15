import tomllib
from pathlib import Path
from uuid import UUID

import asyncpg

from app.config import Settings


LOCAL_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _MISTAKES = tomllib.load(config_file)["mistakes"]


def _sql_texts(values: list[str]) -> str:
    return ", ".join("'" + item.replace("'", "''") + "'" for item in values)


def _configured_text(name: str, allowed: list[str]) -> str:
    value = str(_MISTAKES[name])
    if value not in allowed:
        raise RuntimeError(f"{name} 不在允许的取值里")
    return value.replace("'", "''")


_ERROR_TYPES = list(_MISTAKES["error_types"])
_STATUSES = list(_MISTAKES["statuses"])
_SOURCES = list(_MISTAKES["sources"])
_DEFAULT_STATUS = _configured_text("default_status", _STATUSES)
_DEFAULT_SOURCE = _configured_text("default_source", _SOURCES)

# 第 1 期界面只有本地单账号，表结构仍按多用户来建。
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY,
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversations (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES conversations (id),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation_created_idx
    ON messages (conversation_id, created_at);

-- 旧行留着，标成失效，方便以后解释当时为什么这么讲。
-- 同一用户、同一维度、同一个键，同时只能有一行有效。
CREATE TABLE IF NOT EXISTS profile_facts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    dimension text NOT NULL CHECK (dimension IN ('身份', '目标', '兴趣', '偏好', '学情')),
    fact_key text NOT NULL CHECK (length(btrim(fact_key)) > 0),
    value text NOT NULL CHECK (length(btrim(value)) > 0),
    confidence numeric(3, 2) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source_message_id uuid REFERENCES messages (id),
    status text NOT NULL CHECK (status IN ('有效', '失效', '已结束')),
    ends_on date,
    created_at timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS profile_facts_active_key_idx
    ON profile_facts (user_id, dimension, fact_key)
    WHERE status = '有效';
"""

# 错题、知识点和掌握度都挂在用户上。关联表只负责把同一用户的错题和知识点接起来。
# 向量列不规定维度。写入时按配置的长度补题干向量，检索索引留到后面。
MISTAKE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS knowledge_points (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, user_id),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS mistakes (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    raw_text text NOT NULL CHECK (length(btrim(raw_text)) > 0),
    stem text,
    student_answer text,
    reference_answer text,
    error_type text CHECK (error_type IS NULL OR error_type IN ({_sql_texts(_ERROR_TYPES)})),
    subject text,
    embedding vector,
    status text NOT NULL DEFAULT '{_DEFAULT_STATUS}' CHECK (status IN ({_sql_texts(_STATUSES)})),
    source text NOT NULL DEFAULT '{_DEFAULT_SOURCE}' CHECK (source IN ({_sql_texts(_SOURCES)})),
    next_review_at timestamptz,
    source_message_id uuid REFERENCES messages (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, user_id)
);

CREATE INDEX IF NOT EXISTS mistakes_user_created_idx
    ON mistakes (user_id, created_at);

CREATE TABLE IF NOT EXISTS mastery (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    knowledge_point_id uuid NOT NULL,
    score numeric(3, 2) CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    interval_days integer CHECK (interval_days IS NULL OR interval_days >= 0),
    next_review_on date,
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    last_evidence_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, knowledge_point_id),
    FOREIGN KEY (knowledge_point_id, user_id)
        REFERENCES knowledge_points (id, user_id)
);

CREATE TABLE IF NOT EXISTS mistake_points (
    mistake_id uuid NOT NULL,
    knowledge_point_id uuid NOT NULL,
    user_id uuid NOT NULL,
    PRIMARY KEY (mistake_id, knowledge_point_id),
    FOREIGN KEY (mistake_id, user_id) REFERENCES mistakes (id, user_id),
    FOREIGN KEY (knowledge_point_id, user_id) REFERENCES knowledge_points (id, user_id)
);
"""


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """建会话、画像、错题相关的表，并保证本地用户只有一行。"""
    async with pool.acquire() as connection:
        await connection.execute(SCHEMA)
        await connection.execute(MISTAKE_SCHEMA)
        # 已有库补上考试日期，并把「已结束」放进状态约束。
        await connection.execute("ALTER TABLE profile_facts ADD COLUMN IF NOT EXISTS ends_on date")
        await connection.execute(
            "ALTER TABLE profile_facts DROP CONSTRAINT IF EXISTS profile_facts_status_check"
        )
        await connection.execute(
            """
            ALTER TABLE profile_facts ADD CONSTRAINT profile_facts_status_check
            CHECK (status IN ('有效', '失效', '已结束'))
            """
        )
        await connection.execute(
            """
            INSERT INTO users (id, display_name)
            VALUES ($1, '本地用户')
            ON CONFLICT (id) DO NOTHING
            """,
            LOCAL_USER_ID,
        )
        # 多题先留一个顺序，下一句点名时才知道「第二道」是哪条。删错题或会话时一起去掉。
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mistake_list_items (
                conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
                position integer NOT NULL CHECK (position >= 1),
                mistake_id uuid NOT NULL,
                user_id uuid NOT NULL,
                PRIMARY KEY (conversation_id, position),
                FOREIGN KEY (mistake_id, user_id) REFERENCES mistakes (id, user_id) ON DELETE CASCADE
            )
            """
        )
        # 标成会了留下结果。间隔由掌握度上的 SM-2 决定，不在这张表里重算。
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mistake_reviews (
                id uuid PRIMARY KEY,
                user_id uuid NOT NULL,
                mistake_id uuid NOT NULL,
                outcome text NOT NULL CHECK (outcome IN ('又忘了', '勉强会', '会了', '很熟')),
                source_message_id uuid REFERENCES messages (id) ON DELETE SET NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                FOREIGN KEY (mistake_id, user_id) REFERENCES mistakes (id, user_id) ON DELETE CASCADE
            )
            """
        )
        # 已有掌握度补上难易系数。新错题还没复习过时用默认 2.5。
        await connection.execute(
            "ALTER TABLE mastery ADD COLUMN IF NOT EXISTS ease numeric(4, 2) NOT NULL DEFAULT 2.50"
        )
        await connection.execute("ALTER TABLE mastery DROP CONSTRAINT IF EXISTS mastery_ease_check")
        await connection.execute(
            """
            ALTER TABLE mastery ADD CONSTRAINT mastery_ease_check
            CHECK (ease >= 1.3)
            """
        )
        # 简报和普通回复要能分开。再次打开时只替换还没被回复的那一条。
        await connection.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'chat'"
        )
        await connection.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_kind_check")
        await connection.execute(
            """
            ALTER TABLE messages ADD CONSTRAINT messages_kind_check
            CHECK (kind IN ('chat', 'briefing'))
            """
        )
        # 照片挂在用户和会话上。只有记错题时才另写错题行，收到照片本身不入库成题。删消息时把挂在那一句上的照片行去掉。
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_images (
                id uuid PRIMARY KEY,
                user_id uuid NOT NULL REFERENCES users (id),
                conversation_id uuid NOT NULL REFERENCES conversations (id),
                message_id uuid REFERENCES messages (id) ON DELETE CASCADE,
                media_type text NOT NULL,
                byte_size integer NOT NULL CHECK (byte_size > 0),
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS conversation_images_conversation_idx
            ON conversation_images (conversation_id, created_at)
            """
        )
        # 简报上的条目留住，学生说开始或点名时才知道讲哪一件。
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_items (
                message_id uuid NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
                position integer NOT NULL CHECK (position >= 1),
                user_id uuid NOT NULL,
                mistake_id uuid,
                point_name text,
                PRIMARY KEY (message_id, position),
                CHECK (
                    (mistake_id IS NOT NULL AND point_name IS NULL)
                    OR (
                        mistake_id IS NULL
                        AND point_name IS NOT NULL
                        AND length(btrim(point_name)) > 0
                    )
                ),
                FOREIGN KEY (mistake_id, user_id) REFERENCES mistakes (id, user_id) ON DELETE CASCADE
            )
            """
        )


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """创建一个进程内的异步连接池，并确保 pgvector 扩展存在。

    第 1 期只有一个 API 进程，池子保持很小。扩展语句可重复执行。
    """
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    async with pool.acquire() as connection:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return pool


async def pgvector_version(pool: asyncpg.Pool) -> str:
    """返回已安装的 pgvector 版本，供健康检查确认数据库可用。"""
    async with pool.acquire() as connection:
        version = await connection.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
    if version is None:
        raise RuntimeError("pgvector extension is not installed")
    return version
