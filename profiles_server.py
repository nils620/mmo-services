import os
import uuid as uuid_lib
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
import psycopg
from typing import Optional, List, Literal
import httpx

#mounting processes to use same port
from stream_server import router as stream_router
from worlds_server import router as worlds_router

from auth import verify_steam_ticket, create_token, get_player_id_optional, dev_router

# Read from environment (systemd will provide these)
DB_DSN = os.environ.get("DB_DSN")
PORT = int(os.environ.get("PROFILES_PORT", "8000"))

#Socket Notifications
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET")
CHAT_INTERNAL_URL = os.environ.get("CHAT_INTERNAL_URL", "http://127.0.0.1:4000")

if not DB_DSN:
    raise RuntimeError("DB_DSN is not set. Put it into the systemd Environment/EnvironmentFile.")

app = FastAPI()
#mounting processes to use same port
app.include_router(stream_router, prefix="/stream")
app.include_router(worlds_router, prefix="/worlds")

if dev_router:
    app.include_router(dev_router, prefix="/auth")

class LoginRequest(BaseModel):
    provider: Optional[str] = "steam"
    provider_id: Optional[str] = None   # legacy backwards compatibility Auth is confirmed to work & clients are updated.
    ticket: Optional[str] = None        # new auth ticket


class CreateCharacterRequest(BaseModel):
    player_id: Optional[str] = None   # legacy — remove after client cutover
    character_name: str
    customization_id: str

class UpdateCustomizationRequest(BaseModel):
    player_id: Optional[str] = None   # legacy — remove after client cutover
    customization_id: str


class ColorRGBA(BaseModel):
    r: float
    g: float
    b: float
    a: float

class UpdateProfileRequest(BaseModel):
    player_id: Optional[str] = None   # legacy backwards compatibility Auth is confirmed to work & clients are updated.
    character_id: str

    age: Optional[int] = None
    interests: Optional[str] = ""
    languages: Optional[str] = ""
    about_me: Optional[str] = ""

    share_location: Optional[bool] = False
    text_color: Optional[ColorRGBA] = None
    background_color: Optional[ColorRGBA] = None

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def db():
    return psycopg.connect(DB_DSN)

# ── Socket Notifications ─────────────────────────────────────────────────────
def notify_character(character_id: str, event_type: str, payload: dict):
    """Fire-and-forget push. Never let a failed notify break the actual operation."""
    if not NOTIFY_SECRET:
        return
    try:
        httpx.post(
            f"{CHAT_INTERNAL_URL}/notify",
            headers={"X-Notify-Secret": NOTIFY_SECRET},
            json={"character_id": character_id, "event_type": event_type, "payload": payload},
            timeout=2.0,
        )
    except Exception:
        pass


# ── auth helpers ─────────────────────────────────────────────────────────────
def _resolve_player(token_player: Optional[str], legacy_player_id: Optional[str]) -> str:
    """
    Token wins. Falls back to the client-supplied id until every client is
    updated. Drop the fallback (and this helper) at cutover.
    """
    player_id = token_player or legacy_player_id
    if not player_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _valid_uuid(player_id):
        raise HTTPException(status_code=400, detail="Invalid player_id")
    return player_id


