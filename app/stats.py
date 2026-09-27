"""Agrégats et mesures pour l'UI web : compteurs de tête, volumétrie des
acquisitions et des sorties, chronologie de traitement par machine.

Tout ce qui touche au disque est mis en cache avec un TTL court : ces fonctions
sont appelées à chaque rafraîchissement des partiels (toutes les 3 s), il ne
faut pas re-parcourir l'arborescence à chaque fois.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import Acquisition, Investigation, RunStatus
from app.stores import acquisitions as acq_store
from app.stores import runs as run_store
from app.tools.registry import EXTRACTION_ORDER

logger = logging.getLogger(__name__)

_CACHE_TTL = 10.0
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, producer):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    value = producer()
    _cache[key] = (now + _CACHE_TTL, value)
    return value


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


# --- Volumétrie -------------------------------------------------------------

def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def acquisition_size_bytes(storage_name: str) -> int:
    """Taille cumulée des archives uploadées d'une acquisition, désignée par
    son nom de dossier (`Acquisition.storage_name`)."""
    path = settings.uploads_dir / storage_name
    return _cached(f"acq_size:{storage_name}", lambda: _dir_size(path))


def acquisition_file_sizes(storage_name: str) -> dict[str, int]:
    """Taille de chaque fichier déposé, par nom. Un scandir du seul répertoire
    de l'acquisition (pas de récursion) : assez léger pour la liste des dépôts,
    et mis en cache comme le reste."""
    def compute() -> dict[str, int]:
        sizes: dict[str, int] = {}
        try:
            with os.scandir(settings.uploads_dir / storage_name) as entries:
                for entry in entries:
                    if entry.is_file():
                        sizes[entry.name] = entry.stat().st_size
        except OSError:
            return {}
        return sizes

    return _cached(f"acq_files:{storage_name}", compute)


def _volume(path) -> dict:
    """Occupation du système de fichiers qui héberge `path`.

    `shutil.disk_usage` = un statvfs, pas un parcours d'arborescence : peu
    importe le volume des collectes, c'est instantané."""
    try:
        usage = shutil.disk_usage(path if path.is_dir() else settings.data_dir)
    except OSError:
        return {"total": None, "used": None, "free": None, "percent": None}
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": (usage.used / usage.total * 100) if usage.total else None,
    }


def storage() -> dict:
    """Occupation du volume qui héberge les données.

    Collectes et sorties vivent toutes deux sous `data/` : une seule mesure, le
    système de fichiers de `data_dir`, plutôt que deux cartes qui affichaient la
    même chose."""
    return _cached("storage", lambda: _volume(settings.data_dir))


# --- Ce qui compte dans un « X / Y » ----------------------------------------

def _counted(inv: Investigation, acq: Acquisition, tools, encore: bool) -> list[str]:
    """Traitements à faire figurer au dénominateur : ceux qui ont tourné, plus
    ceux qui peuvent encore démarrer.

    Un outil décoché dans les paramètres, ou dont la phase est terminée sans
    qu'il ait rien eu à traiter (pas de MFT, pas de ruche SYSTEM), ne
    démarrera jamais : le compter afficherait « 9 / 15 » sur une machine
    entièrement traitée. Un outil qui a tourné compte toujours, même décoché
    depuis — sa sortie existe.

    Le total passe par `_phase_counts`."""
    latest = run_store.latest_runs_by_tool(inv, acq.id)
    return [
        tool for tool in tools
        if tool in latest or (encore and settings.tool_enabled(tool))
    ]


def _phase_counts(inv: Investigation, acq: Acquisition) -> list[str]:
    """Tous les traitements qui comptent pour une acquisition."""
    from app.pipeline import stages

    return _counted(inv, acq, EXTRACTION_ORDER,
                    stages.extraction_pending(inv, acq) is not None)


# --- Chronologie d'une acquisition ------------------------------------------

def acquisition_timing(inv: Investigation, acquisition_id: str) -> dict:
    """Réception, durée de traitement et avancement en nombre d'étapes.

    La durée court du premier run démarré au dernier terminé."""
    runs = [r for r in run_store.runs_for(inv, acquisition_id)
            if r.tool in EXTRACTION_ORDER]
    starts = [d for d in (_parse(r.started_at) for r in runs) if d]
    ends = [d for d in (_parse(r.finished_at) for r in runs) if d]
    active = any(r.status.is_active for r in runs)

    duration = None
    if starts:
        last = datetime.now(starts[0].tzinfo or UTC) if active or not ends else max(ends)
        duration = max(0.0, (last - min(starts)).total_seconds())

    from app.pipeline import stages

    acq = inv.acquisitions.get(acquisition_id)
    comptes = (
        _counted(inv, acq, EXTRACTION_ORDER, stages.extraction_pending(inv, acq) is not None)
        if acq is not None
        else list(EXTRACTION_ORDER)
    )
    done = sum(
        1 for tool in comptes if run_store.has_succeeded(inv, acquisition_id, tool)
    )
    return {
        "duration_seconds": duration,
        "running": active,
        "steps_done": done,
        "steps_total": len(comptes),
    }


