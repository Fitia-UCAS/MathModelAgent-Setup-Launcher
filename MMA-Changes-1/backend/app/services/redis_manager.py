# app/services/redis_manager.py

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

import redis.asyncio as aioredis

from app.config.setting import settings
from app.utils.log_util import logger


# ========== 1 工具函数 ==========


def _json_safe(obj: Any) -> Any:
    """尽量转成可 JSON 序列化的结构；容器递归处理，其它不可序列化的转 str。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except TypeError:
        return str(obj)


def _normalize_message_payload(message: Union[Dict[str, Any], Any]) -> Dict[str, Any]:
    """将任意“消息对象”规整为 JSON-safe dict；兼容 pydantic v1/v2；其它对象转字符串兜底。"""
    if hasattr(message, "model_dump"):  # pydantic v2
        raw = message.model_dump()
    elif hasattr(message, "dict"):  # pydantic v1
        raw = message.dict()
    elif isinstance(message, dict):
        raw = dict(message)
    else:
        raw = {"content": str(message), "msg_type": "system", "type": "info"}

    if "msg_type" not in raw or not isinstance(raw.get("msg_type"), str):
        raw["msg_type"] = "system"

    payload = _json_safe(raw)

    # 最终确保可 JSON
    try:
        json.dumps(payload, ensure_ascii=False)
    except TypeError:
        safe: Dict[str, Any] = {}
        for k, v in payload.items():
            try:
                json.dumps({k: v}, ensure_ascii=False)
                safe[k] = v
            except TypeError:
                safe[k] = str(v)
        payload = safe
    return payload


# ========== 2 Redis 管理器 ==========
class RedisManager:
    """
    扩展后的 Redis 管理器：
    - 连接：get_client()
    - 发布消息：publish_message()  -> Redis + 归档到 logs/messages/NNN.json（单文件、数组追加）
    - 订阅：subscribe_to_task()
    - 键值操作：set/get/delete/exists/incr
    - Hash 操作：hset/hgetall
    说明：
    1) 不再写入 backend/logs/errors/*（已删除 error 落盘逻辑）。
    2) 日志编号 NNN 与 launcher 一致：按当前最大号 + 1 生成；一次运行只使用同一个 NNN。
    """

    def __init__(self):
        self.redis_url = settings.REDIS_URL
        self._client: Optional[aioredis.Redis] = None

        # 后端根目录：.../backend
        self.backend_dir = Path(__file__).resolve().parents[2]

        # 统一日志目录
        self.logs_root: Path = self.backend_dir / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True)

        # messages（一次运行仅写一个 NNN.json；内部是 JSON 数组）
        self.messages_dir: Path = self.logs_root / "messages"
        self.messages_dir.mkdir(parents=True, exist_ok=True)

        # 运行编号（与 launcher 规则一致）
        self.run_id: str = self._resolve_run_id()
        self.msg_file: Path = self.messages_dir / f"{self.run_id}.json"

    # ---------- 2.1 连接 ----------
    async def get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._client

    # ---------- 2.2 基础 KV ----------
    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        client = await self.get_client()
        if isinstance(value, (str, bytes)):
            v = value
        else:
            try:
                v = json.dumps(value, ensure_ascii=False)
            except TypeError:
                v = str(value)
        ok = await client.set(key, v, ex=ex)
        return bool(ok)

    async def get(self, key: str) -> Any:
        client = await self.get_client()
        raw = await client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    async def delete(self, *keys: str) -> int:
        client = await self.get_client()
        return int(await client.delete(*keys))

    async def exists(self, key: str) -> bool:
        client = await self.get_client()
        return bool(await client.exists(key))

    async def incr(self, key: str) -> int:
        client = await self.get_client()
        return int(await client.incr(key))

    # ---------- 2.3 Hash ----------
    async def hset(self, name: str, mapping: Dict[str, Any]) -> int:
        client = await self.get_client()
        safe_map: Dict[str, str] = {}
        for k, v in (mapping or {}).items():
            if isinstance(v, (str, bytes, int, float, bool)) or v is None:
                safe_map[str(k)] = "" if v is None else str(v)
            else:
                try:
                    safe_map[str(k)] = json.dumps(v, ensure_ascii=False)
                except TypeError:
                    safe_map[str(k)] = str(v)
        return int(await client.hset(name, mapping=safe_map))

    async def hgetall(self, name: str) -> Dict[str, Any]:
        client = await self.get_client()
        data = await client.hgetall(name)
        out: Dict[str, Any] = {}
        for k, v in data.items():
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        return out

    # ---------- 2.4 发布/订阅 ----------
    async def publish_message(self, task_id: str, message: Union[Dict[str, Any], Any]):
        """
        发布任务消息到 Redis，并把消息**追加**到 logs/messages/NNN.json（数组）。
        不再写入 logs/errors。
        """
        client = await self.get_client()
        channel = f"task:{task_id}:messages"
        payload = _normalize_message_payload(message)

        # 发布到 Redis
        msg_json = json.dumps(payload, ensure_ascii=False)
        publish_result = await client.publish(channel, msg_json)

        # 归档到单一文件（数组追加）
        path = self._append_message_json(payload)

        # 控制台日志
        preview = payload.get("content", "")
        if not isinstance(preview, str):
            try:
                preview = json.dumps(preview, ensure_ascii=False)
            except Exception:
                preview = str(preview)
        logger.info(
            f"发布到频道 {channel} 成功，订阅者数量: {publish_result}. "
            f"消息已写入: {path.name}. 预览: {preview[:200]}"
        )

    async def subscribe_to_task(self, task_id: str):
        client = await self.get_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(f"task:{task_id}:messages")
        return pubsub

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None

    # ---------- 2.5 内部：编号 & 归档 ----------
    def _resolve_run_id(self, pad: int = 3) -> str:
        """与 launcher 相同策略：work_dir -> messages -> laucher，自增 1。"""
        # 1) backend/project/work_dir 下的数字目录
        try:
            work_dir = self.backend_dir / "project" / "work_dir"
            if work_dir.exists():
                max_n = 0
                for p in work_dir.iterdir():
                    m = re.fullmatch(r"(\d+)", p.name)
                    if m:
                        try:
                            n = int(m.group(1))
                            if n > max_n:
                                max_n = n
                        except Exception:
                            pass
                return f"{max_n + 1:0{pad}d}"
        except Exception:
            pass

        # 2) backend/logs/messages 下的 NNN.json
        try:
            msg_dir = self.backend_dir / "logs" / "messages"
            if msg_dir.exists():
                max_n = 0
                for p in msg_dir.iterdir():
                    m = re.search(r"(\d+)\.json$", p.name)
                    if m:
                        try:
                            n = int(m.group(1))
                            if n > max_n:
                                max_n = n
                        except Exception:
                            pass
                return f"{max_n + 1:0{pad}d}"
        except Exception:
            pass

        # 3) backend/logs/laucher 下的 NNN.log
        try:
            lau = self.backend_dir / "logs" / "laucher"
            idx = 1
            if lau.exists():
                for p in lau.glob("*.log"):
                    m = re.match(r"(\d{3})\.log$", p.name)
                    if m:
                        try:
                            idx = max(idx, int(m.group(1)) + 1)
                        except Exception:
                            pass
            return f"{idx:0{pad}d}"
        except Exception:
            return f"{1:0{pad}d}"

    def _append_message_json(self, payload: Dict[str, Any]) -> Path:
        """
        将 payload 追加到 NNN.json 的数组中。
        - 文件不存在：创建为 [payload]
        - 文件是单个对象：转为 [obj, payload]
        - 文件是数组：append 后整体重写（简单可靠）
        """
        try:
            if not self.msg_file.exists():
                self.msg_file.write_text(json.dumps([payload], ensure_ascii=False, indent=2), encoding="utf-8")
                return self.msg_file

            text = self.msg_file.read_text(encoding="utf-8")
            text_stripped = text.strip()
            data: Union[Dict[str, Any], list]

            if not text_stripped:
                data = []
            else:
                try:
                    data = json.loads(text_stripped)
                except json.JSONDecodeError:
                    # 如果历史文件不是合法 JSON，兜底：当成单条对象
                    data = []

            if isinstance(data, dict):
                data = [data]

            if not isinstance(data, list):
                data = []

            data.append(payload)
            self.msg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.exception(f"写入消息 JSON 失败: {e}")
        return self.msg_file


# 单例
redis_manager = RedisManager()
