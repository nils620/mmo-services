"""
worlds_server.py — POTENTIAL UGC world sharing. Mounted under /worlds.

Ownership is hybrid, because money and identity in this stack are
character-level but a world outlives any one character:

    player_id     account-level owner. Permissions, listing cap, "Your Worlds".
                  Survives character deletion.
    character_id  the credited author and the payout target. Nullable, and
                  repointable by the owner. This is also where the author name
                  in the browser comes from — `players` has no display name.

All money goes through economy._move_money, so world sales land in the same
`transactions` ledger as transfers and grants. There is no separate wallet and
no treasury table: the marketplace fee is a burn (to_character_id = NULL) and
the pot is a SUM over the ledger.
"""

import os
import io
import re
import json
import math
import uuid
import base64
import binascii
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from PIL import Image
import boto3
from botocore.client import Config

from auth import get_player_id
from common import (
    db,
    _assert_valid_uuid,
    _assert_character_owned,
    _fetch_character_names,
    notify_character,
)
from economy import (
    _move_money,
    _get_balance,
    REASON_WORLD_PURCHASE,
    REASON_WORLD_ROYALTY,
    REASON_WORLD_FEE,
)

log = logging.getLogger("worlds")
router = APIRouter()


# ── env ──────────────────────────────────────────────────────────────────────
SPACES_KEY = os.environ.get("SPACES_KEY")
SPACES_SECRET = os.environ.get("SPACES_SECRET")
SPACES_BUCKET = os.environ.get("SPACES_BUCKET", "content-server")
SPACES_REGION = os.environ.get("SPACES_REGION", "fra1")
SPACES_ENDPOINT = f"https://{SPACES_REGION}.digitaloceanspaces.com"
CDN_BASE = os.environ.get(
    "CDN_BASE",
    f"https://{SPACES_BUCKET}.{SPACES_REGION}.cdn.digitaloceanspaces.com",
)


# ── tunables ─────────────────────────────────────────────────────────────────
MAX_WORLD_BYTES = 8 * 1024 * 1024
MAX_THUMB_B64_CHARS = 3 * 1024 * 1024      # checked BEFORE decode
MAX_THUMB_BYTES = 2 * 1024 * 1024          # checked after decode
MAX_IMAGE_EDGE = 4096
ASPECT_TOLERANCE = 0.02

CARD_SIZE = (640, 360)
CARD_QUALITY = 85

MAX_LISTED_WORLDS = 50                      # per player, status='ready'
MAX_UPLOADS_PER_HR = 20
PAGE_SIZE = 24
PRESIGN_SECONDS = 300

MARKETPLACE_FEE_PCT = 0.05                  # burned
ROOT_AUTHOR_PCT = 0.10                      # to the original creator
DERIVATIVE_MARKUP = 1.10                    # floor vs ROOT price, not parent

REPORT_HIDE_THRESHOLD = 5

# Reject uploads whose save file will not parse. Flip to False to accept them
# with an empty manifest instead — but then the pre-flight "you're missing N
# assets" check silently does nothing, which is worse than a clear error.
STRICT_MANIFEST = True

Image.MAX_IMAGE_PIXELS = 30_000_000         # decompression bomb guard

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=SPACES_REGION,
            endpoint_url=SPACES_ENDPOINT,
            aws_access_key_id=SPACES_KEY,
            aws_secret_access_key=SPACES_SECRET,
            config=Config(signature_version="s3v4"),
        )
    return _s3_client


# ── keys ─────────────────────────────────────────────────────────────────────
# Versioned, never overwritten. The old scheme reused one key per world, so an
# update left the CDN serving stale bytes while the client's .sync.json thought
# it was current. player_id is deliberately not in the path.
def _world_key(world_id: str, version: int) -> str:
    return f"worlds/{world_id}/v{version}.world"


def _card_key(world_id: str, version: int) -> str:
    return f"worlds/{world_id}/v{version}_card.jpg"

def _cdn(key: Optional[str]) -> Optional[str]:
    return f"{CDN_BASE}/{key}" if key else None


def _presign(key: str) -> str:
    return s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": SPACES_BUCKET, "Key": key},
        ExpiresIn=PRESIGN_SECONDS,
    )

def _image_keys(world_id: str) -> str:
    return f"worlds/{world_id}/img_{uuid.uuid4().hex[:12]}_card.jpg"

