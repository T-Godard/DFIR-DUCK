"""Analyse du shimcache (AppCompatCache) avec Chainsaw — moteur du run
`registry-chainsaw`.

    chainsaw analyse shimcache <ruche SYSTEM> --output <sortie>.csv
        [--regexfile <motifs>] [--amcache <Amcache.hve> --tspair]

Pourquoi un module et pas un simple `argv` d'outil : `analyse shimcache` prend
**une** ruche SYSTEM à la fois, or une acquisition en contient souvent
plusieurs (plusieurs volumes, copies VSS). On boucle donc sur les ruches
extraites par `registry-extract` et on écrit un CSV par ruche.

Ce que ça apporte : le shimcache liste les exécutables vus par le cache de
compatibilité, y compris ceux qui ont disparu du disque et dont les journaux
ont été effacés. Chainsaw en sort une **chronologie d'exécution**.

Toute la difficulté du shimcache est là : l'horodatage stocké est la date de
modification du fichier, pas celle de son exécution. Les entrées sont en
revanche empilées de la plus récente à la plus ancienne. Il suffit donc de
dater quelques entrées pour encadrer toutes les autres, et Chainsaw récupère
ces dates d'insertion de deux façons :

- `--regexfile` : les entrées dont le chemin correspond à un motif sont celles
  dont la date de modification vaut probablement date d'insertion — un binaire
  déposé puis lancé dans la foulée depuis un dossier temporaire, par exemple.
  Les motifs viennent de `app/tools/shimcache_patterns.txt`, commenté pour
  l'exploitant et nettoyé par `load_patterns()` avant d'être passé à Chainsaw,
  qui compilerait sinon les commentaires comme des motifs ;
- `--tspair` (avec `--amcache`) : les entrées dont l'horodatage shimcache et
  l'horodatage Amcache se suivent de près. Cet enrichissement est fragile —
  une entrée d'Amcache sans `LowerCaseLongPath` fait abandonner Chainsaw —
  donc en cas d'échec la ruche est réanalysée sans lui, et le journal le dit.

Sans motifs ni Amcache, la chronologie reste produite mais presque aucune
entrée n'est datée : c'est une liste ordonnée, pas une chronologie.

Utilisable en CLI :

    python -m app.registry_shimcache <dossier ruches> <dossier sortie> \
        --chainsaw /opt/tools/chainsaw/chainsaw [--patterns motifs.txt]
"""

from __future__ import annotations

import argparse
import itertools
import re
import subprocess
import sys
from pathlib import Path

from app.artifact_extract import amcache_hive, hive_kind, is_hive_file, system_hives

# Jeu d'options de `chainsaw analyse shimcache`, vérifié sur Chainsaw 2.16.
# Isolé ici : si une version les renomme, c'est la seule chose à corriger, et le
# message d'échec renvoie vers `<chainsaw> analyse shimcache --help`.
_OUTPUT_FLAG = "--output"
_PATTERNS_FLAG = "--regexfile"
_AMCACHE_FLAG = "--amcache"
_PAIR_FLAG = "--tspair"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def log(message: str) -> None:
    print(message, flush=True)


def _csv_name(hive: Path, taken: set[str]) -> str:
    """Nom de CSV lisible tiré du nom de la ruche : il doit rester parlant
    (volume, copie VSS) pour qui parcourt les sorties."""
    slug = _UNSAFE.sub("_", hive.stem).strip("_")[:100] or "shimcache"
    name = f"{slug}.csv"
    for i in itertools.count(1):
        if name not in taken:
            break
        name = f"{slug}_{i}.csv"
    taken.add(name)
    return name


def load_patterns(source: Path) -> list[str]:
    """Motifs utiles d'un fichier de motifs : lignes vides et commentaires
    retirés.

    Chainsaw lit son `--regexfile` ligne par ligne et compile TOUT, y compris
    un `# commentaire` — qui devient un motif tordu, silencieusement. Le fichier
    livré est donc documenté pour l'exploitant, et nettoyé ici pour l'outil.
    """
    try:
        lignes = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log(f"[warn] motifs illisibles ({source}) : {exc}")
        return []
    return [
        ligne.strip() for ligne in lignes
        if ligne.strip() and not ligne.lstrip().startswith("#")
    ]