def _valid_uuid(value: Optional[str]) -> bool:
    try:
        uuid_lib.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _assert_valid_uuid(value: Optional[str], field_name: str):
    if not _valid_uuid(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")


@app.get("/health")
def health():
    return {"ok": True}


# Server Login + Character Creation & Manipulation

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    # New clients send a ticket; old clients still send provider_id directly.
    # Drop the legacy branch once every client is updated.
    if req.ticket:
        provider_id = verify_steam_ticket(req.ticket)
    elif req.provider_id:
        provider_id = req.provider_id
    else:
        raise HTTPException(status_code=400, detail="ticket is required")

    # Upsert player based on provider identity
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO players (provider, provider_id)
                VALUES (%s, %s)
                ON CONFLICT (provider, provider_id)
                DO UPDATE SET updated_at = now()
                RETURNING id;
                """,
                ("steam", provider_id),
            )
            player_id = cur.fetchone()[0]

    return {
        "player_id": str(player_id),
        "token": create_token(player_id),
    }


@app.post("/characters")
def create_character(
    req: CreateCharacterRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)

    name = req.character_name.strip()
    customization_id = (req.customization_id or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Character name is empty")
    if len(name) > 24:
        raise HTTPException(status_code=400, detail="Character name too long (max 24)")

    # optional sanity checks (safe + helpful)
    if not customization_id:
        raise HTTPException(status_code=400, detail="customization_id is empty")
    if len(customization_id) > 64:
        raise HTTPException(status_code=400, detail="customization_id too long (max 64)")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM players WHERE id = %s;", (player_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Player not found")

            try:
                cur.execute(
                    """
                    INSERT INTO characters (player_id, character_name, customization_id)
                    VALUES (%s, %s, %s)
                    RETURNING id, character_name, customization_id;
                    """,
                    (player_id, name, customization_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=500, detail="Character insert failed (no row returned)")

                character_id, character_name, customization_id = row

                # create default profile row (defaults apply here)
                cur.execute(
                    """
                    INSERT INTO character_profiles (character_id)
                    VALUES (%s)
                    ON CONFLICT (character_id) DO NOTHING;
                    """,
                    (character_id,),
                )

            except psycopg.errors.UniqueViolation:
                # optional but clean: reset transaction state if you ever continue using conn
                conn.rollback()
                raise HTTPException(status_code=409, detail="Character name already used by this player")

    return {
        "character_id": str(character_id),
        "character_name": character_name,
        "customization_id": customization_id,
    }


@app.get("/characters")
def list_characters(
    player_id: Optional[str] = None,   # legacy — remove after client cutover
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    effective_player = _resolve_player(token_player, player_id)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, character_name, customization_id, created_at
                FROM characters
                WHERE player_id = %s
                ORDER BY created_at ASC;
                """,
                (effective_player,),
            )
            rows = cur.fetchall()

    return {
        "player_id": effective_player,
        "characters": [
            {
                "character_id": str(r[0]),
                "character_name": r[1],
                "customization_id": (r[2] or ""),  # allow fallback
                "created_at": r[3].isoformat(),
            }
            for r in rows
        ],
    }


