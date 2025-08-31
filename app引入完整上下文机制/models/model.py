from pydantic import BaseModel


class CoderToWriter(BaseModel):
    status: str
    summary: str
    code_response: str
    code_execution_result: str
    created_images: list[str]
