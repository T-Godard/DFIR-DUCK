"""Helpers partagés par les routes : instance Jinja2 unique, filtres de mise en
forme (français : espace fine insécable, virgule décimale) et construction du
contexte de rendu commun (pages + partiels HTMX).
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from app import stats
from app.config import settings
from app.execution import sysinfo
from app.models import Investigation, Run, RunStatus
from app.stores import acquisitions as acq_store
from app.stores import preferences
from app.stores import runs as run_store
from app.tools.registry import EXTRACTION_ORDER, TOOL_LABELS

# Traitements réglables depuis l'onglet Paramètres (voir app/api/settings.py).
SETTABLE_TOOLS = EXTRACTION_ORDER

templates = Jinja2Templates(directory="app/templates")

# Espace fine insécable : séparateur de milliers français, ne casse pas la ligne.
_NBSP = " "

_MONTHS = (
    "janv.", "févr.", "mars", "avril", "mai", "juin",
    "juill.", "août", "sept.", "oct.", "nov.", "déc.",
)
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def human_bytes(n: int | float | None) -> str:
    if not n:
        return "—"
    val = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if val < 1024:
            text = f"{val:.0f}" if unit == "o" else f"{val:.1f}"
            return f"{text.replace('.', ',')}{_NBSP}{unit}"
        val /= 1024
    return f"{val:.1f}".replace(".", ",") + f"{_NBSP}Po"


def fr_int(n: int | float | None) -> str:
    """12345 -> « 12 345 » (espace fine insécable)."""
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", _NBSP)


def pct(part: float | None, whole: float | None) -> str:
    if not part or not whole:
        return ""
    return f"{part / whole * 100:.0f}{_NBSP}%"


def human_duration(seconds: float | None) -> str:
    """45 s · 11 min 06 · 2 h 14."""
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}{_NBSP}s"
    if seconds < 3600:
        return f"{seconds // 60}{_NBSP}min{_NBSP}{seconds % 60:02d}"
    return f"{seconds // 3600}{_NBSP}h{_NBSP}{(seconds % 3600) // 60:02d}"


def relative_datetime(moment: datetime | None) -> str:
    """« il y a 6 min », « hier, 18:42 », « 3 avril, 08:30 »."""
    if moment is None:
        return "—"
    now = datetime.now(moment.tzinfo or UTC)
    delta = (now - moment).total_seconds()
    clock = moment.strftime("%H:%M")
    if delta < 60:
        return "à l'instant"
    if delta < 3600:
        return f"il y a {int(delta // 60)}{_NBSP}min"
    days = (now.date() - moment.date()).days
    if days <= 0:
        return f"aujourd'hui, {clock}"
    if days == 1:
        return f"hier, {clock}"
    if days < 7:
        return f"{_WEEKDAYS[moment.weekday()]}, {clock}"
    return f"{moment.day}{_NBSP}{_MONTHS[moment.month - 1]}, {clock}"


def elapsed_seconds(run: Run) -> float | None:
    """Durée écoulée depuis le démarrage d'un run encore `running` — pour
    afficher une durée « en cours » tant que le run n'est pas terminé
    (`run.duration_seconds` ne se remplit qu'à la clôture)."""
    if run.status != RunStatus.RUNNING or not run.started_at:
        return None
    try:
        started = datetime.fromisoformat(run.started_at)
    except ValueError:
        return None
    now = datetime.now(started.tzinfo or UTC)
    return max(0.0, (now - started).total_seconds())


def run_duration(run: Run | None) -> float | None:
    """Durée affichable d'un run : la durée finale, ou le temps écoulé s'il tourne."""
    if run is None:
        return None
    return run.duration_seconds if run.duration_seconds is not None else elapsed_seconds(run)


def result_summary(run: Run | None) -> str:
    """Résultat d'un run en clair (« 390 journaux », « 1 493 détections »)
    plutôt que le dictionnaire brut : c'est la même lecture partout dans l'UI."""
    if run is None or not run.result:
        return ""
    r = run.result
    if r.get("findings") is not None:
        high = int(r.get("high") or 0)
        suffix = f", dont {fr_int(high)} élevés" if high else ""
        return f"{fr_int(r['findings'])} constats{suffix}"
    if r.get("documents") is not None:
        rejected = int(r.get("rejected") or 0)
        suffix = f", {fr_int(rejected)} rejetés" if rejected else ""
        return f"{fr_int(r['documents'])} documents indexés{suffix}"
    if r.get("evtx_files") is not None:
        return f"{fr_int(r['evtx_files'])} journaux EVTX"
    if r.get("mft_files") is not None:
        return f"{fr_int(r['mft_files'])} MFT"
    if r.get("detections") is not None:
        return f"{fr_int(r['detections'])} détections"
    if r.get("output_bytes") is not None:
        return f"{human_bytes(r['output_bytes'])} produits"
    return ", ".join(f"{k} = {v}" for k, v in r.items())


# Disponibles comme filtres ({{ x | human_bytes }}) ET comme fonctions
# ({{ pct(a, b) }}) dans les templates.
for _name, _fn in (
    ("human_bytes", human_bytes),
    ("fr_int", fr_int),
    ("pct", pct),
    ("human_duration", human_duration),
    ("relative_datetime", relative_datetime),
    ("elapsed_seconds", elapsed_seconds),
    ("run_duration", run_duration),
    ("result_summary", result_summary),
):
    templates.env.filters[_name] = _fn
    templates.env.globals[_name] = _fn

# Tables de libelles : globales Jinja, donc disponibles y compris dans les
# partiels rendus avec un contexte reduit (fenetre de log par exemple).
templates.env.globals["tool_labels"] = TOOL_LABELS
templates.env.globals["status_labels"] = stats.STATUS_LABELS
templates.env.globals["phase_state_labels"] = stats.PHASE_STATE_LABELS
templates.env.globals["uploads_enabled"] = settings.web_uploads_enabled
# Relu à chaque rendu : les traitements se (dés)activent depuis l'onglet
# Paramètres, sans redémarrage.
templates.env.globals["tool_enabled"] = settings.tool_enabled
templates.env.globals["report_status_labels"] = {
    "complete": "Complet", "partial": "Partiel", "no_data": "Sans données",
}
templates.env.globals["verdict_labels"] = {
    "compromised": "Compromission avérée",
    "suspicious": "Compromission suspectée",
    "not_compromised": "Pas de compromission",
    "undetermined": "Non déterminable",
}


def runs_by_acq(inv: Investigation) -> dict[str, dict]:
    return {aid: run_store.latest_runs_by_tool(inv, aid) for aid in inv.acquisitions}


def base_context(inv: Investigation) -> dict:
    """Contexte de rendu commun. `request` est passé séparément à
    `templates.TemplateResponse(request, name, context)`."""
    return {
        "inv": inv,
        "upload_status": acq_store.upload_status(inv),
        "runs_by_acq": runs_by_acq(inv),
        "resources": sysinfo.app_snapshot(),
        "extraction_order": EXTRACTION_ORDER,
        "headline": stats.headline(inv),
        "facts_by_acq": stats.facts_by_acq(inv),
        "hostname": socket.gethostname(),
        "tools": preferences.tools_state(SETTABLE_TOOLS),
        "resource_refresh_ms": settings.resource_refresh_ms,
    }