# ── row mapping ──────────────────────────────────────────────────────────────
# psycopg here returns tuples, matching profiles_server and the economy
# helpers (_get_balance does row[0]). One column list, one mapper, so column
# order can't drift out from under an index.
WORLD_COLS = """
    w.id, w.player_id, w.character_id, w.title, w.description, w.version,
    w.status, w.world_key, w.card_key, w.price,
    w.allow_derivatives, w.parent_world_id, w.root_world_id,
    w.asset_manifest, w.download_count, w.featured_rank, w.moderation_flag,
    w.created_at, w.updated_at
"""
WORLD_FIELDS = [
    "id", "player_id", "character_id", "title", "description", "version",
    "status", "world_key", "card_key", "price",
    "allow_derivatives", "parent_world_id", "root_world_id",
    "asset_manifest", "download_count", "featured_rank", "moderation_flag",
    "created_at", "updated_at",
]
WORLD_COL_COUNT = len(WORLD_FIELDS)          # 20 — used by ORDER BY ordinals


def _world(row, extra: tuple = ()) -> dict:
    return dict(zip(WORLD_FIELDS + list(extra), row))


def _card(w: dict) -> dict:
    """Shape every grid view binds to."""
    return {
        "world_id": str(w["id"]),
        "title": w["title"],
        "author_character_id": str(w["character_id"]) if w["character_id"] else "",
        "author_name": w.get("author_name") or "",
        "thumbnail_url": _cdn(w["card_key"]),
        "price": int(w["price"]),
        "version": w["version"],
        "download_count": w["download_count"],
        "allow_derivatives": w["allow_derivatives"],
        "is_derivative": w["root_world_id"] is not None,
        "updated_at": w["updated_at"].isoformat(),
    }


# ── asset manifest ───────────────────────────────────────────────────────────
# Derived server-side from the save file. The client sends nothing: it can't
# be spoofed, and every world can be re-scanned if the rules change.
_REF_RE = re.compile(r"^[A-Za-z0-9_.]+'(?P<path>[^']+)'$")


