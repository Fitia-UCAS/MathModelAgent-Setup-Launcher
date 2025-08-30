from pydantic import BaseModel
from typing import Any


class CoordinatorToModeler(BaseModel):
    questions: dict
    ques_count: int


class ModelerToCoder(BaseModel):
    questions_solution: dict[str, str]


class CoderToWriter(BaseModel):
    code_response: str | None = None
    code_output: str | None = None
    created_images: list[str] | None = None


class WriterResponse(BaseModel):
    response_content: Any
    footnotes: list[tuple[str, str]] | None = None
