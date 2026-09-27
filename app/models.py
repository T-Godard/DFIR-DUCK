"""Modèles de persistance (Pydantic) — sérialisés dans un unique
`data/investigation.json` (voir `app/stores/investigation.py`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    PENDING = "pending"      # créé, en file d'attente
    RUNNING = "running"      # sous-processus lancé
    SUCCEEDED = "succeeded"  # code de sortie 0
    FAILED = "failed"        # code de sortie != 0, timeout, ou exception
    CANCELLED = "cancelled"  # interrompu (shutdown du service)

    @property
    def is_active(self) -> bool:
        return self in (RunStatus.PENDING, RunStatus.RUNNING)

    @property
    def is_terminal(self) -> bool:
        return not self.is_active


class Run(BaseModel):
    """Une exécution d'un outil d'analyse sur une acquisition.

    Remplace l'ancien couple `Acquisition.ogre_status` / `ogre_container` : on
    garde désormais l'historique complet (rejeu après erreur, futurs outils
    Chainsaw / Zircolite / agents LLM). Les logs de l'outil vivent dans un
    fichier à part, `log_path` (`data/runs/<id>/run.log`).
    """

    id: str
    acquisition_id: str
    tool: str                       # clé du registre d'outils, ex. "dfir-ogre"
    status: RunStatus = RunStatus.PENDING
    attempt: int = 1                # numéro d'essai pour ce couple (acquisition, outil)
    forced: bool = False            # déclenché manuellement ("Forcer") vs automatiquement

    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    exit_code: int | None = None
    duration_seconds: float | None = None
    command: list[str] = Field(default_factory=list)  # argv réel, pour l'audit
    workdir: str | None = None
    log_path: str | None = None
    output_dir: str | None = None
    error: str | None = None        # message court en cas d'échec

    # Sortie chiffrée de l'outil, remplie en fin de run par ToolPlan.collect_result
    # (ex. {"evtx_files": 128}, {"detections": 42}). Affichée dans l'UI + /metrics.
    result: dict[str, Any] = Field(default_factory=dict)

    # Suivi de performance (échantillonné pendant l'exécution ; détail complet
    # dans data/runs/<id>/metrics.jsonl)
    last_cpu_percent: float | None = None   # % CPU instantané (peut dépasser 100 = multi-cœur)
    last_rss_bytes: int | None = None       # RAM résidente courante (process + enfants)
    peak_rss_bytes: int | None = None       # pic de RAM résidente observé
    metrics_samples: int = 0
    # Limites en vigueur pour ce run, figées à sa création (pour affichage)
    mem_limit_bytes: int | None = None      # plafond RAM (RLIMIT_AS ou conteneur)
    cpu_cores_limit: float | None = None    # nb de cœurs autorisés (None = tous)


class Acquisition(BaseModel):
    """Une acquisition = un Outcome.json DFIR-ORC + la suite d'archives .7z
    qu'il référence (typiquement une machine). L'investigation unique peut en
    regrouper plusieurs.
    """

    id: str
    name: str
    created_at: str

    # Clé interne stable de regroupement des uploads (nom du fichier Outcome.json).
    # `name` est ce qui s'affiche : repris de `dfir-orc.outcome.computer_name`
    # dès que l'Outcome.json est uploadé et parsable, provisoire jusque-là.
    upload_group_key: str = ""

    uploaded_filenames: list[str] = Field(default_factory=list)
    outcome_filename: str | None = None  # détecté auto (*_Outcome.json)

    # Nom du dossier de l'acquisition sur disque : `uploads/<dir_name>/` et
    # `output/<outil>/<dir_name>/`. Tiré du nom de machine pour qu'un parcours
    # manuel des sorties soit lisible — `output/evtx-chainsaw/WIN11/` plutôt
    # que `output/evtx-chainsaw/d6a33dbc56/`.
    #
    # `id` reste la clé interne : URL, enregistrements de runs, champ
    # enregistrements de runs. Renommer un dossier ne casse donc rien de ce
    # qui pointe vers l'acquisition.
    #
    # Vide pour les acquisitions créées avant l'introduction de ce champ :
    # elles restent rangées sous leur identifiant (voir `storage_name`).
    dir_name: str = ""

    @property
    def storage_name(self) -> str:
        """Nom du dossier à utiliser sur disque, quelle que soit l'ancienneté
        de l'acquisition."""
        return self.dir_name or self.id


class Investigation(BaseModel):
    """Instance unique pour toute l'application (voir
    `app/stores/investigation.py`)."""

    id: str
    name: str
    created_at: str

    acquisitions: dict[str, Acquisition] = Field(default_factory=dict)
    runs: dict[str, Run] = Field(default_factory=dict)
