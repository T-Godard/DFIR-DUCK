"""Extraction sélective d'artefacts depuis les archives DFIR-ORC.

Trois familles, même mécanique : les journaux d'événements (`evtx`), la table
de fichiers NTFS (`mft`) et les ruches du registre (`hive`).

Les EVTX sont enfouis : archive top-level (souvent `Général.7z`) -> sous-archive
(souvent `Events.7z`) -> répertoire `evtx/` -> fichiers nommés par ORC de façon
exotique, ex. `...Microsoft-Windows-Bits-Client%4Operational.evtx_{GUID}.data`
(le `.evtx` est AU MILIEU du nom). On ne se fie donc PAS aux noms d'archives : on
parcourt récursivement, on repère les EVTX par « `.evtx` présent dans le nom », et
on **n'extrait que ces fichiers** (+ les sous-archives à re-parcourir) pour ne pas
décompresser des Go de hives / MFT / GetThis inutilement. Les fichiers extraits
sont renommés pour se terminer par `.evtx` (voir `_evtx_filename`) : sans ça,
Chainsaw / Hayabusa / Zircolite — qui filtrent par extension — ignorent
silencieusement les EVTX nommés à la DFIR-ORC.

La MFT suit le même chemin : elle est collectée par DFIR-ORC mais n'apparaît
pas dans la sortie DFIR-OGRE, et ORC la nomme à sa façon
(`..._$MFT_{GUID}.data`). On la repère par `$MFT` dans le nom, et on renomme en
`.mft` — Chainsaw reconnaît la MFT par son extension quand on lui donne un
dossier.

Les ruches (`SYSTEM`, `SOFTWARE`, `NTUSER.DAT`, `Amcache.hve`...) suivent le
même chemin : DFIR-OGRE en tire des vues dérivées (services, tâches, amcache)
mais pas la ruche brute, dont `registry-chainsaw` a besoin pour le shimcache.

S'appuie sur le binaire `7z` (paquet `7zip`, présent dans l'image). Utilisable en
module (`extract`) ou en CLI :

    python -m app.artifact_extract <source> <dest> --kind evtx|mft|hive [--password ...]
"""

from __future__ import annotations

import argparse
import itertools
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

_SEVENZIP = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za") or "7z"

# Une archive à re-parcourir (pas une partie `.002`+ d'un volume splitté).
_ARCHIVE_RE = re.compile(r"(\.7z|\.zip|\.7z\.001)$", re.IGNORECASE)
_SPLIT_PART_RE = re.compile(r"\.7z\.(\d{3})$", re.IGNORECASE)

Log = Callable[[str], None]


def _is_evtx(name: str) -> bool:
    return ".evtx" in name.lower()


def _is_mft(name: str) -> bool:
    """ORC nomme la MFT `..._$MFT_{GUID}.data` : c'est `$MFT` dans le nom qui
    la trahit, pas l'extension. Les fichiers déjà nommés `.mft` (collecte
    montée à la main) sont repris aussi."""
    lowered = name.lower()
    return "$mft" in lowered or lowered.endswith(".mft")


def _evtx_filename(original: str) -> str:
    """Nom de fichier final, garanti terminé par `.evtx` (la vraie extension,
    pas une sous-chaîne) : Chainsaw / Hayabusa / Zircolite détectent les EVTX
    par extension, or DFIR-ORC nomme parfois
    `...Operational.evtx_{GUID}.data` (`.evtx` au milieu, extension réelle
    `.data`). On tronque tout ce qui suit la 1re occurrence de `.evtx` et on
    normalise sa casse."""
    m = re.search(r"\.evtx", original, re.IGNORECASE)
    base = original[: m.start()] if m else original
    return f"{base}.evtx"


def _mft_filename(original: str) -> str:
    """Nom final terminé par `.mft`, en gardant celui d'ORC : il identifie le
    volume, et deux volumes donnent deux MFT dans le même dossier."""
    name = Path(original).name
    return name if name.lower().endswith(".mft") else f"{name}.mft"


