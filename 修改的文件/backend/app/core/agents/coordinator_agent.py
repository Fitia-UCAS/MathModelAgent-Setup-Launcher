from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import COORDINATOR_PROMPT
import json
import re
from app.utils.log_util import logger
from app.schemas.A2A import CoordinatorToModeler

# 右侧“题目信息”面板消费的是 CoordinatorMessage，需要显式发布
from app.services.redis_manager import redis_manager
from app.schemas.response import CoordinatorMessage

JSON_START = "<<<JSON_START>>>"
JSON_END = "<<<JSON_END>>>"


def _cleanup_control_chars(s: str) -> str:
    """去除会导致 json.loads 失败的控制字符"""
    return re.sub(r"[\x00-\x1F\x7F]", "", s or "")


def _extract_json_block(s: str) -> str:
    """
    首选：提取 <<<JSON_START>>> ... <<<JSON_END>>> 之间的内容
    兜底：抓取首个 {...} 块（避免少数模型不听话时崩溃）
    """
    s = s or ""
    i = s.find(JSON_START)
    j = s.find(JSON_END)
    if i != -1 and j != -1 and j > i:
        return s[i + len(JSON_START) : j].strip()

    # 兜底：匹配首个大括号块
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    return m.group(0).strip() if m else s.strip()


def _normalize_common_glitches(s: str) -> str:
    """
    规范常见“小毛病”：
    1) 模型偶尔会把 "ques2" 打成 "qu es2"（中间插了空格）
    2) 偶发性栅栏残留
    """
    s = re.sub(r'"qu\s+es(\d+)"', r'"ques\1"', s)  # "qu es2" -> "ques2"
    s = s.replace("```json", "").replace("```", "")  # 清理代码栅栏
    return s.strip()


class CoordinatorAgent(Agent):
    def __init__(
        self,
        task_id: str,
        model: LLM,
        max_chat_turns: int = 30,
    ) -> None:
        super().__init__(task_id, model, max_chat_turns)
        self.system_prompt = COORDINATOR_PROMPT

    async def run(self, ques_all: str) -> CoordinatorToModeler:
        """用户输入问题 使用LLM 格式化 questions"""
        await self.append_chat_history({"role": "system", "content": self.system_prompt})
        await self.append_chat_history({"role": "user", "content": ques_all})

        response = await self.model.chat(
            history=self.chat_history,
            agent_name=self.__class__.__name__,  # 保持原始写法；LLM 层会归一化
        )
        raw_text = response.choices[0].message.content

        # 1) 基础清理
        raw_text = _cleanup_control_chars(raw_text)

        # 2) 提取 JSON 主体
        json_text = _extract_json_block(raw_text)

        # 3) 规范常见问题
        json_text = _normalize_common_glitches(json_text)

        if not json_text:
            raise ValueError("返回的 JSON 字符串为空，请检查输入内容。")

        # 4) 解析为对象
        try:
            questions = json.loads(json_text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误，原始字符串: {json_text}")
            raise ValueError(f"JSON 解析错误: {e}") from e

        # 5) 兜底：确保 ques_count 存在且为 int
        ques_count = questions.get("ques_count")
        if not isinstance(ques_count, int):
            ques_keys = [k for k in questions.keys() if re.fullmatch(r"ques\d+", k)]
            if not ques_keys:
                raise ValueError("缺少 ques_count 且未找到任何 quesN 键。")
            ques_count = max(int(k[4:]) for k in ques_keys)
            questions["ques_count"] = ques_count

        logger.info(f"questions:{questions}")

        # # 6) 显式发布结构化 CoordinatorMessage —— 右侧“题目信息”面板需要这一条
        # try:
        #     await redis_manager.publish_message(
        #         self.task_id,
        #         CoordinatorMessage(content=json.dumps(questions, ensure_ascii=False)),
        #     )
        # except Exception as e:
        #     logger.warning(f"发布 CoordinatorMessage 失败（继续返回结果给后续流程）: {e}")

        # 7) 返回给后续的 ModelerAgent
        return CoordinatorToModeler(questions=questions, ques_count=ques_count)
