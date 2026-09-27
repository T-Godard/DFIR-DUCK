"""Agrégation des routers HTTP."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import investigation, metrics, resources, runs, settings, upload

router = APIRouter()
router.include_router(investigation.router)
router.include_router(upload.router)
router.include_router(runs.router)
router.include_router(settings.router)
router.include_router(metrics.router)
router.include_router(resources.router)