@app.delete("/characters/{character_id}")
def delete_character(
    character_id: str,
    player_id: Optional[str] = None,   # legacy — remove after client cutover
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    effective_player = _resolve_player(token_player, player_id)
    _assert_valid_uuid(character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            # Only delete if this character belongs to this player
            cur.execute(
                """
                DELETE FROM characters
                WHERE id = %s AND player_id = %s
                RETURNING id;
                """,
                (character_id, effective_player),
            )
            deleted = cur.fetchone()

    if deleted is None:
        # either doesn't exist or not owned by that player
        raise HTTPException(status_code=404, detail="Character not found for this player")

    return {"ok": True, "character_id": character_id}


@app.put("/characters/{character_id}/customization")
def update_character_customization_put(
    character_id: str,
    req: UpdateCustomizationRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_valid_uuid(character_id, "character_id")

    customization_id = (req.customization_id or "").strip()
    if not customization_id:
        raise HTTPException(status_code=400, detail="customization_id is empty")
    if len(customization_id) > 64:
        raise HTTPException(status_code=400, detail="customization_id too long (max 64)")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE characters
                SET customization_id = %s
                WHERE id = %s AND player_id = %s
                RETURNING id, customization_id;
                """,
                (customization_id, character_id, player_id),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Character not found for this player")

    return {"ok": True, "character_id": str(row[0]), "customization_id": row[1]}



# Profile Fetch & Update

# NOTE: public on purpose — any player can view any profile. No ownership check.
@app.get("/profiles/{character_id}")
def get_profile(character_id: str):
    _assert_valid_uuid(character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.id,
                    c.character_name,
                    c.created_at,
                    p.age,
                    p.interests,
                    p.languages,
                    p.about_me,
                    p.share_location,
                    p.text_r, p.text_g, p.text_b, p.text_a,
                    p.bg_r, p.bg_g, p.bg_b, p.bg_a
                FROM characters c
                LEFT JOIN character_profiles p ON p.character_id = c.id
                WHERE c.id = %s;
                """,
                (character_id,),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Character not found")

    return {
        "character_id": str(row[0]),
        "character_name": row[1],
        "created_at": row[2].isoformat(),
        "age": row[3],
        "interests": row[4] or "",
        "languages": row[5] or "",
        "about_me": row[6] or "",
        "share_location": bool(row[7]) if row[7] is not None else False,
        "text_color": {"r": row[8], "g": row[9], "b": row[10], "a": row[11]},
        "background_color": {"r": row[12], "g": row[13], "b": row[14], "a": row[15]},
    }


@app.post("/profiles/update")
def update_profile(
    req: UpdateProfileRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_valid_uuid(req.character_id, "character_id")

    interests = (req.interests or "").strip()
    languages = (req.languages or "").strip()
    about_me = (req.about_me or "").strip()

    # Validation
    if req.age is not None and (req.age < 18 or req.age > 120):
        raise HTTPException(status_code=400, detail="Age must be between 18 and 120")

    if len(interests) > 80:
        raise HTTPException(status_code=400, detail="Interests too long (max 80)")
    if len(languages) > 80:
        raise HTTPException(status_code=400, detail="Languages too long (max 80)")
    if len(about_me) > 800:
        raise HTTPException(status_code=400, detail="About me too long (max 800)")

    share_location = bool(req.share_location) if req.share_location is not None else False

    tc = req.text_color or ColorRGBA(r=1, g=1, b=1, a=1)
    bc = req.background_color or ColorRGBA(r=0.2, g=0.2, b=0.2, a=1)

    text_r, text_g, text_b, text_a = map(clamp01, [tc.r, tc.g, tc.b, tc.a])
    bg_r, bg_g, bg_b, bg_a = map(clamp01, [bc.r, bc.g, bc.b, bc.a])

    with db() as conn:
        with conn.cursor() as cur:
            # Ensure character exists AND belongs to player
            cur.execute(
                """
                SELECT 1
                FROM characters
                WHERE id = %s AND player_id = %s;
                """,
                (req.character_id, player_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="Character not owned by player")

            # UPSERT profile row
            cur.execute(
                """
                INSERT INTO character_profiles (
                    character_id, age, interests, languages, about_me,
                    share_location,
                    text_r, text_g, text_b, text_a,
                    bg_r, bg_g, bg_b, bg_a,
                    updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (character_id)
                DO UPDATE SET
                    age = EXCLUDED.age,
                    interests = EXCLUDED.interests,
                    languages = EXCLUDED.languages,
                    about_me = EXCLUDED.about_me,
                    share_location = EXCLUDED.share_location,
                    text_r = EXCLUDED.text_r,
                    text_g = EXCLUDED.text_g,
                    text_b = EXCLUDED.text_b,
                    text_a = EXCLUDED.text_a,
                    bg_r = EXCLUDED.bg_r,
                    bg_g = EXCLUDED.bg_g,
                    bg_b = EXCLUDED.bg_b,
                    bg_a = EXCLUDED.bg_a,
                    updated_at = now()
                RETURNING
                    character_id, age, interests, languages, about_me, share_location,
                    text_r, text_g, text_b, text_a,
                    bg_r, bg_g, bg_b, bg_a,
                    updated_at;
                """,
                (
                    req.character_id, req.age, interests, languages, about_me,
                    share_location,
                    text_r, text_g, text_b, text_a,
                    bg_r, bg_g, bg_b, bg_a,
                ),
            )

            row = cur.fetchone()

    return {
        "ok": True,
        "character_id": str(row[0]),
        "age": row[1],
        "interests": row[2] or "",
        "languages": row[3] or "",
        "about_me": row[4] or "",
        "share_location": bool(row[5]),
        "text_color": {"r": row[6], "g": row[7], "b": row[8], "a": row[9]},
        "background_color": {"r": row[10], "g": row[11], "b": row[12], "a": row[13]},
        "updated_at": row[14].isoformat(),
    }



# Social Requests aka Friends & Friend Requests aswell as blocks

class SocialActionRequest(BaseModel):
    player_id: Optional[str] = None   # legacy — remove after client cutover
    character_id: str          # the actor (must belong to player)
    target_character_id: str   # who we act on


def _assert_character_owned(cur, character_id: str, player_id: str):
    cur.execute(
        "SELECT 1 FROM characters WHERE id=%s AND player_id=%s;",
        (character_id, player_id),
    )
    if cur.fetchone() is None:
        raise HTTPException(status_code=403, detail="Character not owned by player")

def _fetch_character_names(cur, character_ids: list) -> dict:
    """
    Returns {character_id: character_name} for the given ids.
    Used to put display names into notify payloads so the client can render a
    banner without a second round trip.
    """
    if not character_ids:
        return {}
    cur.execute(
        "SELECT id, character_name FROM characters WHERE id = ANY(%s);",
        (character_ids,),
    )
    return {str(row[0]): row[1] for row in cur.fetchall()}

def _assert_not_self(a: str, b: str):
    if a == b:
        raise HTTPException(status_code=400, detail="Cannot target self")

def _is_blocked_either_way(cur, a: str, b: str) -> tuple[bool, bool]:
    # returns (a_blocked_b, b_blocked_a)
    cur.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM character_blocks WHERE blocker_character_id=%s AND blocked_character_id=%s) AS a_blocks_b,
          EXISTS(SELECT 1 FROM character_blocks WHERE blocker_character_id=%s AND blocked_character_id=%s) AS b_blocks_a
        """,
        (a, b, b, a),
    )
    row = cur.fetchone()
    return bool(row[0]), bool(row[1])

def _friends_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _assert_social_uuids(req: SocialActionRequest):
    _assert_valid_uuid(req.character_id, "character_id")
    _assert_valid_uuid(req.target_character_id, "target_character_id")

def fetch_online_characters(character_ids: list) -> set:
    """
    Asks the chat service which of these characters are currently connected.
    Returns a set of online character_ids. On any failure returns an empty set
    """
    if not NOTIFY_SECRET or not character_ids:
        return set()
    try:
        r = httpx.post(
            f"{CHAT_INTERNAL_URL}/online",
            headers={"X-Notify-Secret": NOTIFY_SECRET},
            json={"character_ids": character_ids},
            timeout=2.0,
        )
        if r.status_code != 200:
            return set()
        return set(r.json().get("online") or [])
    except Exception:
        return set()

@app.post("/friends/request")
def send_friend_request(
    req: SocialActionRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_social_uuids(req)

    a = req.character_id
    b = req.target_character_id
    _assert_not_self(a, b)

    became_friends = False
    names = {}

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, a, player_id)

            a_blocks_b, b_blocks_a = _is_blocked_either_way(cur, a, b)
            if b_blocks_a:
                raise HTTPException(status_code=403, detail="You are blocked by this character")
            if a_blocks_b:
                raise HTTPException(status_code=409, detail="Unblock this character first")

            # already friends?
            ka, kb = _friends_key(a, b)
            cur.execute(
                "SELECT 1 FROM character_friends WHERE character_a_id=%s AND character_b_id=%s;",
                (ka, kb),
            )
            if cur.fetchone() is not None:
                raise HTTPException(status_code=409, detail="Already friends")

            # reverse request exists? auto-accept
            cur.execute(
                "SELECT 1 FROM character_friend_requests WHERE from_character_id=%s AND to_character_id=%s;",
                (b, a),
            )
            if cur.fetchone() is not None:
                # delete reverse request and become friends
                cur.execute(
                    "DELETE FROM character_friend_requests WHERE from_character_id=%s AND to_character_id=%s;",
                    (b, a),
                )
                cur.execute(
                    """
                    INSERT INTO character_friends (character_a_id, character_b_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING;
                    """,
                    (ka, kb),
                )
                became_friends = True
            else:
                # normal request
                try:
                    cur.execute(
                        """
                        INSERT INTO character_friend_requests (from_character_id, to_character_id)
                        VALUES (%s, %s);
                        """,
                        (a, b),
                    )
                except Exception:
                    # if you want specific 409: check existence first (simpler)
                    raise HTTPException(status_code=409, detail="Request already exists")

            # display names for the notify payloads, so the client can render a
            # banner without a second round trip
            names = _fetch_character_names(cur, [a, b])

    # Notifications go out after the DB block so the write is committed and the
    # connection is released before we make an HTTP call.
    # We never notify the actor (a) — their HTTP response already told them.
    if became_friends:
        # b had a pending request out to a; from b's side it just got accepted
        notify_character(b, "friend_accepted", {
            "character_id": a,
            "character_name": names.get(a, ""),
        })
        return {"ok": True, "status": "friends"}

    # notify target char that he got a request
    notify_character(b, "friend_request", {
        "from_character_id": a,
        "character_name": names.get(a, ""),
    })
    return {"ok": True, "status": "requested"}

@app.get("/friends/requests/incoming")
def list_incoming_requests(
    character_id: str,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    _assert_valid_uuid(character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            # Private data — only the owner may read it. Old clients send no
            # token and are still let through; remove this guard at cutover.
            if token_player:
                _assert_character_owned(cur, character_id, token_player)

            cur.execute(
                """
                SELECT r.from_character_id, c.character_name, r.created_at
                FROM character_friend_requests r
                JOIN characters c ON c.id = r.from_character_id
                WHERE r.to_character_id = %s
                ORDER BY r.created_at ASC;
                """,
                (character_id,),
            )
            rows = cur.fetchall()

    return {
        "character_id": character_id,
        "incoming": [
            {
                "from_character_id": str(r[0]),
                "character_name": r[1],
                "created_at": r[2].isoformat(),
            }
            for r in rows
        ],
    }

@app.get("/friends/requests/outgoing")
def list_outgoing_requests(
    character_id: str,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    _assert_valid_uuid(character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            if token_player:
                _assert_character_owned(cur, character_id, token_player)

            cur.execute(
                """
                SELECT r.to_character_id, c.character_name, r.created_at
                FROM character_friend_requests r
                JOIN characters c ON c.id = r.to_character_id
                WHERE r.from_character_id = %s
                ORDER BY r.created_at ASC;
                """,
                (character_id,),
            )
            rows = cur.fetchall()

    return {
        "character_id": character_id,
        "outgoing": [
            {
                "to_character_id": str(r[0]),
                "to_name": r[1],
                "created_at": r[2].isoformat(),
            }
            for r in rows
        ],
    }


@app.post("/friends/request/accept")
def accept_request(
        req: SocialActionRequest,
        token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_social_uuids(req)

    me = req.character_id
    sender = req.target_character_id
    _assert_not_self(me, sender)

    names = {}

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, me, player_id)

            # must exist
            cur.execute(
                """
                SELECT 1 FROM character_friend_requests
                WHERE from_character_id=%s AND to_character_id=%s;
                """,
                (sender, me),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Friend request not found")

            # blocks?
            me_blocks, sender_blocks = _is_blocked_either_way(cur, me, sender)
            if sender_blocks:
                raise HTTPException(status_code=403, detail="You are blocked by this character")
            if me_blocks:
                raise HTTPException(status_code=409, detail="Unblock this character first")

            # delete request + create friendship
            cur.execute(
                "DELETE FROM character_friend_requests WHERE from_character_id=%s AND to_character_id=%s;",
                (sender, me),
            )
            a, b = _friends_key(me, sender)
            cur.execute(
                """
                INSERT INTO character_friends (character_a_id, character_b_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING;
                """,
                (a, b),
            )

            names = _fetch_character_names(cur, [me])
    notify_character(sender, "friend_accepted", {
        "character_id": me,
        "character_name": names.get(me, ""),
    })

    return {"ok": True}


@app.post("/friends/request/decline")
def decline_request(
        req: SocialActionRequest,
        token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_social_uuids(req)

    me = req.character_id
    sender = req.target_character_id
    _assert_not_self(me, sender)

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, me, player_id)
            cur.execute(
                "DELETE FROM character_friend_requests WHERE from_character_id=%s AND to_character_id=%s;",
                (sender, me),
            )
    notify_character(sender, "friendlist_update", {})

    return {"ok": True}


@app.get("/friends/list")
def list_friends(
        character_id: str,
        token_player: Optional[str] = Depends(get_player_id_optional),
):
    _assert_valid_uuid(character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            if token_player:
                _assert_character_owned(cur, character_id, token_player)

            cur.execute(
                """
                SELECT
                  CASE
                    WHEN f.character_a_id = %s THEN f.character_b_id
                    ELSE f.character_a_id
                  END AS friend_id,
                  c.character_name,
                  f.created_at
                FROM character_friends f
                JOIN characters c
                  ON c.id = CASE
                    WHEN f.character_a_id = %s THEN f.character_b_id
                    ELSE f.character_a_id
                  END
                WHERE f.character_a_id = %s OR f.character_b_id = %s
                ORDER BY c.character_name ASC;
                """,
                (character_id, character_id, character_id, character_id),
            )
            rows = cur.fetchall()

    friend_ids = [str(r[0]) for r in rows]
    online_ids = fetch_online_characters(friend_ids)

    return {
        "character_id": character_id,
        "friends": [
            {
                "character_id": str(r[0]),
                "character_name": r[1],
                "since": r[2].isoformat(),
                "online": str(r[0]) in online_ids,
            }
            for r in rows
        ],
    }



@app.post("/friends/remove")
def remove_friend(
    req: SocialActionRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_social_uuids(req)

    a = req.character_id
    b = req.target_character_id
    _assert_not_self(a, b)

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, a, player_id)
            ka, kb = _friends_key(a, b)
            cur.execute(
                "DELETE FROM character_friends WHERE character_a_id=%s AND character_b_id=%s;",
                (ka, kb),
            )
    notify_character(b, "friendlist_update", {})

    return {"ok": True}

@app.post("/blocks/add")
def add_block(
    req: SocialActionRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_social_uuids(req)

    blocker = req.character_id
    blocked = req.target_character_id
    _assert_not_self(blocker, blocked)

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, blocker, player_id)

            # add block
            cur.execute(
                """
                INSERT INTO character_blocks (blocker_character_id, blocked_character_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING;
                """,
                (blocker, blocked),
            )

            # remove friendship if exists
            a, b = _friends_key(blocker, blocked)
            cur.execute(
                "DELETE FROM character_friends WHERE character_a_id=%s AND character_b_id=%s;",
                (a, b),
            )

            # remove any pending requests either direction
            cur.execute(
                """
                DELETE FROM character_friend_requests
                WHERE (from_character_id=%s AND to_character_id=%s)
                   OR (from_character_id=%s AND to_character_id=%s);
                """,
                (blocker, blocked, blocked, blocker),
            )
    notify_character(blocked, "friendlist_update", {})

    return {"ok": True}

@app.post("/blocks/remove")
def remove_block(
    req: SocialActionRequest,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    player_id = _resolve_player(token_player, req.player_id)
    _assert_social_uuids(req)

    blocker = req.character_id
    blocked = req.target_character_id
    _assert_not_self(blocker, blocked)

    with db() as conn:
        with conn.cursor() as cur:
            _assert_character_owned(cur, blocker, player_id)
            cur.execute(
                "DELETE FROM character_blocks WHERE blocker_character_id=%s AND blocked_character_id=%s;",
                (blocker, blocked),
            )
    return {"ok": True}

@app.get("/blocks/list")
def list_blocks(
    character_id: str,
    token_player: Optional[str] = Depends(get_player_id_optional),
):
    _assert_valid_uuid(character_id, "character_id")

    with db() as conn:
        with conn.cursor() as cur:
            if token_player:
                _assert_character_owned(cur, character_id, token_player)

            cur.execute(
                """
                SELECT b.blocked_character_id, c.character_name, b.created_at
                FROM character_blocks b
                JOIN characters c ON c.id = b.blocked_character_id
                WHERE b.blocker_character_id = %s
                ORDER BY c.character_name ASC;
                """,
                (character_id,),
            )
            rows = cur.fetchall()

    return {
        "character_id": character_id,
        "blocked": [
            {"character_id": str(r[0]), "character_name": r[1], "created_at": r[2].isoformat()}
            for r in rows
        ],
    }

