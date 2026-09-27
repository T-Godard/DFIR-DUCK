from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import resource_history
from app.api import router as api_router
from app.config import settings
from app.context import new_request_id, request_id_ctx
from app.execution.jobs import JobManager, set_job_manager
from app.logging_setup import configure_logging
from app.pipeline import orchestrator
from app.stores import investigation as store
from app.stores import preferences

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings)
    # Choix fait depuis l'interface : il prime sur config.yaml, qui est figé
    # dans l'image. À faire avant le premier log et le premier tick.
    preferences.apply_saved()
    logger.info(
        "démarrage : data_dir=%s log_level=%s max_concurrent_runs=%s run_timeout=%ss",
        settings.data_dir,
        settings.log_level,
        settings.max_concurrent_runs,
        settings.run_timeout_seconds,
    )

    store.ensure_investigation()

    job_manager = JobManager(settings)
    await job_manager.reconcile_startup()
    set_job_manager(job_manager)

    task = asyncio.create_task(orchestrator.run_forever(job_manager), name="orchestrator")
    resources_task = asyncio.create_task(resource_history.run_forever(), name="resource_history")
    background = [task, resources_task]
    logger.info("démarrage terminé")
    try:
        yield
    finally:
        logger.info("arrêt : annulation de l'orchestrateur")
        for t in background:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        await job_manager.shutdown()
        logger.info("arrêt terminé")


app = FastAPI(title="DFIR-DUCK", version="0.1", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Accès HTTP (« frontend web ») : journalisé en DEBUG. Le niveau INFO est
    réservé aux événements métier du backend (acquisition créée, run lancé...).
    Seules les erreurs non gérées d'une requête remontent au-dessus de DEBUG."""
    rid = request.headers.get("x-request-id") or new_request_id()
    token = request_id_ctx.set(rid)
    path = request.url.path
    client = request.client.host if request.client else "-"
    start = time.perf_counter()
    logger.debug("request.start %s %s client=%s", request.method, path, client)
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - start) * 1000
        logger.exception("request.error %s %s dur_ms=%.1f", request.method, path, elapsed)
        request_id_ctx.reset(token)
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    elapsed = (time.perf_counter() - start) * 1000
    logger.debug(
        "request.end %s %s status=%s dur_ms=%.1f",
        request.method,
        path,
        response.status_code,
        elapsed,
    )
    response.headers["x-request-id"] = rid
    request_id_ctx.reset(token)
    return response


app.include_router(api_router)
