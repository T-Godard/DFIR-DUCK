"""Configuration centrale de l'application.

Source principale : `app/config.yaml`. Priorité, du plus fort au plus faible :
variables d'environnement > `.env` > `config.yaml` > valeurs par défaut
ci-dessous. Le `.env` sert d'abord à docker compose (image, ports, limites,
versions d'outils) ; il permet aussi de surcharger ponctuellement n'importe
quelle clé de `config.yaml` sans modifier ce fichier.

Ce fork n'orchestre **aucun conteneur Docker** : les outils d'analyse sont
exécutés comme des sous-processus locaux (voir `app/execution/`). L'application
elle-même tourne dans une image Docker unique qui embarque son code ET ces
outils avec leurs dépendances (voir `Dockerfile`) — d'où les chemins
`/opt/tools/...` par défaut.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

CONFIG_FILE = Path(__file__).with_name("config.yaml")

# Marqueur des fichiers en cours d'écriture dans `_pending/` (ex:
# "Archive.7z.part-a1b2c3d4"). `app/upload/service.py` nomme ainsi ses fichiers
# temporaires et `app/stores/acquisitions.py` les ignore tant qu'ils ne sont pas
# renommés atomiquement vers leur nom final — évite qu'un gros upload en cours
# (plusieurs Go, donc plusieurs ticks de l'orchestrateur) soit classifié /
# déplacé prématurément.
UPLOAD_TEMP_INFIX = ".part-"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        yaml_file=CONFIG_FILE,
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    # --- Répertoire de données -------------------------------------------------
    # Monté en volume sur l'hôte (voir docker-compose.yml). En conteneur :
    # /app/data (fixé par le Dockerfile). En dev local : passer DATA_DIR=./data.
    data_dir: Path = Path("/app/data")

    # --- Montages éclatés (déploiement sur une infra partagée) ----------------
    # Par défaut tout vit sous `data_dir`. Ces deux clés permettent de pointer
    # ailleurs les deux dossiers qui portent le volume : le pool d'arrivée des
    # collectes et les sorties d'outils. Chemins absolus, vide = valeur dérivée.
    # En Docker, on peut aussi bien monter les dossiers hôtes directement sur
    # `/app/data/uploads/_pending` et `/app/data/output` (voir docker-compose).
    pending_dir_override: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("pending_dir", "PENDING_DIR", "pending_dir_override"),
    )
    output_dir_override: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("output_dir", "OUTPUT_DIR", "output_dir_override"),
    )

    # Que fait l'application des fichiers trouvés dans `pending_dir` :
    #   move — elle les déplace dans l'acquisition (défaut, le pool lui
    #          appartient et se vide au fur et à mesure) ;
    #   copy — elle les copie et ne touche à rien : le pool appartient à
    #          quelqu'un d'autre (partage SMB alimenté par un serveur
    #          d'upload, dossier déposé par un autre service). Ce qui a déjà
    #          été consommé est mémorisé dans `data/pending_consumed.json`.
    pending_consume: Literal["move", "copy"] = "move"
    # Durée pendant laquelle un fichier du pool doit rester inchangé (taille et
    # date) avant d'être pris : sans cela, une copie en cours serait classée à
    # moitié écrite. Les uploads faits par l'application sont atomiques et n'en
    # ont pas besoin ; un producteur externe, si. Vide = 15 s en mode `copy`,
    # 0 en mode `move`.
    pending_stable_seconds: int | None = None

    # --- Traitements activés --------------------------------------------------
    # Quels outils l'orchestrateur a le droit de lancer. Une entrée absente vaut
    # « activé » : un outil ajouté plus tard fonctionne sans toucher à cette
    # liste. Un outil désactivé ne démarre ni tout seul ni par « Forcer », et ne
    # retient pas la suite du pipeline.
    #
    # Réglable depuis l'onglet Paramètres, qui persiste le choix dans
    # `data/preferences.json` — celui-ci prime alors sur cette pré-configuration
    # (voir app/stores/preferences.py).
    tools_enabled: dict[str, bool] = {}

    # --- Dépôt d'acquisitions via l'interface web ----------------------------
    # À false, l'UI n'expose plus de bouton de dépôt et les routes d'upload
    # répondent 403. Le dépôt manuel dans `data/uploads/_pending/` continue
    # d'être pris en charge par l'orchestrateur : c'est le chemin à utiliser
    # quand l'instance est exposée mais que le dépôt doit rester hors bande.
    web_uploads_enabled: bool = True

    # --- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    # Taille et rotation des fichiers de log applicatifs (app.log / app.jsonl).
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 10

    # --- Orchestrateur / exécution des outils --------------------------------
    orchestrator_tick_seconds: int = 10
    # Nombre de runs d'outils exécutés simultanément. Une acquisition complète
    # déclenche jusqu'à 5 runs (OGRE, evtx-extract, puis Chainsaw/Hayabusa/
    # Zircolite) — 4 permet d'en paralléliser l'essentiel.
    max_concurrent_runs: int = 4
    # Au-delà, un run est tué (SIGTERM puis SIGKILL) et marqué "failed".
    run_timeout_seconds: int = 24 * 3600
    # Limite d'adresse virtuelle par run via RLIMIT_AS, en Mo. 0 = désactivé :
    # la vraie limite est alors `mem_limit` du conteneur (docker-compose.yml).
    # À laisser à 0 sauf besoin précis — RLIMIT_AS est brutal avec les
    # programmes qui réservent beaucoup de mémoire virtuelle.
    run_memory_limit_mb: int = 0
    # Politesse d'ordonnancement des sous-processus (0 = normal, 19 = max).
    run_nice: int = 10
    # Nombre de cœurs CPU sur lesquels un run est autorisé à tourner (affinité
    # CPU). 0 = tous les cœurs. C'est une limite dure : un run bridé à N cœurs
    # ne peut pas dépasser N × 100 % de CPU. Linux uniquement.
    run_cpu_cores: int = 0
    # Garde-fou anti-emballement : temps CPU total (en secondes) qu'un run peut
    # consommer avant d'être tué (RLIMIT_CPU). 0 = désactivé. Linux uniquement.
    run_cpu_seconds: int = 0
    # Intervalle d'échantillonnage CPU/RAM d'un run en cours (0 = pas de suivi).
    run_metrics_interval_seconds: float = 5.0

    # --- Historique de performance (graphique de l'onglet Overview) ---------
    # Échantillonnage CPU/RAM du conteneur, indépendant des runs — alimente
    # data/logs/resources.jsonl et le petit graphique Overview.
    resource_sample_interval_seconds: int = 60
    # Fenêtre minimale de mesure des taux (CPU du conteneur, débit réseau).
    # Ce sont des dérivées de compteurs noyau : sur une fenêtre trop courte, la
    # granularité de `cpu.stat` fait dépasser le plafond (1000 % lus sur 8
    # cœurs). L'affichage peut être plus rapide que la mesure — les lectures
    # intermédiaires reçoivent la dernière valeur calculée. Un peu sous la
    # cadence d'affichage, pour qu'un rafraîchissement à 1 s tombe presque
    # toujours sur une mesure neuve — et déjà cent fois au-dessus du bruit.
    resource_rate_window_seconds: float = 0.75
    # Cadence de rafraîchissement de la carte « Ressources du conteneur » en
    # mode instantané, en millisecondes. Descendre sous
    # `resource_rate_window_seconds` ne rend pas les taux plus frais.
    resource_refresh_ms: int = 1000
    resource_history_days: int = 7

    # --- DFIR-OGRE (installé dans l'image, venv dédié) ----------------------
    ogre_bin: Path = Path("/opt/tools/dfir-ogre/venv/bin/dfir-ogre")
    # Config de base = celle livrée avec le dépôt dfir-ogre (on suit l'amont
    # plutôt que d'en maintenir une copie). Seules les clés de dossiers sont
    # surchargées par run (voir app/tools/ogre.py).
    ogre_base_config: Path = Path("/opt/tools/dfir-ogre/src/configuration/ogre.yaml")
    ogre_plugins_dir: Path = Path("/opt/tools/dfir-ogre/plugins/configuration")
    # Mot de passe des archives internes DFIR-ORC (valeur par défaut ANSSI).
    # Sert aussi à l'extraction des artefacts (app/artifact_extract.py).
    ogre_inner_archive_password: str = "avproof"

    # --- Outils d'analyse EVTX (installés dans l'image) --------------------
    # Règles SigmaHQ brutes, partagées par Chainsaw (-s) et Zircolite (--ruleset).
    sigma_rules_dir: Path = Path("/opt/tools/sigma/rules/windows")
    # Chainsaw : binaire + mapping Sigma + règles natives.
    chainsaw_bin: Path = Path("/opt/tools/chainsaw/chainsaw")
    chainsaw_mapping: Path = Path("/opt/tools/chainsaw/mappings/sigma-event-logs-all.yml")
    chainsaw_rules_dir: Path = Path("/opt/tools/chainsaw/rules")
    # Règles natives Chainsaw pour la MFT (`kind: mft`) : les règles SigmaHQ
    # décrivent des journaux d'événements et ne s'y appliquent pas.
    chainsaw_mft_rules_dir: Path = Path("/opt/tools/chainsaw/rules/mft")
    # Motifs pour `chainsaw analyse shimcache` (--regexfile) : une expression
    # régulière par ligne. Elles désignent les entrées dont la date de
    # modification vaut date d'insertion — les points d'ancrage qui datent la
    # chronologie. Livré avec l'application : l'archive de release de Chainsaw
    # ne l'embarque pas.
    shimcache_patterns: Path = Path(__file__).resolve().parent / "tools" / "shimcache_patterns.txt"
    # Hayabusa : binaire + règles/config intégrées.
    hayabusa_bin: Path = Path("/opt/tools/hayabusa/hayabusa")
    hayabusa_rules_dir: Path = Path("/opt/tools/hayabusa/rules")
    hayabusa_config_dir: Path = Path("/opt/tools/hayabusa/rules/config")
    # Zircolite : venv dédié + script.
    zircolite_python: Path = Path("/opt/tools/zircolite/venv/bin/python")
    zircolite_script: Path = Path("/opt/tools/zircolite/src/zircolite.py")
    # Binaire 7z pour l'extraction EVTX (p7zip-full).
    sevenzip_bin: str = "7z"

    # --- Chemins dérivés ---------------------------------------------------
    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def pending_dir(self) -> Path:
        """Pool d'arrivée des collectes. Peut vivre hors de `data_dir` (partage
        réseau monté par l'infra, volume dédié...) — voir `pending_consume`."""
        return self.pending_dir_override or (self.uploads_dir / "_pending")

    @property
    def output_dir(self) -> Path:
        """Sorties d'outils. Souvent le plus gros volume : il mérite parfois son
        propre disque, indépendant du reste de `data_dir`."""
        return self.output_dir_override or (self.data_dir / "output")

    def tool_output_dir(self, key: str, acquisition_dir: str) -> Path:
        """`data/output/<key>/<dossier d'acquisition>` — sortie d'un outil.

        Le second segment est `Acquisition.storage_name` : le nom de la machine
        quand on le connaît, l'identifiant sinon. Jamais l'identifiant brut
        d'une acquisition récente — le parcours manuel des sorties en dépend.
        `key` : "ogre", "EVTX", "MFT", "Registry", "evtx-chainsaw",
        "evtx-hayabusa", "evtx-zircolite", "mft-chainsaw", "registry-chainsaw"."""
        return self.output_dir / key / acquisition_dir

    def mft_dir(self, acquisition_dir: str) -> Path:
        """MFT tirées des archives, entrée de `mft-chainsaw`."""
        return self.tool_output_dir("MFT", acquisition_dir)

    def registry_dir(self, acquisition_dir: str) -> Path:
        """Ruches tirées des archives, entrée de `registry-chainsaw`."""
        return self.tool_output_dir("Registry", acquisition_dir)

    def evtx_dir(self, acquisition_dir: str) -> Path:
        """EVTX extraits d'une acquisition (entrée des 3 détecteurs)."""
        return self.tool_output_dir("EVTX", acquisition_dir)

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def investigation_file(self) -> Path:
        return self.data_dir / "investigation.json"

    @field_validator("pending_dir_override", "output_dir_override", mode="before")
    @classmethod
    def _ignore_empty_path(cls, value):
        """`pending_dir: ""` dans le YAML (ou `PENDING_DIR=` dans le .env) veut
        dire « non défini », pas le répertoire courant — `Path("")` vaut `.`,
        ce qui enverrait toutes les collectes à la racine de l'application."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def tool_enabled(self, tool: str) -> bool:
        """L'orchestrateur peut-il lancer cet outil ? Inconnu = oui."""
        return bool(self.tools_enabled.get(tool, True))

    @property
    def pending_stability_window(self) -> int:
        """Fenêtre de stabilité effective (voir `pending_stable_seconds`)."""
        if self.pending_stable_seconds is not None:
            return max(0, self.pending_stable_seconds)
        return 15 if self.pending_consume == "copy" else 0

    def ensure_directories(self) -> None:
        for d in (
            self.data_dir,
            self.uploads_dir,
            self.pending_dir,
            self.output_dir,
            self.runs_dir,
            self.logs_dir,
        ):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Cas courant d'un déploiement à volumes éclatés : le montage
                # n'est pas en place. Mieux vaut le dire que laisser une
                # trace d'exécution brute au démarrage.
                raise RuntimeError(
                    f"répertoire inaccessible : {d} ({exc}). Vérifier le montage "
                    "correspondant (data_dir, pending_dir, output_dir)."
                ) from exc


settings = Settings()
settings.ensure_directories()

# --- Constantes de module (raccourcis d'import) ---------------------------------
DATA_DIR = settings.data_dir
UPLOADS_DIR = settings.uploads_dir
PENDING_DIR = settings.pending_dir
OUTPUT_DIR = settings.output_dir
RUNS_DIR = settings.runs_dir
LOGS_DIR = settings.logs_dir
INVESTIGATION_FILE = settings.investigation_file
