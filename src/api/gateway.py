import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile

from . import mcp as mcp_module
from . import uploads
from .service import Gateway, GatewayError

# uvicorn настраивает только свои логгеры; поднимаем app-логгеры до INFO,
# чтобы было видно строки SEARCH (время ответа на запрос) в docker compose logs.
logging.basicConfig(level=logging.INFO)
logging.getLogger("src").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    gateway = Gateway()
    await gateway.start()
    mcp_module.GATEWAY = gateway
    try:
        # session_manager существует только после streamable_http_app() ниже
        async with mcp_module.mcp.session_manager.run():
            yield
    finally:
        mcp_module.GATEWAY = None
        await gateway.close()


app = FastAPI(lifespan=lifespan)
# Точный маршрут POST /mcp без redirect (app.mount дал бы 307 на /mcp -> /mcp/).
# streamable_http_app() вызываем РОВНО один раз: повторный вызов создаёт новый
# session manager, а маршрут остаётся привязан к старому.
#
# host=127.0.0.1 (дефолт) включает DNS-rebinding guard: пускает только
# Host: localhost / 127.0.0.1 / [::1]. Для публичного порта на VPS поставить
# AIFARM_MCP_HOST=0.0.0.0 — это ПОЛНОСТЬЮ выключает guard, так что только за
# доверенным прокси/файрволом (аутентификации у сервера нет).
#
# stateless_http=True: без MCP-сессий, каждый HTTP-запрос — свой транспорт.
# С сессиями клиент, у которого во время длинного search (250+ с) отвалился
# отдельный GET-SSE-стрим, при реконнекте вечно получал 409 "Only one SSE
# stream is allowed per session" — сервер не замечал разрыв, т.к. в этот стрим
# ничего не пишется. Серверного состояния на сессию у нас нет (арендатор — в
# заголовке X-Aifarm-User, вкладки живут на воркере), прогресс идёт в поток
# ответа самого POST, так что сессии не нужны.
_mcp_host = os.getenv("AIFARM_MCP_HOST", "127.0.0.1")
app.router.routes.extend(
    mcp_module.mcp.streamable_http_app(host=_mcp_host, stateless_http=True).routes
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    x_aifarm_user: str | None = Header(default=None),
):
    """Загрузка вложения для search(file_ids=...): возвращает file_id.

    Арендатор тот же, что у MCP (X-Aifarm-User или общий по умолчанию), поэтому
    file_id одного арендатора в беседу другого не попадёт.
    """
    try:
        user = mcp_module.resolve_user(x_aifarm_user)
    except GatewayError:
        raise HTTPException(400, "Заголовок X-Aifarm-User должен быть корректным uuid4") from None
    try:
        return await uploads.save(user, file.filename or "", file)
    except uploads.UploadTooLarge as error:
        raise HTTPException(413, str(error)) from None
    except uploads.UploadError as error:
        raise HTTPException(400, str(error)) from None
