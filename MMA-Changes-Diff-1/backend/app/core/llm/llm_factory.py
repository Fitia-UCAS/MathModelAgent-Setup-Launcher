# app/core/llm/llm_factory.py

from app.config.setting import settings
from app.core.llm.llm import LLM


class LLMFactory:
    task_id: str

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id

    def get_all_llms(self) -> tuple[LLM, LLM, LLM, LLM]:
        coordinator_llm = LLM(
            api_key=settings.COORDINATOR_API_KEY,
            model=settings.COORDINATOR_MODEL,
            base_url=settings.COORDINATOR_BASE_URL,
            task_id=self.task_id,
        )

        modeler_llm = LLM(
            api_key=settings.MODELER_API_KEY,
            model=settings.MODELER_MODEL,
            base_url=settings.MODELER_BASE_URL,
            task_id=self.task_id,
        )

        coder_llm = LLM(
            api_key=settings.CODER_API_KEY,
            model=settings.CODER_MODEL,
            base_url=settings.CODER_BASE_URL,
            task_id=self.task_id,
        )

        writer_llm = LLM(
            api_key=settings.WRITER_API_KEY,
            model=settings.WRITER_MODEL,
            base_url=settings.WRITER_BASE_URL,
            task_id=self.task_id,
        )

        return coordinator_llm, modeler_llm, coder_llm, writer_llm
