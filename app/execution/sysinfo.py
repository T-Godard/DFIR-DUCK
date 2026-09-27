"""Lecture des ressources système / conteneur — pour donner de la visibilité
sur les limites en vigueur (RAM du conteneur, cœurs CPU) et l'usage courant.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from app import config

logger = logging.getLogger(__name__)

_CGROUP_V2 = Path("/sys/fs/cgroup")


class _Rate:
    """Taux dérivé d'un compteur noyau cumulatif, mesuré sur une fenêtre d'au
    moins `window` secondes.

    Pourquoi pas un simple « delta depuis le dernier appel » : ces indicateurs
    ont plusieurs lecteurs (page, widget Ressources, /healthz, /metrics,
    échantillonneur d'historique). Avec un delta consommé par le premier venu,
    la fenêtre de mesure devient l'écart entre deux appels *quelconques* — vite
    quelques millisecondes dès que l'UI rafraîchit souvent. Sur une fenêtre
    aussi courte, la granularité des compteurs (`cpu.stat` n'est pas mis à jour
    à l'instant où on le lit) produit des valeurs aberrantes : un conteneur
    plafonné à 8 cœurs affiche 1000 %.

    On sépare donc la mesure de l'affichage : l'ancre n'avance qu'une fois la
    fenêtre écoulée, et les lectures intermédiaires reçoivent la dernière
    valeur calculée. Rafraîchir plus souvent affiche donc des valeurs plus
    fraîches sans jamais dégrader la mesure.

    Pas de verrou : au pire deux lecteurs simultanés recalculent le même taux
    sur la même fenêtre.
    """

    def __init__(self, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window
        self._clock = clock
        self._anchor: tuple[float, tuple[int, ...]] | None = None
        self._value: tuple[float, ...] | None = None

    def update(self, counters: tuple[int, ...]) -> tuple[float, ...] | None:
        """Taux courant (une valeur par compteur, par seconde), ou None tant
        qu'aucune mesure n'est disponible (premier appel après le démarrage)."""
        now = self._clock()
        anchor = self._anchor
        if anchor is None:
            self._anchor = (now, counters)
            return None

        previous_ts, previous = anchor
        elapsed = now - previous_ts
        if elapsed < self._window:
            # Fenêtre trop courte : on rend la dernière mesure SANS déplacer
            # l'ancre — sinon la mesure suivante porterait sur un intervalle
            # plus court encore, et c'est exactement ce qu'on veut éviter.
            return self._value
        if any(c < p for c, p in zip(counters, previous, strict=True)):
            # Compteur en recul (interface redémarrée, cgroup recréé) : on se
            # réancre et on garde la valeur précédente pour ce tour.
            self._anchor = (now, counters)
            return self._value

        self._anchor = (now, counters)
        self._value = tuple(
            (c - p) / elapsed for c, p in zip(counters, previous, strict=True)
        )
        return self._value


_cpu_rate = _Rate(config.settings.resource_rate_window_seconds)
_net_rate = _Rate(config.settings.resource_rate_window_seconds)


def read_cgroup_memory() -> tuple[int | None, int | None]:
    """(usage, limite) mémoire du conteneur en octets, via cgroup v2.

    Renvoie (None, None) hors conteneur / cgroup v2 indisponible, et une limite
    None quand elle vaut « max » (pas de limite)."""
    try:
        current = int((_CGROUP_V2 / "memory.current").read_text().strip())
    except (OSError, ValueError):
        current = None
    try:
        raw = (_CGROUP_V2 / "memory.max").read_text().strip()
        limit = None if raw == "max" else int(raw)
    except (OSError, ValueError):
        limit = None
    return current, limit


def read_cgroup_cpu_quota() -> float | None:
    """Nombre de cœurs alloués au conteneur via `cpu.max` (cgroup v2), ou None
    si illimité / indisponible."""
    try:
        quota_s, period_s = (_CGROUP_V2 / "cpu.max").read_text().split()
        if quota_s == "max":
            return None
        return int(quota_s) / int(period_s)
    except (OSError, ValueError):
        return None


def host_cpu_count() -> int:
    return os.cpu_count() or 1


def _read_cgroup_cpu_usage_usec() -> int | None:
    """Temps CPU cumulé (tous cœurs confondus) consommé par TOUT le cgroup
    depuis sa création, en microsecondes — `cpu.stat` (cgroup v2), toujours
    présent tant que le contrôleur cpu est actif (accounting), même sans
    limite `cpu.max` configurée."""
    try:
        for line in (_CGROUP_V2 / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def container_cpu_percent() -> float | None:
    """CPU consommé par le CONTENEUR entier (le cgroup, donc tous les
    processus qu'il contient), en % multi-cœur (peut dépasser 100 : 350 % =
    3,5 cœurs pleins) — calculé par delta entre deux appels successifs.

    Contrairement à `psutil.cpu_percent()` (module-level), qui lit
    `/proc/stat` : ce fichier N'EST PAS namespacé par Docker et reflète donc
    l'HÔTE entier, pas le conteneur — piège classique. `cpu.stat`, lui, est un
    fichier de cgroup, propre au conteneur.

    Renvoie None hors cgroup v2 (dev hors conteneur) ou tant que la première
    fenêtre de mesure n'est pas écoulée (voir `_Rate`)."""
    usage = _read_cgroup_cpu_usage_usec()
    if usage is None:
        return None
    rate = _cpu_rate.update((usage,))
    if rate is None:
        return None
    # µs de CPU consommées par seconde écoulée -> % d'un cœur.
    percent = rate[0] / 1_000_000 * 100
    # Le quota du cgroup est un plafond que le noyau fait respecter : au-delà,
    # c'est du bruit de mesure, pas de la consommation. On le borne plutôt que
    # d'afficher un 1000 % impossible sur 8 cœurs.
    ceiling = (read_cgroup_cpu_quota() or host_cpu_count()) * 100
    return round(min(max(0.0, percent), ceiling), 1)


def net_totals_bytes() -> tuple[int, int]:
    """(reçus, émis) cumulés sur toutes les interfaces SAUF la boucle locale.

    En conteneur, l'espace de noms réseau ne contient que `lo` et `eth0` : ces
    compteurs sont donc bien ceux du conteneur. `lo` est exclu parce qu'il ne
    traverse jamais la machine — l'y inclure gonflerait la volumétrie avec du
    trafic qui ne coûte rien."""
    recv = sent = 0
    for name, counters in psutil.net_io_counters(pernic=True).items():
        if name == "lo" or name.startswith("lo:"):
            continue
        recv += counters.bytes_recv
        sent += counters.bytes_sent
    return recv, sent


def net_rates_bps() -> tuple[float | None, float | None]:
    """(reçus, émis) en octets par seconde, par delta entre deux appels.

    None tant que la première fenêtre de mesure n'est pas écoulée. Comme pour
    le CPU, la fenêtre est garantie par `_Rate` et ne dépend donc pas de la
    cadence des appelants."""
    rate = _net_rate.update(net_totals_bytes())
    if rate is None:
        return None, None
    return rate[0], rate[1]


def effective_mem_limit_bytes(settings: config.Settings) -> int | None:
    """Plafond mémoire réel d'un run : la limite par run (RLIMIT_AS) si fixée,
    sinon celle du conteneur (cgroup)."""
    if settings.run_memory_limit_mb and settings.run_memory_limit_mb > 0:
        return settings.run_memory_limit_mb * 1024 * 1024
    _current, cgroup_limit = read_cgroup_memory()
    return cgroup_limit


def effective_cpu_cores_limit(settings: config.Settings) -> float | None:
    """Nombre de cœurs qu'un run peut utiliser au maximum : l'affinité par run
    si fixée, sinon le quota du conteneur, sinon None (tous les cœurs de l'hôte)."""
    if settings.run_cpu_cores and settings.run_cpu_cores > 0:
        return float(settings.run_cpu_cores)
    return read_cgroup_cpu_quota()


def app_snapshot() -> dict:
    """État ressources courant de l'application elle-même + conteneur."""
    proc = psutil.Process()
    with proc.oneshot():
        rss = proc.memory_info().rss
    vm = psutil.virtual_memory()
    cg_current, cg_limit = read_cgroup_memory()

    container_cpu = container_cpu_percent()
    if container_cpu is not None:
        cpu_percent, cpu_scope = container_cpu, "container"
    else:
        # Hors conteneur (dev), ou tout premier appel depuis le démarrage
        # (cpu.stat pas encore amorcé) : repli sur le CPU de l'hôte entier.
        cpu_percent, cpu_scope = psutil.cpu_percent(interval=None), "host"

    net_recv_total, net_sent_total = net_totals_bytes()
    net_recv_bps, net_sent_bps = net_rates_bps()

    return {
        "app_rss_bytes": rss,
        "system_mem_total_bytes": vm.total,
        "system_mem_used_bytes": vm.total - vm.available,
        "cgroup_mem_current_bytes": cg_current,
        "cgroup_mem_limit_bytes": cg_limit,
        "cpu_count": host_cpu_count(),
        # Limite CPU du conteneur (DFIR_DUCK_CPU_LIMIT -> cgroup cpu.max),
        # None si aucune limite (dev hors conteneur, ou conteneur illimité).
        "cpu_limit_cores": read_cgroup_cpu_quota(),
        # % multi-cœur (peut dépasser 100). "container" = tout le cgroup
        # (précis), "host" = repli sur /proc/stat (machine entière, seulement
        # hors conteneur ou le tout premier instant après le démarrage).
        "cpu_percent": cpu_percent,
        "cpu_percent_scope": cpu_scope,
        # Réseau : débit courant (None au premier appel) et volumétrie cumulée
        # depuis le démarrage de la machine, hors boucle locale.
        "net_recv_bps": net_recv_bps,
        "net_sent_bps": net_sent_bps,
        "net_recv_bytes": net_recv_total,
        "net_sent_bytes": net_sent_total,
    }
