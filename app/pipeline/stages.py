"""Étapes du pipeline d'analyse.

Une `Stage` décrit un outil à lancer sur une acquisition et la condition qui le
rend « prêt à démarrer ». L'orchestrateur évalue les étapes à chaque tick, dans
l'ordre, et lance un run par étape prête.

Dès qu'une acquisition a tous ses uploads : **OGRE**, **evtx-extract**,
**mft-extract** et **registry-extract** démarrent en parallèle. Chaque
extraction libère ensuite ses analyseurs : **evtx-chainsaw / evtx-hayabusa /
evtx-zircolite** quand **evtx-extract** a produit ≥ 1 EVTX, **mft-chainsaw**
quand **mft-extract** a produit ≥ 1 MFT, **registry-chainsaw** quand
**registry-extract** a produit ≥ 1 ruche SYSTEM.

Démarrage automatique une seule fois : dès qu'un run de l'outil existe (en cours,
réussi OU échoué), `ready()` = False — un échec se relance à la main (« Forcer »).

Un traitement décoché dans l'onglet Paramètres (`tools_enabled`) ne démarre
jamais et ne retient personne : les étapes suivantes le voient comme un outil
qui n'a rien produit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.config import settings
from app.models import Acquisition, Investigation, RunStatus
from app.stores import acquisitions as acq_store
from app.stores import runs as run_store


class Stage(Protocol):
    name: str
    tool: str

    def ready(self, inv: Investigation, acq: Acquisition) -> bool: ...

    def blocked_reason(self, inv: Investigation, acq: Acquisition) -> str | None: ...


DISABLED = "désactivé dans les paramètres"


def _enabled(tool: str) -> bool:
    """Un traitement décoché dans l'onglet Paramètres ne démarre pas — ni tout
    seul, ni par « Forcer » (voir app/api/runs.py) — et ne retient pas la
    suite : les étapes suivantes le traitent comme un outil qui n'a rien
    produit."""
    return settings.tool_enabled(tool)


def _uploads_complete(acq: Acquisition) -> bool:
    return bool(acq.outcome_filename) and acq_store.acquisition_upload_status(acq)["complete"]


def _already_ran(inv: Investigation, acq: Acquisition, tool: str) -> bool:
    return bool(run_store.runs_for(inv, acq.id, tool))


def _moment(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _last_success_end(inv: Investigation, acq: Acquisition, tool: str) -> datetime | None:
    """Fin du dernier run réussi d'un outil — la date à laquelle sa sortie
    courante a été produite."""
    ends = [
        _moment(run.finished_at)
        for run in run_store.runs_for(inv, acq.id, tool)
        if run.status == RunStatus.SUCCEEDED
    ]
    return max((end for end in ends if end), default=None)


def _last_run_reason(inv: Investigation, acq: Acquisition, tool: str) -> str | None:
    runs = run_store.runs_for(inv, acq.id, tool)
    if not runs:
        return None
    last = runs[-1].status
    return "run en cours" if last.is_active else f"dernier run : {last} (relance via « Forcer »)"


class _FromUploadsStage:
    """Extraction tirée directement des archives : ne dépend que des uploads.

    Même forme pour les EVTX, la MFT et les ruches — seul le nom de l'outil
    change. Un artefact d'un autre sujet s'ajoute en héritant d'ici.
    """

    name: str
    tool: str

    def ready(self, inv: Investigation, acq: Acquisition) -> bool:
        return (
            _enabled(self.tool)
            and _uploads_complete(acq)
            and not _already_ran(inv, acq, self.tool)
        )

    def blocked_reason(self, inv: Investigation, acq: Acquisition) -> str | None:
        if not _enabled(self.tool):
            return DISABLED
        if not _uploads_complete(acq):
            return "uploads incomplets"
        return _last_run_reason(inv, acq, self.tool)


class _AfterExtractStage:
    """Analyse d'artefacts extraits : démarre quand son extraction a réussi ET
    en a produit au moins un.

    Une collecte sans l'artefact attendu (pas de MFT, pas de ruche SYSTEM) ne
    bloque personne : l'étape le dit et s'efface.
    """

    name: str
    tool: str
    _DEP: str           # outil d'extraction dont on dépend
    _COUNT_KEY: str     # champ compté dans son résultat
    _EMPTY: str         # raison affichée quand ce compte est nul

    def _count(self, inv: Investigation, acq: Acquisition) -> int | None:
        dep = run_store.latest_run(inv, acq.id, self._DEP)
        if dep is None or not run_store.has_succeeded(inv, acq.id, self._DEP):
            return None
        return int(dep.result.get(self._COUNT_KEY, 0))

    def ready(self, inv: Investigation, acq: Acquisition) -> bool:
        count = self._count(inv, acq)
        return (
            _enabled(self.tool)
            and count is not None
            and count > 0
            and not _already_ran(inv, acq, self.tool)
        )

    def blocked_reason(self, inv: Investigation, acq: Acquisition) -> str | None:
        if not _enabled(self.tool):
            return DISABLED
        count = self._count(inv, acq)
        if count is None:
            return f"en attente du run {self._DEP}"
        if count == 0:
            return self._EMPTY
        return _last_run_reason(inv, acq, self.tool)


class OgreStage:
    name = "ogre"
    tool = "dfir-ogre"

    def ready(self, inv: Investigation, acq: Acquisition) -> bool:
        return (
            _enabled(self.tool)
            and _uploads_complete(acq)
            and not _already_ran(inv, acq, self.tool)
        )

    def blocked_reason(self, inv: Investigation, acq: Acquisition) -> str | None:
        if not _enabled(self.tool):
            return DISABLED
        if not acq.outcome_filename:
            return "en attente de l'Outcome.json"
        status = acq_store.acquisition_upload_status(acq)
        if not status["complete"]:
            return f"{len(status['missing'])} archive(s) manquante(s)"
        return _last_run_reason(inv, acq, self.tool)


class EvtxExtractStage(_FromUploadsStage):
    name = "evtx-extract"
    tool = "evtx-extract"


class MftExtractStage(_FromUploadsStage):
    """La MFT vient des archives, comme les EVTX : DFIR-OGRE ne la sort pas."""

    name = "mft-extract"
    tool = "mft-extract"


class RegistryExtractStage(_FromUploadsStage):
    """Les ruches aussi : OGRE n'en publie que des vues dérivées (services,
    tâches, amcache), jamais la ruche brute dont vient le shimcache."""

    name = "registry-extract"
    tool = "registry-extract"


class MftChainsawStage(_AfterExtractStage):
    """Chasse sur la MFT, avec les règles natives Chainsaw."""

    name = "mft-chainsaw"
    tool = "mft-chainsaw"
    _DEP = "mft-extract"
    _COUNT_KEY = "mft_files"
    _EMPTY = "aucune MFT dans cette acquisition"


class RegistryChainsawStage(_AfterExtractStage):
    """Analyse du shimcache : il vit dans la ruche SYSTEM, donc c'est elle —
    et pas n'importe quelle ruche — qui conditionne le run."""

    name = "registry-chainsaw"
    tool = "registry-chainsaw"
    _DEP = "registry-extract"
    _COUNT_KEY = "system_hives"
    _EMPTY = "aucune ruche SYSTEM dans cette acquisition"


