"""Classification des uploads en acquisitions.

Consomme le pool d'arrivée (`pending_dir`, par défaut `uploads/_pending/`) :
chaque `*_Outcome.json` crée une acquisition, et les archives `.7z` sont
rattachées à l'acquisition dont l'Outcome.json les référence (correspondance
par basename). Appelé à chaque tick de l'orchestrateur, sur l'investigation
déjà chargée pour ce tour.

Deux façons de consommer, selon `pending_consume` :

- `move` (défaut) — le pool appartient à l'application, les fichiers y sont
  déplacés et le dossier se vide ;
- `copy` — le pool appartient à quelqu'un d'autre (partage SMB, volume déposé
  par un autre service) : rien n'y est supprimé, les fichiers sont copiés et
  ce qui a déjà été pris est mémorisé (voir `app/stores/pending.py`).

Dans les deux cas, un fichier encore en cours d'écriture est laissé de côté :
suffixe `.part-` pour les uploads de l'application, fenêtre de stabilité
(`pending_stable_seconds`) pour tout producteur externe.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import UPLOAD_TEMP_INFIX, UPLOADS_DIR, settings
from app.models import Acquisition, Investigation
from app.stores import pending as pending_ledger
from app.upload.service import CHUNKS_ROOT_NAME

logger = logging.getLogger(__name__)


def _new_acquisition_id() -> str:
    return uuid.uuid4().hex[:10]


def _sanitize_name(name: str) -> str:
    name = name.strip().strip("/")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "acquisition"
    return name[:80]


# Noms que Windows réserve aux périphériques : un dossier ainsi nommé est
# inaccessible. Une machine peut très bien s'appeler `CON`.
_RESERVED_DIR_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _storage_name(inv: Investigation, machine_name: str, acq_id: str) -> str:
    """Nom du dossier de l'acquisition sur disque, tiré du nom de machine.

    Ce nom vient de l'Outcome.json, donc de la machine analysée : on ne le
    laisse pas désigner un chemin. Réduit à `[A-Za-z0-9._-]`, débarrassé des
    points et tirets de tête et de queue — sans quoi `..` remonterait d'un
    cran — et écarté s'il tombe sur un nom réservé. Dans ce cas, et quand le
    nom de machine manque, on garde l'identifiant : un dossier moins lisible
    vaut mieux qu'un dossier impossible à ouvrir.

    Deux collectes de la même machine donnent `WIN11` puis `WIN11-2` :
    l'unicité prime sur la lisibilité."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (machine_name or "").strip()).strip("-.")[:60]
    if not base or base.lower() in _RESERVED_DIR_NAMES:
        return acq_id

    pris = {acq.storage_name.lower() for acq in inv.acquisitions.values()}

    def libre(candidat: str) -> bool:
        # Le disque a le dernier mot : un dossier peut survivre à la
        # suppression de son acquisition.
        return candidat.lower() not in pris and not (UPLOADS_DIR / candidat).exists()

    if libre(base):
        return base
    for n in itertools.count(2):
        candidat = f"{base}-{n}"
        if libre(candidat):
            return candidat
        if n > 999:  # garde-fou : on ne boucle pas indéfiniment
            return acq_id
    return acq_id


def _pending_dir() -> Path:
    return settings.pending_dir


def has_pending_uploads() -> bool:
    """True tant qu'il reste des fichiers complets non classifiés dans
    `_pending/` — l'UI s'en sert pour savoir s'il faut continuer à interroger le
    serveur (polling) en attendant le prochain tick de l'orchestrateur."""
    return len(_iter_pending_files()) > 0


def pending_file_count() -> int:
    """Nombre de fichiers complets encore dans `_pending/` (non classés)."""
    return len(_iter_pending_files())


def _iter_pending_files() -> list[Path]:
    """Ne renvoie que les fichiers pending COMPLETS.

    Sont ignorés : les fichiers encore en cours d'écriture (`<nom>.part-xxxx`) et
    le dossier de staging `_chunks/` (morceaux bruts d'un upload par morceaux en
    cours de réassemblage).

    Chaque fichier final vit sous `_pending/<upload_id>/<nom>` (un sous-dossier
    par fichier) pour éviter que deux fichiers de même nom — fréquent avec
    DFIR-ORC — provenant de deux acquisitions s'écrasent avant classification.
    """
    root = _pending_dir()
    if not root.exists():
        return []
    chunks_root = root / CHUNKS_ROOT_NAME
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and UPLOAD_TEMP_INFIX not in p.name
        and chunks_root not in p.parents
        and _is_settled(p)
        and not (_keep_files() and pending_ledger.is_consumed(p))
    ]


