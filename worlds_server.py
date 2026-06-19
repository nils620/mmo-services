import os
import uuid
import base64
import binascii
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import boto3
from botocore.client import Config
import psycopg

router = APIRouter()

# ── env ──────────────────────────────────────────────────────────────────────
DB_DSN = os.environ.get("DB_DSN")
SPACES_KEY = os.environ.get("SPACES_KEY")
SPACES_SECRET = os.environ.get("SPACES_SECRET")
SPACES_BUCKET = os.environ.get("SPACES_BUCKET", "content-server")
SPACES_REGION = os.environ.get("SPACES_REGION", "fra1")
SPACES_ENDPOINT = f"https://{SPACES_REGION}.digitaloceanspaces.com"
CDN_BASE = os.environ.get(
    "CDN_BASE",
    f"https://{SPACES_BUCKET}.{SPACES_REGION}.cdn.digitaloceanspaces.com"
)

# ── limits ───────────────────────────────────────────────────────────────────
MAX_WORLD_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_THUMB_BYTES = 2 * 1024 * 1024  # 2 MB
PAGE_SIZE = 20


# ── helpers ──────────────────────────────────────────────────────────────────
def db():
    return psycopg.connect(DB_DSN)


def s3():
    return boto3.client(
        "s3",
        region_name=SPACES_REGION,
        endpoint_url=SPACES_ENDPOINT,
        aws_access_key_id=SPACES_KEY,
        aws_secret_access_key=SPACES_SECRET,
        config=Config(signature_version="s3v4"),
    )


def _assert_owns_world(cur, world_id: str, player_id: str):
    cur.execute(
        "SELECT 1 FROM worlds WHERE id = %s AND player_id = %s;",
        (world_id, player_id),
    )
    if cur.fetchone() is None:
        raise HTTPException(status_code=403, detail="World not found or not owned by player")


def _decode_base64(value: str, field_name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} is not valid base64")


class UploadWorldRequest(BaseModel):
    player_id: str
    title: str
    description: Optional[str] = ""
    world_data: str  # raw serialized world string (your existing save format), required
    thumbnail_base64: Optional[str] = None  # base64-encoded .png bytes, optional — binary data only
    world_id: Optional[str] = None  # pass back the world_id from a previous upload to update it in place


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/upload")
def upload_world(req: UploadWorldRequest):
    title = req.title.strip()
    description = (req.description or "").strip()

    if not title:
        raise HTTPException(status_code=400, detail="Title is empty")
    if len(title) > 64:
        raise HTTPException(status_code=400, detail="Title too long (max 64)")
    if len(description) > 500:
        raise HTTPException(status_code=400, detail="Description too long (max 500)")
    if not req.world_data:
        raise HTTPException(status_code=400, detail="world_data is empty")

    world_bytes = req.world_data.encode("utf-8")
    if len(world_bytes) > MAX_WORLD_BYTES:
        raise HTTPException(status_code=400, detail="World file too large (max 5 MB)")

    thumb_data = None
    if req.thumbnail_base64:
        thumb_data = _decode_base64(req.thumbnail_base64, "thumbnail_base64")
        if len(thumb_data) > MAX_THUMB_BYTES:
            raise HTTPException(status_code=400, detail="Thumbnail too large (max 2 MB)")

    is_update = bool(req.world_id)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM players WHERE id = %s;", (req.player_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Player not found")

            if is_update:
                cur.execute(
                    "SELECT world_key, thumb_key FROM worlds WHERE id = %s AND player_id = %s;",
                    (req.world_id, req.player_id),
                )
                existing = cur.fetchone()
                if existing is None:
                    raise HTTPException(status_code=403, detail="World not found or not owned by player")
                world_id, existing_thumb_key = req.world_id, existing[1]
                world_key = existing[0]
            else:
                world_id = str(uuid.uuid4())
                world_key = f"worlds/{req.player_id}/{world_id}.world"
                existing_thumb_key = None

    # only generate a new thumb key if new thumbnail data was actually sent —
    # otherwise keep whatever thumbnail (or lack of one) the world already had
    thumb_key = existing_thumb_key
    if thumb_data:
        thumb_key = f"worlds/{req.player_id}/{world_id}.png"
    thumb_url = f"{CDN_BASE}/{thumb_key}" if thumb_key else None

    # upload to Spaces — world file always overwritten, thumbnail only if provided
    client = s3()
    client.put_object(
        Bucket=SPACES_BUCKET, Key=world_key,
        Body=world_bytes, ContentType="application/json", ACL="public-read",
    )
    if thumb_data:
        client.put_object(
            Bucket=SPACES_BUCKET, Key=thumb_key,
            Body=thumb_data, ContentType="image/png", ACL="public-read",
        )

    with db() as conn:
        with conn.cursor() as cur:
            if is_update:
                cur.execute(
                    """
                    UPDATE worlds
                    SET title = %s, description = %s, thumb_key = %s, thumbnail_url = %s, updated_at = now()
                    WHERE id = %s AND player_id = %s
                    RETURNING id, created_at, updated_at;
                    """,
                    (title, description, thumb_key, thumb_url, world_id, req.player_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO worlds (id, player_id, title, description, world_key, thumb_key, thumbnail_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at, updated_at;
                    """,
                    (world_id, req.player_id, title, description, world_key, thumb_key, thumb_url),
                )
            row = cur.fetchone()

    return {
        "ok": True,
        "world_id": str(row[0]),
        "thumbnail_url": thumb_url,
        "created_at": row[1].isoformat(),
        "updated_at": row[2].isoformat(),
        "is_update": is_update,
    }


@router.get("")
def list_worlds(page: int = 1):
    offset = (max(1, page) - 1) * PAGE_SIZE
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.title, w.description, w.thumbnail_url,
                       w.download_count, w.created_at, w.updated_at, p.provider_id AS steam_id
                FROM worlds w
                JOIN players p ON p.id = w.player_id
                ORDER BY w.created_at DESC
                LIMIT %s OFFSET %s;
                """,
                (PAGE_SIZE, offset),
            )
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM worlds;")
            total = cur.fetchone()[0]

    return {
        "page": page,
        "total": total,
        "worlds": [
            {
                "world_id": str(r[0]),
                "title": r[1],
                "description": r[2] or "",
                "thumbnail_url": r[3],
                "download_count": r[4],
                "created_at": r[5].isoformat(),
                "updated_at": r[6].isoformat(),
                "steam_id": r[7],
            }
            for r in rows
        ],
    }


@router.get("/{world_id}/download")
def download_world(world_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE worlds
                SET download_count = download_count + 1
                WHERE id = %s
                RETURNING world_key;
                """,
                (world_id,),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="World not found")

    return RedirectResponse(url=f"{CDN_BASE}/{row[0]}")


@router.delete("/{world_id}")
def delete_world(world_id: str, player_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            _assert_owns_world(cur, world_id, player_id)
            cur.execute(
                "SELECT world_key, thumb_key FROM worlds WHERE id = %s;",
                (world_id,),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="World not found")

    world_key, thumb_key = row

    client = s3()
    client.delete_object(Bucket=SPACES_BUCKET, Key=world_key)
    if thumb_key:
        client.delete_object(Bucket=SPACES_BUCKET, Key=thumb_key)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM worlds WHERE id = %s AND player_id = %s;",
                (world_id, player_id),
            )

    return {"ok": True, "world_id": world_id}