def _normalize_ref(value) -> Optional[str]:
    """
    /Script/Engine.BlueprintGeneratedClass'/Game/.../BP_X.BP_X_C'
        -> /Game/.../BP_X.BP_X_C
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    m = _REF_RE.match(value)
    path = m.group("path") if m else value
    return path if path.startswith("/") else None


def _extract_manifest(world_data: str) -> dict:
    """
    Walks the save and collects everything the world depends on.

    Materials matter as much as actors: a player can override a material per
    slot, so a world can depend on an asset from a pack the downloader does
    not have even when every actor resolves cleanly.

    `meshComponent` is deliberately NOT collected — those are PIE instance
    paths (UEDPIE_0_L_Basic...), not asset references.
    """
    try:
        data = json.loads(world_data)
    except (ValueError, TypeError):
        if STRICT_MANIFEST:
            raise HTTPException(400, "world_data is not valid JSON")
        return {"actors": [], "materials": []}

    actors, materials = set(), set()

    def walk(node, depth=0):
        if depth > 32:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "actorToSpawn":
                    ref = _normalize_ref(value)
                    if ref:
                        actors.add(ref)
                elif key == "material":
                    ref = _normalize_ref(value)
                    if ref:
                        materials.add(ref)
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data)
    return {"actors": sorted(actors), "materials": sorted(materials)}


# ── image pipeline ───────────────────────────────────────────────────────────
def _process_thumbnail(b64: str) -> bytes:
    """
    Validate and RE-ENCODE an uploaded screenshot.

    The re-encode is the security boundary, not the format check: whatever the
    client sent is decoded, resampled and written back out as fresh JPEG bytes,
    so nothing from the original file survives into the bucket. EXIF, colour
    profiles, appended payloads and alpha all go with it.
    """
    if len(b64) > MAX_THUMB_B64_CHARS:
        raise HTTPException(400, "Thumbnail payload too large")

    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "thumbnail_base64 is not valid base64")

    if len(raw) > MAX_THUMB_BYTES:
        raise HTTPException(400, "Thumbnail too large (max 2 MB)")

    try:
        img = Image.open(io.BytesIO(raw))          # header only, no pixel decode
    except Exception:
        raise HTTPException(400, "Thumbnail is not a readable image")

    if img.format not in ("PNG", "JPEG"):
        raise HTTPException(400, "Thumbnail must be PNG or JPEG")

    w, h = img.size                                 # checked BEFORE decode
    if w <= 0 or h <= 0 or w > MAX_IMAGE_EDGE or h > MAX_IMAGE_EDGE:
        raise HTTPException(400, "Thumbnail dimensions out of range")
    if abs((w / h) - (16 / 9)) > ASPECT_TOLERANCE:
        raise HTTPException(400, "Thumbnail must be 16:9")

    try:
        img = img.convert("RGB")                    # forces decode, drops alpha
    except Exception:
        raise HTTPException(400, "Thumbnail could not be decoded")

    def encode(size, quality) -> bytes:
        buf = io.BytesIO()
        img.resize(CARD_SIZE, Image.LANCZOS).save(
            buf, format="JPEG", quality=CARD_QUALITY, optimize=True
        )
        return buf.getvalue()

    return encode(CARD_SIZE, CARD_QUALITY)


# ── db helpers ───────────────────────────────────────────────────────────────
def _fetch_world(cur, world_id: str, for_update: bool = False) -> dict:
    cur.execute(
        f"SELECT {WORLD_COLS} FROM worlds w WHERE w.id = %s"
        f"{' FOR UPDATE' if for_update else ''};",
        (world_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "World not found")
    return _world(row)


def _has_access(cur, w: dict, player_id: str) -> bool:
    """Owner, purchaser (account-level), or free."""
    if str(w["player_id"]) == player_id:
        return True
    if int(w["price"]) == 0:
        return True
    cur.execute(
        "SELECT 1 FROM world_purchases WHERE world_id = %s AND buyer_player_id = %s;",
        (w["id"], player_id),
    )
    return cur.fetchone() is not None


def _assert_listing_slot(cur, player_id: str):
    cur.execute(
        "SELECT COUNT(*) FROM worlds WHERE player_id = %s AND status = 'ready';",
        (player_id,),
    )
    if cur.fetchone()[0] >= MAX_LISTED_WORLDS:
        raise HTTPException(
            409,
            f"Upload limit reached ({MAX_LISTED_WORLDS} listed worlds). "
            "Take one down to free a slot.",
        )


def _rate_limit(cur, player_id: str):
    cur.execute(
        "SELECT COUNT(*) FROM worlds "
        "WHERE player_id = %s AND updated_at > now() - interval '1 hour';",
        (player_id,),
    )
    if cur.fetchone()[0] >= MAX_UPLOADS_PER_HR:
        raise HTTPException(429, "Too many uploads, try again later")


def _put_objects(world_bytes, card_bytes, wkey, ckey):
    client = s3()
    if world_bytes and wkey:
        client.put_object(
            Bucket=SPACES_BUCKET, Key=wkey, Body=world_bytes,
            ContentType="application/json", ACL="private",
        )
    if card_bytes and ckey:
        client.put_object(
            Bucket=SPACES_BUCKET, Key=ckey, Body=card_bytes,
            ContentType="image/jpeg", ACL="public-read",
            CacheControl="public, max-age=31536000, immutable",
        )


def _purge_prefix(prefix: str):
    client = s3()
    token = None
    while True:
        kwargs = {"Bucket": SPACES_BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=SPACES_BUCKET, Delete={"Objects": keys})
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")


# ── models ───────────────────────────────────────────────────────────────────
class UploadWorldRequest(BaseModel):
    character_id: str                     # credited author + payout target
    title: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    world_data: str
    thumbnail_base64: Optional[str] = None
    price: int = Field(default=0, ge=0, le=100_000_000)
    allow_derivatives: bool = False
    parent_world_id: Optional[str] = None


class UpdateWorldRequest(BaseModel):
    # omit world_data for a metadata-only edit — no version bump, so other
    # players don't see a spurious "update available" after a rename
    world_data: Optional[str] = None
    thumbnail_base64: Optional[str] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=500)
    price: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    character_id: Optional[str] = None


class PurchaseRequest(BaseModel):
    character_id: str                     # who pays


class ReportRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


# ═════════════════════════════════════════════════════════════════════════════
# BROWSE
# ═════════════════════════════════════════════════════════════════════════════
@router.get("/browse")
def browse_worlds(
    sort: str = Query("new", pattern="^(new|popular|featured)$"),
    q: Optional[str] = Query(None, max_length=64),
    page: int = Query(1, ge=1),
    player_id: str = Depends(get_player_id),
):
    """
    One endpoint behind every community tab. The UI switches `sort`, not the
    endpoint. Offset pagination is fine at this catalogue size — keyset is the
    upgrade when the table gets big enough to notice.
    """
    offset = (page - 1) * PAGE_SIZE
    where = ["w.status = 'ready'", "w.moderation_flag IS DISTINCT FROM 'hidden'"]
    params: list = []

    if q:
        where.append("w.title ILIKE %s")
        params.append(f"%{q}%")

    if sort == "featured":
        where.append("w.featured_rank IS NOT NULL")
        order = "w.featured_rank ASC"
    elif sort == "popular":
        order = """(
            SELECT COUNT(*) FROM world_downloads d
            WHERE d.world_id = w.id AND d.created_at > now() - interval '7 days'
        ) DESC, w.created_at DESC"""
    else:
        order = "w.created_at DESC"

    where_sql = " AND ".join(where)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {WORLD_COLS}, c.character_name
                FROM worlds w
                LEFT JOIN characters c ON c.id = w.character_id
                WHERE {where_sql}
                ORDER BY {order}
                LIMIT %s OFFSET %s;
                """,
                (*params, PAGE_SIZE, offset),
            )
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) FROM worlds w WHERE {where_sql};", tuple(params))
            total = cur.fetchone()[0]

    worlds = [_card(_world(r, ("author_name",))) for r in rows]
    return {
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "has_more": offset + len(worlds) < total,
        "worlds": worlds,
    }


