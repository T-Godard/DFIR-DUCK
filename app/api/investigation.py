from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.api.views import base_context, templates
from app.execution import sysinfo
from app.execution.jobs import get_job_manager
from app.pipeline import orchestrator
from app.stores import investigation as store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
def index(request: Request):
    """Une seule investigation dans ce fork : `/` sert directement sa page de
    détail (pas de liste ni de création manuelle)."""
    inv = store.get()
    return templates.TemplateResponse(request, "investigation_detail.html", base_context(inv))


@router.get("/investigations/{inv_id}/header/status")
def header_status(request: Request, inv_id: str):
    """Chiffres de tête + pastille d'état de la topbar (celle-ci voyage en
    swap OOB dans la même réponse : un seul poll pour les deux)."""
    inv = store.get()
    return templates.TemplateResponse(request, "partials/header_status.html", base_context(inv))


@router.get("/investigations/{inv_id}/overview/status")
def overview_status(request: Request, inv_id: str):
    """Partiel de l'onglet Overview (polling) : statut des traitements par
    machine + ressources du conteneur."""
    inv = store.get()
    return templates.TemplateResponse(request, "partials/overview_status.html", base_context(inv))


@router.get("/healthz")
def healthz():
    inv = store.get()
    return {
        "status": "ok",
        "acquisitions": len(inv.acquisitions),
        "runs_total": len(inv.runs),
        "runs_active": get_job_manager().active_count(),
        "orchestrator": orchestrator.stats(),
        "resources": sysinfo.app_snapshot(),
    }
