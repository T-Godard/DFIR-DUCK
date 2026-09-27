#!/usr/bin/env bash
# Construction de l'image et contrôles de l'hôte.
#
# Les versions d'outils ne sont écrites qu'à un seul endroit, les `ARG` du
# Dockerfile. Ce script les y lit pour les afficher, il ne les recopie pas.
set -euo pipefail
cd "$(dirname "$0")"

# Version pinnée d'un outil, lue dans le Dockerfile.
pin() { sed -n "s/^ARG $1=//p" Dockerfile | head -n 1; }

echo "Build de l'image — outils inclus :"
echo "  DFIR-OGRE @ $(pin OGRE_REF) (plugins @ $(pin OGRE_PLUGINS_REF))"
echo "  Chainsaw  @ $(pin CHAINSAW_REF)"
echo "  Hayabusa  @ $(pin HAYABUSA_REF)"
echo "  Zircolite @ $(pin ZIRCOLITE_REF)"
echo "  SigmaHQ   @ $(pin SIGMA_REF)"
echo "  libesedb  @ $(pin LIBESEDB_PY_REF)"
echo

docker compose build "$@"

echo
echo "Image construite. Démarrage : docker compose up -d"
