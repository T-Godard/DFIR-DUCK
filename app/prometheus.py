"""Génération de la page de métriques Prometheus (format texte 0.0.4).

Pas de dépendance `prometheus_client` : les métriques sont un instantané calculé
à la volée depuis l'état en mémoire (`Investigation`) et `psutil` / cgroup. Tout
est en `gauge` sauf les compteurs monotones explicitement marqués `counter`.
"""

from __future__ import annotations

import time
from datetime import datetime

from app.config import settings
from app.execution import sysinfo
from app.models import Investigation, RunStatus
from app.pipeline import orchestrator
from app.stores import acquisitions as acq_store
from app.stores import runs as run_store

_LABEL_ESCAPE = str.maketrans({"\\": r"\\", '"': r"\"", "\n": r"\n"})
_HELP_ESCAPE = str.maketrans({"\\": r"\\", "\n": r"\n"})

_STATE_CODE = {
    RunStatus.PENDING: 0,
    RunStatus.RUNNING: 1,
    RunStatus.SUCCEEDED: 2,
    RunStatus.FAILED: 3,
    RunStatus.CANCELLED: 4,
}


def _lbl(value: object) -> str:
    return str(value).translate(_LABEL_ESCAPE)


def _help(text: str) -> str:
    return text.translate(_HELP_ESCAPE)


