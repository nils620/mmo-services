import os
import asyncio
import hmac

import socketio
import psycopg
from aiohttp import web

from auth import decode_token

#SocketIO ChatService for Dedicated Servers

# Create an Async Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins="*")  # Enable CORS for testing
app = web.Application()  # Create the web application
sio.attach(app)  # Attach Socket.IO to the web app

# ── env ──────────────────────────────────────────────────────────────────────
DB_DSN = os.environ.get("DB_DSN")
# Shared secret so only our own backend can push notifications through /notify.
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET")

if not DB_DSN:
    raise RuntimeError("DB_DSN is not set. Put it into the systemd Environment/EnvironmentFile.")

clients = []  # Tracks connected clients
user_rooms = {}

sid_to_identity = {}  # sid -> {player_id, character_id, character_name}
character_to_sid = {}  # character_id -> sid

global_chat_event = "globalmsg"
private_chat_event = "privatemsg"
local_chat_event = "localmsg"
server_chat_event = "server"
register_event = "register"
notify_event = "notify"

# Serve a simple HTML page for web clients
async def index(request):
    return web.Response(text="<h1>Nothing to see here...</h1>", content_type="text/html")

app.router.add_get('/', index)  # Route '/' to the index handler


async def broadcast_online_count(to_sid=None):
    """
    Counts registered characters, not raw sockets — a connected-but-unregistered
    socket is not a player yet. Pass to_sid to send only to one client.
    """
    json_msg = {"users": len(character_to_sid)}
    if to_sid:
        await sio.emit(server_chat_event, json_msg, room=to_sid)
    else:
        await sio.emit(server_chat_event, json_msg)


def make_sender_payload(sid):
    identity = sid_to_identity.get(sid, {})
    return {
        "player_id": identity.get("player_id", ""),
        "character_id": identity.get("character_id", ""),
        "character_name": identity.get("character_name", "Unknown"),
    }


# ── db ───────────────────────────────────────────────────────────────────────
def _fetch_character_sync(character_id: str, player_id: str):
    """
    Returns character_name if this character belongs to this player, else None.
    Sync psycopg — call via asyncio.to_thread so the event loop keeps running.
    """
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT character_name FROM characters WHERE id = %s AND player_id = %s;",
                (character_id, player_id),
            )
            row = cur.fetchone()
    return row[0] if row else None


# Handle client connection
@sio.event
async def connect(sid, environ):
    clients.append(sid)
    # No count broadcast here — the socket is connected but has no identity yet,
    # and emitting from inside the connect handler is unreliable anyway.
    print(f"Socket connected | total sockets: {len(clients)}")


# Handle client disconnection
@sio.event
async def disconnect(sid):
    if sid in clients:
        clients.remove(sid)

    identity = sid_to_identity.pop(sid, None)
    if identity:
        # only clear the mapping if it still points at this sid
        char_id = identity.get("character_id")
        if character_to_sid.get(char_id) == sid:
            character_to_sid.pop(char_id, None)
        who = identity.get("character_name", "Unknown")
    else:
        who = "Unknown"

    user_rooms.pop(sid, None)

    await broadcast_online_count()
    print(f"{who} disconnected | registered: {len(character_to_sid)} | sockets: {len(clients)}")


@sio.event
async def register(sid, data):
    # Required payload:
    # {
    #   "token": "...",           # JWT from /auth/login — new clients
    #   "character_id": "...",
    #   "player_id": "...",       # legacy fallback, ignored when token present
    #   "character_name": "..."   # legacy fallback, resolved from DB when token present
    # }
    token = (data.get("token") or "").strip()
    character_id = (data.get("character_id") or "").strip()

    if not character_id:
        await sio.emit(server_chat_event, {"error": "register missing character_id"}, room=sid)
        print(f"Register failed for SID {sid}: no character_id")
        return

    if token:
        # Trusted path: identity comes from the token, name comes from the DB.
        try:
            player_id = decode_token(f"Bearer {token}")
        except Exception:
            await sio.emit(server_chat_event, {"error": "register invalid token"}, room=sid)
            print(f"Register failed for SID {sid}: invalid token")
            return

        try:
            character_name = await asyncio.to_thread(
                _fetch_character_sync, character_id, player_id
            )
        except Exception as e:
            await sio.emit(server_chat_event, {"error": "register lookup failed"}, room=sid)
            print(f"Register lookup error for SID {sid}: {e}")
            return

        if not character_name:
            await sio.emit(server_chat_event, {"error": "character not owned by player"}, room=sid)
            print(f"Register rejected for SID {sid}: char {character_id} not owned")
            return
    else:
        # Legacy path — trusts the client. Remove once every client sends a token.
        player_id = (data.get("player_id") or "").strip()
        character_name = (data.get("character_name") or "").strip()
        if not player_id or not character_name:
            await sio.emit(
                server_chat_event,
                {"error": "register missing player_id/character_id/character_name"},
                room=sid,
            )
            print(f"Register failed for SID {sid}: {data}")
            return

    # Prevent two sockets claiming the same character_id
    old_sid = character_to_sid.get(character_id)
    if old_sid and old_sid != sid:
        # drop the stale identity first so its disconnect handler can't
        # clear the mapping we are about to set
        sid_to_identity.pop(old_sid, None)
        try:
            await sio.disconnect(old_sid)
        except Exception:
            pass

    identity = {
        "player_id": player_id,
        "character_id": character_id,
        "character_name": character_name,
    }
    sid_to_identity[sid] = identity
    character_to_sid[character_id] = sid

    print(f"Registered: {character_name} | char_id={character_id} | player_id={player_id} | SID={sid}")

    # Tell everyone the count changed, and make sure the new client gets it
    # immediately rather than waiting for the next join/leave.
    await broadcast_online_count()


