"""evtx-chainsaw — Chainsaw (WithSecureLabs), chasse Sigma sur les EVTX.

    chainsaw hunt <evtx-dir> -s <sigma> --mapping <mapping.yml> -r <rules> --csv --output <dir>

Règles : clone SigmaHQ (`-s`) + règles natives Chainsaw (`-r`). Sortie : plusieurs
CSV dans `data/output/evtx-chainsaw/<acq_id>/`.
"""

from __future__ import annotations

import logging
import os

from app.config import RUNS_DIR, settings
from app.tools._evtx_base import evtx_dir, make_prepare
from app.tools._util import count_csv_rows, tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)


class EvtxChainsawTool:
    name = "evtx-chainsaw"
    label = "EVTX · Chainsaw"
    kind = EXTRACTION
    output_key = "evtx-chainsaw"

    def available(self) -> bool:
        ok = settings.chainsaw_bin.exists() and settings.sigma_rules_dir.is_dir()
        if not ok:
            logger.warning(
                "evtx-chainsaw indisponible : bin=%s (%s) sigma=%s (%s)",
                settings.chainsaw_bin, settings.chainsaw_bin.exists(),
                settings.sigma_rules_dir, settings.sigma_rules_dir.is_dir(),
            )
        return ok

    def version(self) -> str | None:
        return tool_version([str(settings.chainsaw_bin), "--version"])

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        evtx = evtx_dir(acq.storage_name)
        out = settings.tool_output_dir(self.output_key, acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id

        argv = [
            str(settings.chainsaw_bin), "hunt", str(evtx),
            "-s", str(settings.sigma_rules_dir),
            "--mapping", str(settings.chainsaw_mapping),
            "-r", str(settings.chainsaw_rules_dir),
            "--csv", "--output", str(out),
        ]
        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=out,
            env={**os.environ, "NO_COLOR": "1"},
            prepare=make_prepare(evtx, out, run_dir),
            collect_result=lambda: {"detections": count_csv_rows(out)},
        )
