"""Suivi des fichiers déjà consommés dans le pool d'arrivée.

Sert au mode `pending_consume: copy`, où l'application **ne supprime jamais**
ce qu'elle trouve dans `pending_dir` : le dossier appartient à quelqu'un
d'autre (partage SMB alimenté par un serveur d'upload, volume déposé par un
autre service). Sans mémoire de ce qui a été pris, chaque tick recréerait les
mêmes acquisitions.

Le registre vit dans `data_dir` — jamais sur le partage, qui peut être en
lecture seule ou disparaître. Une entrée retient la taille et la date de
modification : un fichier réécrit (collecte corrigée, renvoyée sous le même
nom) est reconsommé, un fichier inchangé est ignoré.

Rien n'est purgé automatiquement : un partage temporairement démonté
apparaîtrait vide, et oublier ses entrées ferait tout réimporter au remontage.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

FILE_NAME = "pending_consumed.json"
_cache: dict[str, dict] | None = None


def path() -> Path:
    return settings.data_dir / FILE_NAME


def signature(file: Path) -> dict | None:
    """Taille et date de modification — ce qui distingue deux versions d'un
    même chemin. None si le fichier a disparu entre-temps."""
    try:
        stat = file.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime": int(stat.st_mtime)}


def key(file: Path) -> str:
    """Chemin relatif au pool, stable d'un tick à l'autre."""
    try:
        return file.relative_to(settings.pending_dir).as_posix()
    except ValueError:
        return file.as_posix()


def _entries() -> dict[str, dict]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(path().read_text(encoding="utf-8"))
            _cache = data.get("files", {}) if isinstance(data, dict) else {}
        except FileNotFoundError:
            _cache = {}
        except (OSError, ValueError) as exc:
            logger.warning("pending: registre illisible, repart de zéro (%s)", exc)
            _cache = {}
    return _cache


def is_consumed(file: Path) -> bool:
    entry = _entries().get(key(file))
    if entry is None:
        return False
    current = signature(file)
    return current is not None and entry.get("signature") == current


def mark(file: Path, acquisition_id: str) -> None:
    current = signature(file)
    if current is None:
        return
    _entries()[key(file)] = {
        "signature": current,
        "acquisition": acquisition_id,
        "consumed_at": datetime.now(UTC).isoformat(),
    }


def save() -> None:
    """Écrit le registre s'il a changé. Appelé après chaque passe de
    classification, avant la persistance de l'investigation."""
    if _cache is None:
        return
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": 1, "files": _cache}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def reset_cache() -> None:
    """Oublie le registre chargé (tests, changement de `data_dir`)."""
    global _cache
    _cache = None