def _num(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


class _Exposition:
    """Bufferise par famille de métrique pour garantir le regroupement (le
    format Prometheus impose que toutes les lignes d'un même nom soient
    contiguës, précédées de leurs HELP/TYPE)."""

    def __init__(self) -> None:
        self._fam: dict[str, dict] = {}

    def add(
        self,
        name: str,
        mtype: str,
        help_: str,
        value: object,
        labels: dict[str, object] | None = None,
    ) -> None:
        if value is None:
            return
        fam = self._fam.setdefault(name, {"type": mtype, "help": help_, "samples": []})
        fam["samples"].append((labels or {}, value))

    def text(self) -> str:
        out: list[str] = []
        for name, fam in self._fam.items():
            out.append(f"# HELP {name} {_help(fam['help'])}")
            out.append(f"# TYPE {name} {fam['type']}")
            for labels, value in fam["samples"]:
                pairs = ",".join(
                    f'{k}="{_lbl(v)}"' for k, v in labels.items() if v is not None
                )
                out.append(f"{name}{{{pairs}}} {_num(value)}" if pairs else f"{name} {_num(value)}")
        return "\n".join(out) + "\n"


def render(inv: Investigation) -> str:
    started = time.perf_counter()
    e = _Exposition()

    e.add("dfir_build_info", "gauge", "Métadonnées de build (toujours 1).", 1, {"version": "0.1"})

    # --- Acquisitions & uploads ---------------------------------------------
    e.add("dfir_acquisitions", "gauge", "Nombre d'acquisitions.", len(inv.acquisitions))

    by_type = {"archive": 0, "outcome": 0, "other": 0}
    complete = incomplete = total_expected = total_missing = 0
    for acq in inv.acquisitions.values():
        st = acq_store.acquisition_upload_status(acq)
        complete, incomplete = (
            (complete + 1, incomplete) if st["complete"] else (complete, incomplete + 1)
        )
        total_expected += len(st["expected"])
        total_missing += len(st["missing"])
        for fname in acq.uploaded_filenames:
            if fname == acq.outcome_filename:
                by_type["outcome"] += 1
            elif fname.lower().endswith(".7z"):
                by_type["archive"] += 1
            else:
                by_type["other"] += 1

        la = {"acquisition_id": acq.id, "acquisition": acq.name}
        e.add("dfir_acquisition_uploaded_files", "gauge",
              "Fichiers uploadés rattachés à l'acquisition.", len(acq.uploaded_filenames), la)
        e.add("dfir_acquisition_expected_archives", "gauge",
              "Archives .7z attendues d'après l'Outcome.json.", len(st["expected"]), la)
        e.add("dfir_acquisition_missing_archives", "gauge",
              "Archives .7z encore manquantes.", len(st["missing"]), la)
        e.add("dfir_acquisition_uploads_complete", "gauge",
              "1 si toutes les archives de l'acquisition sont présentes.",
              1 if st["complete"] else 0, la)

    for t, n in by_type.items():
        e.add("dfir_uploaded_files", "gauge", "Fichiers uploadés, par type.", n, {"type": t})
    e.add("dfir_acquisitions_uploads_complete", "gauge",
          "Acquisitions dont l'upload est complet.", complete)
    e.add("dfir_acquisitions_uploads_incomplete", "gauge",
          "Acquisitions en attente d'archives.", incomplete)
    e.add("dfir_expected_archives", "gauge", "Total d'archives attendues.", total_expected)
    e.add("dfir_missing_archives", "gauge", "Total d'archives manquantes.", total_missing)
    e.add("dfir_pending_files", "gauge",
          "Fichiers dans uploads/_pending/ pas encore classés.", acq_store.pending_file_count())

    # --- Runs -------------------------------------------------------------
    counts = dict.fromkeys(RunStatus, 0)
    for run in inv.runs.values():
        counts[run.status] += 1
    for status, n in counts.items():
        e.add("dfir_runs", "gauge", "Runs par statut.", n, {"status": status.value})
    e.add("dfir_runs_active", "gauge", "Runs en cours (pending ou running).",
          sum(1 for r in inv.runs.values() if r.status.is_active))
    e.add("dfir_runs_created_total", "counter",
          "Runs créés au total (historique complet, persisté).", len(inv.runs))

    for acq in inv.acquisitions.values():
        for tool, run in run_store.latest_runs_by_tool(inv, acq.id).items():
            lr = {"acquisition_id": acq.id, "acquisition": acq.name, "tool": tool}
            e.add("dfir_acquisition_last_run_state", "gauge",
                  "Statut du dernier run (0=pending 1=running 2=succeeded 3=failed 4=cancelled).",
                  _STATE_CODE.get(run.status, -1), lr)
            e.add("dfir_acquisition_last_run_info", "gauge",
                  "Dernier run par acquisition/outil ; statut courant en label.",
                  1, {**lr, "status": run.status.value})
            e.add("dfir_acquisition_last_run_attempt", "gauge",
                  "Numéro d'essai du dernier run.", run.attempt, lr)
            e.add("dfir_acquisition_last_run_exit_code", "gauge",
                  "Code de sortie du dernier run.", run.exit_code, lr)
            e.add("dfir_acquisition_last_run_duration_seconds", "gauge",
                  "Durée du dernier run terminé.", run.duration_seconds, lr)
            e.add("dfir_acquisition_last_run_started_timestamp_seconds", "gauge",
                  "Début du dernier run (epoch).", _epoch(run.started_at), lr)
            e.add("dfir_acquisition_last_run_finished_timestamp_seconds", "gauge",
                  "Fin du dernier run (epoch).", _epoch(run.finished_at), lr)
            e.add("dfir_run_cpu_percent", "gauge",
                  "CPU % du dernier run (dernier échantillon ; >100 = multi-cœur).",
                  run.last_cpu_percent, lr)
            e.add("dfir_run_resident_bytes", "gauge",
                  "RAM résidente du dernier run (dernier échantillon).", run.last_rss_bytes, lr)
            e.add("dfir_run_peak_resident_bytes", "gauge",
                  "Pic de RAM résidente du dernier run.", run.peak_rss_bytes, lr)
            e.add("dfir_run_memory_limit_bytes", "gauge",
                  "Plafond RAM appliqué au run (RLIMIT_AS ou conteneur).", run.mem_limit_bytes, lr)
            e.add("dfir_run_cpu_cores_limit", "gauge",
                  "Cœurs CPU alloués au run (absent = tous).", run.cpu_cores_limit, lr)
            for key, value in (run.result or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    e.add("dfir_run_result", "gauge",
                          "Sortie chiffrée du dernier run (evtx_files, detections, …).",
                          value, {**lr, "key": key})

    # --- Ressources -------------------------------------------------------
    snap = sysinfo.app_snapshot()
    e.add("dfir_process_resident_bytes", "gauge",
          "RAM résidente du process applicatif.", snap["app_rss_bytes"])
    e.add("dfir_system_memory_bytes", "gauge", "Mémoire système.",
          snap["system_mem_total_bytes"], {"state": "total"})
    e.add("dfir_system_memory_bytes", "gauge", "Mémoire système.",
          snap["system_mem_used_bytes"], {"state": "used"})
    e.add("dfir_cgroup_memory_current_bytes", "gauge",
          "Mémoire utilisée par le conteneur (cgroup v2).", snap["cgroup_mem_current_bytes"])
    e.add("dfir_cgroup_memory_limit_bytes", "gauge",
          "Limite mémoire du conteneur (cgroup v2).", snap["cgroup_mem_limit_bytes"])
    e.add("dfir_cgroup_cpu_quota_cores", "gauge",
          "Quota CPU du conteneur en cœurs (cgroup v2).", sysinfo.read_cgroup_cpu_quota())
    e.add("dfir_cpu_count", "gauge", "Cœurs CPU visibles (hôte).", snap["cpu_count"])
    e.add("dfir_cpu_usage_percent", "gauge",
          "CPU consommé (% multi-cœur, peut dépasser 100). Label scope="
          "container : cgroup du conteneur (précis) ; scope=host : repli sur "
          "l'hôte entier (hors conteneur, ou tout premier échantillon).",
          snap["cpu_percent"], {"scope": snap["cpu_percent_scope"]})

    # --- Orchestrateur / config ----------------------------------------
    o = orchestrator.stats()
    e.add("dfir_orchestrator_last_tick_timestamp_seconds", "gauge",
          "Horodatage du dernier tick de l'orchestrateur (epoch).",
          o["last_tick_wall"] or None)
    e.add("dfir_orchestrator_ticks_total", "counter",
          "Ticks de l'orchestrateur depuis le démarrage.", o["tick_count"])
    e.add("dfir_config_max_concurrent_runs", "gauge",
          "MAX_CONCURRENT_RUNS.", settings.max_concurrent_runs)
    e.add("dfir_config_run_timeout_seconds", "gauge",
          "RUN_TIMEOUT_SECONDS.", settings.run_timeout_seconds)

    e.add("dfir_scrape_duration_seconds", "gauge",
          "Durée de génération de cette page.", time.perf_counter() - started)
    return e.text()
