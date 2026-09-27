"""Réglages modifiables depuis l'onglet Paramètres.

`app/config.yaml` (ou le `.env`) donne l'état initial ; dès que l'exploitant
touche à un réglage, le choix est écrit dans `data/preferences.json` et c'est
lui qui fait foi au démarrage suivant. Ce fichier vit dans le volume de
données : il survit à une reconstruction de l'image, contrairement à
`config.yaml` qui, lui, est figé dedans. Le supprimer revient à la
pré-configuration livrée.

Un seul réglage pour l'instant : **quels traitements** l'orchestrateur a le
droit de lancer. Un outil absent du fichier vaut « activé », de sorte qu'un
traitement ajouté plus tard fonctionne sans migration.

Le reste (chemins des binaires, concurrence, limites) reste dans la
configuration : ce sont des réglages de déploiement, pas d'exploitation.

ATTENTION aux sous-processus. Chaque run d'outil est un processus séparé qui
reconstruit `settings` depuis `config.yaml` et l'environnement : il ne sait
rien de ce fichier. Un module lancé par `python -m ...` qui lit un réglage
d'ici doit appeler `apply_saved()` lui-même.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

FILE_NAME = "preferences.json"


def path():
    return settings.data_dir / FILE_NAME


def load() -> dict[str, Any] | None:
    """Réglages enregistrés, ou None s'il n'y en a pas."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("preferences: %s illisible, configuration utilisée (%s)",
                       FILE_NAME, exc)
        return None
    return data if isinstance(data, dict) else None


def apply_saved() -> None:
    """Applique les réglages enregistrés par-dessus la configuration.

    Appelé au démarrage. Une valeur invalide (fichier tronqué, clé inattendue)
    est ignorée : l'instance démarre avec la configuration."""
    saved = load()
    if saved is None:
        return

    tools = saved.get("tools")
    if isinstance(tools, dict):
        settings.tools_enabled = {
            **settings.tools_enabled,
            **{str(name): bool(value) for name, value in tools.items()},
        }

    logger.info(
        "preferences: repris de %s (traitements désactivés=%s)",
        path(),
        sorted(name for name, on in settings.tools_enabled.items() if not on) or "aucun",
    )


def _write(payload: dict[str, Any]) -> None:
    payload["updated_at"] = datetime.now(UTC).isoformat()
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    # 0600 par précaution : ce fichier décrit la configuration de l'instance.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def save_tools(enabled: dict[str, bool]) -> None:
    """Enregistre quels traitements l'orchestrateur peut lancer.

    Seuls les outils connus du registre sont acceptés : une clé inventée
    resterait sans effet tout en donnant l'illusion d'un réglage."""
    from app.tools import registry

    known = registry.all_tools()
    inconnus = [name for name in enabled if name not in known]
    if inconnus:
        raise ValueError(f"outil inconnu : {', '.join(sorted(inconnus))}")

    settings.tools_enabled = {**settings.tools_enabled, **{k: bool(v) for k, v in enabled.items()}}
    # État complet réécrit : un réglage modifié ne doit pas effacer les autres.
    saved = load() or {}
    saved["tools"] = dict(settings.tools_enabled)
    _write(saved)
    logger.info(
        "preferences: traitements désactivés = %s",
        sorted(name for name, on in settings.tools_enabled.items() if not on) or "aucun",
    )


def tools_state(tools: list[str]) -> list[dict]:
    """Un dictionnaire par outil pour l'onglet : nom, libellé, état, et raison
    d'indisponibilité si l'outil n'est pas installé."""
    from app.tools import registry

    rows = []
    for name in tools:
        tool = registry.all_tools().get(name)
        rows.append({
            "name": name,
            "label": registry.TOOL_LABELS.get(name, name),
            "enabled": settings.tool_enabled(name),
            "available": tool.available() if tool else False,
        })
    return rows
