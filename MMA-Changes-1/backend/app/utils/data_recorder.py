# app/utils/data_recorder.py

# 1 导入依赖
import json
import os
from typing import Any, Dict
from app.utils.log_util import logger


# 2 数据记录器
class DataRecorder:
    def __init__(self, log_work_dir: str = ""):
        self.total_cost = 0.0
        self.agents_chat_history = {}  # {"agent_name": [msg1, msg2, ...]}
        self.chat_completion = {}  # {"agent_name": [ChatCompletion, ...]}
        self.log_work_dir = log_work_dir
        self.token_usage = {}
        self.initialized = True

    # 2.1 打印统计摘要
    def print_summary(self):
        logger.info("\n=== Token Usage and Cost Summary ===")

        headers = ["Agent", "Chats", "Prompt", "Completion", "Total", "Cost ($)"]
        rows = []

        for agent_name, usage in self.token_usage.items():
            rows.append(
                [
                    agent_name,
                    usage["chat_count"],
                    usage["prompt_tokens"],
                    usage["completion_tokens"],
                    usage["total_tokens"],
                    f"{usage['cost']:.4f}",
                ]
            )

        total_chats = sum(usage["chat_count"] for usage in self.token_usage.values())
        total_prompt = sum(usage["prompt_tokens"] for usage in self.token_usage.values())
        total_completion = sum(usage["completion_tokens"] for usage in self.token_usage.values())
        total_tokens = sum(usage["total_tokens"] for usage in self.token_usage.values())

        rows.append(["TOTAL", total_chats, total_prompt, total_completion, total_tokens, f"{self.total_cost:.4f}"])

        from utils.RichPrinter import RichPrinter

        RichPrinter.table(
            headers=headers,
            rows=rows,
            title="Token Usage and Cost Summary",
            column_styles=["cyan", "magenta", "blue", "blue", "blue", "green"],
        )

    # 2.2 写入 JSON 文件
    def write_to_json(self, to_save: dict, file_name: str):
        if self.log_work_dir:
            json_path = os.path.join(self.log_work_dir, file_name)
            try:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(to_save, f, ensure_ascii=False, indent=4)
            except Exception as e:
                logger.error(f"写入json文件失败: {e}")

    # 2.3 添加聊天历史
    def append_chat_history(self, msg: dict, agent_name: str) -> None:
        if agent_name not in self.agents_chat_history:
            self.agents_chat_history[agent_name] = []
        self.agents_chat_history[agent_name].append(msg)
        self.write_to_json(self.agents_chat_history, "chat_history.json")

    # 2.4 转换 ChatCompletion 为 dict
    def chat_completion_to_dict(self, completion: Any) -> Dict:
        return {
            "id": completion.id,
            "choices": [
                {
                    "index": choice.index,
                    "message": {
                        "role": choice.message.role,
                        "content": choice.message.content,
                        "tool_calls": (
                            [
                                {
                                    "id": tool_call.id,
                                    "type": tool_call.type,
                                    "function": {
                                        "name": tool_call.function.name,
                                        "arguments": tool_call.function.arguments,
                                    },
                                }
                                for tool_call in (choice.message.tool_calls or [])
                            ]
                            if hasattr(choice.message, "tool_calls")
                            else None
                        ),
                    },
                    "finish_reason": choice.finish_reason,
                }
                for choice in completion.choices
            ],
            "created": completion.created,
            "model": completion.model,
            "usage": (
                {
                    "completion_tokens": completion.usage.completion_tokens,
                    "prompt_tokens": completion.usage.prompt_tokens,
                    "total_tokens": completion.usage.total_tokens,
                }
                if hasattr(completion, "usage")
                else None
            ),
            "system_fingerprint": (
                completion.system_fingerprint if hasattr(completion, "system_fingerprint") else None
            ),
        }

    # 2.5 添加 ChatCompletion
    def append_chat_completion(self, completion: Any, agent_name: str) -> None:
        if agent_name not in self.chat_completion:
            self.chat_completion[agent_name] = []

        completion_dict = self.chat_completion_to_dict(completion)
        self.chat_completion[agent_name].append(completion_dict)

        self.update_token_usage(completion, agent_name)
        self.write_to_json(self.chat_completion, "chat_completion.json")

    # 2.6 更新 token 使用统计
    def update_token_usage(self, completion: Any, agent_name: str) -> None:
        if not hasattr(completion, "usage"):
            return

        if agent_name not in self.token_usage:
            self.token_usage[agent_name] = {
                "completion_tokens": 0,
                "prompt_tokens": 0,
                "total_tokens": 0,
                "chat_count": 0,
                "cost": 0.0,
            }

        usage = completion.usage
        model = completion.model

        self.token_usage[agent_name]["completion_tokens"] += usage.completion_tokens
        self.token_usage[agent_name]["prompt_tokens"] += usage.prompt_tokens
        self.token_usage[agent_name]["total_tokens"] += usage.total_tokens
        self.token_usage[agent_name]["chat_count"] += 1

        cost = self.calculate_cost(model, usage.prompt_tokens, usage.completion_tokens)
        self.token_usage[agent_name]["cost"] += cost
        self.total_cost += cost

        self.write_to_json(self.token_usage, "token_usage.json")

    # 2.7 计算 API 调用费用
    def calculate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        model_prices = {
            "gpt-4-turbo-preview": {"prompt": 0.01, "completion": 0.03},
            "gpt-4": {"prompt": 0.03, "completion": 0.06},
            "gpt-3.5-turbo": {"prompt": 0.0005, "completion": 0.0015},
            "qwen-max-latest": {"prompt": 0.0024, "completion": 0.0096},
        }

        model_price = model_prices.get(model, {"prompt": 0.0001, "completion": 0.0001})
        prompt_cost = (prompt_tokens / 1000.0) * model_price["prompt"]
        completion_cost = (completion_tokens / 1000.0) * model_price["completion"]

        return prompt_cost + completion_cost
