"""mft-chainsaw — Chainsaw sur la MFT, chasse avec les règles natives Chainsaw.

    chainsaw hunt <mft-dir> -r <rules/mft> --csv --output <dir>

Pas de règles Sigma ici : celles de SigmaHQ décrivent des journaux
d'événements (EventID, Channel...), et les mappings livrés par Chainsaw ne
couvrent que ces journaux. Ce sont les règles **natives** livrées avec Chainsaw
(`rules/mft/`, `kind: mft`) qui savent lire une MFT : chemins d'outils
d'attaque, exécutions depuis des dossiers suspects, fichiers supprimés.

Sortie : un ou plusieurs CSV dans `data/output/mft-chainsaw/<acq_id>/`.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from app.config import RUNS_DIR, settings
from app.tools._util import count_csv_rows, tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)


def _make_prepare(mft: Path, output_dir: Path, run_dir: Path) -> Callable[[], None]:
    def _prepare() -> None:
        if not mft.is_dir() or not any(mft.glob("*.mft")):
            raise FileNotFoundError(
                f"aucune MFT à analyser dans {mft} — le run mft-extract n'a rien produit"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

    return _prepare


class MftChainsawTool:
    name = "mft-chainsaw"
    label = "MFT · Chainsaw"
    kind = EXTRACTION
    output_key = "mft-chainsaw"

    def available(self) -> bool:
        ok = settings.chainsaw_bin.exists() and settings.chainsaw_mft_rules_dir.is_dir()
        if not ok:
            logger.warning(
                "mft-chainsaw indisponible : bin=%s (%s) règles MFT=%s (%s)",
                settings.chainsaw_bin, settings.chainsaw_bin.exists(),
                settings.chainsaw_mft_rules_dir, settings.chainsaw_mft_rules_dir.is_dir(),
            )
        return ok

    def version(self) -> str | None:
        return tool_version([str(settings.chainsaw_bin), "--version"])

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        mft = settings.mft_dir(acq.storage_name)
        out = settings.tool_output_dir(self.output_key, acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id

        argv = [
            str(settings.chainsaw_bin), "hunt", str(mft),
            "-r", str(settings.chainsaw_mft_rules_dir),
            "--csv", "--output", str(out),
        ]
        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=out,
            env={**os.environ, "NO_COLOR": "1"},
            prepare=_make_prepare(mft, out, run_dir),
            collect_result=lambda: {"detections": count_csv_rows(out)},
        )
