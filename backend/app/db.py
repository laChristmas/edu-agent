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
"""


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """建会话相关的表，并保证本地用户只有一行。"""
    async with pool.acquire() as connection:
        await connection.execute(SCHEMA)
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
