import redis.asyncio as aioredis
from typing import Optional, Any, Dict, Union
import json
from pathlib import Path
from app.config.setting import settings
from app.schemas.response import Message  # 这里是你定义的 Pydantic 模型（v1/v2 皆可）
from app.utils.log_util import logger
from uuid import uuid4


def _to_str_or_empty(v: Any) -> str:
    """把 None / 非字符串 转成字符串，避免前端/下游被 None 砸到。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return str(v)
    except Exception:
        return ""


def _normalize_message_payload(message: Union[Message, Dict[str, Any]]) -> Dict[str, Any]:
    """
    统一清洗消息对象为 JSON-safe dict：
    1) 支持 Pydantic v1/v2（优先用 model_dump / dict）
    2) 兜底生成 id / msg_type
    3) content 强制非 None、可序列化
    4) 避免不可序列化对象破坏 publish/file-save
    """
    # 1) 提取为字典
    payload: Dict[str, Any]
    if hasattr(message, "model_dump"):  # pydantic v2
        payload = message.model_dump()
    elif hasattr(message, "dict"):      # pydantic v1
        payload = message.dict()
    elif isinstance(message, dict):
        payload = dict(message)
    else:
        # 极端兜底：未知类型
        payload = {
            "id": str(uuid4()),
            "msg_type": "system",
            "content": _to_str_or_empty(message),
            "type": "warning",
        }

    # 2) id / msg_type 兜底
    if not isinstance(payload.get("id"), str) or not payload.get("id"):
        payload["id"] = str(uuid4())
    if not isinstance(payload.get("msg_type"), str) or not payload.get("msg_type"):
        # 默认按 system 处理，避免前端解析失败
        payload["msg_type"] = "system"

    # 3) content 兜底（None → ""；复杂结构 → json.dumps 或 str）
    content = payload.get("content", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        try:
            # 尝试 JSON 序列化（在前端显示时更直观）
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = _to_str_or_empty(content)
    payload["content"] = content

    # 4) 最后再尝试把整个 payload 过一遍 JSON 编码，确保无不可序列化对象
    #    如果失败，就把无法序列化的字段转字符串
    try:
        json.dumps(payload, ensure_ascii=False)
    except TypeError:
        # 逐字段兜底
        safe_payload = {}
        for k, v in payload.items():
            try:
                json.dumps({k: v}, ensure_ascii=False)
                safe_payload[k] = v
            except TypeError:
                safe_payload[k] = _to_str_or_empty(v)
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
                max_connections=settings.REDIS_MAX_CONNECTIONS,
            )
        logger.info(f"Redis 连接建立成功: {self.redis_url}")
        return self._client

    async def set(self, key: str, value: str):
        """设置Redis键值对"""
        client = await self.get_client()
        await client.set(key, value)
        await client.expire(key, 36000)

    async def _save_message_to_file(self, task_id: str, message: Union[Message, Dict[str, Any]]):
        """将消息保存到文件中，同一任务的消息保存在同一个文件中"""
        try:
            # 统一清洗
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
                        # 文件损坏等极端情况，重置
                        messages = []

            # 添加新消息
            messages.append(payload)

            # 保存所有消息到文件
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

            logger.debug(f"消息已追加到文件: {file_path}")
        except Exception as e:
            logger.error(f"保存消息到文件失败: {str(e)}")
            # 不抛出异常，确保主流程不受影响

    async def publish_message(self, task_id: str, message: Union[Message, Dict[str, Any]]):
        """发布消息到特定任务的频道并保存到文件"""
        client = await self.get_client()
        channel = f"task:{task_id}:messages"
        try:
            # —— 统一清洗，避免 content=None / 不可序列化字段 ——
            payload = _normalize_message_payload(message)
            message_json = json.dumps(payload, ensure_ascii=False)

            await client.publish(channel, message_json)
            logger.debug(
                f"消息已发布到频道 {channel}:mes_type:{payload.get('msg_type')}:" 
                f"msg_content:{(payload.get('content') or '')[:200]}"
            )

            # 保存消息到文件（同样使用清洗后的 payload）
            await self._save_message_to_file(task_id, payload)
        except Exception as e:
            logger.error(f"发布消息失败: {str(e)}")
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


redis_manager = RedisManager()
