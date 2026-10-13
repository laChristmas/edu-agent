from uuid import UUID

import asyncpg


async def list_mistakes(pool: asyncpg.Pool, user_id: UUID) -> list[dict]:
    """读出这个用户的错题，带上知识点。这里只给页面查看，不改分数。"""
    rows = await pool.fetch(
        """
        SELECT
            m.id,
            m.raw_text,
            m.stem,
            m.student_answer,
            m.reference_answer,
            m.error_type,
            m.subject,
            m.status,
            m.source,
            m.created_at,
            COALESCE(
                array_agg(k.name ORDER BY k.name) FILTER (WHERE k.name IS NOT NULL),
                ARRAY[]::text[]
            ) AS knowledge_points
        FROM mistakes m
        LEFT JOIN mistake_points mp
            ON mp.mistake_id = m.id AND mp.user_id = m.user_id
        LEFT JOIN knowledge_points k
            ON k.id = mp.knowledge_point_id AND k.user_id = m.user_id
        WHERE m.user_id = $1
        GROUP BY m.id
        ORDER BY m.created_at DESC
        """,
        user_id,
    )
    return [
        {
            "id": str(row["id"]),
            "raw_text": row["raw_text"],
            "stem": row["stem"],
            "student_answer": row["student_answer"],
            "reference_answer": row["reference_answer"],
            "error_type": row["error_type"],
            "subject": row["subject"],
            "status": row["status"],
            "source": row["source"],
            "knowledge_points": list(row["knowledge_points"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
