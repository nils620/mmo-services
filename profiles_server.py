import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psycopg

# Read from environment (systemd will provide these)
DB_DSN = os.environ.get("DB_DSN")
PORT = int(os.environ.get("PROFILES_PORT", "8000"))

if not DB_DSN:
    raise RuntimeError("DB_DSN is not set. Put it into the systemd Environment/EnvironmentFile.")

app = FastAPI()


class LoginRequest(BaseModel):
    provider: str        # "steam"
    provider_id: str     # steam64 as string


class CreateCharacterRequest(BaseModel):
    player_id: str
    character_name: str
    customization_id: str

class UpdateCustomizationRequest(BaseModel):
    player_id: str
    customization_id: str

def db():
    return psycopg.connect(DB_DSN)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/auth/login")
def auth_login(req: LoginRequest):
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
                (req.provider, req.provider_id),
            )
            player_id = cur.fetchone()[0]

    return {"player_id": str(player_id)}


@app.post("/characters")
def create_character(req: CreateCharacterRequest):
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
            cur.execute("SELECT 1 FROM players WHERE id = %s;", (req.player_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Player not found")

            try:
                cur.execute(
                    """
                    INSERT INTO characters (player_id, character_name, customization_id)
                    VALUES (%s, %s, %s)
                    RETURNING id, character_name, customization_id;
                    """,
                    (req.player_id, name, customization_id),
                )
            except psycopg.errors.UniqueViolation:
                raise HTTPException(status_code=409, detail="Character name already used by this player")

            character_id, character_name, customization_id = cur.fetchone()

    return {
        "character_id": str(character_id),
        "character_name": character_name,
        "customization_id": customization_id,
    }


@app.get("/characters")
def list_characters(player_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, character_name, customization_id, created_at
                FROM characters
                WHERE player_id = %s
                ORDER BY created_at ASC;
                """,
                (player_id,),
            )
            rows = cur.fetchall()

    return {
        "player_id": player_id,
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
def delete_character(character_id: str, player_id: str):
    with db() as conn:
        with conn.cursor() as cur:
            # Only delete if this character belongs to this player
            cur.execute(
                """
                DELETE FROM characters
                WHERE id = %s AND player_id = %s
                RETURNING id;
                """,
                (character_id, player_id),
            )
            deleted = cur.fetchone()

    if deleted is None:
        # either doesn't exist or not owned by that player
        raise HTTPException(status_code=404, detail="Character not found for this player")

    return {"ok": True, "character_id": character_id}


@app.put("/characters/{character_id}/customization")
def update_character_customization_put(character_id: str, req: UpdateCustomizationRequest):
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
                (customization_id, character_id, req.player_id),
            )
            row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Character not found for this player")

    return {"ok": True, "character_id": str(row[0]), "customization_id": row[1]}