class _EvtxToolStage(_AfterExtractStage):
    """Base des détecteurs qui dépendent du run `evtx-extract`."""

    _DEP = "evtx-extract"
    _COUNT_KEY = "evtx_files"
    _EMPTY = "aucun EVTX dans cette acquisition"


class EvtxChainsawStage(_EvtxToolStage):
    name = "evtx-chainsaw"
    tool = "evtx-chainsaw"


class EvtxHayabusaStage(_EvtxToolStage):
    name = "evtx-hayabusa"
    tool = "evtx-hayabusa"


class EvtxZircoliteStage(_EvtxToolStage):
    name = "evtx-zircolite"
    tool = "evtx-zircolite"


def extraction_pending(inv: Investigation, acq: Acquisition) -> str | None:
    """Raison pour laquelle l'extraction n'est pas encore exploitable, ou None.

    Un détecteur en échec ne bloque pas, et un détecteur jamais lancé faute
    d'EVTX non plus. Seul OGRE est indispensable."""
    if not run_store.has_succeeded(inv, acq.id, "dfir-ogre"):
        return "en attente d'un run OGRE réussi"
    for stage in EXTRACTION_STAGES:
        latest = run_store.latest_run(inv, acq.id, stage.tool)
        if stage.ready(inv, acq) or (latest and latest.status.is_active):
            return "extraction en cours"
    return None


EXTRACTION_STAGES: list[Stage] = [
    OgreStage(),
    EvtxExtractStage(),
    EvtxChainsawStage(),
    EvtxHayabusaStage(),
    EvtxZircoliteStage(),
    MftExtractStage(),
    MftChainsawStage(),
    RegistryExtractStage(),
    RegistryChainsawStage(),
]

STAGES: list[Stage] = EXTRACTION_STAGES

STAGE_BY_TOOL: dict[str, Stage] = {s.tool: s for s in STAGES}
