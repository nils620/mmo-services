import socketio
import asyncio
from aiohttp import web

#SocketIO ChatService for Dedicated Servers

# Create an Async Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins="*")  # Enable CORS for testing
app = web.Application()  # Create the web application
sio.attach(app)  # Attach Socket.IO to the web app

clients = []  # Tracks connected clients
user_rooms = {}

sid_to_identity = {}  # sid -> {player_id, character_id, character_name}
character_to_sid = {}  # character_id -> sid

global_chat_event = "globalmsg"
private_chat_event = "privatemsg"
local_chat_event = "localmsg"
server_chat_event = "server"
register_event = "register"

# Serve a simple HTML page for web clients
async def index(request):
    return web.Response(text="<h1>Nothing to see here...</h1>", content_type="text/html")

app.router.add_get('/', index)  # Route '/' to the index handler


def make_sender_payload(sid):
    identity = sid_to_identity.get(sid, {})
    return {
        "player_id": identity.get("player_id", ""),
        "character_id": identity.get("character_id", ""),
        "character_name": identity.get("character_name", "Unknown"),
    }


# Handle client connection
@sio.event
async def connect(sid, environ):
    clients.append(sid)
    json_msg = {"users": len(clients)}

    await sio.emit(server_chat_event, json_msg)  # Broadcast to all clients
    print(f"User connected | total users: {len(clients)}")


# Handle client disconnection
@sio.event
async def disconnect(sid):
    if sid in clients:
        clients.remove(sid)

    identity = sid_to_identity.pop(sid, None)
    if identity:
        character_to_sid.pop(identity.get("character_id"), None)
        who = identity.get("character_name", "Unknown")
    else:
        who = "Unknown"

    json_msg = {"users": len(clients)}
    await sio.emit(server_chat_event, json_msg)
    print(f"{who} disconnected, total: {len(clients)}")


@sio.event
async def register(sid, data):
    # Required payload:
    # {
    #   "player_id": "...",
    #   "character_id": "...",
    #   "character_name": "..."
    # }
    player_id = (data.get("player_id") or "").strip()
    character_id = (data.get("character_id") or "").strip()
    character_name = (data.get("character_name") or "").strip()

    if not player_id or not character_id or not character_name:
        await sio.emit(
            server_chat_event,
            {"error": "register missing player_id/character_id/character_name"},
            room=sid,
        )
        print(f"Register failed for SID {sid}: {data}")
        return

    # Optional: prevent two sockets claiming same character_id
    old_sid = character_to_sid.get(character_id)
    if old_sid and old_sid != sid:
        # kick old session mapping (optional)
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


@sio.event
async def enter_local(sid, msg):
    room = msg.get("room")
    await sio.enter_room(sid, room)
    user_rooms[sid] = room
    identity = sid_to_identity.get(sid, {})
    name = identity.get("character_name", "Unknown")
    print(f"{name} joined room {room} users in room: {len(user_rooms)}")
    #await sio.emit("enter_local", {"room": room, "sid": sid}, room=room)


@sio.event
async def leave_local(sid, msg):
    #Removes the client (sid) from a specific room.
    sio.leave_room(sid, room)
    if sid in user_rooms and user_rooms[sid] == room:
        del user_rooms[sid]
    print(f"User {sid} left room {room}")
    #await sio.emit("leave_local", {"room": room, "sid": sid}, room=room)


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


# Return Health Checks from Load Balancer
async def health(request):
    return web.Response(text="ok")  # HTTP 200
app.router.add_get("/health", health)



# Start the server
if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=4000)