@sio.event
async def enter_local(sid, msg):
    room = msg.get("room")
    if not room:
        return
    await sio.enter_room(sid, room)
    user_rooms[sid] = room
    identity = sid_to_identity.get(sid, {})
    name = identity.get("character_name", "Unknown")
    print(f"{name} joined room {room}")


@sio.event
async def leave_local(sid, msg):
    # Removes the client (sid) from a specific room.
    room = (msg or {}).get("room") or user_rooms.get(sid)
    if not room:
        return
    await sio.leave_room(sid, room)
    if user_rooms.get(sid) == room:
        del user_rooms[sid]
    print(f"User {sid} left room {room}")


# Handle messages
@sio.event
async def globalmsg(sid, msg):
    message = msg.get("msg")
    sender = make_sender_payload(sid)
    json_msg = {**sender, "msg": message}
    await sio.emit(global_chat_event, json_msg)
    print(f"Global/{sender['character_name']}: {message}")

@sio.event
async def localmsg(sid, msg):
    message = msg.get("msg")
    sender = make_sender_payload(sid)
    json_msg = {**sender, "msg": message}

    if sid in user_rooms:
        room = user_rooms[sid]
        print(f"Local/{room}/{sender['character_name']}: {message}")
        await sio.emit(local_chat_event, json_msg, room=room)
    else:
        print(f"User {sid} is not in any room. Message ignored.")


@sio.event
async def privatemsg(sid, msg):
    sender = make_sender_payload(sid)
    to_character_id = (msg.get("to_character_id") or "").strip()
    message = msg.get("msg")

    # Can't message yourself
    if to_character_id and to_character_id == sender.get("character_id"):
        await sio.emit(
            private_chat_event,
            {**sender, "to_character_id": to_character_id, "msg": message, "error": "cannot_message_self"},
            room=sid,
        )
        return

    receiver_sid = character_to_sid.get(to_character_id)

    json_msg = {
        **sender,
        "to_character_id": to_character_id,
        "msg": message,
    }

    if receiver_sid and receiver_sid in sio.manager.rooms.get("/", {}):
        await sio.emit(private_chat_event, json_msg, room=receiver_sid)
        print(f"Private/{sender['character_name']} -> {to_character_id}: {message}")
    else:
        await sio.emit(
            private_chat_event,
            {**json_msg, "error": "recipient_not_online"},
            room=sid
        )
        print(f"Private message failed: {to_character_id} is not online.")


# ── internal notify bridge ───────────────────────────────────────────────────
# The profiles service calls this after a successful DB write so the recipient
# gets an instant push instead of waiting for a poll.
#
#   POST /notify
#   Header: X-Notify-Secret: <shared secret>
#   Body:   {"character_id": "...", "event_type": "friend_request", "payload": {...}}
#
# The client listens on a single "notify" socket event and branches on
# event_type, so new notification kinds need no server changes here.
async def notify(request):
    if not NOTIFY_SECRET:
        return web.json_response({"error": "notify not configured"}, status=503)

    provided = request.headers.get("X-Notify-Secret") or ""
    if not hmac.compare_digest(provided, NOTIFY_SECRET):
        return web.json_response({"error": "invalid secret"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    character_id = (data.get("character_id") or "").strip()
    event_type = (data.get("event_type") or "").strip()
    payload = data.get("payload") or {}

    if not character_id or not event_type:
        return web.json_response({"error": "character_id and event_type required"}, status=400)

    sid = character_to_sid.get(character_id)
    if not sid:
        # Not online. Caller decides whether to persist it for later delivery.
        return web.json_response({"ok": True, "delivered": False})

    await sio.emit(
        notify_event,
        {"event_type": event_type, "payload": payload},
        room=sid,
    )
    print(f"Notify/{event_type} -> {character_id}")
    return web.json_response({"ok": True, "delivered": True})

app.router.add_post("/notify", notify)


# Return Health Checks from Load Balancer
async def health(request):
    return web.Response(text="ok")  # HTTP 200
app.router.add_get("/health", health)


#   POST /online used to handle friends online status
#   Header: X-Notify-Secret: <shared secret>
#   Body:   {"character_ids": ["...", "..."]}
#   Returns {"online": ["...", ...]}  — the subset that is currently connected
async def online(request):
    if not NOTIFY_SECRET:
        return web.json_response({"error": "notify not configured"}, status=503)

    provided = request.headers.get("X-Notify-Secret") or ""
    if not hmac.compare_digest(provided, NOTIFY_SECRET):
        return web.json_response({"error": "invalid secret"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    character_ids = data.get("character_ids") or []
    if not isinstance(character_ids, list):
        return web.json_response({"error": "character_ids must be a list"}, status=400)

    online_ids = [cid for cid in character_ids if cid in character_to_sid]
    return web.json_response({"online": online_ids})


app.router.add_post("/online", online)

# Start the server
if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=4000)
