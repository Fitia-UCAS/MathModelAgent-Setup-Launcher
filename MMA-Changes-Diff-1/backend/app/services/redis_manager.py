# app/services/redis_manager.py

import redis.asyncio as aioredis
from typing import Optional, Any, Dict, Union
import json
from pathlib import Path
from app.config.setting import settings
from app.schemas.response import Message  # v1/v2 pydantic model
from app.utils.log_util import logger
from uuid import uuid4


def _json_safe(obj: Any) -> Any:
    """
    递归将对象转换为 JSON 可序列化的结构：
    - Pydantic v1/v2 -> dict()
    - dict/list/tuple/set 递归处理（set -> list；tuple -> list）
    - bytes -> utf-8 解码失败则转十六进制字符串
    - 其余不可序列化对象 -> str(obj)
    """
    # 原生可序列化类型
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    # Pydantic v2/v1
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            return _json_safe(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict") and callable(obj.dict):
        try:
            return _json_safe(obj.dict())
        except Exception:
            pass

    # 容器类型
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return [_json_safe(v) for v in obj]

    # bytes
    if isinstance(obj, (bytes, bytearray, memoryview)):
        try:
            return bytes(obj).decode("utf-8")
        except Exception:
            return bytes(obj).hex()

    # 其他：转字符串兜底
    try:
        return str(obj)
    except Exception:
        return f"<unserializable:{type(obj).__name__}>"


def _normalize_message_payload(message: Union[Message, Dict[str, Any]]) -> Dict[str, Any]:
    """
    统一清洗消息对象为 JSON-safe dict（不强制把 content 变成字符串）：
    1) 支持 Pydantic v1/v2（优先用 model_dump / dict）
    2) 兜底生成 id / msg_type
    3) 对整个 payload 做 _json_safe，确保可序列化
    """
    # 1) 提取为字典
    if hasattr(message, "model_dump"):  # pydantic v2
        raw = message.model_dump()
    elif hasattr(message, "dict"):  # pydantic v1
        raw = message.dict()
    elif isinstance(message, dict):
        raw = dict(message)
    else:
        # 极端兜底：未知类型
        raw = {
            "id": str(uuid4()),
            "msg_type": "system",
            "content": f"{message}",
            "type": "warning",
        }

    # 2) id / msg_type 兜底
    if not isinstance(raw.get("id"), str) or not raw.get("id"):
        raw["id"] = str(uuid4())
    if not isinstance(raw.get("msg_type"), str) or not raw.get("msg_type"):
        raw["msg_type"] = "system"

    # 3) JSON 安全化（保持 content 的原始结构：str/dict/list…）
    payload = _json_safe(raw)

    # 最后校验：如仍不可序列化，逐字段兜底
    try:
        json.dumps(payload, ensure_ascii=False)
    except TypeError:
        safe_payload: Dict[str, Any] = {}
        for k, v in payload.items():
            try:
                json.dumps({k: v}, ensure_ascii=False)
                safe_payload[k] = v
            except TypeError:
                safe_payload[k] = _json_safe(str(v))
        payload = safe_payload

    return payload


class RedisManager:
    def __init__(self):
        self.redis_url = settings.REDIS_URL
        self._client: Optional[aioredis.Redis] = None
        # 创建消息存储目录
        self.messages_dir = Path("logs/messages")
        self.messages_dir.mkdir(parents=True, exist_ok=True)

    async def get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                max_connections=getattr(settings, "REDIS_MAX_CONNECTIONS", None),
            )
        logger.info(f"Redis 连接建立成功: {self.redis_url}")
        return self._client

    async def set(self, key: str, value: str):
        """设置Redis键值对"""
        client = await self.get_client()
        await client.set(key, value)
        # 10 小时过期（保持你原来逻辑）
        await client.expire(key, 36000)

    async def _save_message_to_file(self, task_id: str, message: Union[Message, Dict[str, Any]]):
        """将消息保存到文件中，同一任务的消息保存在同一个文件中"""
        try:
            payload = _normalize_message_payload(message)

            # 确保目录存在
            self.messages_dir.mkdir(exist_ok=True)

            # 使用任务ID作为文件名
            file_path = self.messages_dir / f"{task_id}.json"

            # 读取现有消息（如果文件存在）
            messages = []
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as f:
                    try:
                        messages = json.load(f)
                    except Exception:
                        messages = []

            # 添加新消息
            messages.append(payload)

            # 保存所有消息到文件
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

            logger.debug(f"消息已追加到文件: {file_path}")
        except Exception as e:
            logger.error(f"保存消息到文件失败: {str(e)}")
            # 不抛异常，保证主流程不受影响

    async def publish_message(self, task_id: str, message: Union[Message, Dict[str, Any]]):
        """
        发布消息到特定任务的频道并保存到文件
        - 仅对“最外层 payload”序列化一次
        - 不再把 payload['content'] 强制转为字符串
        """
        client = await self.get_client()
        channel = f"task:{task_id}:messages"
        payload = _normalize_message_payload(message)
        try:
            # 构造预览字符串（不改变 content 原类型）
            try:
                content_preview = (
                    payload.get("content", "")
                    if isinstance(payload.get("content", ""), str)
                    else json.dumps(payload.get("content", ""), ensure_ascii=False)
                )
            except Exception:
                content_preview = "<preview 生成失败>"

            # 仅序列化一层
            message_json = json.dumps(payload, ensure_ascii=False)
            publish_result = await client.publish(channel, message_json)
            logger.info(
                f"发布到频道 {channel} 成功，订阅者数量: {publish_result}. "
                f"msg_type:{payload.get('msg_type')} "
                f"content_preview:{str(content_preview)[:200]}"
            )

            # 落盘
            await self._save_message_to_file(task_id, payload)

            return publish_result

        except Exception as e:
            logger.exception(f"发布消息失败: task_id={task_id} channel={channel} error={e}")
            raise

    async def subscribe_to_task(self, task_id: str):
        """订阅特定任务的消息"""
        client = await self.get_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(f"task:{task_id}:messages")
        return pubsub

    async def close(self):
        """关闭Redis连接"""
        if self._client:
            await self._client.close()
            self._client = None


# singleton
redis_manager = RedisManager()
