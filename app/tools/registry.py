"""Registre des outils d'extraction.

Ajouter un outil : l'implémenter dans `app/tools/<x>.py`, l'ajouter à
`_EXTRACTION` et créer sa `Stage` dans `app/pipeline/stages.py`.
"""

from __future__ import annotations

from app.tools.base import EXTRACTION, Tool
from app.tools.evtx import EvtxExtractTool
from app.tools.evtx_chainsaw import EvtxChainsawTool
from app.tools.evtx_hayabusa import EvtxHayabusaTool
from app.tools.evtx_zircolite import EvtxZircoliteTool
from app.tools.mft import MftExtractTool
from app.tools.mft_chainsaw import MftChainsawTool
from app.tools.ogre import DfirOgreTool
from app.tools.registry_chainsaw import RegistryChainsawTool
from app.tools.registry_extract import RegistryExtractTool

# Regroupés par famille d'artefacts : l'extraction d'abord, puis ce qui
# l'analyse. Un traitement sur un autre sujet (navigateurs, journaux Linux...)
# s'ajoute ici avec son propre préfixe.
_EXTRACTION: tuple[Tool, ...] = (
    DfirOgreTool(),
    EvtxExtractTool(),
    EvtxChainsawTool(),
    EvtxHayabusaTool(),
    EvtxZircoliteTool(),
    MftExtractTool(),
    MftChainsawTool(),
    RegistryExtractTool(),
    RegistryChainsawTool(),
)

_TOOLS: dict[str, Tool] = {t.name: t for t in _EXTRACTION}

# Ordre d'affichage dans l'UI et les métriques.
EXTRACTION_ORDER: list[str] = [t.name for t in _EXTRACTION]

TOOL_LABELS: dict[str, str] = {t.name: t.label for t in _TOOLS.values()}


def get(name: str) -> Tool:
    try:
        return _TOOLS[name]
    except KeyError:
        raise KeyError(f"outil inconnu : {name!r} (connus : {sorted(_TOOLS)})") from None


def all_tools() -> dict[str, Tool]:
    return dict(_TOOLS)


def kind_of(name: str) -> str:
    tool = _TOOLS.get(name)
    return tool.kind if tool else EXTRACTION
