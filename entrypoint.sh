#!/bin/sh
# PID 1 = tini (voir ENTRYPOINT). Ce script tourne en root le temps de
# s'assurer que le volume de données est accessible à l'utilisateur `dfir`,
# puis laisse la main au process applicatif sans privilèges.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR"

# Le volume monté depuis l'hôte appartient souvent à root : on l'aligne sur
# `dfir` (uid 1000) pour que l'app puisse y écrire. Best-effort : sur certains
# montages (NFS, CIFS) le chown échoue sans que ce soit bloquant.
if [ "$(id -u)" = "0" ]; then
    chown dfir:dfir "$DATA_DIR" 2>/dev/null || true
    find "$DATA_DIR" -maxdepth 2 ! -user dfir -exec chown dfir:dfir {} + 2>/dev/null || true

    if [ "$1" = "supervisord" ]; then
        # supervisord reste root (écriture sur la sortie standard du conteneur) ;
        # son programme tourne sous dfir (voir supervisord.conf).
        exec "$@"
    fi

    exec gosu dfir "$@"
fi

exec "$@"