# ═════════════════════════════════════════════════════════════════════════════
# MINE
# ═════════════════════════════════════════════════════════════════════════════
@router.get("/mine")
def my_worlds(player_id: str = Depends(get_player_id)):
    """
    Uploaded + purchased in one list. The client diffs `version` against its
    local .sync.json to derive local-only / synced / local-newer / remote-newer
    / remote-only. Purchases are account-level, so every character of this
    player sees the same library.
    """
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {WORLD_COLS}, c.character_name, 'owner' AS relation
                FROM worlds w
                LEFT JOIN characters c ON c.id = w.character_id
                WHERE w.player_id = %s AND w.status IN ('ready', 'takendown')
                UNION ALL
                SELECT {WORLD_COLS}, c.character_name, 'purchased' AS relation
                FROM worlds w
                LEFT JOIN characters c ON c.id = w.character_id
                JOIN world_purchases wp
                  ON wp.world_id = w.id AND wp.buyer_player_id = %s
                WHERE w.player_id <> %s AND w.status <> 'deleted'
                ORDER BY {WORLD_COL_COUNT} DESC;
                """,
                (player_id, player_id, player_id),
            )
            rows = cur.fetchall()

    out, listed = [], 0
    for r in rows:
        w = _world(r, ("author_name", "relation"))
        item = _card(w)
        item["relation"] = w["relation"]
        item["status"] = w["status"]
        out.append(item)
        if w["relation"] == "owner" and w["status"] == "ready":
            listed += 1

    return {"worlds": out, "listed_count": listed, "listed_limit": MAX_LISTED_WORLDS}


# ═════════════════════════════════════════════════════════════════════════════
# DETAIL
# ═════════════════════════════════════════════════════════════════════════════
@router.get("/{world_id}")
def world_detail(world_id: str, player_id: str = Depends(get_player_id)):
    _assert_valid_uuid(world_id, "world_id")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {WORLD_COLS}, c.character_name
                FROM worlds w
                LEFT JOIN characters c ON c.id = w.character_id
                WHERE w.id = %s;
                """,
                (world_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "World not found")
            w = _world(row, ("author_name",))
            if w["status"] in ("pending", "deleted"):
                raise HTTPException(404, "World not found")

            owned = _has_access(cur, w, player_id)

            root_name, root_price = None, int(w["price"])
            if w["root_world_id"]:
                cur.execute(
                    """
                    SELECT c.character_name, w.price
                    FROM worlds w LEFT JOIN characters c ON c.id = w.character_id
                    WHERE w.id = %s;
                    """,
                    (w["root_world_id"],),
                )
                r = cur.fetchone()
                if r:
                    root_name, root_price = r[0], int(r[1])

    detail = _card(w)
    detail.update({
        "description": w["description"] or "",
        "asset_manifest": w["asset_manifest"],
        "root_world_id": str(w["root_world_id"]) if w["root_world_id"] else "",
        "root_author_name": root_name or "",
        "parent_world_id": str(w["parent_world_id"]) if w["parent_world_id"] else "",
        "status": w["status"],
        "owned": owned,
        "is_author": str(w["player_id"]) == player_id,
        "created_at": w["created_at"].isoformat(),
        # what a derivative of this world would have to cost
        "derivative_price_floor": math.ceil(root_price * DERIVATIVE_MARKUP),
    })
    return detail