# Les ruches, par nom de fichier EXACT tel qu'il est sur la machine. Chercher
# le nom de la ruche « quelque part » dans le nom du fichier ne marche pas :
# ORC y encode le chemin d'origine, et le cache d'images natives .NET est plein
# de `System.Management.ni.dll`, `System.Xaml.ni.dll.aux`, `Security.Principal…`
# qui commencent par le nom d'une ruche sans en être une.
_HIVE_NAMES = {
    "SYSTEM": "SYSTEM",
    "SOFTWARE": "SOFTWARE",
    "SAM": "SAM",
    "SECURITY": "SECURITY",
    "NTUSER.DAT": "NTUSER",
    "USRCLASS.DAT": "USRCLASS",
    "AMCACHE.HVE": "AMCACHE",
    "AMCACHE": "AMCACHE",   # après retrait du `.hve` ajouté à l'extraction
}
# Suffixe que GetThis accole au nom d'origine : `..._{GUID}.data`.
_ORC_SUFFIX_RE = re.compile(r"_\{[0-9A-Fa-f-]+\}\.data$", re.IGNORECASE)
# Journaux de transaction et fichiers d'accompagnement : même nom que la ruche,
# mais illisibles pour un parseur de ruche.
_HIVE_COMPANION_RE = re.compile(r"\.LOG\d?(?![A-Za-z0-9])|\.regtrans-ms|\.blf", re.IGNORECASE)

# Signature d'une ruche : ses quatre premiers octets. Seul contrôle qui ne se
# fasse pas berner par un nom de fichier.
HIVE_MAGIC = b"regf"


def _hive_candidates(name: str) -> set[str]:
    """Noms de fichier possibles d'origine, à partir de ce qu'on a sous la main.

    ORC nomme un fichier collecté
    `{machine}_{FRN}_{parentFRN}_{n}_{nom d'origine}_{GUID}.data`, et notre
    extraction ajoute `.hve`. On remonte donc au nom d'origine en retirant ces
    deux couches, sans supposer qu'elles soient là : une collecte montée à la
    main contient un fichier simplement nommé `SYSTEM`.
    """
    base = Path(name.replace("\\", "/")).name
    formes = {base}
    if base.lower().endswith(".hve"):
        formes.add(base[: -len(".hve")])
    for forme in list(formes):
        sans_guid = _ORC_SUFFIX_RE.sub("", forme)
        formes.add(sans_guid)
        # Le nom d'origine est le dernier segment séparé par `_` — aucun nom de
        # ruche n'en contient.
        formes.add(sans_guid.rsplit("_", 1)[-1])
    return formes


def hive_kind(name: str) -> str | None:
    """Ruche que désigne ce nom de fichier (`SYSTEM`, `AMCACHE`, `NTUSER`...),
    ou None si ce n'en est pas une.

    Le nom seul ne prouve rien : `is_hive_file()` vérifie le contenu avant
    qu'une ruche soit donnée à un outil.
    """
    base = Path(name.replace("\\", "/")).name
    if _is_evtx(base) or _HIVE_COMPANION_RE.search(base):
        return None
    for candidat in _hive_candidates(base):
        famille = _HIVE_NAMES.get(candidat.upper())
        if famille:
            return famille
    return None


def is_hive_file(path: Path) -> bool:
    """Vraie ruche : le fichier commence par `regf`.

    Le nom reste une heuristique — ORC encode le chemin d'origine dedans, et
    une collecte contient des milliers de fichiers. La signature, elle, tranche.
    """
    try:
        with path.open("rb") as fh:
            return fh.read(len(HIVE_MAGIC)) == HIVE_MAGIC
    except OSError:
        return False


def system_hives(directory: Path) -> list[Path]:
    """Ruches SYSTEM exploitables d'un dossier d'extraction : bon nom ET bon
    contenu. C'est là que vit le shimcache."""
    return sorted(
        p for p in directory.glob("*.hve")
        if p.is_file() and hive_kind(p.name) == "SYSTEM" and is_hive_file(p)
    )


def amcache_hive(directory: Path) -> Path | None:
    """Amcache.hve du dossier, s'il a été collecté : il sert à dater les
    entrées du shimcache."""
    return next(
        (p for p in sorted(directory.glob("*.hve"))
         if p.is_file() and hive_kind(p.name) == "AMCACHE" and is_hive_file(p)),
        None,
    )


