import asyncio
import logging
import uuid
from dataclasses import dataclass, field

import aio_pika

from src.config import RABBITMQ_URL, TASK_EXCHANGE, WORKER_ID
from src.contract import Result, Task, worker_id

CONTROL_TIMEOUT_SECONDS = 10

log = logging.getLogger(__name__)


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class InvalidUserSession(GatewayError):
    def __init__(self):
        super().__init__("invalid_user_session_id", "Некорректная пользовательская сессия")


class TabNotFound(GatewayError):
    def __init__(self):
        super().__init__("tab_not_found", "Вкладка не найдена")


class BrokerUnavailable(GatewayError):
    def __init__(self):
        super().__init__("queue_unavailable", "Браузер сейчас не отвечает")


@dataclass
class Job:
    user_session_id: str
    tab_id: str | None
    status: str = "pending"
    answer: str | None = None
    sources: list[dict] = field(default_factory=list)
    error: dict | None = None
    screenshot: str | None = None
    closed_tab_ids: list[str] = field(default_factory=list)
    profile_id: str | None = None
    next_profile_id: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)  # ставится на любом терминальном статусе


class Gateway:
    """HTTP jobs in RAM; browser workers own tabs and browser sessions."""

    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._calls: dict[str, asyncio.Future[Result]] = {}
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._reply_to: str | None = None

    async def start(self):
        self._connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await self._connection.channel(publisher_confirms=True)
        self._exchange = await channel.declare_exchange(
            TASK_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
        )
        replies = await channel.declare_queue(exclusive=True, auto_delete=True)
        await replies.consume(self.on_reply)
        self._reply_to = replies.name

    async def close(self):
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @staticmethod
    def authorize() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def require_user(user_session_id: str | None) -> str:
        try:
            parsed = uuid.UUID(user_session_id or "")
        except (ValueError, AttributeError):
            raise InvalidUserSession() from None
        if parsed.version != 4:
            raise InvalidUserSession()
        return str(parsed)

    async def list_tabs(self, user_session_id: str) -> list[str]:
        result = await self._call(Task(user_session_id=user_session_id))
        self._raise_worker_error(result)
        return result.sessions or []

    async def search(
        self, user_session_id: str, question: str, tab_id: str | None
    ) -> str:
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = Job(user_session_id=user_session_id, tab_id=tab_id)
        try:
            await self._publish(
                Task(user_session_id=user_session_id, session_id=tab_id, query=question),
                job_id,
            )
        except Exception as error:
            log.exception("could not publish job %s", job_id)
            self.jobs.pop(job_id, None)  # job_id наружу не ушёл — забываем целиком
            raise BrokerUnavailable() from error
        return job_id

    async def wait_job(self, user_session_id: str, job_id: str, timeout: float) -> bool:
        """Ждёт терминального статуса работы. False — не дождались за timeout."""
        job = self.jobs.get(job_id)
        if job is None or job.user_session_id != user_session_id:
            raise GatewayError("job_not_found", "Работа не найдена")
        try:
            await asyncio.wait_for(job.done.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def get_job(self, user_session_id: str, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None or job.user_session_id != user_session_id:
            return None
        if job.status == "pending":
            return {"status": "pending"}
        if job.status == "done":
            return {
                "status": "done",
                "tab_id": job.tab_id,
                "answer": job.answer,
                "sources": job.sources,
            }
        if job.status == "blocked":
            return {
                "status": "blocked",
                "tab_id": job.tab_id,
                "closed_tab_ids": job.closed_tab_ids,
                "profile_id": job.profile_id,
                "next_profile_id": job.next_profile_id,
            }
        if job.status == "human_action_required":
            return {"status": "human_action_required", "profile_id": job.profile_id}
        return {
            "status": "failed",
            "tab_id": job.tab_id,
            "error": job.error,
            "screenshot": job.screenshot,
        }

    async def close_tab(self, user_session_id: str, tab_id: str):
        result = await self._call(Task(user_session_id=user_session_id, session_id=tab_id))
        self._raise_worker_error(result)

    async def on_reply(self, message: aio_pika.IncomingMessage):
        if message.correlation_id is None:
            await message.reject(requeue=False)
            return
        try:
            result = Result.model_validate_json(message.body)
        except Exception:
            log.exception("discarding invalid worker reply %s", message.correlation_id)
            await message.reject(requeue=False)
            return

        call = self._calls.get(message.correlation_id)
        if call is not None:
            call.set_result(result)
        else:
            self._record_result(message.correlation_id, result)
        await message.ack()

    def _record_result(self, job_id: str, result: Result):
        job = self.jobs.get(job_id)
        if job is None or job.status != "pending":
            return
        if result.error:
            job.screenshot = result.screenshot
            if result.error == "profile_blocked":
                job.status = "blocked"
                job.closed_tab_ids = result.sessions or []
                job.profile_id = result.profile_id
                job.next_profile_id = result.next_profile_id
            elif result.error == "human_action_required":
                job.status = "human_action_required"
                job.profile_id = result.profile_id
            elif result.error in ("answer_refused", "answer_unavailable"):
                job.status = "failed"
                job.error = {
                    "code": result.error,
                    "message": "Google не сгенерировал ответ",
                }
            elif result.error == "session_not_found":
                job.status = "failed"
                job.error = {"code": "tab_not_found", "message": "Вкладка не найдена"}
            else:
                job.status = "failed"
                job.error = {"code": "processing_failed", "message": "Поиск не был выполнен"}
        else:
            job.status = "done"
            job.tab_id = result.session_id
            job.answer = result.answer
            job.sources = [source.model_dump() for source in result.sources or []]
        job.done.set()

    async def _call(self, task: Task) -> Result:
        correlation_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._calls[correlation_id] = future
        try:
            await self._publish(task, correlation_id)
            return await asyncio.wait_for(future, CONTROL_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise BrokerUnavailable() from error
        finally:
            self._calls.pop(correlation_id, None)

    async def _publish(self, task: Task, correlation_id: str):
        if self._exchange is None or self._reply_to is None:
            raise RuntimeError("gateway is not started")
        route = WORKER_ID if task.session_id is None else worker_id(task.session_id)
        await self._exchange.publish(
            aio_pika.Message(
                body=task.model_dump_json().encode(),
                content_type="application/json",
                correlation_id=correlation_id,
                reply_to=self._reply_to,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=route,
        )

    @staticmethod
    def _raise_worker_error(result: Result):
        if result.error == "session_not_found":
            raise TabNotFound()
        if result.error:
            raise BrokerUnavailable()
