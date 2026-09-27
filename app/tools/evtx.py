"""evtx-extract — extraction sélective des EVTX d'une acquisition vers
`data/output/EVTX/<acq_id>/`. Run dont dépendent Chainsaw / Hayabusa / Zircolite.

Exécute `python -m app.artifact_extract --kind evtx` (voir ce module pour la logique 7z).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from app.config import RUNS_DIR, UPLOADS_DIR, settings
from app.tools._util import tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)

_APP_ROOT = str(Path(__file__).resolve().parents[2])  # .../app/tools/evtx.py -> repo root
_SEVENZIP = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


class EvtxExtractTool:
    name = "evtx-extract"
    label = "EVTX · extraction"
    kind = EXTRACTION
    output_key = "EVTX"

    def available(self) -> bool:
        if not _SEVENZIP:
            logger.warning("evtx-extract indisponible : binaire 7z introuvable")
            return False
        return True

    def version(self) -> str | None:
        return tool_version([_SEVENZIP or "7z"], timeout=10) if _SEVENZIP else None

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        uploads_dir = UPLOADS_DIR / acq.storage_name
        dest = settings.evtx_dir(acq.storage_name)
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
            "evtx",
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
            return {"evtx_files": sum(1 for p in dest.glob("*.evtx") if p.is_file())}

        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=dest,
            env=env,
            prepare=_prepare,
            collect_result=_collect,
        )
