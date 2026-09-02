"""
Нативный MCP-сервер поверх Gateway. Монтируется в тот же FastAPI-app
(src/api/gateway.py) на путь /mcp по Streamable HTTP.

Идентификация арендатора — заголовок X-Aifarm-User (валидируется как uuid4
через Gateway.require_user). Без заголовка все вызовы идут в общий namespace
DEFAULT_USER: env AIFARM_USER_ID либо разовый uuid4 на процесс. Это namespace-
токен, а не аутентификация; при выносе фермы в публичную сеть заменяется на
MCPServer(auth=..., token_verifier=...) — единственная точка правки _user().
"""

import logging
import os
import time
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .service import Gateway, GatewayError

SEARCH_TIMEOUT_SECONDS = int(os.getenv("AIFARM_SEARCH_TIMEOUT_S", "180"))  # запас под медленный прокси
PROGRESS_INTERVAL_SECONDS = 5

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Инструменты этого сервера ведут реальную сессию Google AI Mode в браузере. "
    "Вызовы медленные (10–90 с) и обрабатываются по одному — формулируйте полный "
    "вопрос сразу, не дробите на серию поисков. tab_id из ответа search позволяет "
    "продолжить тот же диалог с сохранением контекста."
)

mcp = MCPServer("aifarm", instructions=INSTRUCTIONS)

# Ставится из lifespan шлюза (src/api/gateway.py); через него ходят все инструменты.
GATEWAY: Gateway | None = None

# Общий арендатор для клиентов, не приславших X-Aifarm-User. Заданный
# AIFARM_USER_ID валидируется как uuid4 — кривое значение роняет старт (так же
# строго, как заголовок), лучше упасть на конфиге, чем молча уйти в чужой namespace.
_configured_user = os.getenv("AIFARM_USER_ID")
DEFAULT_USER = Gateway.require_user(_configured_user) if _configured_user else Gateway.authorize()


class SearchResult(BaseModel):
    tab_id: str
    answer: str


class TabList(BaseModel):
    tabs: list[str]


def _gateway() -> Gateway:
    if GATEWAY is None:
        raise ToolError("Шлюз ещё не готов, повторите запрос позже")
    return GATEWAY


