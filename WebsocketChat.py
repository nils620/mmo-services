from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict

app = FastAPI()

# Store active WebSocket connections
active_connections: Dict[str, WebSocket] = {}

@app.websocket("/ws/{username}")
async def chat_websocket(websocket: WebSocket, username: str):
    await websocket.accept()
    active_connections[username] = websocket
    print(f"User {username} connected.")

    try:
        while True:
            # Wait for a message from the user
            message = await websocket.receive_text()
            print(f"Message from {username}: {message}")

            # Parse the incoming message
            # Expected format: {"to": "user2", "message": "Hello, world!"}
            data = eval(message)  # Use JSON in production
            target_username = data.get("to")
            text_message = data.get("message")

            # Format the outgoing message with sender and recipient details
            formatted_message = {
                "from": username,
                "to": target_username,
                "message": text_message
            }

            # Send the message to the target user if they are online
            if target_username in active_connections:
                target_websocket = active_connections[target_username]
                await target_websocket.send_text(str(formatted_message))  # Use JSON.dumps in production
            else:
                # Inform the sender that the recipient is not online
                error_message = {
                    "error": f"User {target_username} is not online."
                }
                await websocket.send_text(str(error_message))  # Use JSON.dumps in production
    except WebSocketDisconnect:
        print(f"User {username} disconnected.")
        del active_connections[username]