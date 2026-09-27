"""Contexte de requête propagé implicitement dans le code asynchrone.

`request_id` identifie une requête HTTP (ou un traitement de fond) de bout en
bout : il est posé par le middleware de `app/main.py` et repris dans TOUTES les
lignes de log (voir `app/logging_setup.py::RequestIdFilter`) — c'est ce qui
permet de recoller entre elles les lignes d'un même événement quand plusieurs
requêtes ou plusieurs runs progressent en parallèle.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    return request_id_ctx.get()
