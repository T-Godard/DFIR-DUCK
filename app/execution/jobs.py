"""Ordonnancement et supervision des runs d'outils.

Le `JobManager` :

* borne la concurrence par type d'outil via deux sémaphores indépendants :
  extraction (`max_concurrent_runs`) ;
* pour chaque run soumis, crée une tâche asyncio qui : marque `running` ->
  résout l'outil -> `plan()` + `prepare()` -> `run_process()` ->
  `succeeded`/`failed`/`cancelled` ; chaque transition est persistée ;
* réconcilie au démarrage les runs restés `running`/`pending` après un crash ou
  un redémarrage ;
* arrête proprement les runs en cours au shutdown.

Toutes les écritures d'état passent par `stores.investigation.mutate()`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.execution.runner import run_process
from app.models import RunStatus
from app.stores import investigation as store
from app.tools import registry
from app.tools.base import EXTRACTION, ToolContext

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Job:
    task: asyncio.Task
    stop_event: asyncio.Event


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sems = {
            EXTRACTION: asyncio.Semaphore(max(1, settings.max_concurrent_runs)),
        }
        self._timeouts = {EXTRACTION: settings.run_timeout_seconds}
        self._jobs: dict[str, _Job] = {}
        self._shutting_down = False

    def _kind(self, run_id: str) -> str:
        run = store.get().runs.get(run_id)
        kind = registry.kind_of(run.tool) if run else EXTRACTION
        return kind if kind in self._sems else EXTRACTION

    # -- introspection ----------------------------------------------------------
    def active_run_ids(self) -> list[str]:
        return [rid for rid, job in self._jobs.items() if not job.task.done()]

    def active_count(self) -> int:
        return len(self.active_run_ids())

    # -- cycle de vie ---------------------------------------------------------
    async def reconcile_startup(self) -> None:
        interrupted = 0
        async with store.mutate() as inv:
            for run in inv.runs.values():
                if run.status.is_active:
                    run.status = RunStatus.FAILED
                    run.error = "interrompu par un redémarrage du service"
                    run.finished_at = _now()
                    interrupted += 1
        if interrupted:
            logger.warning(
                "reconcile: %d run(s) actif(s) au chargement -> failed", interrupted
            )

    def submit(self, run_id: str) -> bool:
        if self._shutting_down:
            logger.warning("submit refusé (shutdown en cours) run=%s", run_id)
            return False
        job = self._jobs.get(run_id)
        if job and not job.task.done():
            logger.debug("submit ignoré : run %s déjà en cours", run_id)
            return False
        stop = asyncio.Event()
        task = asyncio.create_task(self._execute(run_id, stop), name=f"run:{run_id}")
        self._jobs[run_id] = _Job(task=task, stop_event=stop)
        logger.info("run %s: soumis", run_id)
        return True

    async def request_stop(self, run_id: str) -> None:
        job = self._jobs.get(run_id)
        if job and not job.task.done():
            logger.info("run %s: arrêt demandé", run_id)
            job.stop_event.set()

    async def shutdown(self, grace_seconds: float = 20.0) -> None:
        self._shutting_down = True
        active = self.active_run_ids()
        if not active:
            return
        logger.info("shutdown: arrêt de %d run(s) en cours : %s", len(active), active)
        for rid in active:
            job = self._jobs.get(rid)
            if job:
                job.stop_event.set()
        tasks = [self._jobs[rid].task for rid in active if rid in self._jobs]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=grace_seconds
            )
        except TimeoutError:
            still = self.active_run_ids()
            logger.warning("shutdown: délai dépassé, annulation de %s", still)
            for rid in still:
                self._jobs[rid].task.cancel()

    # -- exécution d'un run --------------------------------------------------
    async def _execute(self, run_id: str, stop: asyncio.Event) -> None:
        try:
            async with self._sems[self._kind(run_id)]:
                if stop.is_set():
                    await self._finish(run_id, RunStatus.CANCELLED, error="annulé avant démarrage")
                    return
                await self._run_once(run_id, stop)
        except asyncio.CancelledError:
            # Annulation en force (shutdown au-delà du délai de grâce, ou arrêt
            # de la boucle). Le sous-processus a déjà été tué par run_process ;
            # le run reste "running" et sera repris par reconcile_startup au
            # prochain démarrage. On ne tente pas d'await ici (l'annulation
            # relancerait aussitôt une CancelledError).
            logger.warning("run %s: tâche annulée en force", run_id)
            raise
        except Exception as exc:  # noqa: BLE001 — filet de sécurité, on veut toujours clôturer le run
            logger.exception("run %s: erreur inattendue", run_id)
            await self._finish(run_id, RunStatus.FAILED, error=str(exc))
        finally:
            self._jobs.pop(run_id, None)

    async def _run_once(self, run_id: str, stop: asyncio.Event) -> None:
        # 1. running + snapshot des données nécessaires (hors lock ensuite)
        async with store.mutate() as inv:
            run = inv.runs.get(run_id)
            if run is None:
                logger.error("run %s introuvable", run_id)
                return
            if run.status != RunStatus.PENDING:
                logger.warning("run %s: statut %s (attendu pending) — abandon", run_id, run.status)
                return
            acq = inv.acquisitions.get(run.acquisition_id)
            tool_name = run.tool
            acq_id = run.acquisition_id
            log_path = run.log_path
            run.status = RunStatus.RUNNING
            run.started_at = _now()
            acq_snapshot = acq.model_copy(deep=True) if acq else None
            run_snapshot = run.model_copy(deep=True)

        logger.info("run %s: RUNNING (tool=%s acquisition=%s)", run_id, tool_name, acq_id)

        if acq_snapshot is None:
            await self._finish(run_id, RunStatus.FAILED, error="acquisition introuvable")
            return

        try:
            tool = registry.get(tool_name)
        except KeyError as exc:
            await self._finish(run_id, RunStatus.FAILED, error=str(exc))
            return

        if not tool.available():
            await self._finish(
                run_id,
                RunStatus.FAILED,
                error=f"outil '{tool_name}' indisponible dans cet environnement",
            )
            return

        logger.info("run %s: %s version=%s", run_id, tool_name, tool.version())
        timeout = self._timeouts.get(tool.kind, self._settings.run_timeout_seconds)

        # 2. plan + prepare (I/O disque -> thread)
        try:
            plan = tool.plan(
                ToolContext(
                    acquisition=acq_snapshot, run=run_snapshot, settings=self._settings
                )
            )
            await asyncio.to_thread(plan.prepare)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run %s: préparation échouée", run_id)
            await self._finish(run_id, RunStatus.FAILED, error=f"préparation : {exc}")
            return

        run_dir = Path(self._settings.runs_dir) / run_id
        async with store.mutate() as inv:
            run = inv.runs.get(run_id)
            if run is not None:
                run.command = list(plan.argv)
                run.workdir = str(plan.cwd)
                run.output_dir = str(plan.output_dir)

        async def _persist_sample(sample: dict) -> None:
            async with store.mutate() as inv:
                run = inv.runs.get(run_id)
                if run is not None and run.status == RunStatus.RUNNING:
                    run.last_rss_bytes = sample["rss_bytes"]
                    run.last_cpu_percent = sample["cpu_percent"]
                    run.peak_rss_bytes = max(run.peak_rss_bytes or 0, sample["rss_bytes"])
                    run.metrics_samples = run.metrics_samples + 1

        # 3. exécution
        result = await run_process(
            argv=plan.argv,
            cwd=plan.cwd,
            log_path=Path(log_path),
            env=plan.env,
            timeout_seconds=timeout,
            memory_limit_mb=self._settings.run_memory_limit_mb,
            cpu_seconds=self._settings.run_cpu_seconds,
            cpu_cores=self._settings.run_cpu_cores,
            nice=self._settings.run_nice,
            stop_event=stop,
            metrics_path=run_dir / "metrics.jsonl",
            metrics_interval=self._settings.run_metrics_interval_seconds,
            on_sample=_persist_sample,
        )

        async with store.mutate() as inv:
            run = inv.runs.get(run_id)
            if run is not None:
                peak = max(run.peak_rss_bytes or 0, result.peak_rss_bytes or 0)
                run.peak_rss_bytes = peak or None
                run.last_rss_bytes = result.last_rss_bytes
                run.last_cpu_percent = result.last_cpu_percent
                run.metrics_samples = result.metrics_samples

        # 4. clôture
        if result.cancelled:
            await self._finish(
                run_id,
                RunStatus.CANCELLED,
                exit_code=result.exit_code,
                duration=result.duration_seconds,
                error="annulé",
            )
        elif result.timed_out:
            await self._finish(
                run_id,
                RunStatus.FAILED,
                exit_code=result.exit_code,
                duration=result.duration_seconds,
                error=f"timeout ({timeout}s)",
            )
        elif result.exit_code == 0:
            await self._collect_result(run_id, plan)
            await self._finish(
                run_id, RunStatus.SUCCEEDED, exit_code=0, duration=result.duration_seconds
            )
        else:
            await self._finish(
                run_id,
                RunStatus.FAILED,
                exit_code=result.exit_code,
                duration=result.duration_seconds,
                error=f"code de sortie {result.exit_code}",
            )

    async def _collect_result(self, run_id: str, plan) -> None:
        """Appelle `plan.collect_result` (best-effort) et stocke le retour dans
        `Run.result`. Une exception est loggée sans faire échouer le run."""
        if plan.collect_result is None:
            return
        try:
            data = await asyncio.to_thread(plan.collect_result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("run %s: collect_result a échoué (%s)", run_id, exc)
            return
        if not isinstance(data, dict) or not data:
            return
        async with store.mutate() as inv:
            run = inv.runs.get(run_id)
            if run is not None:
                run.result = {**run.result, **data}
        logger.info("run %s: résultat %s", run_id, data)

    async def _finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        exit_code: int | None = None,
        duration: float | None = None,
        error: str | None = None,
    ) -> None:
        async with store.mutate() as inv:
            run = inv.runs.get(run_id)
            if run is None:
                return
            if run.status.is_terminal and status != run.status:
                # déjà clôturé (double appel via except + finally) : ne pas écraser
                return
            run.status = status
            run.finished_at = _now()
            if exit_code is not None:
                run.exit_code = exit_code
            if duration is not None:
                run.duration_seconds = round(duration, 2)
            if error is not None:
                run.error = error

        level = logging.INFO if status == RunStatus.SUCCEEDED else logging.WARNING
        logger.log(
            level,
            "run %s: %s (exit=%s dur=%.1fs%s)",
            run_id,
            status.value,
            exit_code,
            duration or 0.0,
            f" error={error}" if error else "",
        )


# --- Singleton, posé par app/main.py au démarrage -----------------------------
_manager: JobManager | None = None


def set_job_manager(manager: JobManager) -> None:
    global _manager
    _manager = manager


def get_job_manager() -> JobManager:
    if _manager is None:
        raise RuntimeError("JobManager non initialisé (app/main.py::lifespan)")
    return _manager
