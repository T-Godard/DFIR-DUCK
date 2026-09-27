from __future__ import annotations

from fastapi import APIRouter, Response

from app import prometheus
from app.stores import investigation as store

router = APIRouter()

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def metrics() -> Response:
    """Métriques au format Prometheus. Route `async` volontairement : le rendu
    lit l'état en mémoire sans `await`, donc sans risque de le voir muter en
    cours de route (contrairement à un handler synchrone exécuté en threadpool)."""
    return Response(prometheus.render(store.get()), media_type=_CONTENT_TYPE)
