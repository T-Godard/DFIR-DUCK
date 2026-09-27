"""evtx-zircolite — Zircolite (wagga40), détection Sigma sur les EVTX, règles SigmaHQ brutes.

    <venv-python> zircolite.py --evtx <evtx> --ruleset <sigma> --csv --outfile <out>/detections.csv

Zircolite convertit les règles Sigma YAML à la volée (pySigma) et crée une base
sqlite temporaire dans son cwd (= dossier du run). Sortie :
`data/output/evtx-zircolite/<acq_id>/detections.csv`.
"""

from __future__ import annotations

import logging
import os

from app.config import RUNS_DIR, settings
from app.tools._evtx_base import evtx_dir, make_prepare
from app.tools._util import count_csv_rows, tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)


class EvtxZircoliteTool:
    name = "evtx-zircolite"
    label = "EVTX · Zircolite"
    kind = EXTRACTION
    output_key = "evtx-zircolite"

    def available(self) -> bool:
        ok = (
            settings.zircolite_python.exists()
            and settings.zircolite_script.exists()
            and settings.sigma_rules_dir.is_dir()
        )
        if not ok:
            logger.warning(
                "evtx-zircolite indisponible : python=%s (%s) script=%s (%s) sigma=%s (%s)",
                settings.zircolite_python, settings.zircolite_python.exists(),
                settings.zircolite_script, settings.zircolite_script.exists(),
                settings.sigma_rules_dir, settings.sigma_rules_dir.is_dir(),
            )
        return ok

    def version(self) -> str | None:
        return tool_version(
            [str(settings.zircolite_python), str(settings.zircolite_script), "--version"]
        )

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        evtx = evtx_dir(acq.storage_name)
        out = settings.tool_output_dir(self.output_key, acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id
        csv_path = out / "detections.csv"

        argv = [
            str(settings.zircolite_python), str(settings.zircolite_script),
            "--evtx", str(evtx),
            "--ruleset", str(settings.sigma_rules_dir),
            "--csv", "--outfile", str(csv_path),
        ]
        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=out,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"},
            prepare=make_prepare(evtx, out, run_dir),
            collect_result=lambda: {"detections": count_csv_rows(csv_path)},
        )
