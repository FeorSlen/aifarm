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
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from src.contract import Source

from . import uploads
from .service import Gateway, GatewayError

SEARCH_TIMEOUT_SECONDS = int(os.getenv("AIFARM_SEARCH_TIMEOUT_S", "180"))  # запас под медленный прокси
PROGRESS_INTERVAL_SECONDS = 5

# Первый вопрос набирается в строку поиска google.com — самый рискованный путь:
# именно на длинных первых запросах профили уезжали на /sorry/. Живой поисковый
# запрос — это несколько слов; замер: 64 символа проходят штатно, простыня на
# ~1500 давала /sorry/ три раза подряд. Держим предел на уровне человеческого
# запроса, а весь контекст уводим в поле AI Mode вторым вызовом.
FIRST_QUERY_MAX_CHARS = int(os.getenv("AIFARM_FIRST_QUERY_MAX", "200"))
FOLLOWUP_QUERY_MAX_CHARS = int(os.getenv("AIFARM_FOLLOWUP_QUERY_MAX", "4000"))

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Инструменты этого сервера ведут реальную сессию Google AI Mode в браузере. "
    "Задавайте короткие, естественные вопросы на английском языке: это даёт наиболее "
    "стабильный переход в AI Mode и лучший поиск источников. Один вызов — один вопрос. "
    "Вызов блокирующий и обычно занимает 1–3 минуты; дождитесь результата и не запускайте "
    "тот же запрос повторно или параллельно. "
    "tab_id из ответа search передавайте в следующий search, чтобы продолжить тот же "
    "диалог с сохранением контекста. "
    f"Первый вопрос беседы (без tab_id) — не длиннее {FIRST_QUERY_MAX_CHARS} символов: "
    "он набирается в обычной строке поиска Google и должен быть особенно коротким. "
    "Если задание длиннее, начните "
    f"беседу коротким вопросом по сути, а развёрнутый текст (до {FOLLOWUP_QUERY_MAX_CHARS} "
    "символов) пришлите вторым вызовом search с полученным tab_id — он уходит уже "
    "в поле AI Mode. "
    "Картинки и файлы загружайте через POST /files шлюза (ответ — file_id) и "
    "передавайте file_ids в search: и вместе с первым вопросом беседы, и в продолжение."
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
    sources: list[Source] = []


class TabList(BaseModel):
    tabs: list[str]

def _diagnostic_error(message: str, screenshot: str | None) -> CallToolResult:
    """Ошибка MCP, к которой клиент может показать снимок браузера."""
    content = [TextContent(type="text", text=message)]
    if screenshot:
        content.append(
            ImageContent(type="image", data=screenshot, mimeType="image/png")
        )
    return CallToolResult(content=content, isError=True)


def _sources_block(sources: list[dict]) -> str | None:
    """Список источников под номерами маркеров [n] из текста ответа.

    Отдельным блоком текста: клиент без structured output иначе не увидит
    источники вовсе, а маркеры в ответе останутся без расшифровки.
    """
    if not sources:
        return None
    lines = [
        f"[{source['index']}] {source['title']} — {source['url']}"
        if source.get("title") else f"[{source['index']}] {source['url']}"
        for source in sources
    ]
    return "Источники:\n" + "\n".join(lines)


def _gateway() -> Gateway:
    if GATEWAY is None:
        raise ToolError("Шлюз ещё не готов, повторите запрос позже")
    return GATEWAY


def _check_question_length(question: str, tab_id: str | None) -> None:
    """Первый вопрос короче: он идёт в строку google.com, продолжение — в AI Mode."""
    limit = FOLLOWUP_QUERY_MAX_CHARS if tab_id else FIRST_QUERY_MAX_CHARS
    if len(question) <= limit:
        return
    if tab_id:
        raise ToolError(
            f"Вопрос длиннее {limit} символов ({len(question)}) — сократите."
        )
    raise ToolError(
        f"Первый вопрос беседы не длиннее {limit} символов (прислано {len(question)}): "
        "он набирается в строке поиска Google. Начните беседу коротким вопросом по "
        f"сути, а развёрнутый текст (до {FOLLOWUP_QUERY_MAX_CHARS} символов) пришлите "
        "вторым вызовом search с полученным tab_id."
    )


def resolve_user(raw: str | None) -> str:
    """Арендатор по заголовку X-Aifarm-User; без заголовка — общий DEFAULT_USER.

    Общая точка для MCP и HTTP-загрузки: file_id одного арендатора в беседу
    другого не попадёт. Кривой заголовок — GatewayError.
    """
    if not raw:
        return DEFAULT_USER
    return Gateway.require_user(raw)


def _check_message(question: str, tab_id: str | None, file_ids: list[str]) -> None:
    """Сообщение — текст, вложения или и то и другое; новая беседа — с текстом."""
    if question.strip():
        return
    if not file_ids:
        raise ToolError("Вопрос не может быть пустым")
    if not tab_id:
        raise ToolError(
            "Новая беседа начинается с текста вопроса; картинки и файлы приложите к нему же."
        )


