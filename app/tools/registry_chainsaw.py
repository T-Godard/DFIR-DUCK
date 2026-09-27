"""registry-chainsaw — Chainsaw sur le registre : analyse du shimcache.

    python -m app.registry_shimcache <ruches> <sortie> --chainsaw <bin> [--patterns <f>]

Le module appelé boucle sur les ruches SYSTEM de l'acquisition (plusieurs
volumes, copies VSS) et lance `chainsaw analyse shimcache` sur chacune — voir
`app/registry_shimcache.py` pour le détail des options.

Ni Sigma ni règles natives ici : le shimcache n'est pas un journal. Chainsaw
en sort une chronologie d'exécution, datée grâce aux motifs de chemins
(`--regexfile`) et, si l'Amcache a été collecté, aux paires d'horodatages
rapprochés (`--tspair`).

Sortie : un CSV par ruche SYSTEM dans `data/output/registry-chainsaw/<acq_id>/`.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from app.artifact_extract import system_hives
from app.config import RUNS_DIR, settings
from app.tools._util import count_csv_rows, tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)

_APP_ROOT = str(Path(__file__).resolve().parents[2])


def _make_prepare(hives: Path, output_dir: Path, run_dir: Path) -> Callable[[], None]:
    def _prepare() -> None:
        if not system_hives(hives):
            raise FileNotFoundError(
                f"aucune ruche SYSTEM exploitable dans {hives} — le run "
                "registry-extract n'en a pas produit (nom ET signature `regf` vérifiés)"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

    return _prepare


class RegistryChainsawTool:
    name = "registry-chainsaw"
    label = "Registre · Chainsaw"
    kind = EXTRACTION
    output_key = "registry-chainsaw"

    def available(self) -> bool:
        ok = settings.chainsaw_bin.exists()
        if not ok:
            logger.warning(
                "registry-chainsaw indisponible : bin=%s (%s)",
                settings.chainsaw_bin, settings.chainsaw_bin.exists(),
            )
        return ok

    def version(self) -> str | None:
        return tool_version([str(settings.chainsaw_bin), "--version"])

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        hives = settings.registry_dir(acq.storage_name)
        out = settings.tool_output_dir(self.output_key, acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id

        argv = [
            sys.executable, "-m", "app.registry_shimcache",
            str(hives), str(out),
            "--chainsaw", str(settings.chainsaw_bin),
        ]
        # Les motifs datent les entrées (voir app/registry_shimcache.py). Le
        # fichier est livré avec l'application, pas seulement espéré dans
        # l'archive de release de Chainsaw, qui ne l'embarque pas.
        if settings.shimcache_patterns.is_file():
            argv += ["--patterns", str(settings.shimcache_patterns)]
        else:
            logger.warning(
                "registry-chainsaw : motifs shimcache introuvables (%s) — "
                "la chronologie sortira avec très peu d'entrées datées",
                settings.shimcache_patterns,
            )

        env = {
            **os.environ,
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": _APP_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=out,
            env=env,
            prepare=_make_prepare(hives, out, run_dir),
            collect_result=lambda: {"shimcache_entries": count_csv_rows(out)},
        )
