"""Onglet Paramètres : quels traitements l'orchestrateur a le droit de lancer.

Réglage **global**, pas par investigation : c'est une décision d'exploitation,
pas une propriété des données. Chaque modification est enregistrée telle quelle
(pas de bouton) et ne renvoie que son accusé de réception, en swap hors cible —
remplacer le formulaire en pleine saisie ferait perdre le focus.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request

from app.api.views import SETTABLE_TOOLS, templates
from app.stores import preferences

router = APIRouter()
logger = logging.getLogger(__name__)


def _feedback(request: Request, error: str | None, message: str | None):
    """Fragments d'état renvoyés hors cible après un enregistrement."""
    return templates.TemplateResponse(
        request,
        "partials/_settings_feedback.html",
        {
            "tools": preferences.tools_state(SETTABLE_TOOLS),
            "error": error,
            "message": message,
        },
    )


@router.post("/settings/tools")
async def set_tools(request: Request):
    """Cases cochées = traitements autorisés à démarrer.

    Le formulaire envoie la liste des cases cochées ; celles qui manquent sont
    donc les traitements désactivés. On repart de la liste complète pour ne pas
    dépendre de ce que le navigateur omet."""
    form = await request.form()
    coches = set(form.getlist("tool"))
    error = message = None

    inconnus = coches - set(SETTABLE_TOOLS)
    if inconnus:
        # Un nom qu'on ne connaît pas ne peut pas venir du formulaire : plutôt
        # que de l'ignorer — ce qui reviendrait à décocher tout le reste — on
        # refuse sans rien changer.
        return _feedback(
            request, f"Traitement inconnu : {', '.join(sorted(inconnus))}.", None
        )

    voulu = {name: name in coches for name in SETTABLE_TOOLS}
    try:
        preferences.save_tools(voulu)
    except (ValueError, OSError) as exc:
        error = f"Enregistrement impossible : {exc}"
    else:
        eteints = [name for name, on in voulu.items() if not on]
        moment = datetime.now().strftime("%H:%M:%S")
        message = (
            f"Enregistré à {moment} — traitements désactivés : {', '.join(eteints)}."
            if eteints else f"Enregistré à {moment} — tous les traitements sont actifs."
        )
    return _feedback(request, error, message)
