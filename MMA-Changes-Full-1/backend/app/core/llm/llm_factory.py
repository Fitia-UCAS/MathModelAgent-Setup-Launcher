# app/core/llm/llm_factory.py

from app.config.setting import settings
from app.core.llm.llm import LLM


# 1 LLM 工厂类（职责说明）
# 1.1 负责为不同的 Agent 分别创建 LLM 实例（Coordinator / Modeler / Coder / Writer）
# 1.2 每个 LLM 实例携带独立的 api_key / model / base_url 配置，便于角色隔离
# 1.3 通过工厂避免不同角色共用同一 LLM，便于单独调整与权限控制
class LLMFactory:
    task_id: str

    # 2 构造器
    # 2.1 绑定当前任务 ID（用于日志与消息发布区分）
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    # 3 公共方法：返回四个角色的 LLM 实例
    # 3.1 返回值顺序固定： (coordinator_llm, modeler_llm, coder_llm, writer_llm)
    # 3.2 各实例配置来源于 settings，便于通过环境/配置文件调整
    def get_all_llms(self) -> tuple[LLM, LLM, LLM, LLM]:
        # 3.3 Coordinator LLM：用于结构化/协调（可单独配置 API KEY 与模型）
        coordinator_llm = LLM(
            api_key=settings.COORDINATOR_API_KEY,
            model=settings.COORDINATOR_MODEL,
            base_url=settings.COORDINATOR_BASE_URL,
            task_id=self.task_id,
        )

        # 3.4 Modeler LLM：用于解析/建模指令与生成建模手册
        modeler_llm = LLM(
            api_key=settings.MODELER_API_KEY,
            model=settings.MODELER_MODEL,
            base_url=settings.MODELER_BASE_URL,
            task_id=self.task_id,
        )

        # 3.5 Coder LLM：用于生成/修正/执行代码相关的交互
        coder_llm = LLM(
            api_key=settings.CODER_API_KEY,
            model=settings.CODER_MODEL,
            base_url=settings.CODER_BASE_URL,
            task_id=self.task_id,
        )

        # 3.6 Writer LLM：用于最终写作与排版，独立配置以便内容风格调整
        writer_llm = LLM(
            api_key=settings.WRITER_API_KEY,
            model=settings.WRITER_MODEL,
            base_url=settings.WRITER_BASE_URL,
            task_id=self.task_id,
        )

        # 3.7 返回四个实例（按上述顺序）
        return coordinator_llm, modeler_llm, coder_llm, writer_llm
