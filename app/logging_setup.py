"""Configuration du logging — appelée une fois au tout début du démarrage
(`app/main.py::lifespan`), avant toute autre initialisation.

Trois sorties, toutes alimentées par le logger racine :

* **console** (stdout) — format humain, visible via `docker compose logs` ;
* **`data/logs/app.log`** — même format humain, fichier rotatif ;
* **`data/logs/app.jsonl`** — une ligne JSON par événement, fichier rotatif,
  destiné à être ingéré tel quel par un collecteur de logs.

Chaque ligne porte le `request_id` du contexte courant (voir `app/context.py`),
ce qui permet de suivre un événement de bout en bout même quand plusieurs
requêtes / runs progressent en parallèle.

Pour attacher des champs structurés à un log (repris dans `app.jsonl`), utiliser
`log_event()` ou passer `extra={"fields": {...}}`.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.config
from typing import Any

from app.config import Settings
from app.context import get_request_id

# Attributs standard d'un LogRecord : tout le reste est considéré comme un
# champ "extra" applicatif et sérialisé dans app.jsonl.
_STD_LOGRECORD_KEYS = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class RequestIdFilter(logging.Filter):
    """Injecte `record.request_id` pour tous les formatters."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """Sérialise un LogRecord en une ligne JSON compacte."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # Champs "extra" : soit regroupés sous fields=..., soit à plat.
        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        for key, value in record.__dict__.items():
            if key not in _STD_LOGRECORD_KEYS and key not in ("request_id", "fields"):
                payload.setdefault(key, value)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    human_fmt = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "request_id": {"()": RequestIdFilter},
            },
            "formatters": {
                "human": {"format": human_fmt},
                "json": {"()": JsonFormatter},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "human",
                    "filters": ["request_id"],
                    "stream": "ext://sys.stdout",
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "formatter": "human",
                    "filters": ["request_id"],
                    "filename": str(settings.logs_dir / "app.log"),
                    "maxBytes": settings.log_max_bytes,
                    "backupCount": settings.log_backup_count,
                    "encoding": "utf-8",
                },
                "jsonl": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "formatter": "json",
                    "filters": ["request_id"],
                    "filename": str(settings.logs_dir / "app.jsonl"),
                    "maxBytes": settings.log_max_bytes,
                    "backupCount": settings.log_backup_count,
                    "encoding": "utf-8",
                },
            },
            "root": {
                "level": settings.log_level.upper(),
                "handlers": ["console", "file", "jsonl"],
            },
            "loggers": {
                # uvicorn a ses propres handlers par défaut : on les retire pour
                # que tout remonte au logger racine (et donc à nos 3 sorties),
                # sans doublon.
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                # Le log d'accès uvicorn (« GET / 200 ») fait doublon avec notre
                # middleware (lui en DEBUG) : silencieux sauf anomalie.
                "uvicorn.access": {"handlers": [], "propagate": True, "level": "WARNING"},
            },
        }
    )


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log un événement nommé avec des champs structurés.

    Le message humain reste lisible (`event key=value ...`) et les champs sont
    repris tels quels dans `app.jsonl`.
    """
    if fields:
        readable = " ".join(f"{k}={v}" for k, v in fields.items())
        logger.log(level, "%s %s", event, readable, extra={"event": event, "fields": fields})
    else:
        logger.log(level, "%s", event, extra={"event": event})
