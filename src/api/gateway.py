import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import mcp as mcp_module
from .service import Gateway

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
_mcp_host = os.getenv("AIFARM_MCP_HOST", "127.0.0.1")
app.router.routes.extend(mcp_module.mcp.streamable_http_app(host=_mcp_host).routes)


@app.get("/health")
async def health():
    return {"status": "ok"}
