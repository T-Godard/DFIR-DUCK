"""Helpers de manipulation des `Run` sur une `Investigation` en mémoire.

Fonctions pures (aucune I/O disque hors création du dossier de log du run) :
l'appelant est responsable de persister via `stores.investigation.mutate()`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.config import RUNS_DIR, settings
from app.execution import sysinfo
from app.models import Investigation, Run, RunStatus

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def runs_for(inv: Investigation, acquisition_id: str, tool: str | None = None) -> list[Run]:
    runs = [
        r
        for r in inv.runs.values()
        if r.acquisition_id == acquisition_id and (tool is None or r.tool == tool)
    ]
    return sorted(runs, key=lambda r: r.created_at)


def latest_run(inv: Investigation, acquisition_id: str, tool: str) -> Run | None:
    runs = runs_for(inv, acquisition_id, tool)
    return runs[-1] if runs else None


def active_run(inv: Investigation, acquisition_id: str, tool: str) -> Run | None:
    for run in runs_for(inv, acquisition_id, tool):
        if run.status.is_active:
            return run
    return None


def has_succeeded(inv: Investigation, acquisition_id: str, tool: str) -> bool:
    return any(
        r.status == RunStatus.SUCCEEDED for r in runs_for(inv, acquisition_id, tool)
    )


def create_run(
    inv: Investigation,
    acquisition_id: str,
    tool: str,
    *,
    forced: bool = False,
) -> Run:
    run_id = uuid.uuid4().hex[:12]
    attempt = len(runs_for(inv, acquisition_id, tool)) + 1
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    run = Run(
        id=run_id,
        acquisition_id=acquisition_id,
        tool=tool,
        status=RunStatus.PENDING,
        attempt=attempt,
        forced=forced,
        created_at=_now(),
        workdir=str(run_dir),
        log_path=str(run_dir / "run.log"),
        mem_limit_bytes=sysinfo.effective_mem_limit_bytes(settings),
        cpu_cores_limit=sysinfo.effective_cpu_cores_limit(settings),
    )
    inv.runs[run_id] = run
    logger.info(
        "run %s: créé (tool=%s acquisition=%s attempt=%d forced=%s)",
        run_id,
        tool,
        acquisition_id,
        attempt,
        forced,
    )
    return run


def latest_runs_by_tool(inv: Investigation, acquisition_id: str) -> dict[str, Run]:
    """`{tool: dernier Run}` pour une acquisition — utilisé par les templates
    pour afficher un badge de statut par outil."""
    result: dict[str, Run] = {}
    for run in runs_for(inv, acquisition_id):
        result[run.tool] = run  # runs_for est trié par created_at croissant
    return result
