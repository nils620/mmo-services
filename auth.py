import os
import time
from typing import Optional
import hmac
import httpx
import jwt  # PyJWT
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

# ── env ──────────────────────────────────────────────────────────────────────
STEAM_PUBLISHER_KEY = os.environ.get("STEAM_PUBLISHER_KEY")
STEAM_APP_ID = int(os.environ.get("STEAM_APP_ID", "3453880"))

# Must byte-match the Identity string passed into GetAuthTicketForWebApi in Unreal.
# ASCII only — the SIK node runs it through TCHAR_TO_ANSI.
STEAM_IDENTITY = os.environ.get("STEAM_IDENTITY", "backend")

JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_ALGO = "HS256"
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", str(6 * 60 * 60)))  # 6h

# Dev token issuance. Must be absent/false on the production droplet.
DEV_AUTH_ENABLED = os.environ.get("DEV_AUTH_ENABLED", "").lower() in ("1", "true", "yes")

STEAM_AUTH_URL = "https://partner.steam-api.com/ISteamUserAuth/AuthenticateUserTicket/v1/"

if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set. Put it into the systemd Environment/EnvironmentFile.")


# ── steam ticket verification ────────────────────────────────────────────────
def verify_steam_ticket(ticket_hex: str) -> str:
    """
    Verify a GetAuthTicketForWebApi ticket against Steam.
    Returns the steam64 as a string. Raises HTTPException on any failure.
    """
    if not STEAM_PUBLISHER_KEY:
        raise HTTPException(status_code=500, detail="STEAM_PUBLISHER_KEY is not configured")

    ticket_hex = (ticket_hex or "").strip()
    if not ticket_hex:
        raise HTTPException(status_code=400, detail="ticket is empty")

    # cheap sanity check before spending a rate-limited call on obvious garbage
    if len(ticket_hex) % 2 != 0 or not all(c in "0123456789abcdefABCDEF" for c in ticket_hex):
        raise HTTPException(status_code=400, detail="ticket is not a hex string")

    try:
        r = httpx.get(
            STEAM_AUTH_URL,
            params={
                "key": STEAM_PUBLISHER_KEY,
                "appid": STEAM_APP_ID,
                "ticket": ticket_hex,
                "identity": STEAM_IDENTITY,
            },
            timeout=10.0,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Could not reach Steam auth service")

    if r.status_code != 200:
        # 401/403 here usually means the publisher key is wrong or not tied to this appid
        raise HTTPException(status_code=503, detail=f"Steam auth returned HTTP {r.status_code}")

    try:
        body = r.json().get("response", {})
    except ValueError:
        raise HTTPException(status_code=503, detail="Steam auth returned malformed JSON")

    if "error" in body:
        # expired ticket, reused ticket, identity mismatch, etc.
        desc = body["error"].get("errordesc", "unknown")
        raise HTTPException(status_code=401, detail=f"Steam ticket rejected: {desc}")

    params = body.get("params", {})

    if params.get("result") != "OK":
        raise HTTPException(status_code=401, detail="Steam ticket not OK")

    steam_id = params.get("steamid")
    if not steam_id:
        raise HTTPException(status_code=401, detail="Steam ticket returned no steamid")

    if params.get("publisherbanned"):
        raise HTTPException(status_code=403, detail="Account is banned")

    # Family sharing: ownersteamid is who owns the app, steamid is who is playing.
    # We identify by the player, not the owner — but this is where you'd gate
    # borrowed copies if you ever want to.
    return str(steam_id)


# ── jwt ──────────────────────────────────────────────────────────────────────
def create_token(player_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": str(player_id),
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(authorization: str) -> str:
    """
    Takes the raw Authorization header value ("Bearer <token>").
    Returns player_id. Raises 401 on anything wrong.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <token>'")

    token = parts[1].strip()

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        # distinct code so the client knows to silently re-auth rather than
        # bounce the user to a login screen
        raise HTTPException(status_code=401, detail="token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    player_id = payload.get("sub")
    if not player_id:
        raise HTTPException(status_code=401, detail="Token has no subject")

    return str(player_id)


# ── fastapi dependencies ─────────────────────────────────────────────────────
def get_player_id(authorization: str = Header(None)) -> str:
    """Required auth. Use once all clients are updated."""
    return decode_token(authorization)


def get_player_id_optional(authorization: str = Header(None)) -> Optional[str]:
    """
    Transitional auth. No header -> None, and the endpoint falls back to the
    client-supplied player_id. A present-but-bad header still 401s, so this
    never silently downgrades a real token.
    """
    if not authorization:
        return None
    return decode_token(authorization)


DEV_AUTH_SECRET = os.environ.get("DEV_AUTH_SECRET")

dev_router: Optional[APIRouter] = None

if DEV_AUTH_SECRET:
    dev_router = APIRouter()

    class DevTokenRequest(BaseModel):
        player_id: str

    @dev_router.post("/dev")
    def dev_token(req: DevTokenRequest, x_dev_secret: str = Header(None)):
        if not x_dev_secret or not hmac.compare_digest(x_dev_secret, DEV_AUTH_SECRET):
            raise HTTPException(status_code=401, detail="Invalid dev secret")
        return {"player_id": req.player_id, "token": create_token(req.player_id)}