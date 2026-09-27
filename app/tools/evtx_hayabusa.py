"""evtx-hayabusa — Hayabusa (Yamato-Security), timeline de détections Sigma sur les EVTX.

    hayabusa dfir-timeline -d <evtx-dir> -o <out>/timeline.csv -r <rules> -c <config>
             -w -q -K -C -U --rfc-3339 -N -p standard

`dfir-timeline` a remplacé `csv-timeline` (renommé par Yamato-Security ;
toujours le même jeu de flags) — voir `hayabusa --help` / `hayabusa help`.

Règles : celles livrées avec le binaire (hayabusa-rules, dérivées de Sigma).
Flags non-interactifs : -w (pas d'assistant), -q (pas de bannière), -K (pas de
couleur), -C (écrase la sortie), -N (pas de résumé). Sortie :
`data/output/evtx-hayabusa/<acq_id>/timeline.csv`.
"""

from __future__ import annotations

import logging
import os

from app.config import RUNS_DIR, settings
from app.tools._evtx_base import evtx_dir, make_prepare
from app.tools._util import count_csv_rows, tool_version
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)


class EvtxHayabusaTool:
    name = "evtx-hayabusa"
    label = "EVTX · Hayabusa"
    kind = EXTRACTION
    output_key = "evtx-hayabusa"

    def available(self) -> bool:
        ok = settings.hayabusa_bin.exists() and settings.hayabusa_rules_dir.is_dir()
        if not ok:
            logger.warning(
                "evtx-hayabusa indisponible : bin=%s (%s) rules=%s (%s)",
                settings.hayabusa_bin, settings.hayabusa_bin.exists(),
                settings.hayabusa_rules_dir, settings.hayabusa_rules_dir.is_dir(),
            )
        return ok

    def version(self) -> str | None:
        return tool_version([str(settings.hayabusa_bin), "--version"])

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        evtx = evtx_dir(acq.storage_name)
        out = settings.tool_output_dir(self.output_key, acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id
        csv_path = out / "timeline.csv"

        argv = [
            str(settings.hayabusa_bin), "dfir-timeline",
            "-d", str(evtx),
            "-o", str(csv_path),
            "-r", str(settings.hayabusa_rules_dir),
            "-c", str(settings.hayabusa_config_dir),
            "-w", "-q", "-K", "-C", "-U", "--rfc-3339", "-N",
            "-p", "standard",
        ]
        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=out,
            env={**os.environ, "NO_COLOR": "1"},
            prepare=make_prepare(evtx, out, run_dir),
            collect_result=lambda: {"detections": count_csv_rows(csv_path)},
        )
