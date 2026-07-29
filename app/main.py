from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import router as api_router
from app.dashboard import NotAuthenticated
from app.dashboard import public_router as dashboard_public_router
from app.dashboard import router as dashboard_router
from app.db import get_connection, init_db
from app.settings import Settings

settings = Settings()
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_connection(settings)
    init_db(conn)
    app.state.settings = settings
    app.state.db = conn
    print(
        f"model={settings.llm_model} "
        f"in=${settings.llm_price_per_1m_input}/1M "
        f"out=${settings.llm_price_per_1m_output}/1M "
        "source=computed"
    )
    yield
    conn.close()


app = FastAPI(title="HSV Invoice Extraction (Lite)", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(NotAuthenticated)
async def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/dashboard/login", status_code=303)


app.include_router(api_router)
app.include_router(dashboard_public_router)
app.include_router(dashboard_router)
