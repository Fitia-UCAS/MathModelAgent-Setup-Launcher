# app/router/ws_router.py

from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from app.services.redis_manager import redis_manager
from app.schemas.response import SystemMessage
import asyncio
from app.services.ws_manager import ws_manager
import json

# 1 路由初始化
router = APIRouter()


# 2 WebSocket 入口
# 2.1 路径：/task/{task_id}
# 2.2 职责：订阅 Redis 频道，并将消息实时转发给当前 WebSocket 客户端
@router.websocket("/task/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    print(f"WebSocket 尝试连接 task_id: {task_id}")

    # 3 任务合法性校验
    # 3.1 获取 Redis 客户端
    redis_async_client = await redis_manager.get_client()
    # 3.2 校验 task_id 是否存在（不存在则关闭连接）
    if not await redis_async_client.exists(f"task_id:{task_id}"):
        print(f"Task not found: {task_id}")
        await websocket.close(code=1008, reason="Task not found")
        return
    print(f"WebSocket connected for task: {task_id}")

    # 4 建立 WebSocket 连接
    # 4.1 注册到 ws_manager
    await ws_manager.connect(websocket)
    # 4.2 设置超时（单位：秒）
    websocket.timeout = 36000
    print(f"WebSocket connection status: {websocket.client}")

    # 5 订阅 Redis 频道（task:{task_id}:messages）
    pubsub = await redis_manager.subscribe_to_task(task_id)
    print(f"Subscribed to Redis channel: task:{task_id}:messages")

    # 6 首条系统消息（便于前端显示“任务开始处理”）
    await redis_manager.publish_message(
        task_id,
        SystemMessage(content="任务开始处理"),
    )

    # 7 消息转发主循环
    try:
        while True:
            try:
                # 7.1 从 Redis 非阻塞获取消息（忽略订阅确认消息）
                msg = await pubsub.get_message(ignore_subscribe_messages=True)
                if msg:
                    print(f"Received message: {msg}")
                    try:
                        # 7.2 解析 JSON 并转发给当前 WebSocket
                        msg_dict = json.loads(msg["data"])
                        await ws_manager.send_personal_message_json(msg_dict, websocket)
                        print(f"Sent message to WebSocket: {msg_dict}")
                    except Exception as e:
                        # 7.3 解析失败：返回错误信息给前端
                        print(f"Error parsing message: {e}")
                        await ws_manager.send_personal_message_json({"error": str(e)}, websocket)

                # 7.4 减少空闲循环占用
                await asyncio.sleep(0.1)

            except WebSocketDisconnect:
                # 7.5 客户端主动断开
                print("WebSocket disconnected")
                break
            except Exception as e:
                # 7.6 其它异常：记录并短暂休眠后继续
                print(f"Error in websocket loop: {e}")
                await asyncio.sleep(1)
                continue

    except Exception as e:
        # 8 顶层异常保护
        print(f"WebSocket error: {e}")
    finally:
        # 9 资源清理
        # 9.1 取消 Redis 订阅
        await pubsub.unsubscribe(f"task:{task_id}:messages")
        # 9.2 从 ws_manager 注销
        ws_manager.disconnect(websocket)
        print(f"WebSocket connection closed for task: {task_id}")