def _is_hive(name: str) -> bool:
    return hive_kind(name) is not None


def _hive_filename(original: str) -> str:
    """Nom final terminé par `.hve`, en gardant celui d'ORC : il porte le chemin
    d'origine, donc le volume, l'utilisateur et l'éventuelle copie VSS — c'est
    ce qui distingue deux ruches SYSTEM dans le même dossier."""
    name = Path(original).name
    return name if name.lower().endswith(".hve") else f"{name}.hve"


# Une famille d'artefacts : comment la reconnaître dans une archive, comment
# nommer le fichier extrait, et le libellé utilisé dans les journaux.
KINDS = {
    "evtx": {"match": _is_evtx, "rename": _evtx_filename, "label": "EVTX"},
    "mft": {"match": _is_mft, "rename": _mft_filename, "label": "MFT"},
    "hive": {"match": _is_hive, "rename": _hive_filename, "label": "ruches"},
}


def _is_archive(name: str) -> bool:
    return _ARCHIVE_RE.search(name) is not None


def _run7z(args: list[str], *, timeout: int = 7200, log: Log | None = None) -> str:
    proc = subprocess.run(  # noqa: S603
        [_SEVENZIP, *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    # 7z: 0 = OK, 1 = warning non fatal, >=2 = erreur.
    if proc.returncode >= 2:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        raise RuntimeError(f"7z (exit {proc.returncode}): {' / '.join(tail)}")
    if proc.returncode == 1 and log is not None:
        for line in (proc.stdout or "").splitlines():
            if "WARNING" in line or "Can not" in line or "Cannot" in line:
                log(f"  [7z warn] {line.strip()}")
    return proc.stdout


def _list_entries(archive: Path, password: str) -> list[str]:
    """Noms des fichiers (pas les dossiers) contenus dans l'archive."""
    out = _run7z(["l", "-slt", "-y", f"-p{password}", str(archive)])
    entries: list[dict[str, str]] = []
    block: dict[str, str] = {}
    body = False
    for line in out.splitlines():
        if not body:
            if line.strip() == "----------":
                body = True
            continue
        if not line.strip():
            if "Path" in block:
                entries.append(block)
            block = {}
            continue
        if " = " in line:
            key, _, value = line.partition(" = ")
            block[key.strip()] = value
    if "Path" in block:
        entries.append(block)

    files = []
    for b in entries:
        attrs = b.get("Attributes", "").strip()
        is_dir = b.get("Folder", "").strip() == "+" or attrs.startswith("D")
        if not is_dir:
            files.append(b["Path"])
    return files


def _extract_members(
    archive: Path,
    members: list[str],
    dest: Path,
    password: str,
    *,
    flatten: bool = False,
    log: Log | None = None,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    listfile = dest.parent / f".7zlist_{uuid.uuid4().hex[:8]}.txt"
    listfile.write_text("\n".join(members) + "\n", encoding="utf-8")
    cmd = "e" if flatten else "x"  # `e` = extrait à plat (chemins plus courts)
    try:
        _run7z(
            [cmd, "-y", "-bd", "-bb0", f"-p{password}", f"-o{dest}", str(archive), f"@{listfile}"],
            log=log,
        )
    finally:
        listfile.unlink(missing_ok=True)


def _place(src: Path, dest_dir: Path, name: str | None = None) -> Path:
    """Déplace `src` à plat dans `dest_dir` sous `name` (par défaut `src.name`),
    nom rendu unique si collision."""
    target = dest_dir / (name or src.name)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for i in itertools.count(1):
            target = dest_dir / f"{stem}_{i}{suffix}"
            if not target.exists():
                break
    shutil.move(str(src), str(target))
    return target


def _split_siblings(member: str, all_members: list[str]) -> list[str]:
    """Pour `X.7z.001`, renvoie toutes les parties `X.7z.NNN` présentes."""
    m = _SPLIT_PART_RE.search(member)
    if not m:
        return [member]
    prefix = member[: m.start()] + ".7z."
    parts = sorted(
        x for x in all_members if x.startswith(prefix) and _SPLIT_PART_RE.search(x)
    )
    return parts or [member]


def extract(
    kind: str,
    sources: list[Path],
    dest: Path,
    *,
    password: str = "",
    scratch: Path,
    max_depth: int = 4,
    log: Log = print,
) -> int:
    """Extrait tous les artefacts de la famille `kind` trouvés (récursivement)
    dans `sources` vers `dest` (à plat). Renvoie le nombre de fichiers écrits."""
    famille = KINDS[kind]
    matches, rename, label = famille["match"], famille["rename"], famille["label"]
    dest.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    seq = itertools.count()
    written = 0

    stack: list[tuple[Path, int]] = []
    for src in sources:
        if src.is_dir():
            for p in sorted(src.iterdir()):
                if p.is_file() and _is_archive(p.name):
                    stack.append((p, 0))
        elif src.is_file():
            stack.append((src, 0))

    if not stack:
        log("aucune archive à parcourir")
        return 0

    while stack:
        archive, depth = stack.pop()
        try:
            members = _list_entries(archive, password)
        except Exception as exc:  # noqa: BLE001
            log(f"[warn] listing de {archive.name} échoué ({exc}) — ignoré")
            continue

        trouves = [m for m in members if matches(m)]
        nested = [m for m in members if _is_archive(m) and not matches(m)]
        log(
            f"[scan] depth={depth} {archive.name}: {len(members)} entrées, "
            f"{len(trouves)} {label}, {len(nested)} sous-archive(s)"
        )

        if trouves:
            staging = scratch / f"e{next(seq)}"
            _extract_members(archive, trouves, staging, password, flatten=True, log=log)
            got = 0
            for f in sorted(staging.rglob("*")):
                if f.is_file():
                    placed = _place(f, dest, name=rename(f.name))
                    written += 1
                    got += 1
                    log(f"  + {placed.name}")
            if got < len(trouves):
                log(
                    f"  [warn] {len(trouves) - got}/{len(trouves)} {label} "
                    f"non extrait(s) de {archive.name}"
                )
            shutil.rmtree(staging, ignore_errors=True)

        if nested and depth >= max_depth:
            log(f"[warn] profondeur max ({max_depth}) — {len(nested)} sous-archive(s) ignorée(s)")
            continue

        handled: set[str] = set()
        for nm in nested:
            if nm in handled:
                continue
            parts = _split_siblings(nm, members)
            handled.update(parts)
            staging = scratch / f"a{next(seq)}"
            try:
                _extract_members(archive, parts, staging, password, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"[warn] extraction de {nm} échouée ({exc}) — ignorée")
                continue
            extracted = staging / parts[0]
            if extracted.exists():
                stack.append((extracted, depth + 1))
            else:  # 7z a pu aplatir : chercher le fichier
                cand = next(
                    (p for p in staging.rglob("*") if p.is_file() and _is_archive(p.name)),
                    None,
                )
                if cand:
                    stack.append((cand, depth + 1))
                else:
                    log(f"[warn] {nm} extrait mais introuvable dans {staging}")

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.artifact_extract")
    parser.add_argument("source", type=Path, help="dossier d'acquisition ou archive")
    parser.add_argument("dest", type=Path, help="dossier de sortie")
    parser.add_argument("--kind", choices=sorted(KINDS), default="evtx",
                        help="famille d'artefacts à extraire")
    parser.add_argument("--password", default="", help="mot de passe des archives internes ORC")
    parser.add_argument("--scratch", type=Path, default=None, help="dossier temporaire")
    parser.add_argument("--max-depth", type=int, default=4)
    args = parser.parse_args(argv)

    if not shutil.which(_SEVENZIP) and not Path(_SEVENZIP).exists():
        print(f"[erreur] binaire 7z introuvable ({_SEVENZIP})", file=sys.stderr)
        return 2

    scratch = args.scratch or (args.dest.parent / f".scratch-{args.dest.name}")

    if args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)

    print(f"7z: {_SEVENZIP}")
    print(f"source: {args.source}")
    print(f"dest:   {args.dest}")
    try:
        count = extract(
            args.kind,
            [args.source],
            args.dest,
            password=args.password,
            scratch=scratch,
            max_depth=args.max_depth,
            log=lambda m: print(m, flush=True),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"{args.kind.upper()}_EXTRACTED={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
