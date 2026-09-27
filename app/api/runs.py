from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request

from app.api.views import base_context, templates
from app.config import settings
from app.execution.jobs import get_job_manager
from app.stores import investigation as store
from app.stores import runs as run_store
from app.tools import registry

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/investigations/{inv_id}/acquisitions/{acq_id}/runs")
async def force_run(request: Request, inv_id: str, acq_id: str, tool: str = Form("dfir-ogre")):
    """Relancement manuel explicite (« Forcer ») — par ex. après une erreur.
    Appelle exactement les mêmes fonctions que l'orchestrateur ; ce n'est pas
    cette route qui décide QUAND lancer un outil normalement."""
    submit_id: str | None = None
    async with store.mutate() as inv:
        acq = inv.acquisitions.get(acq_id)
        if acq is None:
            logger.warning("force_run: acquisition %s inconnue", acq_id)
        elif tool not in registry.all_tools():
            logger.warning("force_run: outil %r inconnu", tool)
        elif not settings.tool_enabled(tool):
            # Sinon le réglage serait cosmétique : il doit aussi tenir face à
            # un bouton « Forcer » resté affiché dans un onglet ouvert.
            logger.warning("force_run: outil %r désactivé dans les paramètres", tool)
        elif tool == "dfir-ogre" and not acq.outcome_filename:
            logger.warning("force_run: acquisition %s sans Outcome.json", acq_id)
        elif run_store.active_run(inv, acq_id, tool) is not None:
            logger.debug("force_run: un run %s est déjà actif pour %s", tool, acq_id)
        else:
            run = run_store.create_run(inv, acq_id, tool, forced=True)
            submit_id = run.id

    if submit_id:
        get_job_manager().submit(submit_id)

    inv = store.get()
    return templates.TemplateResponse(request, "partials/runs_status.html", base_context(inv))


@router.get("/investigations/{inv_id}/runs/status")
def runs_status(request: Request, inv_id: str):
    """Lecture pure : l'état affiché est celui déjà calculé par l'orchestrateur
    et les tâches de run."""
    inv = store.get()
    return templates.TemplateResponse(request, "partials/runs_status.html", base_context(inv))


@router.get("/investigations/{inv_id}/runs/{run_id}/log")
def run_log(request: Request, inv_id: str, run_id: str, tail: int = 400):
    inv = store.get()
    run = inv.runs.get(run_id)
    text = ""
    if run and run.log_path:
        text = _tail_text(Path(run.log_path), tail)
    return templates.TemplateResponse(
        request,
        "partials/run_log.html",
        {"inv": inv, "run": run, "log": text, "tail": tail},
    )


def _tail_text(path: Path, max_lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[-max_lines:])