def _user(ctx: Context) -> str:
    try:
        headers = ctx.headers or {}
    except Exception:
        headers = {}
    raw = headers.get("x-aifarm-user")
    if not raw:
        return DEFAULT_USER
    try:
        return Gateway.require_user(raw)
    except GatewayError:
        raise ToolError("Заголовок X-Aifarm-User должен быть корректным uuid4") from None


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def search(
    ctx: Context,
    question: Annotated[
        str,
        Field(
            min_length=1,
            max_length=10_000,
            description="Вопрос на естественном языке. Не может быть пустым.",
        ),
    ],
    tab_id: Annotated[
        str | None,
        Field(
            description=(
                "Идентификатор беседы вида '<worker_id>:<local_id>' из прошлого "
                "вызова search или из my_searches. Передайте, чтобы продолжить тот "
                "же диалог AI Mode (Google увидит весь контекст беседы). Опустите, "
                "чтобы начать новую независимую беседу. Вкладки живут 24 часа."
            ),
        ),
    ] = None,
) -> Annotated[CallToolResult, SearchResult]:
    """Задаёт вопрос Google AI Mode через реальный браузер и возвращает ответ ИИ.

    Вызывайте, когда нужен свежий ответ из открытого веба, которого нет в
    вашем знании: текущие события, цены, версии, локальная информация. Вызов
    блокирующий, ответ приходит через 10–90 секунд. Возвращает текст ответа
    и tab_id этой беседы.
    """
    if not question.strip():
        raise ToolError("Вопрос не может быть пустым")
    gateway = _gateway()
    user = _user(ctx)
    start = time.monotonic()
    try:
        job_id = await gateway.search(user, question, tab_id)
    except GatewayError as error:
        raise ToolError(error.message) from None

    try:
        deadline = start + SEARCH_TIMEOUT_SECONDS
        while not await gateway.wait_job(user, job_id, PROGRESS_INTERVAL_SECONDS):
            if time.monotonic() >= deadline:
                raise ToolError(
                    f"Ответ не получен за {SEARCH_TIMEOUT_SECONDS} с. Воркер, вероятно, "
                    "всё ещё занят этим запросом — повторите search тем же вопросом позже."
                )
            await ctx.report_progress(
                progress=min(time.monotonic() - start, SEARCH_TIMEOUT_SECONDS),
                total=SEARCH_TIMEOUT_SECONDS,
                message="Google AI Mode отвечает…",
            )
        result = gateway.get_job(user, job_id)
    except GatewayError as error:
        raise ToolError(error.message) from None
    finally:
        gateway.jobs.pop(job_id, None)

    if result is None:
        raise ToolError("Работа исчезла до завершения, повторите поиск")

    status = result["status"]
    log.info(
        "SEARCH %.1fs status=%s tab=%s q=%r",
        time.monotonic() - start,
        status,
        tab_id or "new",
        question[:60],
    )
    if status == "done":
        answer = result["answer"] or ""
        new_tab = result["tab_id"]
        return CallToolResult(
            content=[
                TextContent(type="text", text=answer),
                TextContent(
                    type="text",
                    text=f"tab_id: {new_tab} — передайте его в search, чтобы продолжить этот диалог",
                ),
            ],
            structured_content={"tab_id": new_tab, "answer": answer},
        )
    if status == "blocked":
        closed = ", ".join(result["closed_tab_ids"]) or "—"
        raise ToolError(
            f"Google запросил проверку профиля {result['profile_id']}; профиль и cookies "
            f"сохранены и помечены для ручного прогрева. Следующий запрос пойдёт через "
            f"{result['next_profile_id']}. Текущий запрос не повторён; закрыты вкладки: {closed}."
        )
    if status == "human_action_required":
        profile = result.get("profile_id") or "последний доступный профиль"
        raise ToolError(
            f"Google запросил проверку {profile}, и готовых профилей больше нет. "
            "Требуется ручной прогрев; после него профиль нужно вернуть в пул."
        )
    code = (result.get("error") or {}).get("code")
    if code == "tab_not_found":
        raise ToolError(
            f"Вкладка {tab_id} больше не существует. Повторите search без tab_id, "
            "чтобы начать новую беседу."
        )
    raise ToolError("Поиск не выполнен: браузер вернул ошибку. Попробуйте ещё раз.")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def my_searches(ctx: Context) -> Annotated[CallToolResult, TabList]:
    """Перечисляет открытые беседы (вкладки) с их tab_id.

    Вызывайте, чтобы найти tab_id для продолжения ранее начатой беседы или
    убедиться, что вкладка ещё жива. Ничего не меняет.
    """
    gateway = _gateway()
    user = _user(ctx)
    try:
        tabs = await gateway.list_tabs(user)
    except GatewayError as error:
        raise ToolError(error.message) from None
    listing = "\n".join(tabs) if tabs else "Открытых бесед нет"
    return CallToolResult(
        content=[TextContent(type="text", text=listing)],
        structured_content={"tabs": tabs},
    )


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def close(
    ctx: Context,
    tab_id: Annotated[
        str,
        Field(description="Идентификатор беседы вида '<worker_id>:<local_id>'. Обязателен."),
    ],
) -> CallToolResult:
    """Закрывает беседу и освобождает вкладку браузера.

    Вызывайте, когда беседа точно больше не нужна. Необратимо: продолжить
    закрытую беседу нельзя, tab_id станет недействительным.
    """
    gateway = _gateway()
    user = _user(ctx)
    try:
        await gateway.close_tab(user, tab_id)
    except GatewayError as error:
        raise ToolError(error.message) from None
    return CallToolResult(content=[TextContent(type="text", text=f"Вкладка {tab_id} закрыта")])