# ═════════════════════════════════════════════════════════════════════════════
# UPLOAD
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/upload")
def upload_world(req: UploadWorldRequest, player_id: str = Depends(get_player_id)):
    _assert_valid_uuid(req.character_id, "character_id")
    if req.parent_world_id:
        _assert_valid_uuid(req.parent_world_id, "parent_world_id")

    title = req.title.strip()
    if not title:
        raise HTTPException(400, "Title is empty")

    world_bytes = req.world_data.encode("utf-8")
    if not world_bytes:
        raise HTTPException(400, "world_data is empty")
    if len(world_bytes) > MAX_WORLD_BYTES:
        raise HTTPException(400, "World file too large")

    manifest = _extract_manifest(req.world_data)

    card_bytes =  None
    if req.thumbnail_base64:
        card_bytes = _process_thumbnail(req.thumbnail_base64)

    world_id = str(uuid.uuid4())
    price = req.price
    allow_derivatives = req.allow_derivatives
    root_id = None

    # ── phase 1: validate + reserve the row ──────────────────────────────────
    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, req.character_id, player_id)
            _rate_limit(cur, player_id)
            _assert_listing_slot(cur, player_id)

            if req.parent_world_id:
                parent = _fetch_world(cur, req.parent_world_id)
                if parent["status"] == "deleted":
                    raise HTTPException(404, "Parent world not found")
                if not parent["allow_derivatives"]:
                    raise HTTPException(403, "This world does not allow derivatives")
                if not _has_access(cur, parent, player_id):
                    raise HTTPException(403, "You do not own the parent world")

                root_id = parent["root_world_id"] or parent["id"]

                cur.execute(
                    "SELECT price, allow_derivatives FROM worlds WHERE id = %s;",
                    (root_id,),
                )
                root_price, root_allow = cur.fetchone()

                # The flag is set ONCE by the original creator and inherited
                # unchanged down the chain. Snapshotted here so a later change
                # by the root author cannot retroactively invalidate worlds
                # people have already built and sold.
                allow_derivatives = root_allow

                # No undercutting. Floor is against ROOT price, not the parent,
                # so markup does not compound down a long chain. Free root
                # means a floor of zero.
                floor = math.ceil(int(root_price) * DERIVATIVE_MARKUP)
                if price < floor:
                    raise HTTPException(
                        400, f"Derivative price must be at least {floor} credits"
                    )

            cur.execute(
                """
                INSERT INTO worlds
                    (id, player_id, character_id, title, description, version,
                     status, price, allow_derivatives, parent_world_id,
                     root_world_id, asset_manifest)
                VALUES (%s, %s, %s, %s, %s, 1, 'pending', %s, %s, %s, %s, %s);
                """,
                (world_id, player_id, req.character_id, title,
                 req.description.strip(), price, allow_derivatives,
                 req.parent_world_id, root_id, json.dumps(manifest)),
            )

    # ── phase 2: Spaces, outside the transaction, row already reserved ───────
    wkey = _world_key(world_id, 1)
    ckey = _card_key(world_id, 1) if card_bytes else None
    _put_objects(world_bytes, card_bytes, wkey, ckey )

    # ── phase 3: publish ─────────────────────────────────────────────────────
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE worlds
                SET status='ready', world_key=%s, card_key=%s,
                    updated_at=now()
                WHERE id = %s
                RETURNING created_at, updated_at;
                """,
                (wkey, ckey, world_id),
            )
            created_at, updated_at = cur.fetchone()

    return {
        "ok": True,
        "world_id": world_id,
        "version": 1,
        "thumbnail_url": _cdn(ckey),
        "asset_manifest": manifest,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }


# ═════════════════════════════════════════════════════════════════════════════
# UPDATE — new version of a world you own
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/{world_id}/update")
def update_world(
    world_id: str,
    req: UpdateWorldRequest,
    player_id: str = Depends(get_player_id),
):
    _assert_valid_uuid(world_id, "world_id")

    has_new_save = req.world_data is not None
    world_bytes = manifest = None
    if has_new_save:
        world_bytes = req.world_data.encode("utf-8")
        if not world_bytes:
            raise HTTPException(400, "world_data is empty")
        if len(world_bytes) > MAX_WORLD_BYTES:
            raise HTTPException(400, "World file too large")
        manifest = _extract_manifest(req.world_data)

    card_bytes = None
    if req.thumbnail_base64:
        card_bytes = _process_thumbnail(req.thumbnail_base64)

    # ── phase 1: validate, reserve a version only if the save changed ────────
    with db() as conn:
        with conn.cursor() as cur:
            if has_new_save:
                _rate_limit(cur, player_id)

            w = _fetch_world(cur, world_id, for_update=True)
            if str(w["player_id"]) != player_id:
                raise HTTPException(403, "Not your world")
            if w["status"] == "deleted":
                raise HTTPException(404, "World not found")

            prior_status = w["status"]
            new_version = w["version"] + (1 if has_new_save else 0)

            # price floor before any Spaces work, so a rejection costs nothing
            if req.price is not None and w["root_world_id"]:
                cur.execute("SELECT price FROM worlds WHERE id=%s;", (w["root_world_id"],))
                r = cur.fetchone()
                floor = math.ceil(int(r[0]) * DERIVATIVE_MARKUP) if r else 0
                if req.price < floor:
                    raise HTTPException(400, f"Price must be at least {floor} credits")

            if req.character_id is not None:
                _assert_valid_uuid(req.character_id, "character_id")
                _assert_character_owned(cur, req.character_id, player_id)

            if has_new_save:
                cur.execute(
                    "UPDATE worlds SET version=%s, status='pending' WHERE id=%s;",
                    (new_version, world_id),
                )

    # ── phase 2: Spaces ──────────────────────────────────────────────────────
    wkey = _world_key(world_id, new_version) if has_new_save else w["world_key"]
    if card_bytes:
        ckey = _image_keys(world_id)
    else:
        ckey = w["card_key"]

    if has_new_save or card_bytes:
        _put_objects(
            world_bytes if has_new_save else None,
            card_bytes,
            wkey if has_new_save else None,
            ckey if card_bytes else None,
        )

    # ── phase 3: commit ─────────────────────────────────────────────────────
    with db() as conn:
        with conn.cursor() as cur:
            sets, params = ["updated_at=now()"], []
            if has_new_save:
                # restore the PRIOR status, not 'ready' — updating a taken-down
                # world must not silently relist it past the slot check
                sets += ["status=%s", "world_key=%s", "asset_manifest=%s"]
                params += [prior_status, wkey, json.dumps(manifest)]
            if card_bytes:
                sets += ["card_key=%s"]
                params += [ckey]
            if req.title is not None:
                sets.append("title=%s"); params.append(req.title.strip())
            if req.description is not None:
                sets.append("description=%s"); params.append(req.description.strip())
            if req.price is not None:
                sets.append("price=%s"); params.append(req.price)
            if req.character_id is not None:
                sets.append("character_id=%s"); params.append(req.character_id)

            params.append(world_id)
            cur.execute(
                f"UPDATE worlds SET {', '.join(sets)} WHERE id=%s RETURNING updated_at;",
                params,
            )
            updated_at = cur.fetchone()[0]

    return {
        "ok": True,
        "world_id": world_id,
        "version": new_version,
        "version_bumped": has_new_save,
        "thumbnail_url": _cdn(ckey),
        "asset_manifest": manifest if has_new_save else w["asset_manifest"],
        "updated_at": updated_at.isoformat(),
    }

# ═════════════════════════════════════════════════════════════════════════════
# PURCHASE
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/{world_id}/purchase")
def purchase_world(
    world_id: str,
    req: PurchaseRequest,
    player_id: str = Depends(get_player_id),
):
    """
    Buyer pays the full price to the seller once; the seller then pays the
    royalty and the fee out of it. That keeps the buyer's ledger to a single
    clean line and puts the deductions where they belong — on the person who
    made the sale. All three moves are one transaction, so it's all or nothing.

    The fee move has to_character_id = NULL, which is already a burn in
    _move_money. There is no treasury table; the pot is a SUM over the ledger.
    """
    _assert_valid_uuid(world_id, "world_id")
    _assert_valid_uuid(req.character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, req.character_id, player_id)

            w = _fetch_world(cur, world_id, for_update=True)

            if w["status"] != "ready":
                raise HTTPException(409, "World is not available")
            if str(w["player_id"]) == player_id:
                raise HTTPException(400, "You already own this world")
            if int(w["price"]) == 0:
                raise HTTPException(400, "This world is free — use download")

            seller_character_id = w["character_id"]
            if not seller_character_id:
                # author character was deleted; nobody to pay
                raise HTTPException(409, "World is not available")
            seller_character_id = str(seller_character_id)

            cur.execute(
                "SELECT 1 FROM world_purchases WHERE world_id=%s AND buyer_player_id=%s;",
                (world_id, player_id),
            )
            if cur.fetchone():
                raise HTTPException(409, "Already purchased")

            price = int(w["price"])

            # Pre-check so nothing partially applies, and hand back the real
            # balance — if the client's local number was stale, the failed
            # attempt is what corrects it.
            balance = _get_balance(cur, req.character_id)
            if balance < price:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "insufficient_funds",
                        "balance": balance,
                        "price": price,
                    },
                )

            # ── split ────────────────────────────────────────────────────────
            fee = math.floor(price * MARKETPLACE_FEE_PCT)

            root_character_id = None
            if w["root_world_id"]:
                cur.execute(
                    "SELECT character_id, status FROM worlds WHERE id=%s;",
                    (w["root_world_id"],),
                )
                r = cur.fetchone()
                # An unclaimed share (root deleted, author character gone, or
                # the root author IS the seller) falls through to the seller
                # rather than being destroyed.
                if r and r[1] != "deleted" and r[0] and str(r[0]) != seller_character_id:
                    root_character_id = str(r[0])

            author_cut = math.floor(price * ROOT_AUTHOR_PCT) if root_character_id else 0
            seller_cut = price - author_cut - fee

            # ── move the money ───────────────────────────────────────────────
            _move_money(cur, req.character_id, seller_character_id,
                        price, REASON_WORLD_PURCHASE, world_id)
            if author_cut > 0:
                _move_money(cur, seller_character_id, root_character_id,
                            author_cut, REASON_WORLD_ROYALTY, world_id)
            if fee > 0:
                _move_money(cur, seller_character_id, None,
                            fee, REASON_WORLD_FEE, world_id)

            cur.execute(
                """
                INSERT INTO world_purchases
                    (world_id, buyer_player_id, buyer_character_id, price_paid,
                     seller_cut, author_cut, fee_burned, root_author_character_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (world_id, player_id, req.character_id, price,
                 seller_cut, author_cut, fee, root_character_id),
            )

            new_balance = _get_balance(cur, req.character_id)
            names = _fetch_character_names(cur, [req.character_id, seller_character_id])
            title = w["title"]

    # ── notify after commit ──────────────────────────────────────────────────
    # Cosmetic only. The ledger already has the money; a missed banner costs
    # nothing and an offline seller sees it in their transaction list.
    buyer_name = names.get(req.character_id, "")
    notify_character(seller_character_id, "world_sold", {
        "world_id": world_id,
        "world_title": title,
        "amount": seller_cut,
        "from_character_id": req.character_id,
        "from_name": buyer_name,
        "reason": REASON_WORLD_PURCHASE,
    })
    if root_character_id and author_cut > 0:
        notify_character(root_character_id, "world_royalty", {
            "world_id": world_id,
            "world_title": title,
            "amount": author_cut,
            "from_character_id": seller_character_id,
            "from_name": names.get(seller_character_id, ""),
            "reason": REASON_WORLD_ROYALTY,
        })

    return {
        "ok": True,
        "world_id": world_id,
        "price_paid": price,
        "balance": new_balance,
    }


