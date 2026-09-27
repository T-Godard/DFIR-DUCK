"""Boucle de fond — seule source de vérité qui fait avancer le pipeline.

À chaque tick :

1. purge best-effort des sessions d'upload par morceaux abandonnées ;
2. classification des uploads en attente en acquisitions ;
3. pour chaque acquisition × étape « prête » : création d'un run, puis
   soumission au `JobManager`.

Les routes HTTP ne font jamais avancer le pipeline : elles lisent l'état, ou
déclenchent une action manuelle explicite (« Forcer ») qui passe par les mêmes
fonctions. Le pipeline continue donc même si personne ne consulte l'UI.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import settings
from app.execution.jobs import JobManager
from app.pipeline.stages import STAGES
from app.stores import acquisitions as acq_store
from app.stores import investigation as store
from app.stores import runs as run_store
from app.upload import service as upload_service

logger = logging.getLogger(__name__)

# État observable de la boucle (exposé via /metrics et /healthz).
_state = {"last_tick_wall": 0.0, "tick_count": 0}


def stats() -> dict:
    return dict(_state)


async def run_forever(job_manager: JobManager) -> None:
    logger.info("orchestrateur: démarré (tick=%ss)", settings.orchestrator_tick_seconds)
    while True:
        try:
            await _tick(job_manager)
        except asyncio.CancelledError:
            logger.info("orchestrateur: arrêt")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("orchestrateur: erreur inattendue pendant un tick")
        else:
            _state["last_tick_wall"] = time.time()
            _state["tick_count"] += 1
        await asyncio.sleep(settings.orchestrator_tick_seconds)


async def _tick(job_manager: JobManager) -> None:
    upload_service.cleanup_abandoned_chunks()

    to_submit: list[str] = []
    async with store.mutate() as inv:
        touched = acq_store.classify_pending_uploads(inv)
        if touched:
            logger.info("orchestrateur: classification -> acquisitions %s", sorted(touched))

        for acq in inv.acquisitions.values():
            for stage in STAGES:
                if stage.ready(inv, acq):
                    run = run_store.create_run(inv, acq.id, stage.tool)
                    to_submit.append(run.id)
                    logger.info(
                        "orchestrateur: run %s créé (étape=%s acquisition=%s)",
                        run.id,
                        stage.name,
                        acq.id,
                    )
                else:
                    reason = stage.blocked_reason(inv, acq)
                    if reason:
                        logger.debug(
                            "orchestrateur: étape %s non prête pour %s (%s)",
                            stage.name,
                            acq.id,
                            reason,
                        )

    for run_id in to_submit:
        job_manager.submit(run_id)
