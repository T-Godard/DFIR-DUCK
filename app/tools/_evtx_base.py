"""Base commune aux détecteurs qui consomment les EVTX extraits
(`data/output/EVTX/<acq_id>/`) : Chainsaw, Hayabusa, Zircolite."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from app.config import settings


def evtx_dir(acquisition_id: str) -> Path:
    return settings.evtx_dir(acquisition_id)


def make_prepare(evtx: Path, output_dir: Path, run_dir: Path) -> Callable[[], None]:
    """`prepare` type : vérifie qu'il y a des EVTX, (re)crée un dossier de sortie propre."""

    def _prepare() -> None:
        if not evtx.is_dir() or not any(evtx.iterdir()):
            raise FileNotFoundError(
                f"aucun EVTX à analyser dans {evtx} — le run evtx-extract n'a rien produit"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

    return _prepare