# ═════════════════════════════════════════════════════════════════════════════
# DOWNLOAD
# ═════════════════════════════════════════════════════════════════════════════
@router.get("/{world_id}/download")
def download_world(world_id: str, player_id: str = Depends(get_player_id)):
    """
    World files are private in Spaces. This returns a short-lived presigned
    URL rather than a permanent CDN link, so a paid world can't be given away
    by pasting a link. Costs CDN acceleration on the .world file, which is
    fine at 8 MB. Thumbnails stay public and cached.
    """
    _assert_valid_uuid(world_id, "world_id")

    with db() as conn:
        with conn.cursor() as cur:
            w = _fetch_world(cur, world_id)
            if w["status"] in ("pending", "deleted"):
                raise HTTPException(404, "World not found")
            if not _has_access(cur, w, player_id):
                if w["status"] == "takendown":
                    raise HTTPException(404, "World not found")
                raise HTTPException(402, "Purchase required")
            if not w["world_key"]:
                raise HTTPException(404, "World file not available")

            # Composite PK makes this idempotent and gives distinct-player
            # counts for popularity ranking without a separate dedupe pass.
            cur.execute(
                "INSERT INTO world_downloads (world_id, player_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING;",
                (world_id, player_id),
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE worlds SET download_count = download_count + 1 WHERE id=%s;",
                    (world_id,),
                )

    return {
        "world_id": world_id,
        "version": w["version"],
        "url": _presign(w["world_key"]),
        "thumbnail_url": _cdn(w["card_key"]),
        "expires_in": PRESIGN_SECONDS,
        "asset_manifest": w["asset_manifest"],
    }