# --- Les trois phases d'une acquisition (carte de la vue d'ensemble) --------

PHASE_STATE_LABELS = {
    "done": "Terminée",
    "running": "En cours",
    "failed": "Échec",
    "pending": "En attente",
    "off": "Désactivée",
}


def _collect_phase(acq: Acquisition) -> str:
    """La collecte est finie quand l'Outcome.json et toutes ses archives sont
    arrivés — c'est ce qui autorise l'extraction à démarrer."""
    if not acq.outcome_filename:
        return "pending"
    return "done" if acq_store.acquisition_upload_status(acq)["complete"] else "running"


def _latest(inv: Investigation, acq: Acquisition, tools) -> list:
    latest = run_store.latest_runs_by_tool(inv, acq.id)
    return [run for tool, run in latest.items() if tool in tools]


def _extraction_phase(inv: Investigation, acq: Acquisition) -> str:
    """Un détecteur qui n'a jamais tourné faute d'EVTX ne doit pas laisser
    l'extraction éternellement « en cours » : on s'en remet à la condition qui
    débloque l'analyse dans le pipeline."""
    from app.pipeline import stages

    runs = _latest(inv, acq, EXTRACTION_ORDER)
    if any(run.status.is_active for run in runs):
        return "running"
    failed = any(run.status == RunStatus.FAILED for run in runs)
    if stages.extraction_pending(inv, acq) is None:
        return "failed" if failed else "done"
    if failed:
        return "failed"
    return "running" if runs else "pending"


def acquisition_phases(inv: Investigation, acq: Acquisition) -> list[dict]:
    """Les deux étapes montrées dans la vue d'ensemble. Le détail outil par
    outil est dans les onglets dédiés : ici, on veut savoir où en est la
    machine, pas combien de détections a produit tel moteur."""
    return [
        {"name": "collecte", "label": "Collecte", "state": _collect_phase(acq)},
        {"name": "extraction", "label": "Extraction", "state": _extraction_phase(inv, acq)},
    ]


def acquisition_facts(inv: Investigation, acq: Acquisition) -> dict:
    """Tout ce que la carte machine affiche en en-tête."""
    facts = acquisition_timing(inv, acq.id)
    facts["collect_bytes"] = acquisition_size_bytes(acq.storage_name)
    facts["received_at"] = _parse(acq.created_at)
    facts["file_sizes"] = acquisition_file_sizes(acq.storage_name)
    facts["phases"] = acquisition_phases(inv, acq)
    facts["failed_phase"] = next(
        (p["label"] for p in facts["phases"] if p["state"] == "failed"), None
    )
    return facts


def facts_by_acq(inv: Investigation) -> dict[str, dict]:
    return {aid: acquisition_facts(inv, acq) for aid, acq in inv.acquisitions.items()}


# --- Compteurs de tête ------------------------------------------------------

def headline(inv: Investigation) -> dict:
    """Les deux chiffres de l'en-tête : machines et traitements terminés. Les
    volumétries par outil — journaux, détections, documents — se lisent dans
    les onglets correspondants. `active` et `runs_failed` alimentent la
    pastille d'état.

    Le dénominateur suit la règle de `_counted` : seuls les traitements faits
    ou encore attendus. Conséquence assumée, il peut diminuer en fin de
    parcours, quand on sait enfin ce qui ne tournera pas."""
    done = 0
    total = 0
    failed = 0
    for acq_id, acq in inv.acquisitions.items():
        latest = run_store.latest_runs_by_tool(inv, acq_id)
        for tool in _phase_counts(inv, acq):
            total += 1
            run = latest.get(tool)
            if run is None:
                continue
            if run.status == RunStatus.SUCCEEDED:
                done += 1
            elif run.status == RunStatus.FAILED:
                failed += 1

    return {
        "machines": len(inv.acquisitions),
        "runs_done": done,
        "runs_total": total,
        "runs_failed": failed,
        "active": any(r.status.is_active for r in inv.runs.values()),
    }


# Libellés d'état en français : la valeur brute (`succeeded`, `running`...) reste
# celle du modèle, seul l'affichage est traduit.
STATUS_LABELS = {
    "pending": "En attente",
    "running": "En cours",
    "succeeded": "Réussi",
    "failed": "Échec",
    "cancelled": "Annulé",
}




