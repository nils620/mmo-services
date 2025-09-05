import socketio
from aiohttp import web

# Create a Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins="*")
app = web.Application()
sio.attach(app)

# Track registered game servers
game_servers = {}

# Handle game server registration
@sio.event
async def register_server(sid, data):
    """Register a game server with the master server."""
    server_name = data.get("server_name")
    display_name = data.get("display_name")
    ip = data.get("ip")
    port = data.get("port")
    level = data.get("mapname")
    current_players = data.get("current_players", 0)
    max_players = data.get("max_players")

    # Store the server's information
    game_servers[sid] = {
        "display_name": display_name,
        "server_name": server_name,
        "ip": ip,
        "port": port,
        "level": level,
        "current_players": current_players,
        "max_players": max_players,
        "sid": sid,  # Track the server's Socket.IO connection
    }
    print(f"Registered {server_name} | {ip}:{port} | Players: {current_players}/{max_players} | Level: {level}")

    # Notify other systems (optional)
    await sio.emit("server_registered", {"server_name": server_name})

# Handle server updates (e.g., player counts)
@sio.event
async def update_server(sid, data):
    """Update the server's status (e.g., player count)."""
    server_id = data.get("server_id")
    current_players = data.get("current_players")

    if server_id in game_servers:
        game_servers[server_id]["current_players"] = current_players
        print(f"Updated server {server_id}: Players: {current_players}/{game_servers[server_id]['max_players']}")
    else:
        print(f"Server {server_id} not found!")



# Handle client requests for available servers
@sio.event
async def get_servers(sid, data):
    """Send a list of available servers to the client."""
    available_servers = [
        {
            "server_name": server_name,
            "display_name": info["display_name"],
            "ip": info["ip"],
            "port": info["port"],
            "level": info["level"],
            "current_players": info["current_players"],
            "max_players": info["max_players"],
        }
        for server_name, info in game_servers.items()
        #if info["current_players"] < info["max_players"]  # Only return servers with available slots
    ]
    await sio.emit("server_list", {"servers": available_servers}, room=sid)

# Handle game server disconnection
@sio.event
async def disconnect(sid):
    """Remove a disconnected game server."""
    for server_id, info in list(game_servers.items()):
        if info["sid"] == sid:
            print(f"Server {server_id} disconnected")
            del game_servers[server_id]
            break

# Start the server
if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=3000)
    #web.run_app(app, port=3000)