# ═════════════════════════════════════════════════════════════════════════════
# TAKEDOWN / RELIST / DELETE
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/{world_id}/takedown")
def takedown_world(world_id: str, player_id: str = Depends(get_player_id)):
    """Unlist. Files stay, existing buyers keep downloading, frees a slot."""
    _assert_valid_uuid(world_id, "world_id")
    with db() as conn:
        with conn.cursor() as cur:
            w = _fetch_world(cur, world_id, for_update=True)
            if str(w["player_id"]) != player_id:
                raise HTTPException(403, "Not your world")
            if w["status"] == "deleted":
                raise HTTPException(404, "World not found")
            cur.execute(
                "UPDATE worlds SET status='takendown', featured_rank=NULL, "
                "updated_at=now() WHERE id=%s;",
                (world_id,),
            )
    return {"ok": True, "world_id": world_id, "status": "takendown"}


@router.post("/{world_id}/relist")
def relist_world(world_id: str, player_id: str = Depends(get_player_id)):
    _assert_valid_uuid(world_id, "world_id")
    with db() as conn:
        with conn.cursor() as cur:
            w = _fetch_world(cur, world_id, for_update=True)
            if str(w["player_id"]) != player_id:
                raise HTTPException(403, "Not your world")
            if w["status"] != "takendown":
                raise HTTPException(409, "World is not taken down")
            if not w["character_id"]:
                raise HTTPException(409, "Assign a character to this world first")
            _assert_listing_slot(cur, player_id)
            cur.execute(
                "UPDATE worlds SET status='ready', updated_at=now() WHERE id=%s;",
                (world_id,),
            )
    return {"ok": True, "world_id": world_id, "status": "ready"}


