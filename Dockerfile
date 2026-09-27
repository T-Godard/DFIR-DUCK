# syntax=docker/dockerfile:1
#
# Image unique : embarque le code de l'application ET les outils d'analyse
# (DFIR-OGRE + plugins, Chainsaw, Hayabusa, Zircolite, règles SigmaHQ) avec
# leurs dépendances.
#
# Au démarrage, supervisord lance l'application (voir supervisord.conf et
# entrypoint.sh).
#
# Les versions d'outils sont pinnées dans les `ARG` ci-dessous, et nulle part
# ailleurs : c'est la seule source.

############################################################
# Stage 1 — Outils d'analyse DFIR (venv dédié, isolé de l'app)
############################################################
FROM python:3.13-trixie AS tools

ARG OGRE_REF=main
ARG OGRE_PLUGINS_REF=main
ARG CHAINSAW_REF=v2.16.5
ARG HAYABUSA_REF=v4.0.0
ARG HAYABUSA_VERSION=4.0.0
ARG ZIRCOLITE_REF=v3.8.1
ARG SIGMA_REF=r2026-07-01
ARG LIBESEDB_SYS_REF=9dd92be0b2fd694a5cfa060397c962bc2ea1ba85
ARG LIBESEDB_PY_REF=20260704

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential pkg-config unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

RUN pip install --no-cache-dir uv

# Patch libesedb RUST
RUN git clone https://github.com/sunsetkookaburra/rust-libesedb.git \
      /opt/tools/libesedb-sys-patched \
 && git -C /opt/tools/libesedb-sys-patched checkout "${LIBESEDB_SYS_REF}" \
 && rm -rf /opt/tools/libesedb-sys-patched/.git
RUN cat > /opt/tools/libesedb-sys-patched/libesedb-sys/patches/0001-mask-reserved-available-page-tag-bits.patch <<'EOF'
--- libesedb/libesedb_page_header.c
+++ libesedb/libesedb_page_header.c
@@ -239,7 +239,12 @@
 	byte_stream_copy_to_uint16_little_endian(
 	 ( (esedb_page_header_t *) data )->available_page_tag,
 	 page_header->available_page_tag );
 
+	/* As of format 0x620 revision 0x122 the upper 4 bits of the (first)
+	 * available page tag are reserved and must be ignored.
+	 * See: libyal/libesedb issues #75, #78 - fixed upstream in commit 768a047.
+	 */
+	if( io_handle->format_revision >= 0x00000122UL )
+	{
+		page_header->available_page_tag &= 0x0fff;
+	}
+
 	byte_stream_copy_to_uint32_little_endian(
 	 ( (esedb_page_header_t *) data )->page_flags,
 	 page_header->flags );
EOF
RUN mkdir -p /root/.cargo \
 && printf '[patch.crates-io]\nlibesedb-sys = { path = "/opt/tools/libesedb-sys-patched/libesedb-sys" }\n' \
      >> /root/.cargo/config.toml

# --- DFIR-OGRE (installation alignée sur dfir-ogre/docker/Dockerfile amont) ----
WORKDIR /opt/tools/dfir-ogre
RUN git clone https://github.com/ANSSI-FR/dfir-ogre-plugin-windows.git plugins \
 && git -C plugins checkout "${OGRE_PLUGINS_REF}" \
 && git clone https://github.com/ANSSI-FR/dfir-ogre.git src \
 && git -C src checkout "${OGRE_REF}"
RUN uv venv /opt/tools/dfir-ogre/venv \
 && umask 022 \
 && VIRTUAL_ENV=/opt/tools/dfir-ogre/venv uv pip install ./src
RUN /opt/tools/dfir-ogre/venv/bin/dfir-ogre --version > /opt/tools/dfir-ogre/VERSION 2>&1 || true

# Patch libesedb PYTHON
RUN VIRTUAL_ENV=/opt/tools/dfir-ogre/venv uv pip install "libesedb-python==${LIBESEDB_PY_REF}"

