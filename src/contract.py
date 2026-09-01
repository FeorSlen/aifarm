from typing import Literal

from pydantic import BaseModel, Field


class Task(BaseModel):
    version: Literal[1] = 1
    user_session_id: str = Field(min_length=1)
    session_id: str | None = None
    query: str | None = Field(default=None, min_length=1, max_length=10_000)


class Result(BaseModel):
    version: Literal[1] = 1
    session_id: str | None = None
    answer: str | None = None
    error: str | None = None
    sessions: list[str] | None = None


def worker_id(session_id: str) -> str:
    worker, separator, local = session_id.partition(":")
    if not separator or not worker or not local:
        raise ValueError("session_id must be '<worker_id>:<local_id>'")
    return worker
