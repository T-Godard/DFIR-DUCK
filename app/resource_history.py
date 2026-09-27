"""Historique CPU / mémoire / espace disque du conteneur, échantillonné en
tâche de fond indépendamment des runs — alimente les graphiques de l'onglet
Vue d'ensemble.

Stockage : `data/logs/resources.jsonl` (une ligne JSON par échantillon), purgé
au-delà de `RESOURCE_HISTORY_DAYS`, lui-même plafonné à `MAX_RETENTION_DAYS`.
Pas de base de données : le volume reste modeste (un échantillon par minute
-> ~10 000 lignes pour 7 jours) et `query()` sous-échantillonne à l'affichage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator

from app import stats
from app.config import LOGS_DIR, settings
from app.execution import sysinfo

logger = logging.getLogger(__name__)

HISTORY_FILE = LOGS_DIR / "resources.jsonl"

# Plages d'historique proposées dans l'UI (clé -> durée en secondes). L'UI
# propose en plus un mode « instantané » qui ne lit pas l'historique du tout.
RANGE_SECONDS: dict[str, int] = {
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7j": 7 * 24 * 3600,
}

# Rien n'est conservé au-delà : la plage la plus large de l'UI est 7 jours,
# garder davantage ne ferait que gonfler le fichier sans rien afficher.
MAX_RETENTION_DAYS = 7

# Compaction périodique (pas à chaque échantillon, pour limiter l'I/O).
_PRUNE_EVERY_N_SAMPLES = 60


def record_sample() -> None:
    """Ajoute un échantillon courant à l'historique (append, atomique côté OS
    pour une écriture de cette taille)."""
    snap = sysinfo.app_snapshot()
    mem_used = snap.get("cgroup_mem_current_bytes") or snap.get("system_mem_used_bytes")
    mem_limit = snap.get("cgroup_mem_limit_bytes") or snap.get("system_mem_total_bytes")
    disk = stats.storage()
    line = {
        "ts": round(time.time()),
        "cpu_percent": round(snap.get("cpu_percent") or 0.0, 1),
        "mem_used_bytes": mem_used,
        "mem_limit_bytes": mem_limit,
        # Espace disque : `shutil.disk_usage` est un statvfs, pas un parcours —
        # l'échantillonner à chaque tick ne coûte rien.
        "disk_free_bytes": disk.get("free"),
        "disk_total_bytes": disk.get("total"),
        # Débit réseau, déjà dérivé côté sysinfo : on stocke un taux et non un
        # compteur cumulé, sinon le sous-échantillonnage de `query()` (moyenne
        # par paquet) n'aurait aucun sens sur une valeur monotone croissante.
        "net_recv_bps": snap.get("net_recv_bps"),
        "net_sent_bps": snap.get("net_sent_bps"),
    }
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _iter_samples() -> Iterator[dict]:
    if not HISTORY_FILE.exists():
        return
    with HISTORY_FILE.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def prune(retention_days: int | None = None) -> int:
    """Réécrit le fichier sans les échantillons plus vieux que la rétention.
    Renvoie le nombre d'échantillons conservés."""
    if not HISTORY_FILE.exists():
        return 0
    if retention_days is None:
        retention_days = settings.resource_history_days
    retention_days = min(retention_days, MAX_RETENTION_DAYS)
    cutoff = time.time() - retention_days * 86400
    kept = [s for s in _iter_samples() if s.get("ts", 0) >= cutoff]
    tmp = HISTORY_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for s in kept:
            f.write(json.dumps(s) + "\n")
    tmp.replace(HISTORY_FILE)
    logger.info("resource_history: purge effectuée, %d échantillon(s) conservé(s)", len(kept))
    return len(kept)


def query(range_key: str, max_points: int = 240) -> list[dict]:
    """Échantillons de la plage `range_key` (voir `RANGE_SECONDS`), triés par
    horodatage croissant, sous-échantillonnés (moyenne par bucket) à au plus
    `max_points` pour rester léger côté client."""
    span = RANGE_SECONDS.get(range_key, RANGE_SECONDS["6h"])
    cutoff = time.time() - span
    points = sorted((s for s in _iter_samples() if s.get("ts", 0) >= cutoff), key=lambda s: s["ts"])
    if len(points) <= max_points:
        return points

    bucket_size = len(points) / max_points
    out: list[dict] = []
    i = 0.0
    while int(i) < len(points):
        chunk = points[int(i) : int(i + bucket_size)] or [points[int(i)]]
        cpu_vals = [c.get("cpu_percent") or 0 for c in chunk]
        mem_vals = [c.get("mem_used_bytes") or 0 for c in chunk]
        recv_vals = [c.get("net_recv_bps") or 0 for c in chunk]
        sent_vals = [c.get("net_sent_bps") or 0 for c in chunk]
        last = chunk[-1]
        out.append(
            {
                "ts": chunk[len(chunk) // 2]["ts"],
                "cpu_percent": round(sum(cpu_vals) / len(chunk), 1),
                "mem_used_bytes": round(sum(mem_vals) / len(chunk)),
                "mem_limit_bytes": last.get("mem_limit_bytes"),
                "net_recv_bps": round(sum(recv_vals) / len(chunk)),
                "net_sent_bps": round(sum(sent_vals) / len(chunk)),
                # L'espace disque varie lentement : la dernière valeur du
                # bucket suffit, pas besoin de moyenner.
                "disk_free_bytes": last.get("disk_free_bytes"),
                "disk_total_bytes": last.get("disk_total_bytes"),
            }
        )
        i += bucket_size
    return out


async def run_forever() -> None:
    """Boucle de fond indépendante de l'orchestrateur : échantillonne les
    ressources toutes les `RESOURCE_SAMPLE_INTERVAL_SECONDS`, purge de temps
    en temps."""
    logger.info(
        "resource_history: démarré (intervalle=%ss, rétention=%sj)",
        settings.resource_sample_interval_seconds,
        min(settings.resource_history_days, MAX_RETENTION_DAYS),
    )
    tick = 0
    while True:
        try:
            record_sample()
            tick += 1
            if tick % _PRUNE_EVERY_N_SAMPLES == 0:
                prune()
        except asyncio.CancelledError:
            logger.info("resource_history: arrêt")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("resource_history: erreur d'échantillonnage")
        await asyncio.sleep(settings.resource_sample_interval_seconds)
