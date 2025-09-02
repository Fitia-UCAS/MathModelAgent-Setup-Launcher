# app/services/ws_manager.py

from fastapi import WebSocket


# 1 WebSocket 管理器
class WebSocketManager:
    def __init__(self):
        # 1.1 活跃连接池
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        # 1.2 接入：接受并加入连接池
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        # 1.3 断开：从连接池移除
        self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        # 1.4 单发文本消息
        await websocket.send_text(message)

    async def send_personal_message_json(self, message: dict, websocket: WebSocket):
        # 1.5 单发 JSON 消息
        await websocket.send_json(message)

    async def broadcast(self, message: str):
        # 1.6 广播文本消息
        for connection in self.active_connections:
            await connection.send_text(message)


# 2 单例
ws_manager = WebSocketManager()
