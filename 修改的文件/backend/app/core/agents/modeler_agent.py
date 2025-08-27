from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import MODELER_PROMPT
from app.schemas.A2A import CoordinatorToModeler, ModelerToCoder
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager  # ✅ 新增：用于显式发布
from app.schemas.response import ModelerMessage  # ✅ 新增：右侧面板消费的消息类型
import json
import re
from icecream import ic

# === 工具函数 ===


def cleanup_fences(s: str) -> str:
    """去掉 ```json / ``` 围栏"""
    if not s:
        return ""
    return s.replace("```json", "").replace("```", "").strip()


def extract_first_json(text: str) -> str:
    """提取首个配平的 JSON 对象字符串"""
    if not text:
        return ""
    start = text.find("{")
    if start == -1:
        return ""
    stack = []
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                stack.append("{")
            elif ch == "}":
                if stack:
                    stack.pop()
                if not stack:
                    return text[start : i + 1]
    return ""


def try_json_loads(s: str):
    """尝试解析 JSON，失败返回 None"""
    try:
        return json.loads(s)
    except Exception:
        return None


class ModelerAgent(Agent):  # 继承自Agent类
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 500,  # 添加最大对话轮次限制
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = MODELER_PROMPT

    async def run(self, coordinator_to_modeler: CoordinatorToModeler) -> ModelerToCoder:
        """
        解析大模型返回的 JSON：
        1. 清理 Markdown 围栏
        2. 提取首个 JSON
        3. 多轮修复：LLM JsonFixer -> fallback 强制 AST 校正
        4. 严格校验必须是 dict
        5. 解析成功后显式发布 ModelerMessage（右侧“建模手册”用）
        """
        await self.append_chat_history({"role": "system", "content": self.system_prompt})
        await self.append_chat_history(
            {
                "role": "user",
                "content": json.dumps(coordinator_to_modeler.questions, ensure_ascii=False),
            }
        )

        # 保持原有写法：传类名字符串；配合 llm.py 的归一化，自动映射为 AgentType.MODELER
        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,
        )

        raw_content = response.choices[0].message.content or ""
        logger.debug(f"[ModelerAgent] raw preview: {raw_content[:400]}")

        # Step1: 清理围栏与控制字符
        content = cleanup_fences(raw_content)
        content = re.sub(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]", "", content)

        # Step2: 提取首个 JSON
        json_str = extract_first_json(content)
        if not json_str:
            raise ValueError("返回内容中未找到 JSON。")

        # Step3: 尝试直接解析
        questions_solution = try_json_loads(json_str)

        # Step4: 如果失败，走重量级修复
        if questions_solution is None:
            logger.warning("[ModelerAgent] JSONDecodeError → 启动重量化 JsonFixer")

            fix_history = [
                {
                    "role": "system",
                    "content": (
                        "你是严格的 JSON 修复器。"
                        "要求："
                        "1. 必须只输出一个 JSON 对象，不能有解释说明。"
                        "2. 保证是合法 JSON（双引号、转义符必须正确）。"
                        "3. 必须是单个 dict 对象，不允许数组或多对象。"
                        "4. 输出前请校验语法，保证 Python `json.loads` 能正常解析。"
                    ),
                },
                {"role": "user", "content": json_str},
            ]

            fix_resp = await self.model.chat(
                history=fix_history,
                agent_name=self.__class__.__name__,  # 同样走归一化
                sub_title="JsonFixer",
            )
            fixed = cleanup_fences(fix_resp.choices[0].message.content or "")
            fixed_json_str = extract_first_json(fixed)
            if not fixed_json_str:
                raise ValueError("JsonFixer 未生成可识别的 JSON。")

            questions_solution = try_json_loads(fixed_json_str)

        # Step5: 如果还是失败，强制兜底：修复转义符/尾逗号
        if questions_solution is None:
            logger.error("[ModelerAgent] JsonFixer 失败 → 启动 fallback 规则修复")
            # 强制兜底修复：去掉多余逗号、非法转义
            safe_str = re.sub(r",\s*}", "}", json_str)
            safe_str = re.sub(r",\s*]", "]", safe_str)
            safe_str = safe_str.replace("'", '"')
            questions_solution = try_json_loads(safe_str)

        # Step6: 如果仍然失败 → 抛出错误
        if questions_solution is None:
            raise ValueError("JSON 修复完全失败，无法解析。")

        if not isinstance(questions_solution, dict):
            raise ValueError("解析结果不是 JSON 对象（dict），而是其他类型。")

        ic(questions_solution)

        # Step7: 显式发布结构化 ModelerMessage —— 右侧“建模手册”面板需要这一条
        # try:
        #     await redis_manager.publish_message(
        #         self.task_id,
        #         ModelerMessage(content=json.dumps(questions_solution, ensure_ascii=False)),
        #     )
        # except Exception as e:
        #     logger.warning(f"发布 ModelerMessage 失败（继续返回给后续流程）: {e}")

        # 返回给下一步（CoderAgent）
        return ModelerToCoder(questions_solution=questions_solution)
