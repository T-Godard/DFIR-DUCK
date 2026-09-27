"""Contrat commun à tous les outils d'analyse exécutés en sous-processus.

Ajouter un outil (Chainsaw, Zircolite, ...) = écrire une classe qui implémente
`Tool` dans `app/tools/<x>.py`, l'enregistrer dans `app/tools/registry.py`, et
ajouter une `Stage` correspondante dans `app/pipeline/stages.py`.

Un `Tool` ne lance rien lui-même : il décrit *comment* lancer l'outil
(`plan()` -> `ToolPlan`). C'est `app/execution/` qui exécute réellement le
sous-processus, gère le logging, le timeout et l'annulation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.config import Settings
from app.models import Acquisition, Run


@dataclass
class ToolContext:
    acquisition: Acquisition
    run: Run
    settings: Settings


@dataclass
class ToolPlan:
    """Description exécutable d'un run.

    `prepare` est appelé juste avant le lancement (création des dossiers de
    sortie, rendu d'un fichier de configuration, nettoyage d'une sortie
    précédente, ...).

    `collect_result` est appelé après un run réussi : ce qu'il renvoie est
    stocké dans `Run.result` (ex. `{"detections": 42}`). Doit être robuste
    (une exception y est loggée sans faire échouer le run).
    """

    argv: list[str]
    cwd: Path
    output_dir: Path
    env: dict[str, str] = field(default_factory=dict)
    prepare: Callable[[], None] = lambda: None
    collect_result: Callable[[], dict[str, Any]] | None = None


EXTRACTION = "extraction"


@runtime_checkable
class Tool(Protocol):
    name: str
    label: str
    # EXTRACTION : détermine le pool de concurrence et le timeout.
    kind: str
    # Sous-dossier de sortie : data/output/<output_key>/<acquisition_id>/
    output_key: str

    def available(self) -> bool:
        """L'outil est-il installé et prêt à tourner ?"""
        ...

    def version(self) -> str | None:
        """Version de l'outil, si récupérable (best-effort, pour les logs)."""
        ...

    def plan(self, ctx: ToolContext) -> ToolPlan:
        """Construit la commande et les préparatifs pour ce run."""
        ...