def _write_patterns(motifs: list[str], dest: Path) -> Path:
    """Écrit les motifs nettoyés là où Chainsaw les lira."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(motifs) + "\n", encoding="utf-8")
    return dest


def _header(csv_path: Path) -> str:
    try:
        with csv_path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _run(argv: list[str], timeout: int) -> tuple[bool, str]:
    """Lance Chainsaw. Renvoie (réussi, dernière(s) ligne(s) d'erreur)."""
    log(f"[cmd] {' '.join(argv)}")
    try:
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        sortie = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, " / ".join(sortie[-4:]) or f"code {proc.returncode}"
    return True, ""


def analyse(
    hives_dir: Path,
    out_dir: Path,
    *,
    chainsaw: Path,
    patterns: Path | None = None,
    timeout: int = 1800,
) -> int:
    """Lance Chainsaw sur chaque ruche SYSTEM trouvée. Renvoie le nombre de
    ruches analysées avec succès."""
    fichiers = sorted(p for p in hives_dir.glob("*.hve") if p.is_file())
    systems = system_hives(hives_dir)
    amcache = amcache_hive(hives_dir)

    # Un nom qui ressemble à une ruche sans en être une : on le dit, et on
    # passe. Donner ce fichier à Chainsaw ne produirait qu'une erreur de
    # parsing incompréhensible.
    ecartes = [
        p for p in fichiers
        if hive_kind(p.name) == "SYSTEM" and not is_hive_file(p)
    ]
    for p_ecarte in ecartes:
        log(f"[warn] {p_ecarte.name} : pas une ruche (signature `regf` absente) — ignoré")

    out_dir.mkdir(parents=True, exist_ok=True)

    if not systems:
        log(f"[erreur] aucune ruche SYSTEM exploitable dans {hives_dir} "
            f"({len(fichiers)} fichier(s) extrait(s), {len(ecartes)} écarté(s) "
            f"faute de signature)")
        return 0

    motifs: Path | None = None
    if patterns is None or not patterns.is_file():
        # La chronologie sortira quand même, mais sans point d'ancrage tiré des
        # chemins : beaucoup moins d'entrées datées.
        log(f"[warn] aucun fichier de motifs ({patterns}) : aucune date "
            "d'insertion ne sera déduite des chemins, seulement des paires "
            "shimcache/Amcache si l'Amcache est présent")
    else:
        utiles = load_patterns(patterns)
        if utiles:
            motifs = _write_patterns(utiles, out_dir / ".shimcache_patterns.txt")
            log(f"{len(utiles)} motif(s) d'ancrage chargés depuis {patterns.name}")
        else:
            log(f"[warn] {patterns} ne contient aucun motif exploitable")

    log(f"{len(systems)} ruche(s) SYSTEM à analyser"
        + (f", appariées à {amcache.name}" if amcache else ", sans Amcache (pas d'appariement)"))

    taken: set[str] = set()
    reussies = 0
    derniere_erreur = ""

    sans_enrichissement = 0

    for hive in systems:
        cible = out_dir / _csv_name(hive, taken)
        base = [str(chainsaw), "analyse", "shimcache", str(hive), _OUTPUT_FLAG, str(cible)]
        if motifs is not None:
            base += [_PATTERNS_FLAG, str(motifs)]

        # L'Amcache est un bonus : il ajoute des ancres d'horodatage. Certaines
        # entrées le font planter (`Could not get LowerCaseLongPath`), et
        # Chainsaw abandonne alors toute l'analyse. On réessaie donc sans lui
        # plutôt que de perdre la chronologie entière pour un enrichissement.
        tentatives = []
        if amcache is not None:
            tentatives.append((base + [_AMCACHE_FLAG, str(amcache), _PAIR_FLAG], True))
        tentatives.append((base, False))

        ok = False
        enrichi = False
        for argv, avec_amcache in tentatives:
            cible.unlink(missing_ok=True)   # une tentative précédente a pu écrire à moitié
            ok, erreur = _run(argv, timeout)
            if ok:
                enrichi = avec_amcache
                break
            derniere_erreur = erreur
            if avec_amcache:
                log(f"[warn] {hive.name} : Chainsaw n'a pas exploité l'Amcache "
                    f"({erreur}) — nouvelle tentative sans enrichissement")
            else:
                log(f"[warn] {hive.name} : chainsaw a échoué ({erreur})")

        if not ok:
            continue
        if not cible.exists():
            log(f"[warn] {hive.name} : chainsaw n'a produit aucun fichier")
            continue

        reussies += 1
        if amcache is not None and not enrichi:
            sans_enrichissement += 1
        # L'en-tête réel sert à régler `_DETECTION_COLUMNS` dans
        # app/search/ingest.py : on le met dans le journal du run.
        log(f"  + {cible.name} — colonnes : {_header(cible) or '(vide)'}")

    if sans_enrichissement:
        log(f"[warn] {sans_enrichissement}/{len(systems)} ruche(s) analysées sans "
            "l'Amcache : les ancres viennent des seuls motifs de chemins, pas des "
            "paires d'horodatages")

    if reussies == 0:
        log("[erreur] aucune ruche analysée. Si Chainsaw refuse les arguments, "
            f"vérifier `{chainsaw} analyse shimcache --help` (le binaire n'est "
            "pas dans le PATH du conteneur, il faut son chemin complet) : "
            f"dernière erreur = {derniere_erreur or 'inconnue'}")
    return reussies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.registry_shimcache")
    parser.add_argument("hives", type=Path, help="dossier des ruches extraites")
    parser.add_argument("dest", type=Path, help="dossier de sortie (un CSV par ruche SYSTEM)")
    parser.add_argument("--chainsaw", type=Path, required=True, help="binaire chainsaw")
    parser.add_argument("--patterns", type=Path, default=None,
                        help="fichier de motifs (--regexfile), ignoré s'il est absent")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)

    if not args.chainsaw.exists():
        print(f"[erreur] binaire chainsaw introuvable ({args.chainsaw})", file=sys.stderr)
        return 2
    if not args.hives.is_dir():
        print(f"[erreur] dossier de ruches introuvable ({args.hives})", file=sys.stderr)
        return 2

    reussies = analyse(
        args.hives, args.dest,
        chainsaw=args.chainsaw, patterns=args.patterns, timeout=args.timeout,
    )
    return 0 if reussies else 1


if __name__ == "__main__":
    raise SystemExit(main())
