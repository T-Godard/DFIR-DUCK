"""Persistance de l'investigation unique.

Ce fork ne traite qu'un seul cas par instance : pas de création manuelle, pas de
liste. L'objet est chargé une fois au démarrage, gardé en mémoire (`_cache`), et
réécrit sur disque de façon atomique après chaque mutation.

Toute écriture passe par `mutate()` — un context manager asynchrone qui
sérialise les modifications derrière un `asyncio.Lock` : l'orchestrateur, les
tâches de run et les routes HTTP tournent dans la même boucle asyncio, mais
`mutate()` garantit qu'aucun `load -> modifie -> sauve` ne s'entrelace avec un
autre.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from app.config import INVESTIGATION_FILE, settings
from app.models import Investigation

logger = logging.getLogger(__name__)

SINGLETON_ID = "main"

_lock = asyncio.Lock()
_cache: Investigation | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_from_disk() -> Investigation | None:
    if not INVESTIGATION_FILE.exists():
        return None
    return Investigation.model_validate_json(INVESTIGATION_FILE.read_text(encoding="utf-8"))


def _save(inv: Investigation) -> None:
    # Écriture atomique : un crash en plein write ne laisse jamais un
    # investigation.json tronqué.
    tmp = INVESTIGATION_FILE.with_suffix(".json.tmp")
    tmp.write_text(inv.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, INVESTIGATION_FILE)


def ensure_investigation() -> Investigation:
    """À appeler une fois, synchroniquement, au démarrage (avant l'orchestrateur
    et avant de servir la moindre requête)."""
    global _cache

    settings.ensure_directories()

    inv = _load_from_disk()
    if inv is None:
        inv = Investigation(
            id=SINGLETON_ID,
            name="Pipeline DFIR",
            created_at=_now(),
        )
        _save(inv)
        logger.info("investigation: créée (première exécution sur ce volume)")
    else:
        logger.info(
            "investigation: chargée (%d acquisition(s), %d run(s))",
            len(inv.acquisitions),
            len(inv.runs),
        )

    _cache = inv
    return _cache


def get(_inv_id: str | None = None) -> Investigation:
    """Snapshot en lecture seule pour les routes GET et le rendu des templates.

    `_inv_id` est accepté et ignoré (compat des routes `/investigations/{id}/...`).
    Pas de lock : la boucle asyncio est mono-thread et le rendu Jinja est
    synchrone, donc un GET voit toujours un état cohérent.
    """
    if _cache is None:
        raise RuntimeError(
            "investigation non initialisée — ensure_investigation() aurait dû "
            "être appelée au démarrage (app/main.py)"
        )
    return _cache


@asynccontextmanager
async def mutate() -> AsyncIterator[Investigation]:
    """Section critique de modification : `async with mutate() as inv: ...`.

    L'objet cédé est l'instance en cache, modifiable en place. À la sortie du
    bloc (sans exception), l'état est persisté."""
    if _cache is None:
        raise RuntimeError("investigation non initialisée")
    async with _lock:
        yield _cache
        _save(_cache)
