import socketio
import asyncio
from aiohttp import web

#SocketIO ChatService for the Server

# Create an Async Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins="*")  # Enable CORS for testing
app = web.Application()  # Create the web application
sio.attach(app)  # Attach Socket.IO to the web app

clients = []  # Track connected clients
sid_to_user = {}
user_to_sid = {}
global_chat_event = "globalmsg"
private_chat_event = "privatemsg"# Event name for chat messages
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
    #client_connected_msg = f"User connected {sid}, total: {len(clients)}"
    await sio.emit(server_chat_event, json_msg)  # Broadcast to all clients
    print(f"User connected total connections: {len(clients)}")


# Handle client disconnection
@sio.event
async def disconnect(sid):
    clients.remove(sid)
    username = "Unknown User"
    if sid in sid_to_user:
        username = sid_to_user.pop(sid)
        user_to_sid.pop(username, None)
    client_disconnected_msg = f"{username} disconnected {sid}, total: {len(clients)}"
    await sio.emit(server_chat_event, client_disconnected_msg)  # Broadcast to all clients
    print(client_disconnected_msg)

@sio.event
async def register(sid, data):
    username = data.get("user")

    sid_to_user[sid] = username
    user_to_sid[username] = sid
    print(f"Registered: {username} with SID: {sid}")


# Handle chat messages
@sio.event
async def globalmsg(sid, msg):
    sender = sid_to_user.get(sid)
    message = msg.get("msg")
    json_msg = {"from": sender, "msg": message}
    await sio.emit(global_chat_event, json_msg)  # Broadcast to all clients
    print(f"Global/{sender}: {message}")

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
"""
@sio.event
async def privatemsg(sid, data):
    # Extract the recipient SID and message from the data
    recipient_sid = data.get("to")  # SID of the target client
    sender_username = data.get("from", "Unknown")
    message_content = data.get("message", "")

    # Create a structured JSON message
    json_message = {
        "from": sender_username,
        "message": message_content
    }

    if recipient_sid in sio.manager.rooms["/"]:
        # Send the message only to the specified client
        await sio.emit(private_chat_event, json_message, room=recipient_sid)
        print(f"Private message sent from {sender_username} to {recipient_sid}: {message_content}")
    else:
        # Notify sender that the recipient is not online
        error_message = {
            "error": f"User with SID {recipient_sid} is not online."
        }
        await sio.emit(private_chat_event, error_message, to=sid)
        print(f"Private message failed: User with SID {recipient_sid} is not online.")
"""


# Start the server
if __name__ == '__main__':
    port = 3000  # Port to run the server on
    web.run_app(app, port=port)


