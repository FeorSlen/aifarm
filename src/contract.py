from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Attachment(BaseModel):
    """Вложение сообщения: файл в общем каталоге загрузок (см. src/api/uploads.py)."""

    path: str = Field(min_length=1)  # абсолютный путь, одинаковый у шлюза и воркера
    name: str = Field(min_length=1)  # имя, под которым Google покажет файл
    kind: Literal["image", "file"]   # в какой input AI Mode его отдавать


class Task(BaseModel):
    version: Literal[1] = 1
    user_session_id: str = Field(min_length=1)
    session_id: str | None = None
    # None — управляющая задача (список или закрытие вкладок). Сообщение — это
    # текст, вложения или и то и другое, в том числе первое сообщение беседы.
    query: str | None = Field(default=None, max_length=10_000)
    attachments: list[Attachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_message(self):
        if self.query is None:
            if self.attachments:
                raise ValueError("attachments need a message, not a control task")
            return self
        if not self.query.strip() and not self.attachments:
            raise ValueError("message needs text or attachments")
        return self


class Source(BaseModel):
    """Источник ответа AI Mode; index — номер маркера [n] в тексте ответа."""

    index: int = Field(ge=1)
    url: str = Field(min_length=1)
    title: str = ""


class Result(BaseModel):
    version: Literal[1] = 1
    session_id: str | None = None
    answer: str | None = None
    sources: list[Source] | None = None
    error: str | None = None
    screenshot: str | None = None  # base64 PNG только для диагностической ошибки
    sessions: list[str] | None = None
    profile_id: str | None = None
    next_profile_id: str | None = None


def worker_id(session_id: str) -> str:
    worker, separator, local = session_id.partition(":")
    if not separator or not worker or not local:
        raise ValueError("session_id must be '<worker_id>:<local_id>'")
    return worker
