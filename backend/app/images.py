import asyncio
import tomllib
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException

from app.chat import create_conversation, require_conversation
from app.db import LOCAL_USER_ID

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"
with _CONFIG_PATH.open("rb") as config_file:
    _IMAGES = tomllib.load(config_file)["images"]

MAX_BYTES = int(_IMAGES["max_bytes"])
MEDIA_TYPES = frozenset(str(item) for item in _IMAGES["media_types"])
PHOTO_NOTE = str(_IMAGES["photo_note"]).strip()

# 扩展名由识别出的类型决定，不用上传时的文件名，避免路径被带着跑。
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_ROOT = Path(__file__).resolve().parents[2] / "data" / "images"


def image_file(image_id: UUID, media_type: str) -> Path:
    """照片只放在数据目录里。编号和类型对不上时不给读。"""
    extension = _EXTENSIONS.get(media_type)
    if extension is None:
        raise HTTPException(status_code=404, detail="照片不存在")
    root = _ROOT.resolve()
    path = (root / f"{image_id}{extension}").resolve()
    if path.parent != root:
        raise HTTPException(status_code=404, detail="照片不存在")
    return path


def _media_type(data: bytes) -> str | None:
    """只认文件头。浏览器报的类型可以改，不能拿来决定存成什么。"""
    if data.startswith(b"\xff\xd8\xff"):
        found = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        found = "image/png"
    elif len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        found = "image/webp"
    else:
        return None
    if found not in MEDIA_TYPES:
        return None
    return found


async def save_image(
    pool: asyncpg.Pool,
    *,
    conversation_id: UUID | None,
    data: bytes,
) -> tuple[UUID, UUID, str]:
    """把照片挂到这个用户和一段会话上。还没发到某一句时，消息编号先空着。"""
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="照片格式或大小不符合")
    media_type = _media_type(data)
    if media_type is None:
        raise HTTPException(status_code=400, detail="照片格式或大小不符合")
    if conversation_id is None:
        conversation_id = await create_conversation(pool)
    else:
        await require_conversation(pool, conversation_id)
    image_id = uuid4()
    path = image_file(image_id, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    try:
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO conversation_images (
                    id, user_id, conversation_id, media_type, byte_size
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                image_id,
                LOCAL_USER_ID,
                conversation_id,
                media_type,
                len(data),
            )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return image_id, conversation_id, media_type


async def require_pending_image(
    pool: asyncpg.Pool,
    image_id: UUID,
    conversation_id: UUID,
) -> None:
    """只接受这段会话里还没挂到某一句上的照片。"""
    async with pool.acquire() as connection:
        pending = await connection.fetchval(
            """
            SELECT 1 FROM conversation_images
            WHERE id = $1 AND user_id = $2 AND conversation_id = $3 AND message_id IS NULL
            """,
            image_id,
            LOCAL_USER_ID,
            conversation_id,
        )
    if pending is None:
        raise HTTPException(status_code=404, detail="照片不在这段会话里")


async def attach_image(
    pool: asyncpg.Pool,
    image_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
) -> None:
    """这句话发出去之后，照片才算挂在这一句上。"""
    async with pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE conversation_images
            SET message_id = $4
            WHERE id = $1 AND user_id = $2 AND conversation_id = $3 AND message_id IS NULL
            """,
            image_id,
            LOCAL_USER_ID,
            conversation_id,
            message_id,
        )
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail="照片不在这段会话里")


async def open_image(pool: asyncpg.Pool, image_id: UUID) -> tuple[Path, str]:
    """按编号取出这个用户的照片。不存在的编号不暴露文件位置。"""
    async with pool.acquire() as connection:
        media_type = await connection.fetchval(
            """
            SELECT media_type FROM conversation_images
            WHERE id = $1 AND user_id = $2
            """,
            image_id,
            LOCAL_USER_ID,
        )
    if media_type is None:
        raise HTTPException(status_code=404, detail="照片不存在")
    path = image_file(image_id, str(media_type))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="照片不存在")
    return path, str(media_type)