@router.delete("/{world_id}")
def delete_world(world_id: str, player_id: str = Depends(get_player_id)):
    """
    The author's call. Files are purged from Spaces and the world disappears
    from every listing. The DB row is kept as status='deleted' because
    world_purchases and the transactions ledger reference it — a hard delete
    would take the sales history with it.

    Buyers keep whatever they already pulled to disk. Someone who paid but
    never downloaded loses access; if that ever matters, gate this behind
    "must be taken down for 30 days first".
    """
    _assert_valid_uuid(world_id, "world_id")
    with db() as conn:
        with conn.cursor() as cur:
            w = _fetch_world(cur, world_id, for_update=True)
            if str(w["player_id"]) != player_id:
                raise HTTPException(403, "Not your world")
            cur.execute(
                "UPDATE worlds SET status='deleted', featured_rank=NULL, "
                "world_key=NULL, card_key=NULL, updated_at=now() "
                "WHERE id=%s;",
                (world_id,),
            )

    _purge_prefix(f"worlds/{world_id}/")
    return {"ok": True, "world_id": world_id, "status": "deleted"}


# ═════════════════════════════════════════════════════════════════════════════
# REPORT
# ═════════════════════════════════════════════════════════════════════════════
@router.post("/{world_id}/report")
def report_world(
    world_id: str,
    req: ReportRequest,
    player_id: str = Depends(get_player_id),
):
    _assert_valid_uuid(world_id, "world_id")
    with db() as conn:
        with conn.cursor() as cur:
            w = _fetch_world(cur, world_id)
            if str(w["player_id"]) == player_id:
                raise HTTPException(400, "You cannot report your own world")
            cur.execute(
                "INSERT INTO world_reports (world_id, reporter_player_id, reason) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;",
                (world_id, player_id, req.reason.strip()),
            )
            cur.execute(
                "SELECT COUNT(*) FROM world_reports WHERE world_id=%s;", (world_id,)
            )
            if cur.fetchone()[0] >= REPORT_HIDE_THRESHOLD and w["moderation_flag"] is None:
                cur.execute(
                    "UPDATE worlds SET moderation_flag='flagged' WHERE id=%s;",
                    (world_id,),
                )
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
# janitor — cron, not per-request
# ═════════════════════════════════════════════════════════════════════════════
def reap_pending_uploads():
    """A row stuck in 'pending' means the Spaces write died mid-upload."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM worlds WHERE status='pending' "
                "AND created_at < now() - interval '1 hour' RETURNING id;"
            )
            ids = [str(r[0]) for r in cur.fetchall()]
    for wid in ids:
        _purge_prefix(f"worlds/{wid}/")
    return ids