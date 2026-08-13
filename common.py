"""
common.py — helpers shared by profiles_server and worlds_server.

Extracted verbatim from profiles_server.py. Nothing was renamed and no
behaviour changed; this exists purely because profiles_server imports
worlds_router, so worlds_server cannot import back out of profiles_server.
"""

import os
import uuid as uuid_lib
from typing import Optional

import httpx
import psycopg
from fastapi import HTTPException

DB_DSN = os.environ.get("DB_DSN")
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET")
CHAT_INTERNAL_URL = os.environ.get("CHAT_INTERNAL_URL", "http://127.0.0.1:4000")

if not DB_DSN:
    raise RuntimeError("DB_DSN is not set. Put it into the systemd Environment/EnvironmentFile.")


def db():
    return psycopg.connect(DB_DSN)


# ── uuid validation ──────────────────────────────────────────────────────────
def _valid_uuid(value: Optional[str]) -> bool:
    try:
        uuid_lib.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _assert_valid_uuid(value: Optional[str], field_name: str):
    if not _valid_uuid(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")


# ── ownership ────────────────────────────────────────────────────────────────
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


# ── chat service bridge ──────────────────────────────────────────────────────
def notify_character(character_id: str, event_type: str, payload: dict):
    """Fire-and-forget push. Never let a failed notify break the actual operation."""
    if not NOTIFY_SECRET or not character_id:
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


def fetch_online_characters(character_ids: list) -> set:
    """
    Asks the chat service which of these characters are currently connected.
    Returns a set of online character_ids. On any failure returns an empty set.
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