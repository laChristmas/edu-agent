from uuid import UUID

import asyncpg

from app.config import Settings


LOCAL_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

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


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """建会话和画像表，并保证本地用户只有一行。"""
    async with pool.acquire() as connection:
        await connection.execute(SCHEMA)
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