def _user(ctx: Context) -> str:
    try:
        headers = ctx.headers or {}
    except Exception:
        headers = {}
    try:
        return resolve_user(headers.get("x-aifarm-user"))
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
            max_length=FOLLOWUP_QUERY_MAX_CHARS,
            description=(
                "Короткий естественный вопрос на английском языке; один вызов — один "
                "вопрос. Пустым может быть только в продолжении беседы (с tab_id) и при file_ids. Без tab_id "
                f"(первый вопрос беседы) — максимум {FIRST_QUERY_MAX_CHARS} символов, "
                f"с tab_id — до {FOLLOWUP_QUERY_MAX_CHARS}."
            ),
        ),
    ] = "",
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
    file_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "Вложения сообщения: file_id картинок и файлов, заранее загруженных в "
                "шлюз (POST /files, multipart-поле file, тот же заголовок X-Aifarm-User). "
                "Можно и в новую беседу (вместе с вопросом), и в продолжение (с tab_id; "
                f"тогда question может быть пустым). Не больше {uploads.MAX_ATTACHMENTS}."
            ),
        ),
    ] = None,
) -> Annotated[CallToolResult, SearchResult]:
    """Задаёт вопрос Google AI Mode через реальный браузер и возвращает ответ ИИ.

    Вызывайте, когда нужен свежий ответ из открытого веба, которого нет в
    вашем знании: текущие события, цены, версии, локальная информация.
    Пишите короткий вопрос на английском языке. Вызов блокирующий, обычно занимает
    1–3 минуты: дождитесь результата, не повторяйте запрос и не запускайте параллельный
    search. Возвращает текст ответа, источники и tab_id этой беседы. Ссылки,
    которые Google проставил в ответе, отмечены в тексте маркерами [1], [2]…;
    их расшифровка — в списке источников.

    Длинное задание отправляйте в два хода: короткий первый вопрос, затем
    развёрнутый текст вторым вызовом с полученным tab_id.

    Картинки и файлы: загрузите через POST /files (ответ — file_id) и передайте
    file_ids — вместе с первым вопросом беседы или в продолжение по tab_id.
    """
    file_ids = file_ids or []
    _check_message(question, tab_id, file_ids)
    _check_question_length(question, tab_id)
    gateway = _gateway()
    user = _user(ctx)
    try:
        attachments = uploads.resolve(user, file_ids)
    except uploads.UploadError as error:
        raise ToolError(str(error)) from None
    start = time.monotonic()
    try:
        job_id = await gateway.search(user, question, tab_id, attachments)
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
        "SEARCH %.1fs status=%s tab=%s files=%d q=%r",
        time.monotonic() - start,
        status,
        tab_id or "new",
        len(file_ids),
        question[:60],
    )
    if status == "done":
        answer = result["answer"] or ""
        sources = result.get("sources") or []
        new_tab = result["tab_id"]
        content = [TextContent(type="text", text=answer)]
        listing = _sources_block(sources)
        if listing:
            content.append(TextContent(type="text", text=listing))
        content.append(
            TextContent(
                type="text",
                text=f"tab_id: {new_tab} — передайте его в search, чтобы продолжить этот диалог",
            )
        )
        return CallToolResult(
            content=content,
            structured_content={"tab_id": new_tab, "answer": answer, "sources": sources},
        )
    if status == "blocked":
        closed = ", ".join(result["closed_tab_ids"]) or "—"
        raise ToolError(
            f"Google запросил проверку профиля {result['profile_id']}; профиль и cookies "
            f"сохранены, он вернётся в ротацию после остывания. Следующий запрос пойдёт "
            f"через {result['next_profile_id']}. Текущий запрос не повторён; закрыты вкладки: {closed}."
        )
    if status == "human_action_required":
        profile = result.get("profile_id") or "последний доступный профиль"
        raise ToolError(
            f"Google запросил проверку {profile}, и готовых профилей больше нет. "
            "Требуется ручной прогрев; после него профиль нужно вернуть в пул."
        )
    code = (result.get("error") or {}).get("code")
    if code in ("answer_refused", "answer_unavailable"):
        if code == "answer_refused":
            message = (
                "Google принял уточняющий вопрос, но ответа на него не дал "
                "(«контент не сгенерирован»). Профиль и вкладка целы."
            )
        else:
            message = (
                "Ответ Google AI Mode не появился или не завершился. "
                "Профиль и вкладка оставлены открытыми."
            )
        screenshot = result.get("screenshot")
        diagnostic = (
            " Состояние браузера приложено к ошибке."
            if screenshot else " Скриншот браузера получить не удалось."
        )
        return _diagnostic_error(
            message + diagnostic, screenshot,
        )
    if code == "attachment_failed":
        return _diagnostic_error(
            "Вложения не удалось прикрепить к беседе: Google их не принял или они не "
            "загрузились. Вкладка и профиль целы, search можно повторить.",
            result.get("screenshot"),
        )
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
