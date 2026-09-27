"""DFIR-OGRE — extraction/normalisation des artefacts Windows d'une acquisition
DFIR-ORC (https://github.com/ANSSI-FR/dfir-ogre).

Installé dans l'image dans un venv dédié (`/opt/tools/dfir-ogre/venv`), invoqué
en sous-processus :

    dfir-ogre orc --archive <Outcome.json> --configuration <ogre.yaml> --case <id>

On part de la configuration livrée avec le dépôt dfir-ogre
(`src/configuration/ogre.yaml`, la grosse section `mapping`) et on en dérive, à
chaque run, un `ogre.yaml` dédié avec les bons dossiers d'entrée/sortie.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

import yaml

from app.config import RUNS_DIR, UPLOADS_DIR, settings
from app.tools.base import EXTRACTION, ToolContext, ToolPlan

logger = logging.getLogger(__name__)


class DfirOgreTool:
    name = "dfir-ogre"
    label = "OGRE"
    kind = EXTRACTION
    output_key = "ogre"

    def available(self) -> bool:
        ok = settings.ogre_bin.exists() and settings.ogre_base_config.exists()
        if not ok:
            logger.warning(
                "dfir-ogre indisponible : bin=%s (exists=%s) config=%s (exists=%s)",
                settings.ogre_bin,
                settings.ogre_bin.exists(),
                settings.ogre_base_config,
                settings.ogre_base_config.exists(),
            )
        return ok

    def version(self) -> str | None:
        try:
            out = subprocess.run(
                [str(settings.ogre_bin), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return (out.stdout or out.stderr).strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    def plan(self, ctx: ToolContext) -> ToolPlan:
        acq = ctx.acquisition
        if not acq.outcome_filename:
            raise ValueError(f"acquisition {acq.id} sans Outcome.json — run impossible")

        input_dir = UPLOADS_DIR / acq.storage_name
        outcome_path = input_dir / acq.outcome_filename
        output_dir = settings.tool_output_dir(self.output_key, acq.storage_name)
        run_dir = RUNS_DIR / ctx.run.id
        rendered_config = run_dir / "ogre.yaml"
        temp_dir = run_dir / "tmp"
        report_dir = run_dir / "report"

        def _prepare() -> None:
            if not outcome_path.exists():
                raise FileNotFoundError(f"Outcome.json introuvable : {outcome_path}")

            run_dir.mkdir(parents=True, exist_ok=True)
            temp_dir.mkdir(exist_ok=True)
            report_dir.mkdir(exist_ok=True)

            # La sortie reflète toujours le dernier run : on repart propre.
            if output_dir.exists():
                logger.info("dfir-ogre: purge de la sortie précédente %s", output_dir)
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True)

            config = yaml.safe_load(settings.ogre_base_config.read_text(encoding="utf-8"))
            config["output_folder"] = str(output_dir)
            config["report_folder"] = str(report_dir)
            config["temp_folder"] = str(temp_dir)
            config["plugin_folder"] = str(settings.ogre_plugins_dir)
            config["inner_archive_password"] = settings.ogre_inner_archive_password
            rendered_config.write_text(
                yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            logger.info(
                "dfir-ogre: configuration rendue %s (output=%s)",
                rendered_config,
                output_dir,
            )

        argv = [
            str(settings.ogre_bin),
            "orc",
            "--archive",
            str(outcome_path),
            "--configuration",
            str(rendered_config),
            "--case",
            acq.id,
        ]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        def _collect() -> dict:
            """Volumetrie produite par OGRE, mesuree une seule fois en fin de
            run : l arborescence de sortie peut etre enorme, hors de question
            de la reparcourir a chaque rafraichissement de l UI."""
            total = 0
            for entry in output_dir.rglob("*"):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
            return {"output_bytes": total}

        return ToolPlan(
            argv=argv,
            cwd=run_dir,
            output_dir=output_dir,
            env=env,
            prepare=_prepare,
            collect_result=_collect,
        )
