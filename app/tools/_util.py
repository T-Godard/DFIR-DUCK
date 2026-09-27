"""Petits helpers partagés par les définitions d'outils."""

from __future__ import annotations

import subprocess
from pathlib import Path


def tool_version(argv: list[str], *, timeout: int = 15) -> str | None:
    """Exécute `argv` (ex. `[bin, "--version"]`) et renvoie la 1re ligne utile."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines()[0].strip() if text else None


def count_csv_rows(path: Path) -> int:
    """Nombre de lignes de données (hors en-tête) d'un CSV, ou de tous les CSV
    d'un dossier."""
    files = sorted(path.glob("*.csv")) if path.is_dir() else ([path] if path.is_file() else [])
    total = 0
    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                n = sum(1 for _ in fh)
            total += max(0, n - 1)
        except OSError:
            continue
    return total