def _keep_files() -> bool:
    """Mode `copy` : le pool n'appartient pas à l'application."""
    return settings.pending_consume == "copy"


def _is_settled(file: Path) -> bool:
    """Le fichier a-t-il fini d'arriver ?

    Un producteur externe (partage réseau, copie manuelle) n'a pas la
    convention `.part-` des uploads de l'application : on se rabat sur sa date
    de modification. Tant qu'elle est récente, le fichier peut encore
    grossir — le classer maintenant donnerait une archive tronquée."""
    window = settings.pending_stability_window
    if window <= 0:
        return True
    try:
        age = time.time() - file.stat().st_mtime
    except OSError:
        return False
    if age >= window:
        return True
    logger.debug("pending: '%s' ecrit il y a %.0f s, on attend", file.name, age)
    return False


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass  # non vide ou déjà supprimé


def _take_into_acquisition(acq: Acquisition, src: Path) -> None:
    """Rattache un fichier du pool à une acquisition, en le déplaçant ou en le
    copiant selon `pending_consume`."""
    dest_dir = UPLOADS_DIR / acq.storage_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    src_parent = src.parent

    if _keep_files():
        # Copie à côté, puis bascule : une copie interrompue (partage qui
        # décroche) ne doit pas laisser un fichier tronqué passer pour complet.
        tmp = dest.with_name(f"{dest.name}{UPLOAD_TEMP_INFIX}{uuid.uuid4().hex[:8]}")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
        pending_ledger.mark(src, acq.id)
    else:
        try:
            src.replace(dest)
        except OSError:
            # Pool et acquisitions sur deux montages différents : `rename` ne
            # traverse pas les systèmes de fichiers, `move` recopie.
            shutil.move(str(src), str(dest))
        _rmdir_if_empty(src_parent)

    if src.name not in acq.uploaded_filenames:
        acq.uploaded_filenames.append(src.name)
    logger.info(
        "acquisition %s: fichier rattaché '%s' (%s)",
        acq.id, src.name, "copié" if _keep_files() else "déplacé",
    )


def _create_acquisition_from_outcome(inv: Investigation, outcome_src: Path) -> Acquisition:
    """Un `*_Outcome.json` trouvé dans le pending crée TOUJOURS une nouvelle
    acquisition. `upload_group_key` = nom du fichier Outcome.json (stable)."""
    # Le nom de machine est lu dans le pool, AVANT de déplacer le fichier :
    # c'est lui qui nomme le dossier où ce fichier va atterrir.
    acq_id = _new_acquisition_id()
    computer_name = _outcome_extract_computer_name(outcome_src)
    acq = Acquisition(
        id=acq_id,
        name=_sanitize_name(computer_name) if computer_name
        else f"acquisition-{uuid.uuid4().hex[:6]} (sans nom)",
        dir_name=_storage_name(inv, computer_name or "", acq_id),
        upload_group_key=outcome_src.name,
        created_at=datetime.now(UTC).isoformat(),
    )
    inv.acquisitions[acq.id] = acq

    _take_into_acquisition(acq, outcome_src)
    acq.outcome_filename = outcome_src.name

    logger.info(
        "acquisition %s: créée depuis '%s' (nom='%s', dossier='%s')",
        acq.id,
        outcome_src.name,
        acq.name,
        acq.storage_name,
    )
    return acq


def _outcome_extract_expected_archives(outcome_path: Path) -> set[str]:
    """Noms des archives .7z top-level référencées par le Outcome.json, à
    `dfir-orc.outcome.command_set[].archive.name`.

    On ne scanne PAS tout l'arbre JSON à la recherche de `.7z` : chaque archive
    a sa propre liste `files` qui contient d'autres `.7z` (sous-fichiers
    compressés, ex. SystemHives.7z). Seul `archive.name` désigne une archive
    à uploader.
    """
    try:
        data = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Outcome.json illisible (%s) : %s", outcome_path, exc)
        return set()

    try:
        command_set = data["dfir-orc"]["outcome"]["command_set"]
    except (KeyError, TypeError):
        logger.warning(
            "Outcome.json %s : chemin dfir-orc.outcome.command_set introuvable "
            "(schéma inattendu ?) — aucune archive détectée.",
            outcome_path.name,
        )
        return set()

    if not isinstance(command_set, list):
        return set()

    archives: set[str] = set()
    for entry in command_set:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("archive") or {}).get("name")
        if isinstance(name, str) and name.lower().endswith(".7z"):
            archives.add(Path(name).name)

    return archives


