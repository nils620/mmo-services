import os, jwt, datetime, httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

DB_URL   = os.environ["DATABASE_URL"]
JWT_SEC  = os.environ["JWT_SECRET"]
JWT_ISS  = os.environ.get("JWT_ISS", "potential")
STEAM_APP_ID = os.environ["STEAM_APP_ID"]
STEAM_KEY    = os.environ["STEAM_WEB_API_KEY"]

engine  = create_async_engine(DB_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)
app = FastAPI()

class AuthIn(BaseModel):
    steam_id: str
    ticket: str           # Steam auth session or encrypted app ticket (hex/base64)
    character_name: str

class AuthOut(BaseModel):
    token: str
    player_id: str
    character_id: str
    character_name: str

async def verify_steam_ticket(steam_id: str, ticket: str) -> bool:
    # Use Steam Web API: ISteamUserAuth.AuthenticateUserTicket
    # (Pseudocode; implement with the exact API call you prefer.)
    # Example idea:
    # url = "https://api.steampowered.com/ISteamUserAuth/AuthenticateUserTicket/v1/"
    # data = {"key": STEAM_KEY, "appid": STEAM_APP_ID, "ticket": ticket}
    # async with httpx.AsyncClient() as c:
    #     r = await c.post(url, data=data, timeout=10)
    #     r.raise_for_status()
    #     ok = r.json()["response"]["params"]["result"] == "OK"
    #     claimed_steamid = r.json()["response"]["params"]["steamid"]
    #     return ok and str(claimed_steamid) == str(steam_id)
    return True  # temporarily allow everything while wiring things up

def issue_jwt(player_id: str, character_id: str) -> str:
    now = datetime.datetime.utcnow()
    claims = {
        "iss": JWT_ISS,
        "sub": player_id,
        "cid": character_id,
        "iat": now,
        "exp": now + datetime.timedelta(minutes=30),
    }
    return jwt.encode(claims, JWT_SEC, algorithm="HS256")

@app.post("/auth/steam", response_model=AuthOut)
async def auth_steam(data: AuthIn):
    if not await verify_steam_ticket(data.steam_id, data.ticket):
        raise HTTPException(401, "Steam ticket invalid")

    async with Session() as s:
        # players upsert
        q = await s.execute(text("""
            insert into players (steam_id)
            values (:steam_id)
            on conflict (steam_id) do update set updated_at = now()
            returning id
        """), {"steam_id": data.steam_id})
        player_id = str(q.scalar_one())

        # characters upsert (unique per player_id + name)
        q = await s.execute(text("""
            insert into characters (player_id, name)
            values (:player_id, :name)
            on conflict (player_id, name) do update set updated_at = now()
            returning id
        """), {"player_id": player_id, "name": data.character_name})
        character_id = str(q.scalar_one())

        # ensure profile row exists
        await s.execute(text("""
            insert into profiles (character_id)
            values (:cid)
            on conflict (character_id) do nothing
        """), {"cid": character_id})

        await s.commit()

    token = issue_jwt(player_id, character_id)
    return AuthOut(token=token, player_id=player_id, character_id=character_id, character_name=data.character_name)