# --- Règles SigmaHQ (partagées par Chainsaw -s et Zircolite --ruleset) --------
RUN git clone --depth 1 -b "${SIGMA_REF}" https://github.com/SigmaHQ/sigma.git /opt/tools/sigma \
 && echo "${SIGMA_REF}" > /opt/tools/sigma/VERSION \
 && rm -rf /opt/tools/sigma/.git

# --- Chainsaw (binaire + mapping Sigma + règles natives) ----------------------
RUN curl -fsSL -o /tmp/cs.zip \
      "https://github.com/WithSecureLabs/chainsaw/releases/download/${CHAINSAW_REF}/chainsaw_all_platforms%2Brules.zip" \
 && unzip -q /tmp/cs.zip -d /tmp/cs && rm /tmp/cs.zip \
 && mkdir -p /opt/tools/chainsaw && cp -a /tmp/cs/chainsaw/. /opt/tools/chainsaw/ && rm -rf /tmp/cs \
 && mv /opt/tools/chainsaw/chainsaw_x86_64-unknown-linux-gnu /opt/tools/chainsaw/chainsaw \
 && chmod +x /opt/tools/chainsaw/chainsaw \
 && /opt/tools/chainsaw/chainsaw --version > /opt/tools/chainsaw/VERSION 2>&1 || true

# --- Hayabusa (binaire + règles/config intégrées) ----------------------------
RUN curl -fsSL -o /tmp/hb.zip \
      "https://github.com/Yamato-Security/hayabusa/releases/download/${HAYABUSA_REF}/hayabusa-${HAYABUSA_VERSION}-lin-x64-gnu.zip" \
 && mkdir -p /opt/tools/hayabusa && unzip -q /tmp/hb.zip -d /opt/tools/hayabusa && rm /tmp/hb.zip \
 && mv "/opt/tools/hayabusa/hayabusa-${HAYABUSA_VERSION}-lin-x64-gnu" /opt/tools/hayabusa/hayabusa \
 && chmod +x /opt/tools/hayabusa/hayabusa \
 && /opt/tools/hayabusa/hayabusa --version > /opt/tools/hayabusa/VERSION 2>&1 || true

# --- Zircolite (venv dédié, règles Sigma consommées à la volée) --------------
RUN git clone --depth 1 -b "${ZIRCOLITE_REF}" https://github.com/wagga40/Zircolite.git /opt/tools/zircolite/src \
 && uv venv /opt/tools/zircolite/venv \
 && VIRTUAL_ENV=/opt/tools/zircolite/venv uv pip install -r /opt/tools/zircolite/src/requirements.txt \
 && /opt/tools/zircolite/venv/bin/python /opt/tools/zircolite/src/zircolite.py --version \
      > /opt/tools/zircolite/VERSION 2>&1 || true

############################################################
# Stage 2 — Dépendances Python de l'application
############################################################
FROM python:3.13-slim-trixie AS appdeps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

############################################################
# Stage 3 — Image finale
############################################################
FROM python:3.13-slim-trixie AS runtime

# 7zip : `7z` moderne, utilisé par app/artifact_extract.py pour l'extraction
# sélective des artefacts dans les archives DFIR-ORC.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini gosu 7zip \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 1000 dfir

COPY --from=appdeps /install /usr/local
COPY --from=tools /opt/tools /opt/tools
# --- Contenu applicatif : le code, et le strict nécessaire au démarrage --------
# `WORKDIR` crée /app en 0755 avant tout COPY : un `COPY --chmod=...` qui créerait
# lui-même /app lui appliquerait ce mode, et l'utilisateur dfir ne pourrait plus
# entrer dans le répertoire.
WORKDIR /app
COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
COPY app/ /app/app/
COPY supervisord.conf /app/supervisord.conf

RUN mkdir -p /app/data \
 && chmod 0755 /app \
 && chown -R dfir:dfir /app /opt/tools

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

EXPOSE 8000

# tini = PID 1 ; entrypoint.sh aligne les droits du volume puis passe la main
# à supervisord.
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
CMD ["supervisord", "-c", "/app/supervisord.conf"]
