from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import MODELER_PROMPT
from app.schemas.A2A import CoordinatorToModeler, ModelerToCoder
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager  # 用于右侧面板
from app.schemas.response import ModelerMessage       # 右侧“建模手册”用
import json
import re
from icecream import ic


# === 工具函数 ===

def _cleanup_control_chars(s: str) -> str:
    """去掉会导致 json.loads 失败的控制字符"""
    return re.sub(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]", "", s or "")


def _strip_fences(s: str) -> str:
    """去掉 ```json / ``` 围栏"""
    s = s or ""
    return s.replace("```json", "").replace("```", "").strip()


def _extract_first_json(text: str) -> str:
    """
    用“栈法”提取首个配平的 JSON 对象字符串，比 {.*} 更稳健：
    - 支持跳过字符串中的花括号
    - 找不到时返回空串，让上层按逻辑报错
    """
    if not text:
        return ""
    text = _strip_fences(text)
    start = text.find("{")
    if start == -1:
        return ""
    stack = []
    in_str, esc = False, False
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
    return ""  # 未闭合，交由上层修复/报错


def _try_json_loads(s: str):
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
        max_chat_turns: int = 600,  # 添加最大对话轮次限制
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = MODELER_PROMPT

    async def run(self, coordinator_to_modeler: CoordinatorToModeler) -> ModelerToCoder:
        """
        解析大模型返回的 JSON：
        1. 清理 Markdown 围栏与控制字符
        2. 提取首个 JSON（栈法）
        3. 如失败 → 走 JsonFixer 一次；再失败 → fallback 简单规则修复
        4. 严格校验必须是 dict
        5. 解析成功后显式发布 ModelerMessage（右侧“建模手册”）
        """
        # 只注入一次 system
        if not self._inited:
            await self.append_chat_history({"role": "system", "content": self.system_prompt})
            self._inited = True

        await self.append_chat_history(
            {
                "role": "user",
                "content": json.dumps(coordinator_to_modeler.questions, ensure_ascii=False),
            }
        )

        # 配合 llm.py 的归一化，传类名字符串即可
        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,
        )

        raw_content = getattr(response.choices[0].message, "content", "") or ""
        logger.debug(f"[ModelerAgent] raw preview: {raw_content[:400]}")

        # Step1: 清理围栏与控制字符
        content = _strip_fences(_cleanup_control_chars(raw_content))

        # Step2: 提取首个 JSON
        json_str = _extract_first_json(content)
        if not json_str:
            raise ValueError("返回内容中未找到 JSON。")

        # Step3: 尝试直接解析
        questions_solution = _try_json_loads(json_str)

        # Step4: 如果失败，走重量级修复（JsonFixer）
        if questions_solution is None:
            logger.warning("[ModelerAgent] JSONDecodeError → 启动 JsonFixer")
            fix_history = [
                {
                    "role": "system",
                    "content": (
                        "你是严格的 JSON 修复器。\n"
                        "要求：\n"
                        "1) 仅输出一个 JSON 对象，不能包含解释或额外文本；\n"
                        "2) 保证是合法 JSON（双引号、转义符正确），能被 Python json.loads 解析；\n"
                        "3) 类型必须是对象（dict），不要数组或多对象。"
                    ),
                },
                {"role": "user", "content": json_str},
            ]
            fix_resp = await self.model.chat(
                history=fix_history,
                agent_name=self.__class__.__name__,
                sub_title="JsonFixer",
            )
            fixed = _strip_fences(getattr(fix_resp.choices[0].message, "content", "") or "")
            fixed_json_str = _extract_first_json(fixed)
            if not fixed_json_str:
                raise ValueError("JsonFixer 未生成可识别的 JSON。")
            questions_solution = _try_json_loads(fixed_json_str)

        # Step5: 如果还是失败，fallback 规则修复
        if questions_solution is None:
            logger.error("[ModelerAgent] JsonFixer 失败 → 启动 fallback 规则修复")
            safe_str = re.sub(r",\s*}", "}", json_str)
            safe_str = re.sub(r",\s*]", "]", safe_str)
            safe_str = safe_str.replace("'", '"')
            questions_solution = _try_json_loads(safe_str)

        # Step6: 仍失败 → 抛错；非 dict → 抛错
        if questions_solution is None:
            raise ValueError("JSON 修复完全失败，无法解析。")
        if not isinstance(questions_solution, dict):
            raise ValueError("解析结果不是 JSON 对象（dict）。")

        ic(questions_solution)

        # Step7: 显式发布结构化 ModelerMessage —— 右侧“建模手册”面板需要这一条
        try:
            await redis_manager.publish_message(
                self.task_id,
                ModelerMessage(content=json.dumps(questions_solution, ensure_ascii=False)),
            )
        except Exception as e:
            logger.warning(f"发布 ModelerMessage 失败（继续返回给后续流程）: {e}")

        # 返回给下一步（CoderAgent）
        return ModelerToCoder(questions_solution=questions_solution)
