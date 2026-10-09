import asyncpg

from app.config import Settings


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
