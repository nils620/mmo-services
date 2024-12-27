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


sid_to_user = {}
user_to_sid = {}
global_chat_event = "globalmsg"
private_chat_event = "privatemsg"
local_chat_event = "localmsg"
server_chat_event = "server"
register_event = "register"

# Serve a simple HTML page for web clients
async def index(request):
    return web.Response(text="<h1>Welcome to the Python Socket.IO Server</h1>", content_type="text/html")

app.router.add_get('/', index)  # Route '/' to the index handler


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
    clients.remove(sid)
    username = "Unknown User"
    if sid in sid_to_user:
        username = sid_to_user.pop(sid)
        user_to_sid.pop(username, None)
    client_disconnected_msg = f"{username} disconnected, total: {len(clients)}"
    json_msg = {"users": len(clients)}

    await sio.emit(server_chat_event, json_msg)  # Broadcast to all clients
    print(client_disconnected_msg)


@sio.event
async def register(sid, data):
    username = data.get("user")
    sid_to_user[sid] = username
    user_to_sid[username] = sid
    print(f"Registered: {username} with SID: {sid}")


@sio.event
async def enter_local(sid, msg):
    room = msg.get("room")
    await sio.enter_room(sid, room)
    user_rooms[sid] = room
    username = sid_to_user[sid]
    print(f"{username} joined room {room} users in room: {len(user_rooms)}")
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
    sender = sid_to_user.get(sid)
    message = msg.get("msg")
    json_msg = {"from": sender, "msg": message}
    await sio.emit(global_chat_event, json_msg)  # Broadcast to all clients
    print(f"Global/{sender}: {message}")

@sio.event
async def localmsg(sid, msg):
    sender = sid_to_user.get(sid)
    message = msg.get("msg")
    json_msg = {"from": sender, "msg": message}
    if sid in user_rooms:
        room = user_rooms[sid]
        print(f"Local/{room}/{sender}: {message}")
        await sio.emit(local_chat_event, json_msg, room=room)
        #Debug Room members
        #members = sio.manager.rooms.get('/', {}).get(room, [])
        #print(f"Room '{room}' members: {members}")
    else:
        print(f"User {sid} is not in any room. Message ignored.")


@sio.event
async def privatemsg(sid, msg):
    sender = sid_to_user.get(sid)
    receiver = msg.get("to")
    message = msg.get("msg")
    receiver_sid = user_to_sid.get(receiver)
    json_msg = {"from":sender, "msg": message}

    if receiver_sid in sio.manager.rooms["/"]:
        # Send the message only to the specified client
        await sio.emit(private_chat_event, json_msg, room=receiver_sid)  # Broadcast to receiver
        print(f"Private/{sender} to {receiver}: {message}")
    else:
        # Notify sender that the recipient is not online
        error_message = f"{receiver} is not online."
        json_error_msg = {"from": sender, "msg": error_message}

        await sio.emit(private_chat_event, json_error_msg, room=sid)
        print(f"Private message failed: {error_message}")



# Start the server
if __name__ == '__main__':
    port = 3000  # Port to run the server on
    web.run_app(app, port=port)


