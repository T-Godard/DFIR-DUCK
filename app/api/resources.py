from __future__ import annotations

import time

from fastapi import APIRouter, Query

from app import resource_history, stats
from app.execution import sysinfo

router = APIRouter()


@router.get("/investigations/{inv_id}/resources/history")
def resources_history(inv_id: str, range: str = Query("now")):  # noqa: A002 - nom du query param
    """Tout ce qu'affiche la carte « Ressources du conteneur » de l'onglet
    Vue d'ensemble.

    `range=now` (mode instantané) ne renvoie que les valeurs courantes : pas de
    lecture de l'historique, l'UI le rafraîchit souvent. Les autres plages y
    ajoutent les séries CPU / mémoire / espace disponible."""
    key = range if range in resource_history.RANGE_SECONDS else "now"
    points = [] if key == "now" else resource_history.query(key)
    snap = sysinfo.app_snapshot()

    # Bornes de la plage demandée, pas de l'historique disponible : c'est l'axe
    # des abscisses des courbes, pour qu'un conteneur récent dessine un trait
    # court à droite plutôt qu'une courbe étirée sur toute la largeur.
    now = int(time.time())
    span = resource_history.RANGE_SECONDS.get(key)

    return {
        "range": key,
        "from_ts": now - span if span else None,
        "to_ts": now,
        "points": points,
        "current": {
            "cpu_percent": snap["cpu_percent"],
            "cpu_scope": snap["cpu_percent_scope"],
            "cpu_limit_cores": snap["cpu_limit_cores"],
            "cpu_count": snap["cpu_count"],
            "mem_used_bytes": snap["cgroup_mem_current_bytes"] or snap["system_mem_used_bytes"],
            "mem_limit_bytes": snap["cgroup_mem_limit_bytes"] or snap["system_mem_total_bytes"],
            "net_recv_bps": snap["net_recv_bps"],
            "net_sent_bps": snap["net_sent_bps"],
            "net_recv_bytes": snap["net_recv_bytes"],
            "net_sent_bytes": snap["net_sent_bytes"],
        },
        "storage": stats.storage(),
    }