def _outcome_extract_computer_name(outcome_path: Path) -> str | None:
    """Lit `dfir-orc.outcome.computer_name` — devient le nom affiché de
    l'acquisition."""
    try:
        data = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    try:
        name = data["dfir-orc"]["outcome"]["computer_name"]
    except (KeyError, TypeError):
        return None

    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def classify_pending_uploads(inv: Investigation) -> set[str]:
    """En une passe :

    1. tout `*_Outcome.json` du pending crée une nouvelle acquisition ;
    2. les autres fichiers sont rattachés à n'importe quelle acquisition dont
       l'Outcome.json les référence (par basename) ;
    3. un fichier sans acquisition correspondante reste en pending (archive
       arrivée avant son Outcome.json) — reclassé au tick suivant.

    NE persiste PAS : c'est au tick de l'orchestrateur de le faire. Renvoie les
    ids d'acquisitions touchées (créées ou complétées).
    """
    touched: set[str] = set()

    outcomes = [p for p in _iter_pending_files() if p.name.endswith("_Outcome.json")]
    for outcome_src in outcomes:
        acq = _create_acquisition_from_outcome(inv, outcome_src)
        touched.add(acq.id)

    remaining = [p for p in _iter_pending_files() if not p.name.endswith("_Outcome.json")]
    if not remaining:
        return touched

    for acq in inv.acquisitions.values():
        if not acq.outcome_filename:
            continue
        outcome_path = UPLOADS_DIR / acq.storage_name / acq.outcome_filename
        expected = _outcome_extract_expected_archives(outcome_path)
        if not expected:
            continue

        for src in list(remaining):
            if src.name in expected and src.name not in acq.uploaded_filenames:
                _take_into_acquisition(acq, src)
                remaining.remove(src)
                touched.add(acq.id)

    # Doublons orphelins : un fichier déjà présent dans une acquisition qui
    # réapparaît en pending (retry client après un faux échec réseau). Sans ce
    # nettoyage il resterait orphelin indéfiniment (jamais re-matché).
    already_claimed = {
        fname for acq in inv.acquisitions.values() for fname in acq.uploaded_filenames
    }
    for src in list(remaining):
        if src.name not in already_claimed:
            continue
        if _keep_files():
            # Rien n'est supprimé du pool : il suffit de le noter pour ne plus
            # le reproposer à chaque tick.
            pending_ledger.mark(src, "")
            logger.info("pending: doublon '%s' ignoré (déjà rattaché)", src.name)
        else:
            src.unlink(missing_ok=True)
            _rmdir_if_empty(src.parent)
            logger.info("pending: doublon '%s' supprimé (déjà rattaché)", src.name)
        remaining.remove(src)

    for src in remaining:
        logger.debug(
            "pending: '%s' conservé (aucune acquisition ne le référence encore)",
            src.name,
        )

    if _keep_files():
        pending_ledger.save()
    return touched


def acquisition_upload_status(acq: Acquisition) -> dict:
    have = set(acq.uploaded_filenames)
    expected: set[str] = set()
    if acq.outcome_filename:
        outcome = UPLOADS_DIR / acq.storage_name / acq.outcome_filename
        if outcome.exists():
            expected = _outcome_extract_expected_archives(outcome)
    missing = sorted(expected - have)
    return {
        "expected": sorted(expected),
        "missing": missing,
        "complete": bool(expected) and not missing,
    }


def upload_status(inv: Investigation) -> dict:
    per_acquisition = {
        acq_id: acquisition_upload_status(acq)
        for acq_id, acq in inv.acquisitions.items()
    }
    return {
        "per_acquisition": per_acquisition,
        "complete": bool(per_acquisition)
        and all(s["complete"] for s in per_acquisition.values()),
    }


def delete_upload(inv: Investigation, acq_id: str, filename: str) -> None:
    acq = inv.acquisitions.get(acq_id)
    if not acq:
        return
    path = UPLOADS_DIR / acq.storage_name / filename
    path.unlink(missing_ok=True)
    if filename in acq.uploaded_filenames:
        acq.uploaded_filenames.remove(filename)
    if acq.outcome_filename == filename:
        acq.outcome_filename = None
    logger.info("acquisition %s: fichier '%s' supprimé par l'utilisateur", acq_id, filename)
