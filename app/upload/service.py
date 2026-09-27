"""Réception et réassemblage des uploads par morceaux.

Le navigateur découpe chaque fichier en morceaux de quelques Mo (voir
`base.html`) envoyés un par un. Ça permet de survivre à un lien instable
(partage de connexion mobile + VPN) : une requête HTTP unique de plusieurs Go
est fragile, alors qu'un morceau en échec se réémet seul. Toutes les écritures
sont atomiques (fichier temporaire + `fsync` + renommage) : un morceau ou un
fichier à moitié écrit ne peut jamais être vu comme complet.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import aiofiles

from app.config import UPLOAD_TEMP_INFIX, settings

logger = logging.getLogger(__name__)

# Granularité des I/O disque côté serveur (sans rapport avec la taille des
# morceaux envoyés par le client).
IO_BUFFER = 1024 * 1024

CHUNKS_ROOT_NAME = "_chunks"


@dataclass
class UploadError:
    filename: str
    message: str


@dataclass
class FinalizeResult:
    status: str  # "pending" (morceaux manquants) | "done" | "error"
    error: str | None = None


def _pending_dir() -> Path:
    return settings.pending_dir


def _chunks_dir(upload_id: str) -> Path:
    return _pending_dir() / CHUNKS_ROOT_NAME / upload_id


def _safe_basename(relpath: str) -> str:
    """N'utilise QUE le nom de fichier final, jamais l'arborescence éventuelle
    (upload par dossier). La classification ne matche que par basename et
    l'anti-collision est déjà assurée par `upload_id`. Protège aussi d'un
    relpath du type '../../etc/passwd'."""
    name = PurePosixPath(relpath.replace("\\", "/")).name
    return name or "fichier"


async def save_chunk(upload_id: str, chunk_index: int, chunk_file) -> None:
    """Écrit un morceau sous `_pending/_chunks/<upload_id>/<index>`."""
    chunks_dir = _chunks_dir(upload_id)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    final_chunk_path = chunks_dir / f"{chunk_index:06d}"
    tmp_path = chunks_dir / f"{chunk_index:06d}{UPLOAD_TEMP_INFIX}{uuid.uuid4().hex[:8]}"

    size = 0
    async with aiofiles.open(tmp_path, "wb") as output:
        while data := await chunk_file.read(IO_BUFFER):
            await output.write(data)
            size += len(data)
        await output.flush()
        os.fsync(output.fileno())

    os.replace(tmp_path, final_chunk_path)
    logger.debug(
        "upload %s: morceau %d reçu (%d octets)", upload_id, chunk_index, size
    )


def chunks_received(upload_id: str) -> int:
    chunks_dir = _chunks_dir(upload_id)
    if not chunks_dir.exists():
        return 0
    return len([p for p in chunks_dir.iterdir() if UPLOAD_TEMP_INFIX not in p.name])


async def try_finalize_upload(
    upload_id: str, relpath: str, total_chunks: int, total_size: int
) -> FinalizeResult:
    """Si tous les morceaux sont là, les réassemble dans l'ordre en un fichier
    complet sous `_pending/<upload_id>/<nom>` — visible par l'orchestrateur
    seulement une fois entièrement écrit et `fsync`."""
    chunks_dir = _chunks_dir(upload_id)
    received = chunks_received(upload_id)
    if received < total_chunks:
        return FinalizeResult(status="pending")

    basename = _safe_basename(relpath)
    dest_dir = _pending_dir() / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_dir / f"{basename}{UPLOAD_TEMP_INFIX}{uuid.uuid4().hex[:8]}"

    logger.info(
        "upload %s: réassemblage de '%s' (%d morceaux, %d octets attendus)",
        upload_id,
        basename,
        total_chunks,
        total_size,
    )

    bytes_written = 0
    try:
        async with aiofiles.open(tmp_path, "wb") as output:
            for i in range(total_chunks):
                chunk_path = chunks_dir / f"{i:06d}"
                async with aiofiles.open(chunk_path, "rb") as src:
                    while data := await src.read(IO_BUFFER):
                        await output.write(data)
                        bytes_written += len(data)
            await output.flush()
            os.fsync(output.fileno())

        if bytes_written != total_size:
            raise OSError(
                f"taille réassemblée ({bytes_written} octets) != taille annoncée "
                f"({total_size} octets) — morceau manquant ou corrompu"
            )

        os.replace(tmp_path, dest_dir / basename)
        logger.info("upload %s: '%s' réassemblé (%d octets)", upload_id, basename, bytes_written)
        return FinalizeResult(status="done")

    except Exception as exc:  # noqa: BLE001 — on veut capturer et reporter toute erreur d'I/O
        logger.exception(
            "upload %s: échec du réassemblage de '%s'", upload_id, basename
        )
        tmp_path.unlink(missing_ok=True)
        return FinalizeResult(status="error", error=str(exc))

    finally:
        shutil.rmtree(chunks_dir, ignore_errors=True)


def cleanup_abandoned_chunks(max_age_seconds: int = 24 * 3600) -> int:
    """Supprime les sessions d'upload par morceaux jamais terminées (onglet
    fermé en plein transfert). Best-effort, appelé depuis le tick de
    l'orchestrateur. Renvoie le nombre de sessions supprimées."""
    root = _pending_dir() / CHUNKS_ROOT_NAME
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            if (now - d.stat().st_mtime) > max_age_seconds:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("upload: %d session(s) de morceaux abandonnée(s) purgée(s)", removed)
    return removed
