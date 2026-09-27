"""registry-extract — extraction des ruches du registre d'une acquisition vers
`data/output/Registry/<acq_id>/`. Run dont dépend `registry-chainsaw`.

(Le module s'appelle `registry_extract` et non `registry` : ce dernier nom est
pris par le registre des outils, `app/tools/registry.py`.)

DFIR-ORC collecte les ruches, et DFIR-OGRE en tire des vues dérivées (services,
tâches planifiées, amcache) — mais pas la ruche brute. Or le shimcache
(AppCompatCache) vit dans `SYSTEM` et n'apparaît dans aucune de ces vues. On
tire donc les ruches des archives comme les EVTX et la MFT, via
`python -m app.artifact_extract --kind hive`.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from app.artifact_extract import hive_kind, is_hive_file
from app.config import RUNS_DIR, UPLOADS_DIR, settings
from app.tools._util import tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)

_APP_ROOT = str(Path(__file__).resolve().parents[2])
_SEVENZIP = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


class RegistryExtractTool:
    name = "registry-extract"
    label = "Registre · extraction"
    kind = EXTRACTION
    output_key = "Registry"

    def available(self) -> bool:
        if not _SEVENZIP:
            logger.warning("registry-extract indisponible : binaire 7z introuvable")
            return False
        return True

    def version(self) -> str | None:
        return tool_version([_SEVENZIP or "7z"], timeout=10) if _SEVENZIP else None

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        uploads_dir = UPLOADS_DIR / acq.storage_name
        dest = settings.registry_dir(acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id
        scratch = run_dir / "tmp"

        def _prepare() -> None:
            if not uploads_dir.is_dir():
                raise FileNotFoundError(f"dossier d'acquisition introuvable : {uploads_dir}")
            run_dir.mkdir(parents=True, exist_ok=True)
            # `dest` et `scratch` sont (re)créés par le CLI lui-même.

        argv = [
            sys.executable,
            "-m",
            "app.artifact_extract",
            str(uploads_dir),
            str(dest),
            "--kind",
            "hive",
            "--password",
            settings.ogre_inner_archive_password,
            "--scratch",
            str(scratch),
        ]
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": _APP_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }

        def _collect() -> dict:
            # Signature vérifiée : le nom d'un fichier collecté par ORC porte
            # le chemin d'origine, et une collecte contient assez de fichiers
            # pour qu'un nom trompeur finisse par passer. Compter des ruches
            # qui n'en sont pas ferait démarrer `registry-chainsaw` pour rien.
            familles = [
                hive_kind(p.name) for p in dest.glob("*.hve")
                if p.is_file() and is_hive_file(p)
            ]
            return {
                "hive_files": len(familles),
                "system_hives": sum(1 for f in familles if f == "SYSTEM"),
                "amcache": any(f == "AMCACHE" for f in familles),
            }

        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=dest,
            env=env,
            prepare=_prepare,
            collect_result=_collect,
        )
