from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api.views import base_context, templates
from app.config import settings
from app.stores import acquisitions as acq_store
from app.stores import investigation as store
from app.upload.service import save_chunk, try_finalize_upload

logger = logging.getLogger(__name__)

router = APIRouter()


def require_uploads_enabled() -> None:
    """Garde des routes qui écrivent dans `uploads/`. Masquer les boutons ne
    suffit pas : les URL restent appelables directement."""
    if not settings.web_uploads_enabled:
        raise HTTPException(status_code=403, detail="Dépôt via l'interface web désactivé")


@router.post(
    "/investigations/{inv_id}/upload/chunk",
    dependencies=[Depends(require_uploads_enabled)],
)
async def upload_chunk(
    inv_id: str,
    upload_id: str = Form(...),
    relpath: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    total_size: int = Form(...),
    chunk: UploadFile = File(...),
):
    """Upload par morceaux : un fichier volumineux est découpé côté navigateur
    (voir base.html) et chaque morceau est posté ici. Une fois tous les
    morceaux reçus, `try_finalize_upload` réassemble le fichier complet dans
    `_pending/{upload_id}/`, visible par l'orchestrateur seulement à ce
    moment-là."""
    await save_chunk(upload_id, chunk_index, chunk)
    result = await try_finalize_upload(upload_id, relpath, total_chunks, total_size)
    return JSONResponse({"status": result.status, "error": result.error})


@router.get("/investigations/{inv_id}/upload/status")
def upload_status(request: Request, inv_id: str):
    """Interrogé en polling par l'UI tant qu'il reste des fichiers non
    classifiés dans `_pending/` (jusqu'à un tick de latence côté orchestrateur)."""
    inv = store.get()
    return templates.TemplateResponse(request, "partials/upload_status.html", base_context(inv))


@router.delete(
    "/investigations/{inv_id}/acquisitions/{acq_id}/upload/{filename}",
    dependencies=[Depends(require_uploads_enabled)],
)
async def delete_uploaded_file(request: Request, inv_id: str, acq_id: str, filename: str):
    async with store.mutate() as inv:
        acq_store.delete_upload(inv, acq_id, filename)
    inv = store.get()
    return templates.TemplateResponse(request, "partials/upload_result.html", base_context(inv))
