#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAIsterMind — l'app au-dessus des binaires (cockpit launcher).

PRINCIPE (cadrage V3, note v2) :
  - L'app NE remplace PAS les orchestrateurs : elle les découvre (orchestrators.json),
    les lance dans une session tmux dédiée 'mm-run-<hash-projet>', lit leur écran
    (capture-pane) et l'état du run dans les FICHIERS du projet (spec.md,
    blackboard.yaml, sentinelles), et répond aux portes y/n par send-keys.
  - Symétrie assumée : l'app pilote les orchestrateurs exactement comme les
    orchestrateurs pilotent leur agent IA (tmux + fichiers + sentinelles).
  - AUCUN état applicatif : fermer l'app ne tue pas un run (il vit dans tmux) ;
    au redémarrage, l'app retrouve les runs par le nom des sessions 'mm-run-*'.
    Le seul fichier de l'app est un registre de chemins de projets (confort d'UI).
  - Les binaires restent dans le dossier d'installation et sont lancés avec la
    RACINE DU PROJET comme répertoire courant : tous leurs chemins sont relatifs
    au cwd (vérifié sur les binaires v2). « Équiper un projet » ne copie à la
    racine du projet que ce que les orchestrateurs y attendent : .agents/,
    les artefacts du harness choisi (.opencode/ ou .codex/ + AGENTS.md) et un
    gabarit need.md.
  - Le besoin se décrit DANS l'app : la Bibliothèque commence par un bloc de texte
    (étape 1) dont le contenu est écrit tel quel dans le need.md du projet — le
    fichier reste le contrat des binaires, mais l'utilisateur n'a plus à le
    connaître ni à le créer à la main avant de choisir un orchestrateur (étape 2).
  - Portes en MODE ÉCRAN (contrat v1, zéro modification des binaires) : les
    libellés exacts des prompts vivent dans le manifeste orchestrators.json,
    jamais dans ce code. Le mode sentinelle (.gates/) viendra avec les binaires
    V3, ce fichier gardera le mode écran en repli.

SÉCURITÉ : serveur strictement 127.0.0.1 + jeton de session (URL au premier
chargement, cookie SameSite=Strict ensuite) + contrôle de l'en-tête Host
(anti DNS-rebinding). Pas d'exposition LAN en V3.

DISTRIBUTION : fichier unique, stdlib seule (PyYAML utilisé s'il est présent,
repli mécanique sinon) — compilable Nuitka comme les orchestrateurs, aucun
data-file. Le dossier d'installation se déduit de sys.argv[0] (jamais de
__file__, incompatible onefile).

ZÉRO GESTE TECHNIQUE (V3.0) : l'app remet elle-même ses moteurs en état d'exécution
— bit exécutable perdu par un zip ou une copie via l'explorateur, quarantaine
Gatekeeper d'un téléchargement navigateur — au démarrage et avant chaque lancement :
aucun chmod/xattr n'est jamais demandé à l'utilisateur (heal_engine_binaries).
Lancée sans terminal (double-clic : bundle .app macOS, .desktop, .bat WSL), l'app
journalise dans .mm-app/launcher.log et s'éteint par le bouton ⏻ de « Statut &
réglages » (POST /api/quit) — les runs, eux, vivent dans tmux et y survivent.
En dev, un moteur peut contenir les sources : binaire absent → repli sur
<binary>.py au même endroit (le shebang fait le reste).
"""

import fnmatch
import hashlib
import html as html_mod
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import yaml  # embarqué dans les builds Nuitka de l'usine ; repli mécanique sinon
except ImportError:  # pragma: no cover
    yaml = None

# ─── CONFIG ───────────────────────────────────────────────────────────────────

APP_VERSION          = "3.0.0"
APP_CONTRACT_VERSION = 1                     # version du contrat app <-> binaires comprise par cette app
HOST                 = "127.0.0.1"           # localhost STRICT (décision actée)
PORT                 = int(os.environ.get("MM_APP_PORT", "8748"))

# Dossier d'installation = là où vit ce binaire (sys.argv[0] : seul chemin fiable
# sous Nuitka onefile ; __file__ pointerait dans le répertoire temporaire d'extraction).
INSTALL_DIR   = os.path.dirname(os.path.abspath(sys.argv[0]))

# Un « moteur » = un dossier contenant ce manifeste + les binaires qu'il déclare + les
# sources d'équipement (.agents/ + les artefacts de chaque harness). Cherché dans le
# dossier de l'app (installation à plat, historique) puis dans ses sous-dossiers
# immédiats (installation
# « hub » : l'app au-dessus, point d'entrée unique ; les moteurs en dessous, immobiles —
# leurs couplages tmux/harness/skills interdisent de les déplacer, c'est l'app qui bouge).
MANIFEST_NAME     = "orchestrators.json"
ENGINE_FLAT_LABEL = "."               # label du moteur « à plat » (le dossier de l'app)

# Espace de travail de l'app (registre de projets + verrou d'instance + journal).
# Ce ne sont PAS des états de run : l'état des runs vit dans les projets et dans tmux.
# Si le dossier d'installation n'est pas inscriptible (lecture seule, /opt, clé USB),
# resolve_app_dir() replie ces chemins sur ~/.maistermind-app au démarrage.
APP_DIR       = os.path.join(INSTALL_DIR, ".mm-app")
REGISTRY_FILE = os.path.join(APP_DIR, "projects.json")
LOCK_FILE     = os.path.join(APP_DIR, "app.lock")
LOG_FILE      = os.path.join(APP_DIR, "app.log")
LOG_MAX_BYTES = 1_000_000          # rotation simple du journal : un seul fichier d'archive (.1)
EQUIP_BACKUPS_KEPT = 3             # sauvegardes .bak-* conservées par dossier équipé

# Équipement copié dans la racine du projet cible (contrat des orchestrateurs v2 :
# tout est lu relativement au cwd). '.agents/' est COMMUN aux deux harness — les skills
# du pipeline sont neutres et doivent le rester ; le reste dépend du harness choisi.
EQUIP_DIRS_COMMON = [".agents"]

# Ce que l'app sait du harness, et rien de plus : préflight, artefacts d'équipement,
# libellés, préfixe de session. Elle n'importe PAS 'engine/mm_runner.py' — elle doit
# rester mono-fichier stdlib, compilable Nuitka en onefile. Cette table est donc la
# contrepartie assumée du registre RUNNERS du moteur : ajouter un harness se fait des
# DEUX côtés, et tools/check_variants_sync.py vérifie que les artefacts suivent.
HARNESSES = {
    "opencode": {
        "label":          "OpenCode",
        "binary":         "opencode",
        "dirs":           [".opencode"],          # dossiers copiés à l'équipement
        "files":          [],                     # fichiers copiés à l'équipement
        "session_prefix": "oc-",                  # préfixe de session orchestrateur -> agent
        "config":         ".opencode/opencode.json",
        "auth_cmd":       ["opencode", "auth", "list"],
        "install_hint":   "https://opencode.ai/docs",
        "auth_hint":      "opencode auth login",
    },
    "codex": {
        "label":          "Codex",
        "binary":         "codex",
        "dirs":           [".codex"],
        "files":          ["AGENTS.md"],          # consignes « usine » lues à la racine
        "session_prefix": "cx-",
        "config":         ".codex/config.toml",
        "auth_cmd":       ["codex", "login", "status"],
        "install_hint":   "npm install -g @openai/codex",
        "auth_hint":      "codex login",
    },
}
# Harness proposé quand RIEN ne tranche (projet vierge, aucun binaire installé) : le
# harness historique, pour qu'un dépôt équipé par l'ancien fork OpenCode se comporte comme avant par défaut.
DEFAULT_HARNESS = "opencode"

NEED_FILE  = "need.md"

NEED_TEMPLATE = """# Besoin fonctionnel

<!-- Décris ici ton besoin, en français, comme à un collègue :
     - le problème à résoudre et pour qui ;
     - ce que l'outil doit faire (fonctionnalités attendues) ;
     - les contraintes connues (stack imposée, fichiers à référencer, etc.).
     L'Agent PO affinera ce brief en spécification (spec.md) que tu valideras. -->
"""

NEED_TEMPLATE_ENG = """# Functional need

<!-- Describe your need here, in English, as you would to a colleague:
     - the problem to solve, and for whom;
     - what the tool must do (expected features);
     - the known constraints (imposed stack, files to reference, etc.).
     The PO Agent will refine this brief into a specification (spec.md) for you to validate. -->
"""

# Les deux gabarits sont reconnus comme « non rempli » quelle que soit la langue active.
NEED_TEMPLATES = (NEED_TEMPLATE, NEED_TEMPLATE_ENG)

# Fichiers d'état / livrables connus des orchestrateurs, lus à la racine du projet
# (usine v2 + orchestrateurs lecture seule : documentation, audits design et a11y).
STATE_FILES = ["need.md", "spec.md", "plan.md", "impact.md", "blackboard.yaml",
               "refactoring_report.md", "review_report.md", "failReport.md",
               "documentation.md", "doc_map.yaml", "a11y_map.yaml",
               "design_audit_report.md",
               "accessibility_pre_audit_report.md", "accessibility_pre_audit_summary.md",
               "skill_adapt_profile.yaml", "skill_adapt_report.md"]
# Fichiers à NOM DYNAMIQUE autorisés en lecture par /api/doc : les rapports d'arbitrage
# mid-run des orchestrateurs Yolo ('impact-phase-<id>.md'), les rapports d'audit de
# Guided-Fix ('fix_report-<uid>.md') et les propositions de l'adaptateur de skills
# ('skill_adapt-<name>.md'). Motif STRICT (nom nu, jamais de séparateur de chemin) :
# la liste reste fermée, elle gagne juste des motifs.
DOC_DYNAMIC_RE = re.compile(
    r"^(impact-phase|fix_report|skill_adapt)-[A-Za-z0-9_.-]{1,64}\.md$")
# Fichiers autorisés en LECTURE par l'API /api/doc (liste fermée : jamais de chemin libre).
DOC_WHITELIST = set(STATE_FILES)
# Fichiers autorisés en ÉCRITURE par l'éditeur intégré : les entrées de l'humain dans le
# pipeline — les cartes (doc_map, a11y_map) se corrigent avant validation, comme le
# blackboard — jamais les rapports (sorties de l'usine, lecture seule).
EDIT_WHITELIST = {"need.md", "spec.md", "plan.md", "impact.md", "blackboard.yaml",
                  "doc_map.yaml", "a11y_map.yaml",
                  # Adaptateur de skills : le profil ET les propositions se corrigent
                  # avant validation (le fichier fait foi à la porte d'écrasement).
                  "skill_adapt_profile.yaml",
                  "skill_adapt-backend-coding.md", "skill_adapt-frontend-coding.md",
                  "skill_adapt-backend-testing.md", "skill_adapt-frontend-testing.md"}
# Fichiers SUPPRIMABLES depuis l'app (bouton « Nettoyer ») : les artefacts produits par
# l'usine ET need.md — repartir de zéro, c'est aussi pouvoir repartir d'un AUTRE besoin.
# need.md est le seul fichier écrit par l'humain (le reste se régénère) : il n'est jamais
# nettoyé en silence — cleanable_present() ne le propose que s'il porte un vrai besoin, la
# confirmation le nomme, et sa corbeille de ligne permet de vider le besoin SEUL.
CLEANABLE = list(STATE_FILES)
# Sentinelles de reprise ATTACHÉES à un artefact : les fichiers sont l'état de reprise de
# l'usine, et supprimer l'un sans l'autre laisse un état incohérent — une spec régénérée
# serait crue « déjà approuvée par l'humain » (.spec_approved) et sa porte sautée.
CLEAN_SENTINELS = {
    "spec.md":               [".spec_approved", ".pipeline_spec.done"],
    "plan.md":               [".pipeline_plan.done"],
    # Une revue d'impact régénérée sans sa sentinelle serait crue « déjà approuvée » et sa
    # porte sautée — même piège que la spec (orchestrateurs Yolo).
    "impact.md":             [".impact_approved", ".pipeline_impact.done"],
    "blackboard.yaml":       [".pipeline_blackboard.done"],
    "refactoring_report.md": [".pipeline_refacto.done"],
}
# Nettoyer impact.md emporte aussi les rapports d'arbitrage mid-run du même run (motif
# serveur fermé, jamais un chemin du navigateur) : un impact-phase-<id>.md orphelin d'une
# revue disparue n'arbitre plus rien.
CLEAN_COMPANION_GLOBS = {
    "impact.md": ["impact-phase-*.md"],
    # Nettoyer le profil d'adaptation emporte les propositions de skills orphelines :
    # sans profil, une proposition n'a plus de porte pour être arbitrée.
    "skill_adapt_profile.yaml": ["skill_adapt-*.md"],
}
# DOSSIERS de constats intermédiaires des orchestrateurs lecture seule (une passe = un
# fichier). Ils sont AUSSI de l'état de reprise, et c'est le piège que le seul nettoyage
# des rapports laissait entier : « un fichier de constats conservé fait sauter sa passe »
# (Audit-Design), les passes déjà exploitables de pre_audit_a11y/ « seront sautées ».
# Refaire un audit ou une doc COMPLETS exige donc de les retirer — récursivement.
CLEANABLE_DIRS = ["doc_zones", "audit_nielsen", "pre_audit_a11y"]

# Marqueur d'équipement écrit à la racine du projet : permet de proposer une mise à jour
# des prompts quand la distro évolue (comparé au 'distro_version' du manifeste).
EQUIP_MARKER = ".mm-equip.json"

FEEDBACK_MAX_CHARS = 4000  # critic_feedback tronqué côté app (même ordre que l'usine)

# Sentinelles v2 (informatif : l'inférence d'étape repose d'abord sur fichiers + porte
# détectée + blackboard ; les sentinelles sont éphémères et peuvent évoluer entre versions).
KNOWN_SENTINELS = [".spec_approved", ".impact_approved", ".pipeline_spec.done",
                   ".pipeline_plan.done", ".pipeline_impact.done",
                   ".pipeline_blackboard.done", ".pipeline_refacto.done",
                   ".pipeline_skill_adapt.done", ".pipeline_skill_review.done"]

# Sentinelle de code de sortie, écrite par le WRAPPER de lancement (pas par les binaires) :
# sur tmux 3.4, un 'tmux kill-session' lancé depuis l'intérieur du pane juste avant l'exit
# (ce que font les orchestrateurs v2 pour fermer leur session d'agent) laisse parfois
# #{pane_dead_status} DÉFINITIVEMENT vide (reproduit : ~2 fois sur 5). Le wrapper relaie
# donc le code dans ce fichier, lu en repli — l'état du run reste dans le projet + tmux.
RUN_EXIT_SENTINEL = ".mm-run-exit"

# ─── LANGUE DES MESSAGES (app bilingue à bord) ────────────────────────────────
# Chaque requête HTTP porte sa langue (&lang=fr|eng, posé par l'UI qui suit le choix
# de l'utilisateur) dans un état PAR THREAD : les messages destinés à l'UI passent par
# L(). Hors requête (démarrage, journal), le français reste la langue par défaut.
_REQ = threading.local()


def L(fr: str, eng: str) -> str:
    return eng if getattr(_REQ, "lang", "fr") == "eng" else fr

SESSION_PREFIX    = "mm-run-"     # session tmux du run (l'app -> orchestrateur)
# Session tmux de l'orchestrateur -> agent IA : un préfixe PAR HARNESS, complété par le
# RÔLE de l'orchestrateur ('<préfixe><rôle>-<hash>' : oc-factory-, oc-proto-, oc-spec-,
# cx-tdd-, …). Le rôle n'est connu que du binaire : l'app DÉCOUVRE la session par son
# préfixe et son hash au lieu de supposer le rôle (voir agent_session_name).
AGENT_SESSION_PREFIXES = {k: h["session_prefix"] for k, h in HARNESSES.items()}

CAPTURE_LINES   = 300             # profondeur d'historique remontée par capture-pane
GATE_TAIL_LINES = 4               # la porte n'est « ouverte » que si le prompt est dans les toutes dernières lignes
PREREQ_CACHE_S  = 60

# Flux d'événements (SSE) : chaque connexion tient un thread du serveur — borné, et le
# client bascule sur son polling de repli quand la limite est atteinte (réponse 429).
SSE_MAX_CLIENTS   = 6
SSE_RUN_PERIOD_S  = 1.0           # relecture du payload run (poussé seulement s'il change)
SSE_STATE_PERIOD_S = 4.0          # battement state : rythme du polling remplacé + détection client parti

_registry_lock = threading.Lock()
_prereq_cache  = {"at": 0.0, "data": None}
_sse_lock      = threading.Lock()
_sse_clients   = 0


# ─── PETITS UTILITAIRES ───────────────────────────────────────────────────────

def project_hash(path: str) -> str:
    """Même convention que les orchestrateurs (<préfixe><rôle>-<hash>) : sha1 du cwd.
    os.getcwd() renvoie un chemin résolu -> on hache le realpath pour retrouver
    la session d'agent du même projet, quel que soit le harness."""
    return hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()[:8]


def run_session_name(path: str) -> str:
    return SESSION_PREFIX + project_hash(path)


def agent_session_name(path: str, harness: str) -> str:
    """Session tmux de l'agent IA piloté par l'orchestrateur, pour CE harness.

    Chaque orchestrateur suffixe son RÔLE entre le préfixe du harness et le hash du
    projet (oc-factory-<hash> pour l'usine, oc-proto-<hash>, oc-spec-<hash>, …) : on
    DÉCOUVRE donc la session existante parmi celles de tmux — c'est ce qui rend
    l'onglet agent vivant pour TOUS les orchestrateurs, pas seulement 'factory'.
    Sans session vivante, repli sur le nom historique (rôle 'factory'), purement
    informatif : l'UI n'affiche l'onglet actif que si la session existe."""
    prefix = AGENT_SESSION_PREFIXES[harness]
    suffix = "-" + project_hash(path)
    for session in tmux_list_sessions():
        if session.startswith(prefix) and session.endswith(suffix):
            return session
    return prefix + "factory" + suffix


def harness_of(path: str) -> str:
    """Harness d'un projet, par ordre de priorité — MÊME logique que
    resolve_runner() du moteur, redite ici parce que l'app ne l'importe pas :
      1. le marqueur d'équipement, écrit par l'app à l'équipement ;
      2. les artefacts présents (projet équipé par une version antérieure) ;
      3. le seul harness installé sur la machine ;
      4. DEFAULT_HARNESS.
    Ne lève jamais : cette valeur sert à afficher un libellé et à nommer une session."""
    marked = str(read_json(os.path.join(path, EQUIP_MARKER), {}).get("harness") or "")
    if marked in HARNESSES:
        return marked
    equipped = [k for k, h in HARNESSES.items()
                if all(os.path.isdir(os.path.join(path, d)) for d in h["dirs"])]
    if len(equipped) == 1:
        return equipped[0]
    installed = [k for k, h in HARNESSES.items() if shutil.which(h["binary"])]
    if len(installed) == 1:
        return installed[0]
    return DEFAULT_HARNESS


def equip_dirs(harness: str) -> list[str]:
    """Dossiers attendus à la racine d'un projet équipé pour ce harness."""
    return EQUIP_DIRS_COMMON + HARNESSES[harness]["dirs"]


def equip_files(harness: str) -> list[str]:
    """Fichiers attendus à la racine d'un projet équipé pour ce harness."""
    return list(HARNESSES[harness]["files"])


def is_equipped(path: str, harness: str) -> bool:
    """Un projet est équipé pour un harness quand TOUS ses artefacts sont là."""
    return (all(os.path.isdir(os.path.join(path, d)) for d in equip_dirs(harness))
            and all(os.path.isfile(os.path.join(path, f)) for f in equip_files(harness)))


def configured_model(path: str, harness: str) -> str | None:
    """Modèle épinglé pour CE projet (JSON côté OpenCode, TOML minimal côté Codex —
    même parsing que le runner du moteur, cf. correctif hérité du fork Codex). None si rien n'est
    épinglé : le harness utilise alors son modèle par défaut."""
    config = os.path.join(path, HARNESSES[harness]["config"])
    try:
        with open(config, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    if config.endswith(".json"):
        try:
            return (json.loads(raw) or {}).get("model") or None
        except ValueError:
            return None
    match = re.search(r'^\s*model\s*=\s*"([^"]*)"', raw, re.MULTILINE)
    return match.group(1) if match and match.group(1) else None


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ─── ESPACE DE TRAVAIL & JOURNAL ──────────────────────────────────────────────

def resolve_app_dir() -> bool:
    """Choisit l'espace de travail au démarrage : .mm-app/ dans le dossier
    d'installation si inscriptible, sinon repli sur ~/.maistermind-app (installation
    en lecture seule, /opt, clé USB…). Retourne True si le repli a été nécessaire."""
    global APP_DIR, REGISTRY_FILE, LOCK_FILE, LOG_FILE
    for candidate, is_fallback in ((os.path.join(INSTALL_DIR, ".mm-app"), False),
                                   (os.path.join(os.path.expanduser("~"), ".maistermind-app"), True)):
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError:
            continue
        APP_DIR       = candidate
        REGISTRY_FILE = os.path.join(candidate, "projects.json")
        LOCK_FILE     = os.path.join(candidate, "app.lock")
        LOG_FILE      = os.path.join(candidate, "app.log")
        return is_fallback
    return False  # aucun espace inscriptible : les écritures échoueront explicitement


_log_lock = threading.Lock()


def log_server_error(context: str, code: int, message: str):
    """Journal minimal des réponses d'erreur : elles partaient en silence dans le JSON,
    invisibles pour déboguer à distance. Best-effort (ne gêne jamais la réponse),
    rotation simple à LOG_MAX_BYTES, jeton de session masqué (un fichier ne doit
    pas l'exposer)."""
    context = re.sub(r"([?&]t=)[^&\s]+", r"\1***", context)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] HTTP {code} {context} — {message}\n"
    with _log_lock:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                os.replace(LOG_FILE, LOG_FILE + ".1")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


# ─── MOTEURS (manifestes) ─────────────────────────────────────────────────────

def discover_engine_dirs() -> list[tuple[str, str]]:
    """[(label, chemin absolu)] des moteurs découverts, ordre déterministe : le dossier
    de l'app d'abord (installation à plat), puis ses sous-dossiers immédiats non cachés,
    triés. La présence du manifeste est la signature d'un moteur : un dossier de binaires
    sans manifeste (autre plateforme, sauvegarde...) est ignoré par construction."""
    dirs = []
    if os.path.isfile(os.path.join(INSTALL_DIR, MANIFEST_NAME)):
        dirs.append((ENGINE_FLAT_LABEL, INSTALL_DIR))
    try:
        names = sorted(os.listdir(INSTALL_DIR))
    except OSError:
        names = []
    for name in names:
        sub = os.path.join(INSTALL_DIR, name)
        if not name.startswith(".") and os.path.isfile(os.path.join(sub, MANIFEST_NAME)):
            dirs.append((name, sub))
    return dirs


def resolve_binary_path(home: str, name: str) -> str | None:
    """Chemin réel de l'orchestrateur : le binaire compilé d'abord (release), sinon
    la source <name>.py au même endroit (repo de dev — son shebang fait le reste).
    None si aucun des deux n'existe. Le manifeste, lui, ne connaît que le nom nu."""
    for candidate in (os.path.join(home, name), os.path.join(home, name + ".py")):
        if os.path.isfile(candidate):
            return candidate
    return None


def _clear_quarantine(path: str):
    """macOS : lève la quarantaine Gatekeeper héritée d'un téléchargement navigateur
    (Archive Utility la propage aux fichiers extraits ; un binaire quarantiné est
    tué au premier exec). Best-effort : xattr vit dans /usr/bin sur tout macOS, et
    son échec (attribut déjà absent = cas nominal) ne concerne pas l'utilisateur."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", path],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def heal_engine_binaries() -> list[str]:
    """Remet les moteurs en état d'exécution : bit exécutable perdu (zip, copie via
    l'explorateur, clé USB FAT) et quarantaine macOS. C'est la promesse « zéro
    chmod » de la distribution : l'app répare elle-même, au démarrage et en filet
    avant chaque lancement. Best-effort assumé — en dernier recours, le contrôle
    bloquant de _start_run explique la situation à l'utilisateur."""
    healed = []
    for _, home in discover_engine_dirs():
        data = read_json(os.path.join(home, MANIFEST_NAME), None)
        if not isinstance(data, dict) or not isinstance(data.get("orchestrators"), list):
            continue
        for entry in data["orchestrators"]:
            if not isinstance(entry, dict) or not entry.get("binary"):
                continue
            path = resolve_binary_path(home, entry["binary"])
            if path is None:
                continue
            _clear_quarantine(path)
            if not os.access(path, os.X_OK):
                try:
                    os.chmod(path, os.stat(path).st_mode | 0o755)
                    healed.append(os.path.relpath(path, INSTALL_DIR))
                except OSError:
                    pass
    return healed


def load_manifests() -> dict:
    """Fusionne les manifestes de tous les moteurs découverts (la bibliothèque de l'app
    est générée de cette fusion). Aucun moteur = app utilisable mais bibliothèque vide +
    avertissement. Chaque entrée d'orchestrateur reçoit son moteur (engine = label,
    home = dossier) et l'état de son binaire ; les id d'orchestrateurs doivent rester
    uniques dans une distribution (collision : le premier gagne + avertissement)."""
    out = {"engines": [], "orchestrators": [], "warnings": [], "error": None}
    engine_dirs = discover_engine_dirs()
    if not engine_dirs:
        out["error"] = L(f"Aucun moteur : {MANIFEST_NAME} n'est ni à côté de l'app, "
                         f"ni dans ses sous-dossiers ({INSTALL_DIR}).",
                         f"No engine: {MANIFEST_NAME} is neither next to the app "
                         f"nor in its subfolders ({INSTALL_DIR}).")
        return out
    seen_ids = {}
    for label, home in engine_dirs:
        data = read_json(os.path.join(home, MANIFEST_NAME), None)
        if not isinstance(data, dict) or not isinstance(data.get("orchestrators"), list):
            out["warnings"].append(L(f"Manifeste invalide ignoré : {os.path.join(home, MANIFEST_NAME)}",
                                     f"Invalid manifest ignored: {os.path.join(home, MANIFEST_NAME)}"))
            continue
        engine = {"label": label, "home": home,
                  "contract_version": data.get("contract_version"),
                  "distro_version": data.get("distro_version"),
                  "declared": 0, "found": 0}
        for entry in data["orchestrators"]:
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("binary"):
                continue
            if entry["id"] in seen_ids:
                out["warnings"].append(L(
                    f"Orchestrateur « {entry['id']} » en double dans le moteur {label} : "
                    f"entrée ignorée, celle du moteur {seen_ids[entry['id']]} est retenue.",
                    f"Duplicate orchestrator “{entry['id']}” in engine {label}: "
                    f"this entry is ignored, the one from engine {seen_ids[entry['id']]} is used."))
                continue
            seen_ids[entry["id"]] = label
            binary_path = resolve_binary_path(home, entry["binary"])
            entry = dict(entry)
            entry["engine"] = label
            entry["home"] = home
            entry["binary_found"] = binary_path is not None
            entry["binary_executable"] = binary_path is not None and os.access(binary_path, os.X_OK)
            out["orchestrators"].append(entry)
            engine["declared"] += 1
            engine["found"] += 1 if entry["binary_found"] else 0
        out["engines"].append(engine)
    return out


def manifest_orchestrator(orch_id: str) -> dict | None:
    for entry in load_manifests()["orchestrators"]:
        if entry["id"] == orch_id:
            return entry
    return None


def resolve_engine(manifest: dict, label: str | None) -> dict:
    """Le moteur visé par une action d'équipement : implicite quand il n'y en a qu'un,
    explicite sinon (le choix des skills copiés dans le projet n'est pas devinable)."""
    engines = manifest["engines"]
    if label:
        for engine in engines:
            if engine["label"] == label:
                return engine
        raise ValueError(L(f"Moteur inconnu : {label}.", f"Unknown engine: {label}."))
    if len(engines) == 1:
        return engines[0]
    if not engines:
        raise ValueError(L("Aucun moteur détecté : impossible d'équiper un projet.",
                           "No engine detected: there's nothing to equip a project with."))
    raise ValueError(L("Plusieurs moteurs sont installés : indique lequel doit équiper ce projet "
                       f"({', '.join(e['label'] for e in engines)}).",
                       "Several engines are installed: tell the app which one should equip this project "
                       f"({', '.join(e['label'] for e in engines)})."))


# ─── INTEROP WSL ──────────────────────────────────────────────────────────────
# Sur Windows, tout tourne DANS WSL (voir INSTALL.md) : l'app est un processus
# Linux et les disques Windows vivent sous /mnt/<lettre>. Un chemin collé depuis
# l'explorateur Windows (D:\dev\projet) doit donc être traduit, et le dialogue
# natif de sélection de dossier est celui de Windows, ouvert via powershell.exe.

_WIN_PATH_RE = re.compile(r'^"?([A-Za-z]):[\\/]([^"]*)"?$')
_IS_WSL = None


def running_under_wsl() -> bool:
    global _IS_WSL
    if _IS_WSL is None:
        _IS_WSL = bool(os.environ.get("WSL_DISTRO_NAME"))
        if not _IS_WSL:
            try:
                with open("/proc/version", encoding="utf-8") as f:
                    _IS_WSL = "microsoft" in f.read().lower()
            except OSError:
                _IS_WSL = False
    return _IS_WSL


def translate_windows_path(path: str) -> str:
    """Traduit un chemin Windows (D:\\dev\\projet — guillemets de « Copier en tant
    que chemin d'accès » tolérés) vers son équivalent WSL (/mnt/d/dev/projet).
    Inchangé hors WSL ou si le chemin n'est pas de forme Windows."""
    stripped = path.strip()
    m = _WIN_PATH_RE.match(stripped)
    if not m or not running_under_wsl():
        return path
    if shutil.which("wslpath"):  # gère les racines de montage personnalisées (/etc/wsl.conf)
        try:
            proc = subprocess.run(["wslpath", "-u", stripped.strip('"')],
                                  capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    rest = m.group(2).replace("\\", "/")
    return "/mnt/" + m.group(1).lower() + "/" + rest


def _windows_powershell() -> str | None:
    """powershell.exe vu depuis WSL : via le PATH (interop activé par défaut),
    sinon à son emplacement standard. None si l'interop Windows est coupé."""
    found = shutil.which("powershell.exe")
    if found:
        return found
    fallback = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    return fallback if os.path.isfile(fallback) else None


# ─── REGISTRE DES PROJETS ─────────────────────────────────────────────────────

def load_registry() -> list[dict]:
    data = read_json(REGISTRY_FILE, {"projects": []})
    return [p for p in data.get("projects", []) if isinstance(p, dict) and p.get("path")]


def save_registry(projects: list[dict]):
    write_json_atomic(REGISTRY_FILE, {"projects": projects})


def register_project(path: str) -> dict:
    path = os.path.realpath(os.path.expanduser(translate_windows_path(path.strip())))
    if not os.path.isdir(path):
        raise ValueError(L(f"Dossier introuvable : {path}", f"Folder not found: {path}"))
    with _registry_lock:
        projects = load_registry()
        if not any(p["path"] == path for p in projects):
            projects.append({"path": path, "added": int(time.time())})
            save_registry(projects)
    return {"path": path}


def forget_project(path: str):
    with _registry_lock:
        projects = [p for p in load_registry() if p["path"] != path]
        save_registry(projects)


def adopt_legacy_registry() -> str | None:
    """Montée d'un niveau sans perte : si l'app n'a pas encore de registre mais qu'UN SEUL
    moteur découvert en possède un (le .mm-app/ d'une installation à plat qui vivait là),
    il est repris par COPIE — l'app n'écrit jamais dans un dossier-moteur, et l'ancienne
    installation reste utilisable telle quelle. Les projets dont le dossier a disparu ne
    sont pas repris (dossiers temporaires, sessions de test adoptées par une app vivante :
    vérifié en vrai). Ambiguïté (plusieurs candidats) = on ne devine pas : l'utilisateur
    ré-ajoutera ses projets (ou l'adoption tmux les retrouvera)."""
    if os.path.exists(REGISTRY_FILE):
        return None
    candidates = [os.path.join(home, ".mm-app", "projects.json")
                  for label, home in discover_engine_dirs() if home != INSTALL_DIR]
    candidates = [c for c in candidates if os.path.isfile(c)]
    if len(candidates) != 1:
        return None
    projects = [p for p in read_json(candidates[0], {}).get("projects", [])
                if isinstance(p, dict) and p.get("path") and os.path.isdir(p["path"])]
    if not projects:
        return None
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        write_json_atomic(REGISTRY_FILE, {"projects": projects})
    except OSError:
        return None
    return candidates[0]


def adopt_orphan_sessions():
    """Reprise : des sessions mm-run-* existent mais leur projet n'est plus dans le
    registre (registre perdu, app réinstallée...). On retrouve la racine du projet via
    le répertoire courant du pane et on ré-enregistre — même philosophie que la
    reprise par fichiers des orchestrateurs : tmux est la source de vérité."""
    known = {project_hash(p["path"]) for p in load_registry()}
    for session in tmux_list_sessions():
        if not session.startswith(SESSION_PREFIX):
            continue
        if session[len(SESSION_PREFIX):] in known:
            continue
        pane_path = tmux_pane_path(session)
        if pane_path and os.path.isdir(pane_path):
            try:
                register_project(pane_path)
            except ValueError:
                pass


# ─── COUCHE TMUX ──────────────────────────────────────────────────────────────
# Tous les appels retirent TMUX de l'environnement : sans cela, 'tmux new-session'
# refuse de tourner depuis l'intérieur d'une session (cas : l'app elle-même lancée
# dans tmux). Même précaution pour le binaire lancé (env -u TMUX) : l'orchestrateur
# fait ses propres 'tmux new-session' et subirait le même refus depuis son pane.

def _tmux_env() -> dict:
    env = dict(os.environ)
    env.pop("TMUX", None)
    return env


def tmux(*args, check=False, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True,
                          env=_tmux_env(), check=check, timeout=timeout)


def tmux_list_sessions() -> list[str]:
    proc = tmux("list-sessions", "-F", "#{session_name}")
    if proc.returncode != 0:  # pas de serveur tmux = aucune session
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def tmux_has_session(name: str) -> bool:
    return tmux("has-session", "-t", name).returncode == 0


def tmux_pane_path(session: str) -> str | None:
    """Racine du projet d'une session de run. session_path = répertoire de démarrage
    (posé par new-session -c) : il survit à la mort du pane, contrairement à
    pane_current_path (gardé en repli au cas où)."""
    proc = tmux("display-message", "-p", "-t", session,
                "#{session_path}\t#{pane_current_path}")
    if proc.returncode != 0:
        return None
    parts = (proc.stdout.strip("\n") + "\t").split("\t")
    return parts[0] or parts[1] or None


def tmux_pane_binary(session: str) -> str | None:
    """Nom du binaire lancé dans le pane (affichage) : dernier segment de la
    commande de démarrage, wrappers retirés ('env -u TMUX' et la suite '; …' de
    la sentinelle de code de sortie)."""
    proc = tmux("display-message", "-p", "-t", session, "#{pane_start_command}")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    command = proc.stdout.strip()
    # tmux 3.4 rapporte la commande re-quotée ("…\$…") dès qu'elle contient des
    # caractères spéciaux — ce que le wrapper de sentinelle introduit : déshabiller.
    if command.startswith('"') and command.endswith('"'):
        command = command[1:-1].replace("\\$", "$").replace('\\"', '"')
    command = command.split(";")[0].strip()
    try:
        tokens = [t for t in shlex.split(command) if t]
    except ValueError:  # quoting exotique : découpage naïf, c'est un nom d'affichage
        tokens = command.split()
    while tokens and (tokens[0] in ("env", "-u", "TMUX")
                      or ("=" in tokens[0] and not tokens[0].startswith("/"))):
        tokens.pop(0)   # wrappers et affectations VAR=… (PATH explicite) retirés
    return os.path.basename(tokens[0]) if tokens else None


def tmux_pane_display(session: str) -> tuple[bool, int | None, int | None]:
    """(écran alternatif actif ?, largeur, hauteur du pane). L'écran alternatif
    signale une TUI (celle du harness) : l'app la rend en grille à géométrie fixe — replier
    ses lignes détruirait le dessin. Un pane normal est un log défilant, rendu
    replié comme avant. Largeur et hauteur servent à caler la police pour que
    l'écran TUI ENTIER tienne dans le panneau (fitTerm)."""
    proc = tmux("display-message", "-p", "-t", session,
                "#{alternate_on} #{pane_width} #{pane_height}")
    if proc.returncode != 0 or not proc.stdout.strip():
        return False, None, None
    parts = proc.stdout.split()

    def _num(idx: int) -> int | None:
        try:
            return int(parts[idx])
        except (IndexError, ValueError):
            return None

    return parts[0] == "1", _num(1), _num(2)


def tmux_capture(session: str, lines: int = CAPTURE_LINES, ansi: bool = False) -> str | None:
    """Capture de l'écran. ansi=True ré-encode les attributs SGR (-e) pour l'AFFICHAGE ;
    la détection de porte, elle, travaille toujours sur la capture brute (les regex ne
    doivent jamais voir d'échappements)."""
    args = ["capture-pane", "-p", "-t", session, "-S", f"-{lines}"]
    if ansi:
        args.insert(1, "-e")
    proc = tmux(*args)
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def tmux_dead_status(session: str, project: str | None = None) -> tuple[bool, int | None]:
    """(pane mort ?, code de sortie). Le pane survit à la fin du binaire grâce à
    remain-on-exit : l'écran final et le verdict restent lisibles dans l'app.
    Repli : quand tmux 3.4 perd pane_dead_status (voir RUN_EXIT_SENTINEL), le code
    est relu depuis la sentinelle écrite par le wrapper de lancement."""
    proc = tmux("list-panes", "-t", session, "-F", "#{pane_dead} #{pane_dead_status}")
    if proc.returncode != 0 or not proc.stdout.strip():
        return False, None
    first = proc.stdout.splitlines()[0].split()
    dead = bool(first) and first[0] == "1"
    code = None
    if dead and len(first) > 1:
        try:
            code = int(first[1])
        except ValueError:
            code = None
    if dead and code is None and project:
        try:
            with open(os.path.join(project, RUN_EXIT_SENTINEL), "r", encoding="utf-8") as f:
                code = int(f.read().strip())
        except (OSError, ValueError):
            code = None
    return dead, code


def tmux_start_run(project: str, binary_path: str) -> str:
    """Contrat de lancement : session dédiée par projet, cwd = racine du projet,
    pane large (capture lisible), remain-on-exit activé dans LA MÊME commande tmux
    (un binaire qui meurt immédiatement laisse quand même son écran)."""
    session = run_session_name(project)
    if tmux_has_session(session):
        dead, _ = tmux_dead_status(session, project)
        if not dead:
            raise RuntimeError(L(f"Un run est déjà actif pour ce projet (session {session}).",
                                 f"A run is already active for this project (session {session})."))
        tmux("kill-session", "-t", session)  # run terminé : on recycle la session
    archive_run_exit(project)   # le code de sortie du run précédent rejoint son run.json
    try:
        os.remove(os.path.join(project, RUN_EXIT_SENTINEL))  # sentinelle d'un run précédent
    except OSError:
        pass
    # Le wrapper relaie le code de sortie dans la sentinelle (cwd = projet) PUIS le rend
    # au pane : pane_dead_status reste la source primaire, la sentinelle couvre les cas
    # où tmux 3.4 le perd (kill-session de la session d'agent depuis le pane, cf. config).
    # PATH passé EXPLICITEMENT : la commande d'une session tmux hérite sinon du PATH du
    # SERVEUR tmux, c'est-à-dire du processus qui l'a créé (une app headless d'il y a
    # trois jours, par exemple) — l'orchestrateur doit voir le PATH de l'app d'aujourd'hui,
    # enrichi du shell de login (enrich_path), comme l'agent dans son propre pane.
    command = (f"env -u TMUX PATH={shlex.quote(os.environ.get('PATH', ''))} "
               f"{shlex.quote(os.path.abspath(binary_path))}; __mm=$?; "
               f"echo $__mm > {RUN_EXIT_SENTINEL}; exit $__mm")
    proc = tmux("new-session", "-d", "-s", session, "-x", "220", "-y", "50",
                "-c", project, command,
                ";", "set-window-option", "-t", session, "remain-on-exit", "on")
    if proc.returncode != 0:
        raise RuntimeError(L(f"tmux new-session a échoué : {proc.stderr.strip()}",
                             f"tmux new-session failed: {proc.stderr.strip()}"))
    return session


def tmux_send_answer(session: str, answer: str):
    tmux("send-keys", "-t", session, "-l", answer, check=True)
    tmux("send-keys", "-t", session, "Enter", check=True)


def tmux_interrupt(session: str):
    """Ctrl-C simulé : les orchestrateurs gèrent déjà l'interruption proprement
    (signal handler + kill de leur session d'agent)."""
    tmux("send-keys", "-t", session, "C-c")


def tmux_kill(session: str):
    tmux("kill-session", "-t", session)


def kill_run(project: str):
    """Arrêt d'un run depuis l'app : Ctrl-C d'abord (l'orchestrateur clôt son journal
    'interrupted' et tue sa session d'agent), puis kill de la session de run si elle
    survit, ET de la session d'agent quel que soit son état — un agent orphelin finissait
    d'écrire son livrable dans un projet que plus personne ne pilotait (carte résiduelle
    trompeuse au relancement, 22/08/2026)."""
    session = run_session_name(project)
    if tmux_has_session(session):
        dead, _ = tmux_dead_status(session, project)
        if not dead:
            tmux_interrupt(session)
            deadline = time.time() + 3
            while time.time() < deadline:
                if not tmux_has_session(session):
                    break
                dead, _ = tmux_dead_status(session, project)
                if dead:
                    break
                time.sleep(0.2)
        if tmux_has_session(session):
            tmux_kill(session)
    agent = agent_session_name(project, harness_of(project))
    if tmux_has_session(agent):
        tmux_kill(agent)


def archive_run_exit(project: str):
    """Avant d'effacer la sentinelle de code de sortie du run précédent, la recopie dans le
    run.json du dernier dossier .mm-runs/ : le code de sortie survivait au pane, pas au run
    suivant. Best-effort, jamais bloquant."""
    try:
        with open(os.path.join(project, RUN_EXIT_SENTINEL), "r", encoding="utf-8") as f:
            code = int(f.read().strip())
    except (OSError, ValueError):
        return
    runs_root = os.path.join(project, ".mm-runs")
    try:
        runs = sorted(d for d in os.listdir(runs_root)
                      if os.path.isdir(os.path.join(runs_root, d)))
    except OSError:
        return
    if not runs:
        return
    run_json = os.path.join(runs_root, runs[-1], "run.json")
    data = read_json(run_json, None)
    if not isinstance(data, dict) or "exit_code" in data:
        return
    data["exit_code"] = code
    try:
        write_json_atomic(run_json, data)
    except OSError:
        pass


# ─── CONVERSION ANSI → HTML (affichage des écrans tmux) ──────────────────────
# capture-pane -e ré-encode les attributs en SGR ; on convertit couleurs (16/256/
# truecolor) et styles en <span>, on SUPPRIME tout le reste (CSI divers, OSC). Le texte
# est échappé HTML avant habillage : rien de ce qu'affiche un terminal ne peut injecter
# de balisage dans la page.

# Palette 16 couleurs lisible sur fond sombre (0-7 normales, 8-15 brillantes).
_ANSI16 = ["#3D4C59", "#F87171", "#4ADE80", "#FACC15", "#7EB3E8", "#D8A6E8", "#67E8F9", "#C7D5DF",
           "#64798A", "#FCA5A5", "#86EFAC", "#FDE047", "#A5CCF2", "#E9C2F5", "#A5F3FC", "#F1F5F8"]


def _ansi256(n: int) -> str:
    if n < 16:
        return _ANSI16[n]
    if n < 232:  # cube 6×6×6
        n -= 16
        levels = [0, 95, 135, 175, 215, 255]
        r, g, b = levels[n // 36], levels[(n // 6) % 6], levels[n % 6]
    else:        # rampe de gris
        r = g = b = 8 + (n - 232) * 10
    return f"#{r:02X}{g:02X}{b:02X}"


_SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
# CSI non-SGR : le lookahead préserve les SGR (finale 'm' à paramètres numériques),
# qui sont convertis ensuite — sans lui, ce nettoyage les avalerait avant conversion.
_ANSI_OTHER_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"        # OSC (titre…)
                            r"|\x1b\[[>?][0-9;]*[a-zA-Z]"                # CSI privées
                            r"|\x1b\[(?![0-9;:]*m)[0-?]*[ -/]*[@-~]"     # CSI non-SGR
                            r"|\x1b[@-Z\\-_]")                           # échappements simples


def ansi_to_html(text: str) -> str:
    text = _ANSI_OTHER_RE.sub("", text.replace("\x1b\\", ""))
    default = {"fg": None, "bg": None, "bold": False, "dim": False,
               "italic": False, "underline": False, "reverse": False}
    state = dict(default)
    out, pos = [], 0

    def emit(chunk: str):
        if not chunk:
            return
        escaped = html_mod.escape(chunk)
        fg, bg = state["fg"], state["bg"]
        if state["reverse"]:
            fg, bg = (bg or "var(--term-bg)"), (fg or "var(--term-ink)")
        styles = []
        if fg:
            styles.append(f"color:{fg}")
        if bg:
            styles.append(f"background:{bg}")
        if state["bold"]:
            styles.append("font-weight:700")
        if state["dim"]:
            styles.append("opacity:.6")
        if state["italic"]:
            styles.append("font-style:italic")
        if state["underline"]:
            styles.append("text-decoration:underline")
        out.append(f'<span style="{";".join(styles)}">{escaped}</span>' if styles else escaped)

    for match in _SGR_RE.finditer(text):
        emit(text[pos:match.start()])
        pos = match.end()
        codes = [int(c) for c in re.split("[;:]", match.group(1)) if c.isdigit()] or [0]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                state = dict(default)
            elif code == 1:
                state["bold"] = True
            elif code == 2:
                state["dim"] = True
            elif code == 3:
                state["italic"] = True
            elif code == 4:
                state["underline"] = True
            elif code == 7:
                state["reverse"] = True
            elif code in (21, 22):
                state["bold"] = state["dim"] = False
            elif code == 23:
                state["italic"] = False
            elif code == 24:
                state["underline"] = False
            elif code == 27:
                state["reverse"] = False
            elif 30 <= code <= 37:
                state["fg"] = _ANSI16[code - 30]
            elif code == 39:
                state["fg"] = None
            elif 40 <= code <= 47:
                state["bg"] = _ANSI16[code - 40]
            elif code == 49:
                state["bg"] = None
            elif 90 <= code <= 97:
                state["fg"] = _ANSI16[code - 90 + 8]
            elif 100 <= code <= 107:
                state["bg"] = _ANSI16[code - 100 + 8]
            elif code in (38, 48):
                key = "fg" if code == 38 else "bg"
                if i + 1 < len(codes) and codes[i + 1] == 5 and i + 2 < len(codes):
                    state[key] = _ansi256(min(codes[i + 2], 255))
                    i += 2
                elif i + 1 < len(codes) and codes[i + 1] == 2 and i + 4 < len(codes):
                    r, g, b = codes[i + 2] % 256, codes[i + 3] % 256, codes[i + 4] % 256
                    state[key] = f"#{r:02X}{g:02X}{b:02X}"
                    i += 4
            i += 1
    emit(text[pos:])
    return "".join(out)


def _sgr_bg_state(params: str, active: bool) -> bool:
    """Suit l'état « un fond non-défaut est actif » à travers une séquence SGR
    (sous-ensemble suffisant ici : 0/49 effacent ; 40-47, 100-107, 48;… posent ;
    38;… consomme ses paramètres sans toucher au fond)."""
    codes = [int(c) for c in re.split("[;:]", params) if c.isdigit()] or [0]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c in (0, 49):
            active = False
        elif 40 <= c <= 47 or 100 <= c <= 107:
            active = True
        elif c in (38, 48):
            if c == 48:
                active = True
            i += 2 if i + 1 < len(codes) and codes[i + 1] == 5 else 4
        i += 1
    return active


def tui_trim(raw: str | None, ansi: str | None) -> tuple[str | None, str | None, int | None]:
    """Rogne la marge gauche commune d'un écran TUI. Les TUI centrent leur contenu :
    en 220 colonnes, l'essentiel vit au milieu et fitTerm écraserait la police pour
    faire tenir du vide. La borne se décide sur la capture brute ; la version SGR est
    rognée en préservant tous ses échappements (ils portent l'état couleur du reste
    de la ligne). Garde-fou : si la marge contient du texte ou des espaces PEINTS
    (fond actif), on renonce en bloc — mieux vaut du vide qu'une grille faussée.
    Jamais appliqué aux captures que lisent les regex de porte (sessions log).
    Retourne (brut rogné, sgr rogné, largeur utile) pour data-cols/fitTerm."""
    if not raw:
        return raw, ansi, None
    lines = raw.split("\n")
    useful = [l for l in lines if l.strip()]
    if not useful:
        return raw, ansi, None
    margin = max(0, min(len(l) - len(l.lstrip(" ")) for l in useful) - 2)  # matelas 2 col.
    full_width = max(len(l.rstrip()) for l in useful)
    if margin == 0:
        return raw, ansi, full_width
    trimmed_ansi = ansi
    if ansi is not None:
        out = []
        for line in ansi.split("\n"):
            kept, pos, todo, bg = [], 0, margin, False

            def eat(chunk: str) -> str | None:
                """Consomme la part de marge restante du segment ; None = renoncer."""
                nonlocal todo
                if not todo:
                    return chunk
                cut = chunk[:todo]
                if cut.strip(" ") or (bg and cut):
                    return None
                todo -= len(cut)
                return chunk[len(cut):]

            for m in _SGR_RE.finditer(line):
                seg = eat(line[pos:m.start()])
                if seg is None:
                    return raw, ansi, full_width
                kept.append(seg)
                kept.append(m.group(0))
                bg = _sgr_bg_state(m.group(1), bg)
                pos = m.end()
            seg = eat(line[pos:])
            if seg is None:
                return raw, ansi, full_width
            kept.append(seg)
            out.append("".join(kept))
        trimmed_ansi = "\n".join(out)
    trimmed_raw = "\n".join(l[margin:] for l in lines)
    return trimmed_raw, trimmed_ansi, full_width - margin


# ─── LECTURE DE L'ÉTAT D'UN PROJET (fichiers = source de vérité) ─────────────

def parse_blackboard(path: str) -> dict | None:
    """Parse blackboard.yaml. PyYAML si disponible (cas nominal : il est embarqué
    dans les builds Nuitka de l'usine) ; sinon repli mécanique ligne à ligne qui
    n'extrait que les champs scalaires utiles à l'affichage (id, name, status,
    nature, verdict + project/verify_cmd) — jamais bloquant."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    # Repli sans PyYAML : suffisant pour la colonne des phases, pas pour valider quoi que ce soit.
    data: dict = {"phases": [], "_degraded": True}
    current: dict | None = None
    in_phases = False
    for line in raw.splitlines():
        if re.match(r"^phases\s*:", line):
            in_phases = True
            continue
        top = re.match(r"^(\w[\w_]*)\s*:\s*(.*)$", line)
        if top and not line.startswith(" "):
            in_phases = top.group(1) == "phases"
            if top.group(1) in ("project", "verify_cmd") and top.group(2):
                data[top.group(1)] = top.group(2).strip().strip("'\"")
            continue
        if not in_phases:
            continue
        item = re.match(r"^\s*-\s+(\w[\w_]*)\s*:\s*(.*)$", line)
        if item:
            current = {}
            data["phases"].append(current)
            key, value = item.group(1), item.group(2)
        else:
            field = re.match(r"^\s+(\w[\w_]*)\s*:\s*(.*)$", line)
            if not field or current is None:
                continue
            key, value = field.group(1), field.group(2)
        if key in ("id", "name", "status", "nature", "verdict") and value:
            value = value.strip().strip("'\"")
            if key == "id":
                try:
                    value = int(value)
                except ValueError:
                    pass
            current[key] = value
    return data


def normalize_phases(blackboard: dict | None) -> list[dict]:
    """Sémantique v2 vérifiée dans les sources : une phase non convergée repasse
    status=TODO avec verdict=REJECTED et garde son critic_feedback — l'état AFFICHÉ
    (champ 'state') se déduit donc du couple statut/verdict, jamais du statut seul."""
    if not blackboard or not isinstance(blackboard.get("phases"), list):
        return []
    phases = []
    for p in blackboard["phases"]:
        if not isinstance(p, dict):
            continue
        status = str(p.get("status", "TODO")).upper()
        verdict = str(p.get("verdict") or "").upper() or None
        if status == "DONE":
            state = "DONE"
        elif status == "PENDING":
            state = "PENDING"
        elif verdict == "REJECTED":
            state = "REJECTED"
        else:
            state = "TODO"
        feedback = p.get("critic_feedback")
        feedback = str(feedback).strip()[:FEEDBACK_MAX_CHARS] if feedback else None
        phases.append({
            "id": p.get("id"),
            "name": str(p.get("name", "?")),
            "status": status,
            "verdict": verdict,
            "state": state,
            "feedback": feedback,
            "nature": p.get("nature"),
            "covers": p.get("covers") if isinstance(p.get("covers"), list) else [],
            "tasks": len(p.get("tasks", [])) if isinstance(p.get("tasks"), list) else None,
        })
    return phases


def _resolve_gate_file(gate: dict, project: str | None) -> str | None:
    """Fichier d'aperçu d'une porte : le nom FIXE du manifeste s'il existe, sinon son
    'file_glob' (porte à fichier DYNAMIQUE, ex. 'impact-phase-<id>.md' des orchestrateurs
    Yolo) résolu vers le nom nu le PLUS RÉCENT à la racine du projet. Le motif résolu doit
    aussi passer DOC_DYNAMIC_RE : ce que cette fonction rend, /api/doc doit pouvoir le
    servir. None sans correspondance : la porte s'affiche alors hint seul, comme les
    portes de périmètre (documentation, audits)."""
    if gate.get("file"):
        return gate.get("file")
    pattern = gate.get("file_glob")
    if not pattern or not project:
        return None

    def mtime(name: str) -> float:
        try:
            return os.stat(os.path.join(project, name)).st_mtime
        except OSError:
            return 0.0

    try:
        candidates = [n for n in os.listdir(project)
                      if fnmatch.fnmatch(n, pattern) and DOC_DYNAMIC_RE.match(n)
                      and os.path.isfile(os.path.join(project, n))
                      and not os.path.islink(os.path.join(project, n))]
    except OSError:
        return None
    return max(candidates, key=mtime) if candidates else None


def detect_gate(orch: dict, screen: str | None, project: str | None = None) -> dict | None:
    """Mode écran : une porte est « ouverte » si le libellé exact du prompt (regex du
    manifeste) est la DERNIÈRE ligne non vide de l'écran et que rien n'est tapé après
    le ':' — après la réponse, l'orchestrateur imprime la suite et la porte se referme
    d'elle-même. Couplage assumé aux libellés (contrat v1), levé par les sentinelles V3.
    'project' sert aux portes à fichier dynamique ('file_glob', cf. _resolve_gate_file)."""
    if not screen or not orch:
        return None
    tail = [line.rstrip() for line in screen.splitlines() if line.strip()][-GATE_TAIL_LINES:]
    if not tail:
        return None
    last = tail[-1]
    for gate in orch.get("gates", []):
        pattern = gate.get("prompt_regex")
        if not pattern:
            continue
        try:
            match = re.search(pattern, last)
        except re.error:
            continue
        if not match:
            continue
        typed = last[match.end():].strip().strip(":").strip()
        if typed:  # réponse déjà saisie (attente d'Enter) : ne pas re-proposer les boutons
            continue
        return {"id": gate.get("id"), "title": gate.get("title"),
                "file": _resolve_gate_file(gate, project),
                "hint": gate.get("hint"), "yes_label": gate.get("yes_label"),
                "no_label": gate.get("no_label"),
                # Portes v1.1 : 'yn' (défaut historique, boutons Valider/Refuser),
                # 'choice' (un bouton par choix déclaré au manifeste : triage r/e/o,
                # questionnaires 1/2/3), 'text' (saisie libre d'une ligne, envoyée
                # au pane telle quelle : stack cible…).
                "kind": gate.get("kind", "yn"), "choices": gate.get("choices"),
                "placeholder": gate.get("placeholder")}
    return None


def manifest_orchestrator_by_binary(manifest: dict, binary: str | None) -> dict | None:
    """Entrée du manifeste correspondant au binaire d'un run : 'Pre-Audit-A11Y-RGAA.py'
    en dev (source), sans extension en release (binaire compilé)."""
    if not binary:
        return None
    stem = binary[:-3] if binary.endswith(".py") else binary
    for entry in manifest["orchestrators"]:
        if entry.get("binary") == stem:
            return entry
    return None


def declared_steps(entry: dict | None) -> list | None:
    """La timeline déclarée au manifeste ('steps'), ou None → repli sur le modèle usine
    à 5 étapes (infer_step) : les pipelines de production ne déclarent rien."""
    steps = entry.get("steps") if isinstance(entry, dict) else None
    return steps if isinstance(steps, list) and steps else None


def _step_done(project: str, marker: str | None) -> bool:
    """Preuve DURABLE qu'une étape déclarée est franchie : un fichier livré, un dossier
    apparu ('doc_zones/'), ou un motif à la racine ('skill_adapt-*.md') — jamais une
    sentinelle (éphémère, même principe qu'infer_step)."""
    if not marker:
        return False
    if "*" in marker:
        try:
            return any(fnmatch.fnmatch(name, marker) for name in os.listdir(project))
        except OSError:
            return False
    path = os.path.join(project, marker)
    return os.path.isdir(path) if marker.endswith("/") else os.path.exists(path)


def infer_declared_step(project: str, steps: list, gate_id: str | None,
                        run_alive: bool, run_finished_ok: bool) -> dict:
    """Position dans une timeline DÉCLARÉE au manifeste (audits, documentation, spec,
    outillage) : l'étape courante est la première sans preuve livrée, une porte ouverte
    force son étape comme courante, et un run mort code 0 marque tout terminé. Le payload
    porte les libellés ('labels') : l'UI ne retombe sur I18N.steps qu'en leur absence."""
    labels = [L(str((s.get("title") or {}).get("fr", "")),
                str((s.get("title") or {}).get("eng", ""))) for s in steps]
    total = len(steps)
    if run_finished_ok:
        return {"index": total, "labels": labels, "completed": True,
                "detail": L(f"Run terminé — {labels[-1]}", f"Run finished — {labels[-1]}")}
    if gate_id:
        for i, s in enumerate(steps):
            if gate_id in (s.get("gates") or []):
                return {"index": i + 1, "labels": labels, "completed": False,
                        "detail": L("Porte ouverte : à toi de répondre",
                                    "Gate open — over to you")}
    index = total
    for i, s in enumerate(steps):
        if not _step_done(project, s.get("done")):
            index = i + 1
            break
    return {"index": index, "labels": labels, "completed": False,
            "detail": L(f"{labels[index - 1]} — en cours",
                        f"{labels[index - 1]} — in progress") if run_alive else labels[index - 1]}


def infer_step(files: dict, phases: list[dict], gate_id: str | None,
               run_alive: bool, binary: str | None = None) -> dict:
    """Où en est le pipeline (étapes 1 à 5 du README) ? Inféré de la porte détectée,
    des fichiers présents et des statuts du blackboard — jamais des sentinelles
    (éphémères). C'est un indicateur d'affichage, pas un état d'exécution.
    'binary' (nom du binaire du run, ex. Design-Prototype) distingue la passe finale :
    review UX pour le prototype, refactoring pour les pipelines de production.
    Les libellés sont des messages d'UI : ils passent par L() comme tout le reste (les
    deux appelants tournent dans un handler HTTP, où _REQ.lang est posé)."""
    def step(index, detail):
        return {"index": index, "detail": detail}
    if gate_id == "spec":
        return step(1, L("Spécification prête : à toi de valider",
                         "Specification ready — over to you"))
    if gate_id == "impact":
        # Orchestrateurs Yolo : la revue d'impact s'arbitre entre le plan et le blackboard.
        return step(3, L("Revue d'impact prête : à toi de valider",
                         "Impact review ready — over to you"))
    if gate_id == "impact-phase":
        # Orchestrateurs Yolo : arbitrage mid-run d'un impact imprévu (production).
        return step(4, L("Impact imprévu détecté : à toi de trancher",
                         "Unplanned impact detected — your call"))
    if gate_id == "blackboard":
        return step(3, L("Blackboard prêt : à toi de valider",
                         "Blackboard ready — over to you"))
    if gate_id:
        # Portes des orchestrateurs hors pipeline usine (périmètre d'audit, carte de
        # zones…) : le déroulé 5 étapes ne s'applique pas, on annonce juste la porte.
        return step(1, L("On attend ta réponse (porte ouverte à l'écran)",
                         "Waiting on your answer (gate open on screen)"))
    if files.get("refactoring_report.md", {}).get("exists"):
        return step(5, L("Rapport de refactoring généré", "Refactoring report generated"))
    if files.get("review_report.md", {}).get("exists") and phases:
        # Fin de run du prototype : la review UX remplace le refactoring (un proto n'a
        # ni tests ni filet — on ne « refactore » jamais un livrable jetable).
        return step(5, L("Review UX finale en cours (review_report.md généré)",
                         "Final UX review in progress (review_report.md generated)") if run_alive
                    else L("Rapport de review UX généré", "UX review report generated"))
    if phases:
        done = sum(1 for p in phases if p["state"] == "DONE")
        rejected = [p for p in phases if p["state"] == "REJECTED"]
        pending = [p for p in phases if p["state"] == "PENDING"]
        if pending:
            return step(4, L(f"Phase {pending[0]['id']} en cours ({done}/{len(phases)} terminées)",
                             f"Phase {pending[0]['id']} in progress ({done}/{len(phases)} done)"))
        if rejected:
            return step(4, L(f"Phase {rejected[0]['id']} rejetée : toutes les tentatives ont "
                             f"échoué — le retour du vérificateur est en dessous",
                             f"Phase {rejected[0]['id']} rejected — every attempt failed. "
                             f"The verifier's feedback is below."))
        if done == len(phases):
            final_pass = (L("review UX finale", "final UX review")
                          if binary and "proto" in binary.lower()
                          else L("refactoring final", "final refactoring"))
            return step(5, L(f"Toutes les phases terminées — {final_pass}",
                             f"All phases done — {final_pass}") if run_alive
                        else L("Toutes les phases terminées", "All phases done"))
        return step(4, L(f"{done}/{len(phases)} phases terminées",
                         f"{done}/{len(phases)} phases done"))
    if files.get("blackboard.yaml", {}).get("exists"):
        return step(3, L("Blackboard présent", "Blackboard ready"))
    if files.get("impact.md", {}).get("exists"):
        # Orchestrateurs Yolo : la revue d'impact suit le plan et précède le blackboard.
        return step(3, L("Revue d'impact présente : compilation du blackboard",
                         "Impact review ready — compiling the blackboard"))
    if files.get("plan.md", {}).get("exists"):
        return step(3, L("Plan présent : compilation du blackboard",
                         "Plan ready — compiling the blackboard"))
    if files.get("spec.md", {}).get("exists"):
        return step(2, L("Spécification présente : plan d'implémentation",
                         "Spec written — drafting the implementation plan"))
    return step(1, L("Transformation du besoin en spécification",
                     "Refining the need into a specification"))


def scan_files(project: str) -> dict:
    files = {}
    for name in STATE_FILES:
        path = os.path.join(project, name)
        try:
            stat = os.stat(path)
            files[name] = {"exists": True, "mtime": int(stat.st_mtime), "size": stat.st_size}
        except OSError:
            files[name] = {"exists": False}
    return files


def need_md_state(project: str) -> dict:
    path = os.path.join(project, NEED_FILE)
    if not os.path.isfile(path):
        return {"present": False, "ready": False, "why": L("need.md absent", "need.md missing")}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {"present": False, "ready": False, "why": L("need.md illisible", "need.md unreadable")}
    if not content.strip():
        return {"present": True, "ready": False, "why": L("need.md vide", "need.md empty")}
    if content.strip() in (t.strip() for t in NEED_TEMPLATES):
        return {"present": True, "ready": False,
                "why": L("need.md contient encore le texte d'exemple : décris ton besoin",
                         "need.md still has the sample text — describe your need")}
    return {"present": True, "ready": True, "why": None}


def project_summary(path: str, manifest: dict) -> dict:
    """Vue « carte projet » : existence, équipement, run éventuel (vivant ou mort),
    porte ouverte. Tout est recalculé à la demande depuis disque + tmux."""
    exists = os.path.isdir(path)
    summary = {
        "path": path,
        "name": os.path.basename(path.rstrip("/")) or path,
        "hash": project_hash(path),
        "exists": exists,
        "equipped": False,
        "equip_version": None,
        "equip_engine": None,
        "harness": None,
        "harness_label": None,
        "model": None,
        "update_available": False,
        "timeouts": None,
        "need": {"present": False, "ready": False, "why": None},
        "run": None,
        "deliverables": [],
        "cleanable": [],
    }
    if not exists:
        return summary
    # Le harness du projet est déterminé AVANT l'équipement : c'est lui qui dit quels
    # artefacts on doit y trouver (un projet équipé en Codex n'a pas de '.opencode/').
    harness = harness_of(path)
    summary["harness"] = harness
    summary["harness_label"] = HARNESSES[harness]["label"]
    summary["model"] = configured_model(path, harness)
    summary["equipped"] = is_equipped(path, harness)
    summary["need"] = need_md_state(path)
    if summary["equipped"]:
        marker = read_json(os.path.join(path, EQUIP_MARKER), {})
        summary["equip_version"] = marker.get("distro_version")
        summary["equip_engine"] = marker.get("engine")
        # Timeouts personnalisés du projet (section écrite par /api/project/timeouts) :
        # exposés à l'UI pour pré-remplir le dialogue ⏱ et afficher la pastille.
        if isinstance(marker.get("timeouts"), dict):
            summary["timeouts"] = marker["timeouts"]
        # La référence est le moteur qui a équipé le projet (marqueur) ; marqueur legacy
        # sans moteur : le premier moteur découvert fait foi (installation mono-moteur).
        engine = next((e for e in manifest["engines"] if e["label"] == marker.get("engine")),
                      (manifest["engines"] or [None])[0])
        distro = engine.get("distro_version") if engine else None
        # Marqueur absent (équipé avant cette évolution) ou version différente de la
        # distro courante : proposer la mise à jour des prompts.
        summary["update_available"] = bool(distro) and summary["equip_version"] != distro
    files = scan_files(path)
    summary["deliverables"] = [
        {"file": name, "mtime": meta["mtime"]}
        for name, meta in files.items()
        if meta["exists"] and name not in (NEED_FILE,)
    ]
    summary["cleanable"] = cleanable_present(path)
    session = run_session_name(path)
    if tmux_has_session(session):
        dead, exit_code = tmux_dead_status(session, path)
        agent_session = agent_session_name(path, harness)
        run = {"session": session, "alive": not dead, "exit_code": exit_code,
               "agent_session": agent_session,
               "agent_alive": tmux_has_session(agent_session),
               "orchestrator": None, "gate": None, "step": None}
        screen = tmux_capture(session, 60)
        gate, orch_id = None, None
        if not dead:
            for orch in manifest["orchestrators"]:
                gate = detect_gate(orch, screen, path)
                if gate:
                    orch_id = orch["id"]
                    break
        blackboard = parse_blackboard(os.path.join(path, "blackboard.yaml"))
        phases = normalize_phases(blackboard)
        run["gate"] = gate
        run["orchestrator"] = orch_id
        run_binary = tmux_pane_binary(session)
        steps_decl = declared_steps(manifest_orchestrator_by_binary(manifest, run_binary))
        run["step"] = (infer_declared_step(path, steps_decl, gate["id"] if gate else None,
                                           not dead, dead and exit_code == 0)
                       if steps_decl else
                       infer_step(files, phases, gate["id"] if gate else None, not dead,
                                  binary=run_binary))
        summary["run"] = run
    return summary


# ─── PRÉREQUIS ────────────────────────────────────────────────────────────────

def check_prereqs() -> list[dict]:
    now = time.time()
    if _prereq_cache["data"] is not None and now - _prereq_cache["at"] < PREREQ_CACHE_S:
        return _prereq_cache["data"]
    checks = []
    for name, version_args in [("tmux", ["-V"]), ("git", ["--version"]),
                               ("node", ["--version"])]:
        found = shutil.which(name) is not None
        check = {"name": name, "found": found,
                 "version": _tool_version(name, version_args) if found else None,
                 "harness": False, "warn": None}
        if name == "node" and found:
            check.update(node_check(check["version"]))
        checks.append(check)
    # Les harness sont des prérequis ALTERNATIFS : l'absence de l'un n'est pas une
    # erreur tant que l'autre est là. Ils portent 'harness': True — l'UI ne les compte
    # donc pas dans les prérequis manquants et signale seulement le cas « aucun des deux ».
    for key, harness in HARNESSES.items():
        found = shutil.which(harness["binary"]) is not None
        check = {"name": harness["binary"], "label": harness["label"], "harness": True,
                 "key": key, "found": found,
                 "version": _tool_version(harness["binary"], ["--version"]) if found else None,
                 "authed": None, "auth_detail": None,
                 "hint": None if found else harness["install_hint"]}
        if found:
            check["authed"], check["auth_detail"] = _harness_auth(key)
            if not check["authed"]:
                check["hint"] = harness["auth_hint"]
        checks.append(check)
    _prereq_cache.update(at=now, data=checks)
    return checks


NODE_MIN_MAJOR = 20   # en-deçà, l'outillage JS courant (vite 7+, vitest 3+, rolldown) refuse de tourner


def node_check(version: str | None) -> dict:
    """Compléments du préflight Node : chemin résolu, majeure, et un avertissement si la
    version est trop ancienne ou si un shell de login (donc l'agent dans son pane tmux)
    n'aurait PAS le même node que l'app — la présence seule ne dit rien : le 23/08/2026,
    « node ✓ » masquait un v18 système sous un v22 nvm."""
    path = shutil.which("node") or ""
    match = re.match(r"v?(\d+)", version or "")
    major = int(match.group(1)) if match else None
    warnings = []
    if major is not None and major < NODE_MIN_MAJOR:
        warnings.append(L(f"Node {version} < {NODE_MIN_MAJOR} : trop ancien pour l'outillage JS "
                          f"courant (vite, vitest…) ; les verdicts des orchestrateurs échoueront.",
                          f"Node {version} < {NODE_MIN_MAJOR}: too old for current JS tooling "
                          f"(vite, vitest…); orchestrator verdicts will fail."))
    login_path = probe_login_path()
    login_node = shutil.which("node", path=login_path) if login_path else None
    if login_path and login_node and os.path.realpath(login_node) != os.path.realpath(path):
        warnings.append(L(f"L'app résout {path}, un shell de login résoudrait {login_node} : "
                          f"lance l'app depuis un terminal ou via l'icône posée par install.sh.",
                          f"The app resolves {path}, a login shell would resolve {login_node}: "
                          f"launch the app from a terminal or via the icon set up by install.sh."))
    return {"path": path, "major": major, "login_node": login_node,
            "warn": " ".join(warnings) or None}


def _tool_version(name: str, args: list) -> str | None:
    """Première ligne de `<outil> --version`. Ne lève jamais : un préflight qui casse
    serait pire qu'un préflight imprécis."""
    try:
        proc = subprocess.run([name, *args], capture_output=True, text=True, timeout=5)
        return (proc.stdout or proc.stderr).strip().splitlines()[0][:40] or None
    except Exception:
        return None


def _harness_auth(key: str) -> tuple[bool, str | None]:
    """(authentifié ?, détail lisible) pour un harness installé.

    'opencode auth list' sort 0 même sans identifiant : c'est le compte de sa dernière
    ligne (« N credentials ») qui tranche. 'codex login status' sort ≠ 0 quand personne
    n'est connecté. Format inattendu (nouvelle version du CLI) → on ne bloque pas."""
    harness = HARNESSES[key]
    try:
        proc = subprocess.run(harness["auth_cmd"], capture_output=True,
                              text=True, timeout=20)
    except Exception:
        return False, L("contrôle impossible", "couldn't check")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        first = out.splitlines()[0].strip()[:80] if out else ""
        return False, first or L("non authentifié", "not authenticated")
    if key == "opencode":
        match = re.search(r"(\d+)\s+credential", out)
        if not match:
            return True, L("impossible de compter les identifiants", "couldn't count the credentials")
        count = int(match.group(1))
        return count > 0, L(f"{count} identifiant(s)", f"{count} credential(s)")
    return True, (out.splitlines()[0].strip()[:80] if out else None)


# ─── ÉQUIPER UN PROJET ────────────────────────────────────────────────────────

def _prune_equip_backups(dst: str, keep: int = EQUIP_BACKUPS_KEPT) -> list[str]:
    """Ne garde que les `keep` sauvegardes les plus récentes de cet élément équipé
    (dossier OU fichier) — sans rotation, chaque mise à jour des prompts en accumulait
    une de plus. Les horodatages AAAAMMJJ-HHMMSS trient chronologiquement en
    lexicographique. Correctif hérité du fork Codex : la purge ne savait supprimer que des dossiers,
    et les sauvegardes d'AGENTS.md s'accumulaient sans fin."""
    parent, base = os.path.split(dst.rstrip("/"))
    prefix = base + ".bak-"
    try:
        backups = sorted(n for n in os.listdir(parent or ".") if n.startswith(prefix))
    except OSError:
        return []
    removed = []
    for name in (backups[:-keep] if len(backups) > keep else []):
        target = os.path.join(parent, name)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            try:
                os.remove(target)
            except OSError:
                pass
        removed.append(name)
    return removed


def equip_project(path: str, engine: dict, harness: str | None = None) -> dict:
    """Copie .agents/ (commun) + les artefacts du HARNESS choisi vers la racine du
    projet, et crée un gabarit need.md s'il manque. Un équipement existant est
    SAUVEGARDÉ avant d'être recouvert (copie par-dessus : les skills custom de
    l'utilisateur, absents de la distro, ne sont jamais supprimés — et la sauvegarde
    couvre le reste), puis les sauvegardes au-delà des EQUIP_BACKUPS_KEPT plus récentes
    sont purgées.

    Le harness choisi est écrit dans le marqueur : c'est LUI que les orchestrateurs
    liront (resolve_runner) et que l'app affichera. Ré-équiper dans l'autre harness ne
    supprime pas les artefacts du premier — ils deviennent inertes, et le marqueur
    tranche."""
    if not os.path.isdir(path):
        raise ValueError(L(f"Dossier introuvable : {path}", f"Folder not found: {path}"))
    harness = harness if harness in HARNESSES else harness_of(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copied, backups, pruned = [], [], []
    for name in equip_dirs(harness):
        src = os.path.join(engine["home"], name)
        dst = os.path.join(path, name)
        if not os.path.isdir(src):
            raise RuntimeError(L(f"Installation incomplète : {src} est absent du moteur.",
                                 f"Incomplete installation: {src} is missing from the engine."))
        if os.path.isdir(dst):
            backup = f"{dst}.bak-{stamp}"
            shutil.copytree(dst, backup)
            backups.append(os.path.basename(backup))
        shutil.copytree(src, dst, dirs_exist_ok=True)
        pruned.extend(_prune_equip_backups(dst))
        copied.append(name)
    # Fichiers d'équipement (AGENTS.md côté Codex) : mêmes règles, sauvegarde comprise.
    for name in equip_files(harness):
        src = os.path.join(engine["home"], name)
        dst = os.path.join(path, name)
        if not os.path.isfile(src):
            raise RuntimeError(L(f"Installation incomplète : {src} est absent du moteur.",
                                 f"Incomplete installation: {src} is missing from the engine."))
        if os.path.isfile(dst):
            backup = f"{dst}.bak-{stamp}"
            shutil.copy2(dst, backup)
            backups.append(os.path.basename(backup))
        shutil.copy2(src, dst)
        pruned.extend(_prune_equip_backups(dst))
        copied.append(name)
    need_created = False
    need_path = os.path.join(path, NEED_FILE)
    if not os.path.exists(need_path):
        with open(need_path, "w", encoding="utf-8") as f:
            f.write(L(NEED_TEMPLATE, NEED_TEMPLATE_ENG))  # gabarit dans la langue active
        need_created = True
    # Marqueur de version : c'est lui qui permet le « mise à jour des prompts disponible »
    # quand la distro évolue, qui retient QUEL moteur a fourni les skills (garde
    # anti-mélange au lancement) et QUEL harness pilote ce projet.
    # Un projet équipé avant cette évolution n'a pas de marqueur : le moteur retombe
    # alors sur l'inférence par artefacts.
    distro_version = engine.get("distro_version")
    marker = {"distro_version": distro_version, "engine": engine["label"],
              "harness": harness,
              "app_version": APP_VERSION, "equipped_at": int(time.time())}
    # Les timeouts personnalisés (dialogue ⏱ de la carte projet) survivent au
    # ré-équipement : c'est un réglage de l'utilisateur, pas un artefact de la distro.
    previous = read_json(os.path.join(path, EQUIP_MARKER), {})
    if isinstance(previous, dict) and isinstance(previous.get("timeouts"), dict):
        marker["timeouts"] = previous["timeouts"]
    write_json_atomic(os.path.join(path, EQUIP_MARKER), marker)
    return {"copied": copied, "backups": backups, "pruned": pruned,
            "need_created": need_created, "distro_version": distro_version,
            "engine": engine["label"], "harness": harness,
            "harness_label": HARNESSES[harness]["label"]}


# ─── TIMEOUTS DU PROJET (section 'timeouts' du marqueur d'équipement) ─────────
# Deux réglages exposés à l'utilisateur, en secondes : 'phase' (garde-fou d'une passe
# d'agent, défaut 600) et 'verify' (commande de vérification — compilation + tests —,
# défaut 300). Les orchestrateurs les lisent au démarrage (mm_runner.resolve_timeout :
# env MM_*_TIMEOUT > marqueur > défaut). Les filets de résilience (retries, backstops
# de mutation) ne sont volontairement PAS exposés.
TIMEOUT_KEYS = ("phase", "verify")
TIMEOUT_MIN, TIMEOUT_MAX = 60, 7200


def set_project_timeouts(project: str, values: dict) -> dict:
    """Écrit la section 'timeouts' du marqueur. Valeur absente/None/'' = retour au
    défaut de l'orchestrateur (clé retirée) ; valeur hors bornes ou non entière =
    refus explicite (le moteur ignorerait silencieusement, l'app doit prévenir).
    Marqueur absent (projet jamais équipé par une app qui pose le marqueur) : créé minimal — le
    moteur tolère un marqueur partiel, et equip_project préserve la section."""
    marker_path = os.path.join(project, EQUIP_MARKER)
    marker = read_json(marker_path, {})
    if not isinstance(marker, dict):
        marker = {}
    timeouts = dict(marker.get("timeouts")) if isinstance(marker.get("timeouts"), dict) else {}
    for key in TIMEOUT_KEYS:
        if key not in values:
            continue
        raw = values.get(key)
        if raw in (None, ""):
            timeouts.pop(key, None)
            continue
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            raise ValueError(L(f"Timeout « {key} » : nombre entier de secondes attendu.",
                               f"Timeout “{key}”: expected a whole number of seconds."))
        if not TIMEOUT_MIN <= seconds <= TIMEOUT_MAX:
            raise ValueError(L(f"Timeout « {key} » : entre {TIMEOUT_MIN} et {TIMEOUT_MAX} secondes.",
                               f"Timeout “{key}”: between {TIMEOUT_MIN} and {TIMEOUT_MAX} seconds."))
        timeouts[key] = seconds
    if timeouts:
        marker["timeouts"] = timeouts
    else:
        marker.pop("timeouts", None)
    write_json_atomic(marker_path, marker)
    return {"timeouts": marker.get("timeouts")}


# ─── NETTOYER UN PROJET (repartir d'une base propre) ─────────────────────────

def cleanable_present(project: str) -> list[dict]:
    """Artefacts de l'usine PRÉSENTS à la racine, dans l'ordre du pipeline : fichiers, puis
    dossiers de constats. Le serveur est seul juge de ce qu'il sait nettoyer — l'UI affiche
    et renvoie cette liste au lieu de la deviner (elle ignore CLEANABLE_DIRS).

    need.md n'y entre que s'il porte un VRAI besoin : sur un projet fraîchement équipé, le
    gabarit n'a rien à perdre et le bouton n'a aucune raison d'apparaître pour lui."""
    present = []
    for name in CLEANABLE:
        if not os.path.lexists(os.path.join(project, name)):
            continue
        if name == NEED_FILE and not need_md_state(project)["ready"]:
            continue
        present.append({"name": name, "dir": False})
    present += [{"name": name, "dir": True} for name in CLEANABLE_DIRS
                if os.path.isdir(os.path.join(project, name))
                and not os.path.islink(os.path.join(project, name))]
    return present


def clean_project(path: str, names: list | None) -> dict:
    """Supprime des artefacts de l'usine à la racine du projet (spec, plan, blackboard,
    rapports, cartes, dossiers de constats) ET les sentinelles de reprise qui leur sont
    attachées. Sans ce geste, un run relancé REPREND là où le précédent s'est arrêté (les
    fichiers SONT l'état de reprise) au lieu de repartir du besoin — et il fallait jusqu'ici
    passer par le terminal pour les effacer. need.md, l'équipement et le code produit ne
    sont jamais touchés ; les listes sont fermées (CLEANABLE, CLEANABLE_DIRS), jamais un
    chemin venu du navigateur — et un lien symbolique n'est jamais suivi.

    `names` à None nettoie tout ce qui est présent. Un fichier absent n'est pas une
    erreur (geste idempotent) ; un échec réel (droits, dossier en lecture seule) est
    RENDU à l'appelant plutôt qu'avalé — l'UI le dit au lieu d'annoncer un faux succès."""
    if not os.path.isdir(path):
        raise ValueError(L(f"Dossier introuvable : {path}", f"Folder not found: {path}"))
    if names is None:
        targets = CLEANABLE + CLEANABLE_DIRS
    else:
        if not isinstance(names, list):
            raise ValueError(L("Liste de fichiers attendue.", "Expected a list of files."))
        targets = [str(name) for name in names]
        unknown = [name for name in targets
                   if name not in CLEANABLE and name not in CLEANABLE_DIRS]
        if unknown:
            raise ValueError(L(f"Ce fichier ne peut pas être supprimé depuis l'app : {unknown[0]}",
                               f"This file can't be deleted from the app: {unknown[0]}"))
    removed, sentinels, failed = [], [], []
    for name in targets:
        target = os.path.join(path, name)
        if not os.path.lexists(target):
            continue
        is_dir = name in CLEANABLE_DIRS and os.path.isdir(target) and not os.path.islink(target)
        try:
            if is_dir:
                shutil.rmtree(target)
            else:
                os.remove(target)
        except OSError as err:
            failed.append(f"{name} ({err.strerror or err})")
            continue
        removed.append(name + "/" if is_dir else name)
        for sentinel in CLEAN_SENTINELS.get(name, []):
            sentinel_path = os.path.join(path, sentinel)
            if not os.path.lexists(sentinel_path):
                continue
            try:
                os.remove(sentinel_path)
            except OSError:
                continue  # sentinelle récalcitrante : l'orchestrateur purge la sienne au run
            sentinels.append(sentinel)
        # Compagnons à nom dynamique (motif serveur fermé, cf. CLEAN_COMPANION_GLOBS) :
        # nettoyer impact.md emporte les impact-phase-<id>.md du même run.
        for pattern in CLEAN_COMPANION_GLOBS.get(name, []):
            try:
                companions = sorted(n for n in os.listdir(path) if fnmatch.fnmatch(n, pattern))
            except OSError:
                companions = []
            for companion in companions:
                companion_path = os.path.join(path, companion)
                if os.path.islink(companion_path) or not os.path.isfile(companion_path):
                    continue
                try:
                    os.remove(companion_path)
                except OSError as err:
                    failed.append(f"{companion} ({err.strerror or err})")
                    continue
                removed.append(companion)
    return {"removed": removed, "sentinels": sentinels, "failed": failed}


# ─── MINI-RENDU MARKDOWN (aperçu des portes et livrables) ────────────────────
# Volontairement minimal (titres, listes, tableaux, code, gras/italique/liens) :
# assez pour lire une spec ou un rapport, sans dépendance. blackboard.yaml et les
# fichiers non-markdown passent en bloc préformaté.

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD        = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC      = re.compile(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)")
_LINK        = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline_md(text: str) -> str:
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return text


def render_markdown(source: str) -> str:
    lines = source.splitlines()
    out, i = [], 0
    list_stack: list[str] = []

    def close_lists(to_depth=0):
        while len(list_stack) > to_depth:
            out.append(f"</{list_stack.pop()}>")

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        escaped = html_mod.escape(line)
        if line.startswith("```"):
            close_lists()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            out.append("<pre><code>" + html_mod.escape("\n".join(block)) + "</code></pre>")
            i += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            close_lists()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline_md(html_mod.escape(heading.group(2)))}</h{level}>")
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,})$", line):
            close_lists()
            out.append("<hr>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1].strip()):
            close_lists()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<table><thead><tr>" +
                       "".join(f"<th>{_inline_md(html_mod.escape(c))}</th>" for c in header) +
                       "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_inline_md(html_mod.escape(c))}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        bullet = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if bullet:
            depth = len(bullet.group(1)) // 2 + 1
            kind = "ol" if bullet.group(2)[0].isdigit() else "ul"
            while len(list_stack) > depth:
                out.append(f"</{list_stack.pop()}>")
            while len(list_stack) < depth:
                list_stack.append(kind)
                out.append(f"<{kind}>")
            out.append(f"<li>{_inline_md(html_mod.escape(bullet.group(3)))}</li>")
            i += 1
            continue
        if line.startswith(">"):
            close_lists()
            out.append(f"<blockquote>{_inline_md(html_mod.escape(line.lstrip('> ')))}</blockquote>")
            i += 1
            continue
        if not line.strip():
            close_lists()
            i += 1
            continue
        close_lists()
        out.append(f"<p>{_inline_md(escaped)}</p>")
        i += 1
    close_lists()
    return "\n".join(out)


def render_doc(project: str, name: str) -> dict:
    # Liste fermée + motif fermé : les noms fixes de DOC_WHITELIST, ou un rapport
    # d'arbitrage Yolo 'impact-phase-<id>.md' (nom NU exigé, jamais de chemin).
    if name not in DOC_WHITELIST \
            and not (os.path.basename(name) == name and DOC_DYNAMIC_RE.match(name)):
        raise ValueError(L("Ce fichier ne peut pas être ouvert depuis l'app.", "This file can't be opened from the app."))
    path = os.path.join(project, name)
    stat = os.stat(path)  # OSError -> 404 côté handler
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if name.endswith((".md", ".markdown")):
        rendered = render_markdown(content)
    else:
        rendered = "<pre><code>" + html_mod.escape(content) + "</code></pre>"
    return {"file": name, "mtime": int(stat.st_mtime), "html": rendered}


# ─── EXPLORATEUR DE DOSSIERS (sélection du projet à la souris) ───────────────
# Une page web ne peut pas obtenir le chemin ABSOLU d'un dossier via l'explorateur du
# navigateur (sandbox). Deux mécanismes, cumulés côté UI : le dialogue natif de l'OS
# quand il existe (powershell.exe sous WSL, zenity, osascript), et un explorateur servi par l'app en repli
# universel. L'app étant locale, elle a légitimement accès au système de fichiers de
# l'utilisateur — le jeton de session reste exigé comme partout.

FS_MAX_ENTRIES = 400


def list_directories(path: str | None) -> dict:
    base = os.path.realpath(os.path.expanduser(path or "~"))
    if not os.path.isdir(base):
        raise ValueError(L(f"Dossier introuvable : {base}", f"Folder not found: {base}"))
    dirs, truncated = [], False
    try:
        with os.scandir(base) as entries:
            for entry in sorted(entries, key=lambda e: e.name.lower()):
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    if len(dirs) >= FS_MAX_ENTRIES:
                        truncated = True
                        break
                    dirs.append({
                        "name": entry.name,
                        "path": entry.path,
                        # repère visuel : un dossier déjà équipé est probablement le bon
                        "equipped": os.path.isdir(os.path.join(entry.path, ".agents")),
                    })
    except PermissionError:
        raise ValueError(L(f"Accès refusé : {base}", f"Access denied: {base}"))
    parent = os.path.dirname(base)
    return {"path": base, "parent": parent if parent != base else None,
            "home": os.path.expanduser("~"), "dirs": dirs, "truncated": truncated}


def native_pick_directory() -> dict:
    """Dialogue natif de sélection de dossier, best-effort. {native: False} si aucun
    outil utilisable : l'UI bascule alors sur l'explorateur intégré."""
    env = _tmux_env()
    title = L("MAIsterMind — choisir le dossier du projet",
              "MAIsterMind — choose the project folder")
    ps_dialog = False
    if running_under_wsl() and (ps := _windows_powershell()):
        # Le vrai explorateur est côté Windows : powershell.exe ouvre le dialogue
        # natif, et le chemin choisi (D:\...) est traduit en /mnt/d/... au retour.
        ps_dialog = True
        ps_title = title.replace("'", "''")
        cmd = [ps, "-NoProfile", "-STA", "-Command",
               "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
               "Add-Type -AssemblyName System.Windows.Forms;"
               "$d=New-Object Windows.Forms.FolderBrowserDialog;"
               f"$d.Description='{ps_title}';$d.ShowNewFolderButton=$true;"
               "$o=New-Object Windows.Forms.Form -Property @{TopMost=$true};"
               "if($d.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"]
    elif shutil.which("zenity") and (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        cmd = ["zenity", "--file-selection", "--directory", "--title", title]
    elif sys.platform == "darwin" and shutil.which("osascript"):
        cmd = ["osascript", "-e",
               f'POSIX path of (choose folder with prompt "{title}")']
    else:
        return {"native": False}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return {"native": False}
    if proc.returncode != 0:
        # zenity/osascript sortent non-zéro quand on annule ; un échec de
        # powershell.exe est une vraie panne : repli sur l'explorateur intégré.
        return {"native": False} if ps_dialog else {"native": True, "cancelled": True}
    picked = translate_windows_path(proc.stdout.strip())
    return {"native": True, "path": picked} if picked else {"native": True, "cancelled": True}


# ─── LECTURE / ÉDITION DES FICHIERS DU PIPELINE ──────────────────────────────

def read_editable(project: str, name: str) -> dict:
    if name not in EDIT_WHITELIST:
        raise ValueError(L("Ce fichier ne peut pas être édité depuis l'app.", "This file can't be edited from the app."))
    path = os.path.join(project, name)
    stat = os.stat(path)  # OSError -> 404 côté handler
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    out = {"file": name, "mtime": int(stat.st_mtime), "content": content}
    if name == NEED_FILE:
        # Le bloc besoin de la Bibliothèque masque le gabarit (il affiche un champ vide
        # avec placeholder) : les gabarits vivent côté Python, jamais dupliqués en JS.
        out["is_template"] = content.strip() in (t.strip() for t in NEED_TEMPLATES)
    return out


def save_editable(project: str, name: str, content: str, base_mtime) -> dict:
    """Écriture atomique avec VERROU OPTIMISTE : la sauvegarde déclare le mtime sur
    lequel elle se fonde ; si le fichier a bougé entre-temps (orchestrateur, éditeur
    externe), on refuse (409) au lieu d'écraser en silence — même philosophie que le
    rechargement du blackboard pendant le prompt côté binaire."""
    if name not in EDIT_WHITELIST:
        raise ValueError(L("Ce fichier ne peut pas être édité depuis l'app.", "This file can't be edited from the app."))
    if not isinstance(content, str):
        raise ValueError(L("Contenu manquant.", "Missing content."))
    if len(content) > 2_000_000:
        raise ValueError(L("Contenu trop volumineux.", "Content too large."))
    path = os.path.join(project, name)
    if os.path.exists(path) and base_mtime is not None \
            and int(os.stat(path).st_mtime) != int(base_mtime):
        raise RuntimeError(L(f"{name} a été modifié entre-temps : recharge avant d'écraser.",
                             f"{name} changed while you were editing — reload before overwriting."))
    tmp = path + ".mm-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    return {"file": name, "mtime": int(os.stat(path).st_mtime)}


# ─── OUVERTURE DANS L'ÉDITEUR LOCAL (best-effort) ────────────────────────────

def _is_wsl() -> bool:
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


IS_WSL = _is_wsl()


def open_in_editor(target: str) -> str | None:
    # Sous WSL, xdg-open est souvent présent mais échoue sans serveur X : wslview
    # (paquet wslu) relaie vers l'application Windows associée — on le préfère là-bas.
    openers = (["wslview"], ["xdg-open"], ["open"]) if IS_WSL \
        else (["xdg-open"], ["wslview"], ["open"])
    for opener in openers:
        if shutil.which(opener[0]):
            subprocess.Popen([*opener, target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=_tmux_env())
            return opener[0]
    return None


# ─── API HTTP ─────────────────────────────────────────────────────────────────

SESSION_TOKEN = ""  # renseigné au démarrage
# Référence du serveur HTTP en cours : /api/quit l'éteint proprement depuis l'UI
# (lancement par double-clic = pas de terminal, donc pas de Ctrl+C possible).
_SERVER: dict = {"instance": None}


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MAIsterMindApp/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # — plomberie —

    def log_message(self, fmt, *args):  # journal serveur silencieux (l'UI suffit)
        pass

    def _deny(self, code: int, message: str):
        log_server_error(f"{self.command} {self.path}", code, message)
        self._send_json({"error": message}, code)

    def _send_json(self, payload, code: int = 200, extra_headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, code: int = 200, extra_headers: dict | None = None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _token_ok(self, query: dict) -> bool:
        provided = (query.get("t", [None])[0]
                    or self.headers.get("X-MM-Token"))
        if not provided:
            cookie = self.headers.get("Cookie") or ""
            match = re.search(r"(?:^|;\s*)mm_token=([\w-]+)", cookie)
            provided = match.group(1) if match else None
        return bool(provided) and provided == SESSION_TOKEN

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _registered_project(self, path: str | None) -> str:
        """Toute opération de run cible un projet DU REGISTRE (jamais un chemin
        arbitraire venu du navigateur : le registre est la liste d'autorisation)."""
        if path:
            path = os.path.realpath(os.path.expanduser(path))
            for p in load_registry():
                if p["path"] == path:
                    return path
        raise ValueError(L("Ce projet n'est pas dans ta liste : ajoute-le d'abord.",
                           "This project isn't in your list yet — add it first."))

    # — routes —

    def do_GET(self):
        if not self._host_ok():
            return self._deny(403, L("Hôte non autorisé.", "Host not allowed."))
        url = urlparse(self.path)
        query = parse_qs(url.query)
        _REQ.lang = "eng" if query.get("lang", ["fr"])[0] == "eng" else "fr"
        if not self._token_ok(query):
            if url.path.startswith("/api/"):
                return self._deny(403, L("Jeton de session invalide.", "Invalid session token."))
            return self._send_html(FORBIDDEN_PAGE, 403)
        try:
            if url.path == "/":
                return self._send_html(HTML_PAGE, extra_headers={
                    "Set-Cookie": f"mm_token={SESSION_TOKEN}; Path=/; SameSite=Strict; HttpOnly"})
            if url.path == "/api/ping":
                return self._send_json({"app": "maistermind", "version": APP_VERSION})
            if url.path == "/api/state":
                return self._send_json(self._state_payload())
            if url.path == "/api/run":
                project = self._registered_project(query.get("path", [None])[0])
                return self._send_json(self._run_payload(project))
            if url.path == "/api/doc":
                project = self._registered_project(query.get("path", [None])[0])
                name = query.get("file", [""])[0]
                try:
                    return self._send_json(render_doc(project, name))
                except OSError:
                    return self._deny(404, L(f"{name} introuvable dans ce projet.",
                                             f"{name} not found in this project."))
            if url.path == "/api/file":
                project = self._registered_project(query.get("path", [None])[0])
                name = query.get("file", [""])[0]
                try:
                    return self._send_json(read_editable(project, name))
                except OSError:
                    return self._deny(404, L(f"{name} introuvable dans ce projet.",
                                             f"{name} not found in this project."))
            if url.path == "/api/fs":
                return self._send_json(list_directories(query.get("path", [None])[0]))
            if url.path == "/api/events":
                return self._serve_events(query)
            return self._deny(404, L("Route inconnue.", "Unknown route."))
        except ValueError as err:
            return self._deny(400, str(err))
        except Exception as err:  # jamais de trace brute vers le navigateur
            return self._deny(500, L(f"Erreur interne : {err}", f"Internal error: {err}"))

    def do_POST(self):
        if not self._host_ok():
            return self._deny(403, L("Hôte non autorisé.", "Host not allowed."))
        url = urlparse(self.path)
        query = parse_qs(url.query)
        _REQ.lang = "eng" if query.get("lang", ["fr"])[0] == "eng" else "fr"
        if not self._token_ok(query):
            return self._deny(403, L("Jeton de session invalide.", "Invalid session token."))
        body = self._read_body()
        try:
            if url.path == "/api/project/add":
                project = register_project(str(body.get("path", "")))
                return self._send_json({"ok": True, **project})
            if url.path == "/api/project/forget":
                forget_project(self._registered_project(body.get("path")))
                return self._send_json({"ok": True})
            if url.path == "/api/project/equip":
                project = self._registered_project(body.get("path"))
                engine = resolve_engine(load_manifests(), str(body.get("engine") or "") or None)
                harness = str(body.get("harness") or "") or None
                if harness is not None and harness not in HARNESSES:
                    raise ValueError(L(f"Harness inconnu : {harness}.",
                                       f"Unknown harness: {harness}."))
                return self._send_json({"ok": True, **equip_project(project, engine, harness)})
            if url.path == "/api/project/timeouts":
                project = self._registered_project(body.get("path"))
                values = body.get("timeouts")
                if not isinstance(values, dict):
                    raise ValueError(L("Corps attendu : {timeouts: {phase, verify}}.",
                                       "Expected body: {timeouts: {phase, verify}}."))
                return self._send_json({"ok": True, **set_project_timeouts(project, values)})
            if url.path == "/api/project/clean":
                return self._clean_project(body)
            if url.path == "/api/run/start":
                return self._start_run(body)
            if url.path == "/api/run/gate":
                return self._answer_gate(body)
            if url.path == "/api/run/interrupt":
                project = self._registered_project(body.get("path"))
                tmux_interrupt(run_session_name(project))
                return self._send_json({"ok": True})
            if url.path == "/api/run/kill":
                project = self._registered_project(body.get("path"))
                kill_run(project)
                return self._send_json({"ok": True})
            if url.path == "/api/fs/pick":
                return self._send_json(native_pick_directory())
            if url.path == "/api/file/save":
                project = self._registered_project(body.get("path"))
                try:
                    result = save_editable(project, str(body.get("file", "")),
                                           body.get("content"), body.get("base_mtime"))
                except OSError as err:
                    return self._deny(500, L(f"Écriture impossible : {err}",
                                             f"Write failed: {err}"))
                return self._send_json({"ok": True, **result})
            if url.path == "/api/open":
                project = self._registered_project(body.get("path"))
                name = str(body.get("file", ""))
                # Même contrat que /api/doc : noms fixes de la liste, ou rapport
                # d'arbitrage Yolo 'impact-phase-<id>.md' (nom nu, motif fermé).
                if name not in DOC_WHITELIST \
                        and not (os.path.basename(name) == name and DOC_DYNAMIC_RE.match(name)):
                    raise ValueError(L("Ce fichier ne peut pas être ouvert depuis l'app.", "This file can't be opened from the app."))
                opener = open_in_editor(os.path.join(project, name))
                if opener is None:
                    raise ValueError(L(f"Aucune application associée : ouvre {os.path.join(project, name)} à la main.",
                                       f"No app is associated with this file — open {os.path.join(project, name)} yourself."))
                return self._send_json({"ok": True, "opener": opener})
            if url.path == "/api/quit":
                server = _SERVER.get("instance")
                if server is None:
                    raise RuntimeError(L("Serveur non initialisé.", "Server not initialized."))
                print(L("\n⏻ Arrêt demandé depuis l'interface — les runs tmux continuent.",
                        "\n⏻ Shutdown requested from the UI — tmux runs keep going."))
                # shutdown() attend la fin de serve_forever : depuis un thread dédié,
                # pour que CETTE réponse parte avant que le serveur ne se ferme.
                threading.Thread(target=server.shutdown, daemon=True).start()
                return self._send_json({"ok": True})
            return self._deny(404, L("Route inconnue.", "Unknown route."))
        except ValueError as err:
            return self._deny(400, str(err))
        except RuntimeError as err:
            return self._deny(409, str(err))
        except Exception as err:
            return self._deny(500, L(f"Erreur interne : {err}", f"Internal error: {err}"))

    # — flux d'événements (SSE) —

    def _serve_events(self, query: dict):
        """GET /api/events[?path=…] : text/event-stream. Pousse 'run' (payload /api/run)
        quand son contenu change (relu toutes les ~1 s, comparé par hash) et 'state'
        (payload /api/state) toutes les ~4 s — ce battement sert aussi à détecter les
        clients partis. Le client garde son polling en repli automatique."""
        global _sse_clients
        project = None
        path_param = query.get("path", [None])[0]
        if path_param:
            project = self._registered_project(path_param)  # ValueError -> 400 avant les en-têtes
        with _sse_lock:
            if _sse_clients >= SSE_MAX_CLIENTS:
                return self._deny(429, L("Trop d'onglets connectés en direct : celui-ci se mettra à jour un peu moins vite.",
                                         "Too many tabs connected live — this one will just refresh a bit more slowly."))
            _sse_clients += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(10)  # écriture bloquée = client parti : on ferme le thread
            self.wfile.write(b"retry: 2000\n\n")
            last_run_digest = None
            last_state_at = 0.0
            while True:
                if project:
                    payload = self._run_payload(project)
                    digest = hashlib.sha1(json.dumps(payload, sort_keys=True,
                                                     ensure_ascii=False).encode("utf-8")).hexdigest()
                    if digest != last_run_digest:
                        last_run_digest = digest
                        self._sse_emit("run", payload)
                now = time.time()
                if now - last_state_at >= SSE_STATE_PERIOD_S:
                    last_state_at = now
                    self._sse_emit("state", self._state_payload())
                time.sleep(SSE_RUN_PERIOD_S)
        except OSError:
            pass  # client parti (onglet fermé, veille, timeout d'écriture) : fin propre du thread
        finally:
            with _sse_lock:
                _sse_clients -= 1

    def _sse_emit(self, event: str, payload):
        data = json.dumps(payload, ensure_ascii=False)  # \n échappés : une seule ligne data
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # — vues —

    def _state_payload(self) -> dict:
        manifest = load_manifests()
        warnings = list(manifest["warnings"])
        if manifest.get("error"):
            warnings.append(manifest["error"])
        for engine in manifest["engines"]:
            if (engine.get("contract_version") or 0) > APP_CONTRACT_VERSION:
                warnings.append(L(
                    f"Le moteur {engine['label']} déclare contract_version="
                    f"{engine['contract_version']} ; cette app ne gère que la version "
                    f"{APP_CONTRACT_VERSION}. Mets l'app à jour.",
                    f"Engine {engine['label']} declares contract_version="
                    f"{engine['contract_version']}; this app understands version "
                    f"{APP_CONTRACT_VERSION}. Update the app."))
        adopt_orphan_sessions()
        return {
            "app": {"version": APP_VERSION, "contract_version": APP_CONTRACT_VERSION,
                    "install_dir": INSTALL_DIR, "warnings": warnings},
            "prereqs": check_prereqs(),
            # Les harness disponibles à l'équipement : l'UI en fait un bouton chacun.
            # Ordre stable (celui de la table) pour que les boutons ne dansent pas.
            "harnesses": [{"key": k, "label": h["label"], "config": h["config"]}
                          for k, h in HARNESSES.items()],
            "engines": manifest["engines"],
            "orchestrators": manifest["orchestrators"],
            "projects": [project_summary(p["path"], manifest) for p in load_registry()],
        }

    def _run_payload(self, project: str) -> dict:
        manifest = load_manifests()
        session = run_session_name(project)
        # Un seul onglet « agent » : la session est celle du harness de CE projet.
        harness = harness_of(project)
        agent_session = agent_session_name(project, harness)
        exists = tmux_has_session(session)
        dead, exit_code = tmux_dead_status(session, project) if exists else (False, None)
        agent_alive = tmux_has_session(agent_session)
        alt, cols, rows = tmux_pane_display(session) if exists else (False, None, None)
        agent_alt, agent_cols, agent_rows = tmux_pane_display(agent_session) if agent_alive else (False, None, None)
        # Capture brute pour la détection de porte (jamais d'échappements sous les regex),
        # capture -e séparée pour l'affichage en couleurs. En TUI (écran alternatif),
        # l'historique n'a aucun sens — l'écran se redessine sur place : on ne capture
        # que l'écran visible (sinon des lignes de l'écran de base s'y glissent).
        depth = 0 if alt else CAPTURE_LINES
        agent_depth = 0 if agent_alt else CAPTURE_LINES
        screen = tmux_capture(session, depth) if exists else None
        screen_ansi = tmux_capture(session, depth, ansi=True) if exists else None
        agent_screen = tmux_capture(agent_session, agent_depth) if agent_alive else None
        agent_screen_ansi = tmux_capture(agent_session, agent_depth, ansi=True) if agent_alive else None
        # Mode TUI : la marge de centrage est rognée avant affichage et cols devient la
        # largeur UTILE (fitTerm cale la police dessus). Sans risque pour detect_gate :
        # il lit la session orchestrateur, un log jamais en écran alternatif — et ses
        # regex strip()ent chaque ligne de toute façon.
        if alt:
            screen, screen_ansi, _eff = tui_trim(screen, screen_ansi)
            cols = _eff or cols
        if agent_alt:
            agent_screen, agent_screen_ansi, _agent_eff = tui_trim(agent_screen, agent_screen_ansi)
            agent_cols = _agent_eff or agent_cols
        gate, orch_id = None, None
        if exists and not dead:
            for orch in manifest["orchestrators"]:
                gate = detect_gate(orch, screen, project)
                if gate:
                    orch_id = orch["id"]
                    break
        files = scan_files(project)
        # Porte à fichier DYNAMIQUE (impact-phase-<id>.md) : son nom n'est pas dans
        # STATE_FILES — on l'ajoute au payload pour que l'UI charge et recharge l'aperçu
        # (ingestRun suit files[gate.file].mtime, exactement comme pour un nom fixe).
        if gate and gate.get("file") and gate["file"] not in files:
            gate_path = os.path.join(project, gate["file"])
            try:
                gate_stat = os.stat(gate_path)
                files[gate["file"]] = {"exists": True, "mtime": int(gate_stat.st_mtime),
                                       "size": gate_stat.st_size}
            except OSError:
                files[gate["file"]] = {"exists": False}
        blackboard = parse_blackboard(os.path.join(project, "blackboard.yaml"))
        phases = normalize_phases(blackboard)
        sentinels = [s for s in KNOWN_SENTINELS if os.path.exists(os.path.join(project, s))]
        run_binary = tmux_pane_binary(session) if exists else None
        steps_decl = declared_steps(manifest_orchestrator_by_binary(manifest, run_binary))
        return {
            "project": {"path": project, "name": os.path.basename(project.rstrip("/")),
                        "hash": project_hash(project)},
            "session": {"name": session, "exists": exists, "alive": exists and not dead,
                        "exit_code": exit_code,
                        "binary": run_binary,
                        "alt": alt, "cols": cols, "rows": rows},
            "agent_session": {"name": agent_session, "alive": agent_alive,
                              "alt": agent_alt, "cols": agent_cols, "rows": agent_rows},
            "screen": screen,
            "agent_screen": agent_screen,
            "screen_html": ansi_to_html(screen_ansi) if screen_ansi is not None else None,
            "agent_screen_html": ansi_to_html(agent_screen_ansi) if agent_screen_ansi is not None else None,
            "harness": {"key": harness, "label": HARNESSES[harness]["label"],
                        "model": configured_model(project, harness)},
            "gate": gate,
            "gate_orchestrator": orch_id,
            "step": (infer_declared_step(project, steps_decl, gate["id"] if gate else None,
                                         exists and not dead,
                                         exists and dead and exit_code == 0)
                     if steps_decl else
                     infer_step(files, phases, gate["id"] if gate else None,
                                exists and not dead, binary=run_binary)),
            "phases": phases,
            "blackboard": {
                "project": (blackboard or {}).get("project"),
                "verify_cmd": (blackboard or {}).get("verify_cmd"),
                "last_test_count": (blackboard or {}).get("last_test_count"),
                "degraded": bool((blackboard or {}).get("_degraded")),
            },
            "files": files,
            "cleanable": cleanable_present(project),
            "sentinels": sentinels,
        }

    # — actions —

    def _start_run(self, body: dict):
        project = self._registered_project(body.get("path"))
        orch = manifest_orchestrator(str(body.get("orchestrator", "")))
        if orch is None:
            raise ValueError(L("Cet orchestrateur n'est pas déclaré dans orchestrators.json.",
                               "This orchestrator isn't declared in orchestrators.json."))
        binary_path = resolve_binary_path(orch["home"], orch["binary"])
        if binary_path is None:
            missing = os.path.join(orch["home"], orch["binary"])
            raise ValueError(L(f"Binaire absent : {missing}", f"Missing binary: {missing}"))
        if not os.access(binary_path, os.X_OK):
            # Filet de la promesse « zéro chmod » : distribution recopiée ou mise à
            # jour pendant que l'app tourne — on répare avant de refuser.
            heal_engine_binaries()
        if not os.access(binary_path, os.X_OK):
            raise ValueError(L(
                f"Binaire toujours non exécutable après réparation automatique : {binary_path} "
                f"— dossier en lecture seule ? Déplace l'installation là où tu as le droit d'écrire.",
                f"Binary still not executable after automatic repair: {binary_path} "
                f"— read-only folder? Move the installation to a writable folder."))
        harness = harness_of(project)
        if not is_equipped(project, harness):
            expected = ", ".join([d + "/" for d in equip_dirs(harness)] + equip_files(harness))
            raise ValueError(L(f"Projet non équipé pour {HARNESSES[harness]['label']} : "
                               f"{expected} manquent (bouton « Équiper »).",
                               f"Project not equipped for {HARNESSES[harness]['label']}: "
                               f"{expected} are missing (“Equip” button)."))
        # Garde anti-mélange de skills : un projet équipé depuis un moteur X ne lance pas
        # un orchestrateur d'un moteur Y (marqueur legacy sans moteur : on laisse passer,
        # impossible de savoir — et les installations mono-moteur ne sont jamais bloquées).
        marker_engine = read_json(os.path.join(project, EQUIP_MARKER), {}).get("engine")
        if marker_engine and marker_engine != orch["engine"]:
            raise ValueError(L(
                f"Projet équipé depuis le moteur {marker_engine} : ré-équipe-le depuis le "
                f"moteur {orch['engine']} avant de lancer cet orchestrateur.",
                f"Project equipped from engine {marker_engine}: re-equip it from engine "
                f"{orch['engine']} before starting this orchestrator."))
        if orch.get("needs_need_md", True):
            need = need_md_state(project)
            if not need["ready"]:
                raise ValueError(need["why"] or L("need.md n'est pas prêt.", "need.md is not ready."))
        session = tmux_start_run(project, binary_path)
        return self._send_json({"ok": True, "session": session, "hash": project_hash(project)})

    def _clean_project(self, body: dict):
        project = self._registered_project(body.get("path"))
        # JAMAIS sous les pieds d'un run : l'orchestrateur lit ces fichiers comme état
        # (spec approuvée, blackboard rechargé à chaque phase) — les retirer en vol le
        # ferait dérailler, voire réécrire par-dessus. On refuse, l'UI dit quoi faire.
        session = run_session_name(project)
        if tmux_has_session(session):
            dead, _ = tmux_dead_status(session, project)
            if not dead:
                raise RuntimeError(L(
                    "Un run est actif sur ce projet : interromps-le avant de nettoyer.",
                    "A run is active for this project: interrupt it before cleaning."))
        files = body.get("files")
        return self._send_json({"ok": True, **clean_project(project, files)})

    def _answer_gate(self, body: dict):
        project = self._registered_project(body.get("path"))
        session = run_session_name(project)
        if not tmux_has_session(session):
            raise ValueError(L("Aucune session de run pour ce projet.",
                               "No run session for this project."))
        # Garde : ne JAMAIS taper à l'aveugle dans le pane. On répond seulement si une
        # porte est effectivement ouverte à l'écran à cet instant — et la réponse est
        # validée CONTRE le TYPE de cette porte : y/n (défaut historique), clé d'un
        # choix déclaré au manifeste, ou texte libre borné à une ligne.
        screen = tmux_capture(session, 60)
        manifest = load_manifests()
        gate = None
        for orch in manifest["orchestrators"]:
            gate = detect_gate(orch, screen)
            if gate:
                break
        if gate is None:
            raise RuntimeError(L("Aucune porte détectée à l'écran : rien n'a été envoyé.",
                                 "No gate detected on screen: nothing was sent."))
        answer = str(body.get("answer", "")).strip()
        kind = gate.get("kind") or "yn"
        if kind == "yn":
            answer = answer.lower()
            if answer not in ("y", "n"):
                raise ValueError(L("Réponse attendue : y ou n.", "Expected answer: y or n."))
        elif kind == "choice":
            answer = answer.lower()
            keys = [str(choice.get("key", "")).strip().lower()
                    for choice in (gate.get("choices") or [])]
            keys = [key for key in keys if key]
            if not keys or answer not in keys:
                raise ValueError(L(f"Réponse attendue : {', '.join(keys) or '(aucun choix déclaré)'}.",
                                   f"Expected answer: {', '.join(keys) or '(no declared choice)'}."))
        else:  # 'text' : une seule ligne imprimable — jamais de caractère de contrôle vers le pane
            if not answer or len(answer) > 200 or any(ord(ch) < 32 for ch in answer):
                raise ValueError(L("Réponse texte attendue : une ligne non vide, 200 caractères max.",
                                   "Expected text answer: one non-empty line, 200 chars max."))
        tmux_send_answer(session, answer)
        return self._send_json({"ok": True, "gate": gate["id"], "answer": answer})


# ─── PAGES EMBARQUÉES ─────────────────────────────────────────────────────────

FORBIDDEN_PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8">
<title>MAIsterMind — accès refusé</title>
<body style="font-family:system-ui;background:#191521;color:#ECE5DC;display:grid;place-items:center;min-height:100vh;margin:0">
<div style="max-width:52ch;padding:24px">
<h1 style="font-size:22px">Jeton de session manquant ou invalide<br>
<span style="font-size:16px;color:#9C93A6">Missing or invalid session token</span></h1>
<p style="color:#9C93A6;line-height:1.6">Ouvre l'app depuis l'URL complète affichée dans le terminal au lancement
(elle contient <code>?t=…</code>), ou relance <code>./MAIsterMind_App</code> : si une instance tourne déjà,
elle rouvrira le navigateur avec la bonne URL.</p>
<p style="color:#9C93A6;line-height:1.6">Open the app via the full URL printed in the terminal at launch
(it contains <code>?t=…</code>), or run <code>./MAIsterMind_App</code> again: if an instance is already
running, it will reopen the browser with the right URL.</p>
</div></body></html>"""

# UI volontairement en vanilla JS embarqué : zéro build front, zéro data-file Nuitka,
# et le contrat « fichier unique » de la famille est préservé. Les chaînes UI vivent
# dans l'objet I18N ci-dessous : la déclinaison ENG traduit cet objet, rien d'autre.
HTML_PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAIsterMind — cockpit</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%23FF9E63'/><stop offset='1' stop-color='%23F26A45'/></linearGradient></defs><rect x='6' y='6' width='88' height='88' rx='26' fill='url(%23g)'/></svg>">
<script>
/* Anti-FOUC : pose le thème AVANT le premier rendu — Aurore (clair) / Crépuscule (sombre).
   Priorité : ?theme= (débogage, captures — miroir de ?lang=) > choix mémorisé > OS. */
(function () {
  var q = new URLSearchParams(location.search).get("theme");
  var saved = (q === "dark" || q === "light") ? q : localStorage.getItem("mm_theme");
  var dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
})();
</script>
<style>
  /* ── Thèmes : Aurore (clair, défaut) / Crépuscule (sombre) — maquettes XLVII/XLVIII
     de PROPOSITIONS-DESIGN.html. data-theme sur <html> est la seule source de vérité
     (posé avant le premier rendu par le script du <head> ; bascule 🌙/☀️ en topbar).
     Contrastes : chaque paire texte/fond des deux thèmes est vérifiée ≥ 4.5:1 par
     tests/check_contrast.py — le relancer après toute retouche de ces variables.
     Les dégradés (--accent-hi/lo/rose), voiles et halos sont décoratifs : jamais
     porteurs de texte, hors périmètre du test (comme les LED). */
  :root {
    /* color-scheme : les composants natifs (liste déroulée d'un select, scrollbars)
       suivent le thème AFFICHÉ — celui de la bascule, plus seulement celui de l'OS. */
    color-scheme: light;
    --bg:#FAF3EA; --panel:#FFFFFF; --panel-warm:#FFFCF8; --panel-2:#F5EEE3;
    --ink:#2A2521; --ink-soft:#645E54; --ink-faint:#6E6960;
    --line:#F2E7D9; --line-strong:#E0D2BF;
    --accent:#BC431E; --accent-soft:#FCEAE1; --accent-ink:#40150A;
    --accent-hi:#FF9E63; --accent-lo:#F26A45; --accent-rose:#F58AA2;
    --ok:#127035; --ok-soft:#DFF0DA; --warn:#8A5406; --warn-soft:#F6ECD1;
    --err:#B3261E; --err-soft:#FAE1DC; --steel:#6B4FA0; --steel-soft:#EFE9F7;
    --term-bg:#201A2B; --term-ink:#DDD5E4; --track:#EFE4D6;
    --topbar:rgba(255,252,247,.75); --seg:rgba(255,255,255,.55); --seg-on:#FFFFFF;
    --seg-on-shadow:0 1px 2px rgba(0,0,0,.06),0 6px 16px -8px rgba(0,0,0,.25);
    --shadow:0 1px 2px rgba(42,37,33,.04),0 14px 30px -24px rgba(42,37,33,.28);
    --gate-shadow:0 1px 2px rgba(42,37,33,.03),0 34px 60px -36px rgba(242,106,69,.5);
    --backdrop:rgba(42,31,26,.45);
    /* Lumière du fond (signature des maquettes) : voile pêche-lavande diffus en haut de
       page — sans disque solaire/lunaire (astre décoratif retiré : il lisait comme un
       objet d'interface) ; --horizon (bas) réservé au soir. */
    --veil:radial-gradient(120% 150% at 50% -40%,rgba(245,138,162,.20),transparent 55%),
           linear-gradient(180deg,rgba(255,158,99,.24),rgba(242,106,69,.09) 40%,transparent 82%);
    --horizon:none;
    /* Chevron des <select>, dessiné par l'app (couleur = --ink du thème) : la flèche
       native varie selon navigateur/version/échelle d'affichage — non maîtrisable. */
    --select-arrow:url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' fill='none' stroke='%232A2521' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg:#141019; --panel:#221C2E; --panel-warm:#1B1725; --panel-2:#191423;
    --ink:#ECE5DC; --ink-soft:#B4ABBE; --ink-faint:#9C93A6;
    --line:#332C41; --line-strong:#4A4160;
    --accent:#F0AE63; --accent-soft:#3F3035; --accent-ink:#241E30;
    --accent-hi:#F5C07A; --accent-lo:#EC9E58; --accent-rose:#E98BA6;
    --ok:#4ADE80; --ok-soft:#16301F; --warn:#FACC15; --warn-soft:#322A12;
    --err:#F87171; --err-soft:#3A1D1C; --steel:#C9AEE7; --steel-soft:#2C2440;
    --term-bg:#120F17; --term-ink:#CFC7D4; --track:#2C2830;
    --topbar:rgba(20,16,25,.65); --seg:rgba(255,255,255,.05); --seg-on:rgba(255,255,255,.11);
    --seg-on-shadow:none;
    --shadow:0 1px 0 rgba(255,255,255,.04) inset,0 14px 30px -24px rgba(0,0,0,.6);
    --gate-shadow:0 1px 0 rgba(255,255,255,.06) inset,0 34px 60px -36px rgba(0,0,0,.85),0 0 46px -22px rgba(236,158,88,.4);
    --backdrop:rgba(8,6,12,.6);
    --veil:radial-gradient(120% 150% at 50% -30%,rgba(233,139,166,.16),transparent 55%),
           radial-gradient(130% 100% at 50% -12%,#2A2334,rgba(42,35,52,0) 58%),
           linear-gradient(180deg,rgba(240,174,99,.18),rgba(200,139,212,.05) 46%,transparent 82%);
    --horizon:linear-gradient(0deg,rgba(240,174,99,.10),rgba(233,139,166,.04) 40%,transparent);
    --select-arrow:url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' fill='none' stroke='%23ECE5DC' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); position:relative; min-height:100vh;
         font:14px/1.55 "Inter","SF Pro Text",system-ui,-apple-system,"Segoe UI",sans-serif; }
  /* Le lever/coucher de soleil des maquettes : deux calques décoratifs en z NÉGATIF,
     sous tout le contenu (le fond de body est peint par le canvas, donc encore derrière).
     Surtout pas de z-index sur .layout pour compenser : ça enfermerait le terminal
     plein écran (⛶, z-index:40) dans un contexte d'empilement sous la topbar sticky
     (z-index:5), qui recouvrirait alors ses onglets et le bouton de réduction.
     (Les halos utilisent color-mix ; navigateur sans color-mix = même UI, sans halos.) */
  body::before { content:""; position:absolute; top:0; left:0; right:0; height:280px;
                 background:var(--veil); pointer-events:none; z-index:-1; }
  body::after { content:""; position:fixed; bottom:0; left:0; right:0; height:130px;
                background:var(--horizon); pointer-events:none; z-index:-1; }
  a { color:var(--accent); text-decoration:none; }
  button { font:inherit; cursor:pointer; }
  /* Focus clavier VISIBLE partout (navigation Tab) — distinct de tout état « actif ». */
  :where(a, button, input, select, textarea, summary, [tabindex]):focus-visible {
    outline:2px solid var(--accent); outline-offset:2px; border-radius:6px;
  }
  .linklike { background:none; border:none; padding:0; color:var(--accent);
              cursor:pointer; font:inherit; }
  .linklike:disabled { opacity:.4; cursor:not-allowed; }
  code { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.9em;
         background:var(--panel-2); border:1px solid var(--line); border-radius:5px; padding:0 5px; }

  .topbar { display:flex; align-items:center; gap:16px; padding:10px 20px;
            background:var(--topbar); -webkit-backdrop-filter:blur(14px) saturate(1.15);
            backdrop-filter:blur(14px) saturate(1.15);
            border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
  .brand { font-weight:600; letter-spacing:-.01em; font-size:15.5px;
           display:flex; align-items:center; gap:9px; }
  .mark { width:24px; height:24px; border-radius:8px; flex:none;
          background:linear-gradient(145deg,var(--accent-hi),var(--accent-lo));
          box-shadow:0 5px 14px -4px color-mix(in srgb,var(--accent-lo) 55%,transparent),
                     0 1px 0 rgba(255,255,255,.35) inset; }
  .brand small { color:var(--ink-faint); font-weight:500; margin-left:2px; }
  .leds { display:flex; gap:6px; }
  .led { width:8px; height:8px; border-radius:50%; background:var(--line-strong); }
  .led.on { background:var(--ok); box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 18%,transparent); }
  .led.busy { background:var(--accent-lo); animation:pulse 2.2s infinite;
              box-shadow:0 0 0 3px color-mix(in srgb,var(--accent-lo) 18%,transparent); }
  @keyframes pulse { 50% { opacity:.35; } }
  @media (prefers-reduced-motion: reduce) { .led.busy { animation:none; } }
  /* Annoncé par les lecteurs d'écran mais invisible (annonce d'ouverture de porte). */
  .visually-hidden { position:absolute; width:1px; height:1px; margin:-1px; padding:0;
                     overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; border:0; }
  .iconbtn { border:1px solid var(--line); background:var(--seg); color:var(--ink-soft);
             border-radius:9px; padding:5px 11px; font-size:14px; line-height:1; }
  .iconbtn.ledsbtn { display:inline-flex; align-items:center; padding:8px 9px; }
  /* Bascule de thème : icône SVG au trait (currentColor), pas un emoji — rendu net et
     cohérent avec le reste de la topbar sur tous les OS. */
  .iconbtn.themebtn { display:inline-flex; align-items:center; padding:6px 9px; }
  .iconbtn.themebtn svg { display:block; }
  .iconbtn.themebtn:hover { color:var(--ink); }
  /* Sélecteur de projet en topbar : le contexte voyage avec l'app-shell (même id
     #projsel qu'avant : le handler change ne bouge pas). */
  .topbar select { width:auto; min-width:140px; max-width:240px;
                   padding-top:5px; padding-bottom:5px; font-size:12.5px; }
  nav.tabs { display:flex; gap:3px; margin-left:auto; background:var(--seg);
             padding:4px; border-radius:12px; }
  nav.tabs a { padding:6px 14px; border-radius:9px; color:var(--ink-soft);
               font-size:13px; font-weight:500; }
  nav.tabs a.active { background:var(--seg-on); color:var(--ink); font-weight:600;
                      box-shadow:var(--seg-on-shadow); }
  @media (max-width:900px) { .topbar { flex-wrap:wrap; row-gap:8px; } }

  .layout { max-width:1180px; margin:0 auto; padding:20px; }
  /* La Bibliothèque se lit comme un parcours : colonne centrée, largeur de lecture.
     Les vues denses (Run, Projets) gardent toute la largeur. */
  .flow { max-width:900px; margin:0 auto; }
  .card { background:linear-gradient(180deg,var(--panel),var(--panel-warm));
          border:1px solid var(--line); border-radius:14px;
          padding:14px 16px; box-shadow:var(--shadow); }
  .label { font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
           color:var(--ink-faint); font-weight:600; margin-bottom:8px; }
  .check { display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--ink-soft); padding:2px 0; }
  .dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
  .dot.ok { background:var(--ok); } .dot.ko { background:var(--err); } .dot.warn { background:var(--warn); }

  select, input[type=text] { width:100%; background:var(--panel-2); color:var(--ink);
    border:1px solid var(--line); border-radius:9px; padding:7px 10px; font:inherit; }
  select { appearance:none; -webkit-appearance:none; padding-right:28px;
    background-image:var(--select-arrow); background-repeat:no-repeat;
    background-position:right 10px center; background-size:10px 6px; }

  .kicker { font-size:11.5px; font-weight:600; letter-spacing:.08em; text-transform:uppercase;
            color:var(--accent); margin:0 0 3px; }
  h1.page { font-size:23px; margin:0 0 4px; letter-spacing:-.02em; font-weight:600; }
  .sub { color:var(--ink-faint); font-size:12.5px; margin-bottom:16px; }
  /* Pastille d'état à côté du titre (la « pastille d'heure » des maquettes). */
  .statchip { display:inline-flex; align-items:center; gap:6px; margin-left:10px; padding:3px 10px;
              border-radius:999px; font-size:11px; font-weight:600; letter-spacing:.02em;
              color:var(--accent); background:var(--accent-soft); vertical-align:3px; }
  .statchip::before { content:""; width:6px; height:6px; border-radius:50%; flex:none;
                      background:linear-gradient(180deg,var(--accent-hi),var(--accent-lo)); }
  .addrow { display:flex; gap:6px; margin-top:12px; }
  .addrow:first-child { margin-top:0; }
  .addrow input { flex:1; }

  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }
  .ocard { border:1px solid var(--line); border-radius:14px;
           background:linear-gradient(180deg,var(--panel),var(--panel-warm));
           padding:13px 14px; display:flex; flex-direction:column; gap:7px; box-shadow:var(--shadow); }
  /* Pas de bordure accent permanente sur la carte recommandée : ça se lit comme une
     sélection ou un focus figé. La recommandation est dite par un badge, pas par un cadre. */
  .oname { font-weight:700; font-size:13.5px; }
  .odesc { font-size:12px; color:var(--ink-soft); flex:1; }
  .badge { align-self:flex-start; font-size:9.5px; letter-spacing:.1em; text-transform:uppercase;
           border-radius:999px; padding:2px 9px; background:var(--accent-soft); color:var(--accent); }
  .badge.ok { background:var(--ok-soft); color:var(--ok); }
  .badge.warn { background:var(--warn-soft); color:var(--warn); }
  .badge.steel { background:var(--steel-soft); color:var(--steel); }
  .badge.err { background:var(--err-soft); color:var(--err); }

  /* Bouton principal : le dégradé de lever des maquettes, encre foncée par-dessus
     (le geste du bouton Crépuscule, appliqué aux deux thèmes : AA ≥ 4.5 garanti,
     là où le blanc de la maquette claire plafonnait à ~3:1). */
  .btn { border:none; border-radius:10px; padding:7px 15px; font-size:12.5px; font-weight:600;
         background:linear-gradient(145deg,var(--accent-hi),var(--accent-lo)); color:var(--accent-ink);
         box-shadow:0 6px 16px -6px color-mix(in srgb,var(--accent-lo) 55%,transparent),
                    0 1px 0 rgba(255,255,255,.3) inset; }
  .btn:disabled { opacity:.45; cursor:not-allowed; box-shadow:none; }
  .btn.ghost { background:var(--panel-2); border:1px solid var(--line); color:var(--ink-soft);
               font-weight:500; box-shadow:none; }
  .btn.danger { background:var(--err-soft); color:var(--err); box-shadow:none; }
  .btn.small { padding:3px 10px; font-size:11.5px; border-radius:8px; }
  .hintline { font-size:11px; color:var(--ink-faint); }

  .toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
           background:var(--term-bg); color:var(--term-ink); border-radius:10px; padding:10px 18px;
           font-size:13px; box-shadow:var(--shadow); z-index:20; max-width:80vw; }
  .toast.err { outline:1px solid var(--err); }

  /* run */
  .runmeta { display:flex; flex-wrap:wrap; gap:8px 18px; font-size:12px; color:var(--ink-faint); margin-bottom:14px; }
  .runmeta b { color:var(--ink-soft); }
  /* Ligne d'étapes signature (maquettes) : piste + fil en dégradé de lever + points
     done ✓ / now (halo) / todo, libellés sous les points. Sous 640px, les libellés
     non courants restent lus par les lecteurs d'écran mais disparaissent de l'écran. */
  .steps { position:relative; height:52px; margin:4px 0 16px; }
  .steps .trk { position:absolute; left:10px; right:10px; top:8px; height:4px; border-radius:3px;
                background:var(--track); }
  .steps .fil { position:absolute; left:10px; top:8px; height:4px; border-radius:3px;
                background:linear-gradient(90deg,var(--accent-hi),var(--accent-lo));
                box-shadow:0 1px 6px -1px color-mix(in srgb,var(--accent-lo) 50%,transparent); }
  .steps .st { position:absolute; top:0; transform:translateX(-50%); text-align:center; }
  .steps .st .d { width:18px; height:18px; border-radius:50%; margin:0 auto 8px; background:var(--panel);
                  border:2px solid var(--line-strong); display:grid; place-items:center; }
  .steps .st.done .d { background:linear-gradient(145deg,var(--accent-hi),var(--accent-lo)); border-color:transparent; }
  .steps .st.done .d::after { content:"✓"; color:var(--accent-ink); font-size:10px; font-weight:700; }
  .steps .st.now .d { border-color:var(--accent-lo);
                      box-shadow:0 0 0 4px color-mix(in srgb,var(--accent-lo) 16%,transparent); }
  .steps .st.now .d::after { content:""; width:7px; height:7px; border-radius:50%; background:var(--accent-lo); }
  .steps .st .l { font-size:11px; color:var(--ink-faint); white-space:nowrap; }
  .steps .st.done .l { color:var(--ink-soft); }
  .steps .st.now .l { color:var(--ink); font-weight:600; }
  @media (max-width:640px) {
    .steps .st:not(.now) .l { position:absolute; width:1px; height:1px; margin:-1px; padding:0;
                              overflow:hidden; clip:rect(0 0 0 0); border:0; }
  }
  .run-grid { display:grid; grid-template-columns:280px 1fr; gap:14px; align-items:start; }
  @media (max-width:900px) { .run-grid { grid-template-columns:1fr; } }
  .phase { display:flex; align-items:center; gap:9px; border:1px solid var(--line); border-radius:10px;
           padding:8px 11px; margin-bottom:7px; background:var(--panel); font-size:12px; }
  .phase .num { color:var(--ink-faint); min-width:16px; text-align:right; }
  .phase .nm { flex:1; font-weight:600; }
  .phase small { display:block; font-weight:400; color:var(--ink-faint); }
  .pstate { font-size:9px; letter-spacing:.09em; text-transform:uppercase; border-radius:999px; padding:2px 8px; }
  .pstate.done { background:var(--ok-soft); color:var(--ok); }
  .pstate.run { background:var(--accent-soft); color:var(--accent); }
  .pstate.todo { background:var(--panel-2); color:var(--ink-faint); }
  .pstate.rej { background:var(--err-soft); color:var(--err); }
  .phase.active { border-color:var(--accent-lo); }
  .phase.rejected { border-color:var(--err); }

  .termtabs { display:flex; gap:4px; margin-bottom:8px; }
  .termtabs button { border:1px solid var(--line); background:var(--panel); color:var(--ink-soft);
                     border-radius:8px 8px 0 0; padding:6px 14px; font-size:12px; border-bottom:none; }
  .termtabs button.active { background:var(--term-bg); color:var(--term-ink); border-color:var(--term-bg); }
  .termtabs .zoombtn { margin-left:auto; border-radius:8px; border-bottom:1px solid var(--line); }
  pre.term { background:var(--term-bg); color:var(--term-ink); border-radius:0 12px 12px 12px;
             margin:0; padding:12px 14px; font:11.5px/1.55 ui-monospace,Menlo,Consolas,monospace;
             height:430px; overflow:auto; white-space:pre-wrap; word-break:break-word; }
  /* Écran TUI (écran alternatif tmux : OpenCode, Codex) : grille à géométrie fixe, jamais
     repliée — replier une ligne détruit cadres et alignements. line-height:1 : les
     cellules se touchent comme dans un vrai terminal (des rangées espacées zèbrent
     fonds colorés et blocs ▀▄█ du logo). fitTerm() cale la police pour que l'écran
     ENTIER (largeur et hauteur) tienne dans le panneau. */
  pre.term.tui { white-space:pre; word-break:normal; line-height:1;
                 font-variant-ligatures:none; }
  .termwrap.zoom { position:fixed; inset:12px; z-index:40; display:flex; flex-direction:column;
                   background:var(--panel); border:1px solid var(--line-strong); border-radius:12px;
                   padding:14px; box-shadow:var(--shadow); }
  .termwrap.zoom pre.term { flex:1; height:auto; }

  /* La porte : carte nimbée, liseré d'aurore (corail→abricot→rose / or→ambre→rose)
     et kicker à pastille — le moment central des maquettes. */
  .gate { position:relative; overflow:hidden; border:1px solid var(--line); border-radius:16px;
          background:linear-gradient(180deg,var(--panel),var(--panel-warm));
          padding:18px 22px 16px; box-shadow:var(--gate-shadow); margin-bottom:16px; }
  .gate::before { content:""; position:absolute; top:0; left:0; right:0; height:3px;
                  background:linear-gradient(90deg,var(--accent-hi),var(--accent-lo) 55%,var(--accent-rose)); }
  .gate-k { display:inline-flex; align-items:center; gap:8px; margin:0 0 8px; font-size:11.5px;
            font-weight:600; letter-spacing:.06em; text-transform:uppercase; color:var(--accent); }
  .gate-k::before { content:""; width:7px; height:7px; border-radius:50%; background:var(--accent-lo); flex:none;
                    box-shadow:0 0 0 4px color-mix(in srgb,var(--accent-lo) 16%,transparent); }
  .gate h2 { margin:0 0 4px; font-size:19px; letter-spacing:-.02em; }
  .gate .hint { font-size:13px; color:var(--ink-soft); margin-bottom:12px; max-width:70ch; }
  .gate-doc { border:1px solid var(--line); border-radius:10px; background:var(--panel-2);
              padding:6px 16px; max-height:340px; overflow:auto; font-size:13px; margin-bottom:12px; }
  .gate-doc h1 { font-size:17px; } .gate-doc h2 { font-size:15px; } .gate-doc h3 { font-size:13.5px; }
  .gate-doc table { border-collapse:collapse; margin:8px 0; }
  .gate-doc th, .gate-doc td { border:1px solid var(--line); padding:4px 9px; font-size:12px; text-align:left; }
  /* Blocs de code des DOCUMENTS (spec, plan, blackboard, rapports) : couleurs du thème,
     pas celles du terminal — en thème clair, --term-bg/--term-ink donnaient un bloc noir
     à texte clair, et la règle globale `code {}` (fond --panel-2, bordure) s'appliquait
     dedans : « surlignage » clair sous du texte clair, illisible. */
  .gate-doc pre { background:var(--panel); color:var(--ink); border:1px solid var(--line);
                  padding:10px 12px; border-radius:8px; overflow:auto; font-size:12px; line-height:1.5; }
  .gate-doc pre code { background:none; border:none; padding:0; font-size:inherit; color:inherit; }
  .gate-doc blockquote { margin:8px 0; padding:4px 12px; border-left:3px solid var(--line-strong);
                         color:var(--ink-soft); }
  .gate-bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }

  .banner { border-radius:10px; padding:11px 16px; font-size:13px; margin-bottom:14px; }
  .banner.ok { background:var(--ok-soft); color:var(--ok); }
  .banner.err { background:var(--err-soft); color:var(--err); }
  .banner.warn { background:var(--warn-soft); color:var(--warn); }

  .pcard { border:1px solid var(--line); border-radius:14px; padding:13px 15px;
           background:linear-gradient(180deg,var(--panel),var(--panel-warm));
           box-shadow:var(--shadow); display:flex; flex-direction:column; gap:6px; }
  .path { font-size:11px; color:var(--ink-faint); word-break:break-all;
          font-family:ui-monospace,Menlo,monospace; }
  .chips { display:flex; gap:6px; flex-wrap:wrap; }
  .actions { display:flex; gap:6px; flex-wrap:wrap; margin-top:4px; }
  /* Livrables : lignes en liste dans les cartes de run (div, horodatage calé à droite),
     mentions en ligne sur les cartes projet (span) — deux usages, deux formes. */
  div.dlv { display:flex; align-items:center; gap:7px; padding:7px 0; font-size:12px;
            color:var(--ink-soft); border-bottom:1px solid var(--line); }
  div.dlv:last-child { border-bottom:none; }
  div.dlv .hintline { margin-left:auto; font-variant-numeric:tabular-nums; }
  span.dlv { font-size:11.5px; color:var(--ink-soft); }
  .dlv a { cursor:pointer; }

  /* État actif SANS cadre plein (un fond+bordure accent se lit comme un anneau de focus
     figé à côté du titre) : icône colorée + point, le focus clavier reste le seul anneau. */
  .bell { opacity:.6; position:relative; }
  .bell.on { opacity:1; color:var(--accent); }
  .bell.on::after { content:""; position:absolute; top:3px; right:4px; width:6px; height:6px;
                    border-radius:50%; background:var(--ok); }

  .fs-bar { display:flex; gap:8px; align-items:center; padding:10px 18px; border-bottom:1px solid var(--line);
            font-family:ui-monospace,Menlo,monospace; font-size:11.5px; color:var(--ink-soft); flex-wrap:wrap; }
  .fs-list { max-height:52vh; overflow:auto; padding:6px 10px; }
  .fs-item { display:flex; align-items:center; gap:9px; width:100%; text-align:left;
             border:none; background:transparent; color:var(--ink); border-radius:8px;
             padding:7px 10px; font-size:13px; }
  .fs-item:hover { background:var(--panel-2); }
  .fs-item .tag { margin-left:auto; font-size:9px; letter-spacing:.08em; text-transform:uppercase;
                  background:var(--ok-soft); color:var(--ok); border-radius:999px; padding:1px 7px; }

  /* Champs de saisie (éditeur, bloc besoin) : matière PAPIER du thème courant (--panel/
     --ink), pas la matière terminal — un champ de texte sombre au milieu d'un thème
     clair lisait comme un corps étranger. La matière sombre reste réservée aux
     TERMINAUX (pre.term) et aux blocs de code, qui sont des sorties, pas des saisies. */
  textarea.editor { width:100%; min-height:46vh; resize:vertical; background:var(--panel); color:var(--ink);
                    border:1px solid var(--line-strong); border-radius:9px; padding:12px 14px;
                    font:12.5px/1.6 ui-monospace,Menlo,Consolas,monospace; }
  /* Le bloc besoin de la Bibliothèque : même matière que l'éditeur, hauteur de brief
     (quelques phrases suffisent — le champ s'étire à la demande). */
  textarea.needta { width:100%; min-height:120px; resize:vertical; background:var(--panel); color:var(--ink);
                    border:1px solid var(--line-strong); border-radius:9px; padding:12px 14px;
                    font:12.5px/1.6 ui-monospace,Menlo,Consolas,monospace; }
  textarea.editor::placeholder, textarea.needta::placeholder { color:var(--ink-faint); }
  textarea.needta:disabled { opacity:.55; }

  details.fb { margin:4px 0 8px 26px; font-size:12px; color:var(--ink-soft); }
  details.fb summary { cursor:pointer; color:var(--err); font-weight:600; font-size:11.5px; }
  details.fb.retry summary { color:var(--warn); }
  details.fb pre { white-space:pre-wrap; word-break:break-word; background:var(--panel-2);
                   border:1px solid var(--line); border-radius:8px; padding:9px 11px; margin:6px 0 0; }

  dialog { border:1px solid var(--line-strong); border-radius:16px; background:var(--panel); color:var(--ink);
           max-width:min(860px,92vw); width:100%; padding:0; box-shadow:var(--shadow); }
  dialog::backdrop { background:var(--backdrop); }
  .dlg-head { display:flex; align-items:center; gap:10px; padding:12px 18px; border-bottom:1px solid var(--line); }
  .dlg-body { padding:8px 20px 20px; max-height:70vh; overflow:auto; font-size:13.5px; }
  .empty { color:var(--ink-faint); font-size:13px; border:1px dashed var(--line-strong);
           border-radius:10px; padding:22px; text-align:center; }
</style>
</head>
<body>

<div class="topbar">
  <button class="iconbtn ledsbtn" id="ledsbtn" data-action="sys-open" data-i18n-aria="sys_open">
    <span class="leds" id="leds"><span class="led"></span><span class="led"></span><span class="led"></span></span>
  </button>
  <span class="brand"><span class="mark" aria-hidden="true"></span>MAIsterMind <small id="appver"></small></span>
  <select id="projsel" data-i18n-aria="active_project" hidden></select>
  <button class="iconbtn bell" id="bell" title="Notifications de porte et de fin de run"
          aria-label="Notifications de porte et de fin de run" aria-pressed="false" data-action="toggle-notif">🔔</button>
  <button class="iconbtn themebtn" id="themebtn" data-action="toggle-theme"></button>
  <nav class="tabs">
    <a href="#/bibliotheque" data-tab="bibliotheque" data-i18n="nav_library">Bibliothèque</a>
    <a href="#/run" data-tab="run" data-i18n="nav_run">Run</a>
    <a href="#/projets" data-tab="projets" data-i18n="nav_projects">Projets</a>
  </nav>
</div>

<div class="layout" id="view"></div>

<dialog id="docdlg" aria-labelledby="docttl">
  <div class="dlg-head"><b id="docttl"></b><span class="hintline" id="docmeta"></span>
    <button class="btn ghost" style="margin-left:auto" data-action="dlg-close" data-i18n="close">Fermer</button></div>
  <div class="dlg-body gate-doc" style="max-height:70vh;border:none;background:var(--panel)" id="docbody"></div>
</dialog>

<dialog id="fsdlg" aria-labelledby="fsttl">
  <div class="dlg-head"><b id="fsttl" data-i18n="fs_title">Choisir le dossier du projet</b>
    <button class="btn ghost" style="margin-left:auto" data-action="dlg-close" data-i18n="cancel">Annuler</button></div>
  <div class="fs-bar"><span id="fspath" style="word-break:break-all;flex:1"></span>
    <button class="btn ghost" data-action="fs-up" id="fsup" data-i18n="fs_up">⬆ Dossier parent</button>
    <button class="btn ghost" data-action="fs-home" aria-label="Dossier personnel" data-i18n-aria="fs_home_label">🏠</button>
    <button class="btn" data-action="fs-choose" data-i18n="choose_here">Choisir ce dossier</button></div>
  <div class="fs-list" id="fslist"></div>
</dialog>

<dialog id="editdlg" aria-labelledby="editttl">
  <div class="dlg-head"><b id="editttl"></b><span class="hintline" id="editmeta"></span>
    <button class="btn" style="margin-left:auto" id="editsave" data-action="editor-save" data-i18n="save_btn">Enregistrer</button>
    <button class="btn ghost" data-action="dlg-close" data-i18n="close">Fermer</button></div>
  <div class="dlg-body" style="padding-top:14px">
    <textarea class="editor" id="editta" spellcheck="false"></textarea>
    <div class="hintline" style="margin-top:6px" data-i18n="editor_hint">Ctrl+S pour enregistrer. Si le fichier change de son côté pendant l'édition (orchestrateur, autre éditeur), l'app refuse d'écraser et te propose de recharger.</div>
  </div>
</dialog>

<dialog id="tmodlg" aria-labelledby="tmottl" style="max-width:min(520px,92vw)">
  <div class="dlg-head"><b id="tmottl" data-i18n="tmo_title">⏱ Timeouts du projet</b>
    <button class="btn ghost" style="margin-left:auto" data-action="dlg-close" data-i18n="close">Fermer</button></div>
  <div class="dlg-body" style="padding-top:14px">
    <div class="hintline" data-i18n="tmo_hint"></div>
    <div class="label" style="margin-top:12px" data-i18n="tmo_verify_label"></div>
    <input type="number" id="tmoverify" min="60" max="7200" step="30" style="max-width:180px" placeholder="300">
    <div class="label" style="margin-top:12px" data-i18n="tmo_phase_label"></div>
    <input type="number" id="tmophase" min="60" max="7200" step="30" style="max-width:180px" placeholder="600">
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="btn" data-action="timeouts-save" data-i18n="tmo_save">Enregistrer</button>
    </div>
  </div>
</dialog>

<dialog id="sysdlg" aria-labelledby="systtl" style="max-width:min(560px,92vw)">
  <div class="dlg-head"><b id="systtl" data-i18n="sys_title">Statut &amp; réglages</b>
    <button class="btn ghost" style="margin-left:auto" data-action="dlg-close" data-i18n="close">Fermer</button></div>
  <div class="dlg-body" style="padding-top:14px">
    <div id="sysbody"></div>
    <div class="label" style="margin-top:14px" data-i18n="lang_label">Langue / Language</div>
    <select id="langsel" data-i18n-aria="lang_label" style="max-width:220px">
      <option value="fr">Français</option>
      <option value="eng">English</option>
    </select>
  </div>
</dialog>

<script>
"use strict";
/* Chaînes UI centralisées, bilingue à bord : I18N pointe vers l'objet de la langue
   active (sélecteur du panneau latéral). Ajouter une clé = l'ajouter aux DEUX objets. */
const I18N_FR = {
  library_title: "Bibliothèque",
  project_step: "1 · Choisis le projet",
  project_step_hint: "Choisis le dossier d'un projet à équiper : colle son chemin, ou va le chercher dans tes dossiers.",
  need_step: "2 · Décris ton besoin",
  orch_step: "3 · Choisis un orchestrateur",
  need_ph: "Explique le problème à résoudre et pour qui, ce que l'outil doit faire, les contraintes connues (stack imposée, fichiers à référencer…) — comme tu l'expliquerais à un collègue. L'Agent PO en fera une spécification (spec.md) que tu valideras.",
  need_save: "Enregistrer le besoin",
  need_ready: "✓ need.md prêt",
  need_unsaved: "modifications non enregistrées — elles partiront dans need.md quand tu enregistreras, ou au lancement d'un run",
  need_absent: "need.md sera créé à la racine du projet quand tu enregistreras",
  library_sub: (n) => n + " binaire(s) présent(s) · découverts via orchestrators.json · un run interrompu reprend là où il en était",
  no_manifest: "Aucun orchestrateur : vérifie orchestrators.json, à côté de l'app ou dans le sous-dossier d'un moteur.",
  no_binaries: "Aucun binaire trouvé : ceux annoncés par orchestrators.json ont disparu. Remets un binaire dans le dossier de son moteur pour le revoir ici.",
  engine_name: (l) => l === "." ? "dossier de l'app" : l,
  engines: "Moteurs", no_engine: "aucun moteur découvert", binaries_word: "binaire(s)",
  active_project: "Projet actif", prereqs: "Prérequis", install_dir: "Dossier de l'app",
  add_project_ph: "/chemin/vers/ton/projet", add: "Ajouter", equip: "Équiper", reequip: "Mettre à jour l'équipement",
  launch: "Lancer", open_run: "Ouvrir le run", forget: "Oublier", equipped: "équipé", not_equipped: "non équipé",
  tmo_btn: "Timeouts",
  tmo_title: "⏱ Timeouts du projet",
  tmo_hint: "En secondes, vide = défaut de l'orchestrateur. Pris en compte au prochain lancement (les orchestrateurs lisent .mm-equip.json au démarrage) — un run en cours n'est pas modifié. À augmenter si ta suite de tests dépasse 5 minutes.",
  tmo_verify_label: "Vérification (compilation + tests) — défaut 300 s",
  tmo_phase_label: "Garde-fou d'une passe d'agent — défaut 600 s",
  tmo_save: "Enregistrer",
  tmo_saved: "Timeouts enregistrés (.mm-equip.json)",
  tmo_invalid: "Valeurs entières entre 60 et 7200 secondes (vide = défaut).",
  tmo_badge: (t) => "⏱ " + [t.verify != null ? "verify " + t.verify + " s" : "", t.phase != null ? "phase " + t.phase + " s" : ""].filter(Boolean).join(" · "),
  tmo_badge_title: "Timeouts personnalisés de ce projet (bouton Timeouts pour les changer)",
  running: "run en cours", finished: "terminé", need_missing: "besoin à décrire",
  projects_title: "Projets équipés",
  projects_sub: "L'app copie .agents/ et les fichiers du harness choisi à la racine du projet · les binaires, eux, ne bougent jamais du dossier de leur moteur",
  no_projects: "Aucun projet enregistré : ajoute le dossier de ton projet ci-dessus.",
  run_title: "Suivi de run", no_run: "Aucun run pour ce projet. Lance un orchestrateur depuis la Bibliothèque.",
  no_project_selected: "Sélectionne un projet (Bibliothèque ou Projets).",
  steps: ["Spécification (PO)", "Plan (Architecte)", "Blackboard", "Production", "Refactoring"],
  tab_orch: "Orchestrateur", tab_agent: (h) => h + " (IA)",
  harness_word: "harness", harness_absent: "absent du PATH", harness_unauth: "non authentifié",
  no_harness: "Aucun harness d'agent IA installé : installe OpenCode (https://opencode.ai/docs) ou Codex CLI (npm install -g @openai/codex), puis connecte-toi. Détail dans Statut & réglages.",
  harness_model: (m) => "modèle : " + m, harness_model_default: "modèle par défaut du harness",
  term_zoom: "Agrandir le terminal", term_unzoom: "Réduire le terminal (Échap)",
  gate_watch: (f) => f + " est surveillé : si tu le modifies ailleurs, l'aperçu se met à jour tout seul",
  open_editor: "Ouvrir dans l'éditeur", validate: "Valider", reject: "Refuser (l'orchestrateur s'arrête)",
  gate_send: "Envoyer",
  interrupt: "Interrompre (Ctrl-C)", kill: "Fermer la session", restart: "Relancer",
  run_dead_ok: (c) => "Run terminé proprement (code " + c + "). L'écran final reste affiché ci-dessous.",
  run_dead_err: (c) => "Run terminé en erreur (code " + c + "). Consulte failReport.md et l'écran ci-dessous.",
  attach_hint: (s) => "Pour aller plus loin : tmux attach -t " + s + " (Ctrl-B puis D pour sortir)",
  deliverables: "Fichiers de l'usine", sessions: "sessions", verify_cmd: "verify_cmd", last_green: "nombre de tests au dernier passage réussi",
  confirm_kill: "Fermer la session tmux du run ? (le run sera perdu s'il est encore actif)",
  confirm_reject: "Refuser : l'orchestrateur s'arrête proprement et ferme la session de l'IA. Continuer ?",
  binary_chmod: (b) => "binaire présent mais non exécutable : fais « chmod +x " + b + " » dans le dossier de son moteur",
  select_project_first: "Ajoute d'abord un projet (Bibliothèque, étape 1).",
  gate_sent: "Réponse envoyée à l'orchestrateur.",
  equipped_msg: (r, multi) => "Projet équipé (" + r.copied.join(", ") + ")" +
     (r.harness_label ? " · harness " + r.harness_label : "") +
     (multi && r.engine ? " · moteur " + I18N.engine_name(r.engine) : "") +
     (r.distro_version ? " · v" + r.distro_version : "") +
     (r.backups.length ? " · sauvegarde : " + r.backups.join(", ") : "") +
     (r.need_created ? " · need.md créé (décris ton besoin à l'étape 2)" : ""),
  clean: (n) => "Nettoyer les fichiers de l'usine (" + n + ")",
  clean_hint: "Efface tout ce que les orchestrateurs ont produit : besoin (need.md), spec, plan, blackboard, rapports, cartes, dossiers d'audit et fichiers de reprise. Le prochain run repart de zéro, sans sauter d'étape. Ton code et l'équipement (.agents/ et les fichiers du harness) ne sont pas touchés.",
  clean_blocked: "Run en cours : interromps-le avant de nettoyer.",
  clean_none: "Aucun fichier de l'usine à nettoyer.",
  clean_dir_hint: "dossier des observations de l'audit (un fichier par passe)",
  clean_confirm: (names) => "Supprimer définitivement ces " + names.length + " élément(s) ?\n\n" + names.join("\n") +
     (names.includes("need.md")
        ? "\n\n⚠️ need.md est dans le lot : TON BESOIN sera effacé, et c'est le seul fichier de cette liste que les orchestrateurs ne savent pas recréer. Pour le garder, annule et utilise la corbeille 🗑️ de chaque ligne."
        : "") +
     "\n\nLes fichiers de reprise (.spec_approved…) sont supprimés en même temps : le prochain run ne sautera aucune étape. Ton code n'est pas touché. Les dossiers sont supprimés avec leur contenu.",
  clean_one: (f) => "Supprimer " + f,
  clean_one_confirm: (f) => "Supprimer définitivement " + f + " ? Ce fichier sert à reprendre le travail en cours : le prochain run le recréera.",
  cleaned_msg: (r) => (r.removed.length ? "Supprimé : " + r.removed.join(", ") : I18N.clean_none) +
     (r.sentinels.length ? " · fichiers de reprise : " + r.sentinels.join(", ") : ""),
  clean_failed: (list) => "Suppression impossible : " + list.join(", "),
  browse: "Parcourir…", choose_here: "Choisir ce dossier", truncated: "(liste tronquée)",
  edit: "Modifier", edit_need: "Éditer need.md", saved: "Enregistré.",
  conflict_reload: "Conflit : le fichier a changé pendant l'édition. Recharger son contenu ? (tes modifications locales seront perdues)",
  notif_on: "Notifications activées : porte ouverte et fin de run.",
  notif_denied: "Notifications refusées par le navigateur (réglages du site).",
  notif_gate: (g, p) => "🚪 " + g + " — " + p,
  notif_end: (c, p) => (c === 0 ? "✅" : "❌") + " Run terminé (code " + (c ?? "?") + ") — " + p,
  fb_rejected: "Ce que le vérificateur a reproché (phase rejetée)",
  fb_retry: "Feedback de la tentative précédente",
  gate_live: (g) => "Porte ouverte : " + g + " — à toi de valider.",
  update_prompts: "mise à jour de l'équipement disponible",
  equipped_v: (v) => "équipé" + (v ? " v" + v : " · version inconnue"),
  recommended: "⭐ recommandé",
  beta: "🧪 bêta",
  phase_states: { DONE: "terminée", PENDING: "en cours", REJECTED: "rejetée", TODO: "à faire" },
  tasks_word: (n) => n + " tâche(s)",
  covers_word: " · couvre ",
  step_word: (i, t) => "étape " + i + "/" + (t || 5),
  no_blackboard: "blackboard.yaml pas encore généré — les phases apparaîtront ici.",
  no_subdirs: "(aucun sous-dossier)",
  binary_label: "binaire", project_label: "projet",
  missing_dir: "dossier introuvable",
  gate_open: "porte ouverte",
  connecting: "Connexion…",
  notif_title_on: "Notifications activées (cliquer pour les désactiver)",
  notif_title_off: "Activer les notifications de porte et de fin de run",
  nav_library: "Bibliothèque", nav_run: "Run", nav_projects: "Projets",
  close: "Fermer", cancel: "Annuler", save_btn: "Enregistrer",
  fs_title: "Choisir le dossier du projet",
  fs_home_label: "Dossier personnel",
  fs_up: "⬆ Dossier parent",
  no_capture: "(pas de capture)",
  editor_hint: "Ctrl+S pour enregistrer. Si le fichier change de son côté pendant l'édition (orchestrateur, autre éditeur), l'app refuse d'écraser et te propose de recharger.",
  lang_label: "Langue / Language",
  theme_dark: "Passer en thème sombre (Crépuscule)",
  theme_light: "Passer en thème clair (Aurore)",
  gate_kicker: (i, n) => "Étape " + i + " sur " + n + " · on attend ta réponse",
  sys_title: "Statut & réglages",
  sys_open: "Statut du système (prérequis, moteurs, langue)",
  prereq_missing: (names) => "Prérequis manquant(s) : " + names + " — détail dans Statut & réglages (bouton en haut à gauche).",
  sys_quit: "Quitter l'app",
  sys_quit_hint: "Ferme le cockpit (le serveur local). Les runs en cours continuent dans tmux : relance l'app pour les retrouver.",
  quit_confirm: "Quitter l'app ? Les runs en cours continuent dans tmux.",
  quit_done_title: "App fermée",
  quit_done: "Tu peux fermer cet onglet. Les runs en cours continuent en arrière-plan (tmux) — relance l'app pour les retrouver.",
  gate_screen: "Le détail à valider s'affiche sur l'écran du run, juste en dessous.",
};

const I18N_ENG = {
  library_title: "Library",
  project_step: "1 · Choose the project",
  project_step_hint: "Choose the project folder you want to equip: paste its path, or go find it in your folders.",
  need_step: "2 · Describe your need",
  orch_step: "3 · Choose an orchestrator",
  need_ph: "Explain the problem to solve and who it's for, what the tool must do, and any known constraints (a required stack, files to reference…) — the way you'd explain it to a colleague. The PO Agent will turn this into a specification (spec.md) for you to approve.",
  need_save: "Save the need",
  need_ready: "✓ need.md ready",
  need_unsaved: "unsaved changes — they'll go into need.md when you save, or when you start a run",
  need_absent: "need.md will be created at the project root when you save",
  library_sub: (n) => n + " binary(ies) present · discovered via orchestrators.json · an interrupted run picks up where it left off",
  no_manifest: "No orchestrator: check orchestrators.json, next to the app or in an engine's subfolder.",
  no_binaries: "No binary found: the ones orchestrators.json announces are gone. Put a binary back in its engine folder to see it here again.",
  engine_name: (l) => l === "." ? "app folder" : l,
  engines: "Engines", no_engine: "no engine discovered", binaries_word: "binaries",
  active_project: "Active project", prereqs: "Prerequisites", install_dir: "App folder",
  add_project_ph: "/path/to/your/project", add: "Add", equip: "Equip", reequip: "Update equipment",
  launch: "Start", open_run: "Open the run", forget: "Forget", equipped: "equipped", not_equipped: "not equipped",
  tmo_btn: "Timeouts",
  tmo_title: "⏱ Project timeouts",
  tmo_hint: "In seconds, empty = orchestrator default. Applies to the next launch (orchestrators read .mm-equip.json at startup) — a running run is not affected. Raise it if your test suite takes more than 5 minutes.",
  tmo_verify_label: "Verification (build + tests) — default 300 s",
  tmo_phase_label: "Agent pass watchdog — default 600 s",
  tmo_save: "Save",
  tmo_saved: "Timeouts saved (.mm-equip.json)",
  tmo_invalid: "Whole values between 60 and 7200 seconds (empty = default).",
  tmo_badge: (t) => "⏱ " + [t.verify != null ? "verify " + t.verify + " s" : "", t.phase != null ? "phase " + t.phase + " s" : ""].filter(Boolean).join(" · "),
  tmo_badge_title: "Custom timeouts for this project (Timeouts button to change them)",
  running: "run in progress", finished: "finished", need_missing: "need not described yet",
  projects_title: "Equipped projects",
  projects_sub: "The app copies .agents/ and the chosen harness's files to the project root · the binaries never leave their engine folder",
  no_projects: "No project registered: add your project folder above.",
  run_title: "Run monitor", no_run: "No run for this project. Start an orchestrator from the Library.",
  no_project_selected: "Select a project (Library or Projects).",
  steps: ["Specification (PO)", "Plan (Architect)", "Blackboard", "Production", "Refactoring"],
  tab_orch: "Orchestrator", tab_agent: (h) => h + " (AI)",
  harness_word: "harness", harness_absent: "not in PATH", harness_unauth: "not signed in",
  no_harness: "No AI agent harness installed: install OpenCode (https://opencode.ai/docs) or Codex CLI (npm install -g @openai/codex), then log in. Details in Status & settings.",
  harness_model: (m) => "model: " + m, harness_model_default: "the harness's default model",
  term_zoom: "Expand the terminal", term_unzoom: "Collapse the terminal (Esc)",
  gate_watch: (f) => "Watching " + f + " — edit it elsewhere and the preview updates on its own",
  open_editor: "Open in editor", validate: "Validate", reject: "Reject (stops the orchestrator)",
  gate_send: "Send",
  interrupt: "Interrupt (Ctrl-C)", kill: "Close the session", restart: "Restart",
  run_dead_ok: (c) => "Run finished cleanly (code " + c + "). The final screen is still shown below.",
  run_dead_err: (c) => "Run failed (code " + c + "). Check failReport.md and the screen below.",
  attach_hint: (s) => "Power users: tmux attach -t " + s + " (Ctrl-B then D to detach)",
  deliverables: "Factory files", sessions: "sessions", verify_cmd: "verify_cmd", last_green: "tests at last green run",
  confirm_kill: "Close the run's tmux session? (the run will be lost if it is still active)",
  confirm_reject: "Reject: the orchestrator stops cleanly and closes the AI session. Continue?",
  binary_chmod: (b) => "binary present but not executable: run “chmod +x " + b + "” in its engine folder",
  select_project_first: "Add a project first (Library, step 1).",
  gate_sent: "Answer sent to the orchestrator.",
  equipped_msg: (r, multi) => "Project equipped (" + r.copied.join(", ") + ")" +
     (r.harness_label ? " · harness " + r.harness_label : "") +
     (multi && r.engine ? " · engine " + I18N.engine_name(r.engine) : "") +
     (r.distro_version ? " · v" + r.distro_version : "") +
     (r.backups.length ? " · backup: " + r.backups.join(", ") : "") +
     (r.need_created ? " · need.md created (describe your need at step 2)" : ""),
  clean: (n) => "Clean factory files (" + n + ")",
  clean_hint: "Erases everything the orchestrators produced: the need (need.md), spec, plan, blackboard, reports, maps, audit folders and resume files. The next run starts from scratch, without skipping any step. Your code and the equipment (.agents/ and the harness files) are left alone.",
  clean_blocked: "Run in progress: interrupt it before cleaning.",
  clean_none: "No factory file to clean.",
  clean_dir_hint: "audit findings (one file per pass)",
  clean_confirm: (names) => "Permanently delete these " + names.length + " item(s)?\n\n" + names.join("\n") +
     (names.includes("need.md")
        ? "\n\n⚠️ need.md is in the list: YOUR NEED will be erased, and it's the only thing here the factory can't recreate. To keep it, cancel and use the 🗑️ button on its row."
        : "") +
     "\n\nThe resume files (.spec_approved…) go too, so the next run won't skip any step. Your code is untouched. Folders are deleted with everything in them.",
  clean_one: (f) => "Delete " + f,
  clean_one_confirm: (f) => "Permanently delete " + f + "? It's what lets a run pick up where it left off — the next run will recreate it.",
  cleaned_msg: (r) => (r.removed.length ? "Deleted: " + r.removed.join(", ") : I18N.clean_none) +
     (r.sentinels.length ? " · resume files: " + r.sentinels.join(", ") : ""),
  clean_failed: (list) => "Deletion failed: " + list.join(", "),
  browse: "Browse…", choose_here: "Choose this folder", truncated: "(list truncated)",
  edit: "Edit", edit_need: "Edit need.md", saved: "Saved.",
  conflict_reload: "Conflict: the file changed while you were editing. Reload its content? (your local changes will be lost)",
  notif_on: "Notifications on — you'll be pinged when a gate opens and when a run ends.",
  notif_denied: "Notifications denied by the browser (site settings).",
  notif_gate: (g, p) => "🚪 " + g + " — " + p,
  notif_end: (c, p) => (c === 0 ? "✅" : "❌") + " Run finished (code " + (c ?? "?") + ") — " + p,
  fb_rejected: "What the verifier flagged (rejected phase)",
  fb_retry: "Feedback from the previous attempt",
  gate_live: (g) => "Gate open: " + g + " — it's your call.",
  update_prompts: "equipment update available",
  equipped_v: (v) => "equipped" + (v ? " v" + v : " · unknown version"),
  recommended: "⭐ recommended",
  beta: "🧪 beta",
  phase_states: { DONE: "done", PENDING: "in progress", REJECTED: "rejected", TODO: "to do" },
  tasks_word: (n) => n + " task(s)",
  covers_word: " · covers ",
  step_word: (i, t) => "step " + i + "/" + (t || 5),
  no_blackboard: "blackboard.yaml not generated yet — the phases will appear here.",
  no_subdirs: "(no subfolder)",
  binary_label: "binary", project_label: "project",
  missing_dir: "folder not found",
  gate_open: "gate open",
  connecting: "Connecting…",
  notif_title_on: "Notifications enabled (click to mute)",
  notif_title_off: "Enable gate and end-of-run notifications",
  nav_library: "Library", nav_run: "Run", nav_projects: "Projects",
  close: "Close", cancel: "Cancel", save_btn: "Save",
  fs_title: "Choose the project folder",
  fs_home_label: "Home folder",
  fs_up: "⬆ Parent folder",
  no_capture: "(no capture)",
  editor_hint: "Ctrl+S to save. If the file changes underneath you (orchestrator, another editor), the app won't overwrite it — it offers to reload instead.",
  lang_label: "Langue / Language",
  theme_dark: "Switch to dark theme (Crépuscule)",
  theme_light: "Switch to light theme (Aurore)",
  gate_kicker: (i, n) => "Step " + i + " of " + n + " · waiting on you",
  sys_title: "Status & settings",
  sys_open: "System status (prerequisites, engines, language)",
  prereq_missing: (names) => "Missing prerequisite(s): " + names + " — details in Status & settings (top-left button).",
  sys_quit: "Quit the app",
  sys_quit_hint: "Closes the cockpit (the local server). Runs in progress keep going in tmux — relaunch the app to pick them back up.",
  quit_confirm: "Quit the app? Runs in progress keep going in tmux.",
  quit_done_title: "App closed",
  quit_done: "You can close this tab. Runs in progress keep going in the background (tmux) — relaunch the app to pick them back up.",
  gate_screen: "The details to review are on the run screen, right below.",
};

const LANGS = { fr: I18N_FR, eng: I18N_ENG };
let I18N = I18N_FR;   // pointe vers la langue active ; bascule via applyLang()
/* Champs bilingues du manifeste ({fr, eng}) : la langue active, sinon l'autre. */
const tr = (x) => x ? (x[ui.lang] || x.fr || x.eng || "") : "";

const EDITABLE = new Set(["need.md", "spec.md", "plan.md", "impact.md", "blackboard.yaml",
                          "skill_adapt_profile.yaml",
                          "skill_adapt-backend-coding.md", "skill_adapt-frontend-coding.md",
                          "skill_adapt-backend-testing.md", "skill_adapt-frontend-testing.md"]);

/* État SERVEUR : les deux payloads bruts (/api/state, /api/run). Rien d'autre n'y vit. */
const state = { data: null, run: null };

/* État UI, regroupé : tout ce que l'interface retient entre deux rendus est ici. */
const ui = {
  // Langue : URL (?lang=, pratique pour tester) > choix mémorisé > langue du navigateur.
  lang: (() => {
    const q = new URLSearchParams(location.search).get("lang");
    if (q === "fr" || q === "eng") return q;
    const saved = localStorage.getItem("mm_lang");
    if (saved === "fr" || saved === "eng") return saved;
    return (navigator.language || "fr").toLowerCase().startsWith("fr") ? "fr" : "eng";
  })(),
  activeProject: localStorage.getItem("mm_active") || null,
  termTab: "orch",                 // onglet terminal affiché (orch | agent)
  termZoom: false,                 // terminal en plein écran (⛶ ; Échap referme)
  busy: false,                     // action en cours : boutons de porte désactivés
  notif: localStorage.getItem("mm_notif") === "1",
  prevRuns: null,                  // instantané précédent des runs (détection porte / fin)
  gateMtime: null,                 // mtime du fichier de porte affiché (rechargement d'aperçu)
  gateDocHtml: "",                 // aperçu rendu du fichier de porte
  fs: { path: null, parent: null, home: null },                      // explorateur de dossiers
  edit: { hash: null, file: null, mtime: null, isGateFile: false },  // éditeur intégré
  need: { hash: null, mtime: null, saved: "" },  // bloc besoin : need.md chargé pour le projet actif
  needDrafts: {},                  // brouillons du bloc besoin par projet (hash) : survivent aux re-rendus
  scaffold: null,                  // squelette actuellement monté dans #view
  regions: {},                     // cache HTML par région : on ne re-rend que ce qui change
  projselOptions: null,            // options du sélecteur de projet (topbar) : re-rendues au changement seul
  sysHtml: "",                     // corps du dialogue Statut & réglages (même principe)
  toastTimer: null,
  sse: { es: null, path: undefined, last: 0, attemptAt: 0 },  // flux d'événements
  modalOpener: null,               // élément à re-focus à la fermeture d'une modale
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
/* Les data-arg générés ne véhiculent JAMAIS un chemin pour les actions de projet
   (apostrophes, quotes…) : ils passent le hash du projet, résolu ici. */
const byHash = (h) => (state.data ? state.data.projects.find(p => p.hash === h) : null);
const P = (h) => (byHash(h) || {}).path;

/* La langue voyage en query (&lang=) : les messages serveur suivent le choix de l'UI,
   et EventSource ne sait pas poser d'en-tête. */
const withLang = (path) => path + (path.includes("?") ? "&" : "?") + "lang=" + ui.lang;

async function api(path, body) {
  const opts = body ? { method: "POST", headers: {"Content-Type":"application/json"},
                        body: JSON.stringify(body) } : {};
  const res = await fetch(withLang(path), opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || ("HTTP " + res.status));
    err.status = res.status;   // les décisions UI se prennent sur le statut, jamais sur le texte
    throw err;
  }
  return data;
}

/* Bascule de langue : re-rendu complet (caches de régions vidés), texte statique
   estampillé via data-i18n, flux SSE relancé pour que les avertissements suivent. */
function applyLang(lang) {
  ui.lang = LANGS[lang] ? lang : "fr";
  I18N = LANGS[ui.lang];
  localStorage.setItem("mm_lang", ui.lang);
  document.documentElement.lang = ui.lang === "eng" ? "en" : "fr";
  document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = I18N[el.dataset.i18n]; });
  document.querySelectorAll("[data-i18n-aria]").forEach(el => el.setAttribute("aria-label", I18N[el.dataset.i18nAria]));
  updateThemeBtn();                        // le libellé de la bascule de thème suit la langue
  $("#ledsbtn").title = I18N.sys_open;     // l'infobulle aussi (l'aria passe par data-i18n-aria)
  $("#langsel").value = ui.lang;           // le sélecteur statique reflète la langue active
}

/* ── thème : Aurore (clair) / Crépuscule (sombre). Le script du <head> a posé data-theme
      AVANT le premier rendu ; ici la bascule (choix persisté) et le suivi de l'OS tant
      que l'utilisateur n'a pas choisi explicitement. ── */
function setTheme(theme, persist) {
  document.documentElement.dataset.theme = theme;
  if (persist) localStorage.setItem("mm_theme", theme);
  updateThemeBtn();
}
/* Icônes au trait de la bascule (currentColor : elles suivent la couleur du bouton). */
const ICON_SUN = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/>
  <path d="M12 2.6v2.5M12 18.9v2.5M2.6 12h2.5M18.9 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8"/></svg>`;
const ICON_MOON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d="M20.2 14.4A8.4 8.4 0 0 1 9.6 3.8a8.4 8.4 0 1 0 10.6 10.6Z"/></svg>`;
function updateThemeBtn() {
  const btn = $("#themebtn"), dark = document.documentElement.dataset.theme === "dark";
  btn.innerHTML = dark ? ICON_SUN : ICON_MOON;   // l'icône montre le thème CIBLE de la bascule
  btn.title = dark ? I18N.theme_light : I18N.theme_dark;
  btn.setAttribute("aria-label", btn.title);
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  if (!localStorage.getItem("mm_theme")) setTheme(e.matches ? "dark" : "light", false);
});

function toast(msg, isErr) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.setAttribute("role", "status");       // annoncé par les lecteurs d'écran
  el.setAttribute("aria-live", "polite");
  el.textContent = msg;
  document.body.appendChild(el);
  clearTimeout(ui.toastTimer);
  ui.toastTimer = setTimeout(() => el.remove(), 4200);
}

function route() { return (location.hash || "#/bibliotheque").slice(2).split("/")[0] || "bibliotheque"; }

/* ── modales : Escape ferme (natif <dialog>), et le focus REVIENT à l'élément qui a
      ouvert — mémorisé par clé de focus (le re-render peut avoir remplacé le nœud). ── */
function openDialog(dlg) {
  const el = document.activeElement;
  ui.modalOpener = (el && el !== document.body)
      ? { key: (el.dataset && (el.dataset.fkey || el.id)) || null, el } : null;
  dlg.showModal();
}
for (const id of ["docdlg", "fsdlg", "editdlg", "tmodlg", "sysdlg"]) {
  document.getElementById(id).addEventListener("close", () => {
    const saved = ui.modalOpener;
    ui.modalOpener = null;
    if (!saved) return;
    let target = saved.key
        ? document.querySelector(`[data-fkey="${CSS.escape(saved.key)}"]`) || document.getElementById(saved.key)
        : null;
    if (!target && saved.el && saved.el.isConnected) target = saved.el;
    if (target) target.focus({ preventScroll: true });
  });
}

function currentProject() {
  if (!state.data) return null;
  return state.data.projects.find(p => p.path === ui.activeProject) || state.data.projects[0] || null;
}

/* ── réception des payloads : un seul point d'entrée par source (polling aujourd'hui,
      flux d'événements demain — les deux passent par ingestState / ingestRun) ── */
function ingestState(data) {
  state.data = data;
  if (!ui.activeProject && data.projects.length) ui.activeProject = data.projects[0].path;
  watchRunEvents(data.projects);
  render();
  if ($("#sysdlg").open) renderSys();   // le dialogue ouvert suit l'état (prérequis, moteurs)
}
function ingestRun(run) {
  const project = currentProject();
  if (!project || (run.project && run.project.path !== project.path)) return; // payload d'un autre projet
  state.run = run;
  // rechargement de l'aperçu de porte si le fichier a changé (édition externe)
  const gate = run.gate;
  if (gate) {
    const meta = run.files[gate.file];
    if (meta && meta.exists && meta.mtime !== ui.gateMtime) {
      ui.gateMtime = meta.mtime;
      loadGateDoc(project.path, gate.file);
    }
  } else { ui.gateMtime = null; }
  render();
}

/* ── flux d'événements (SSE) : le serveur pousse run/state ; le polling ci-dessous
      reste actif en repli et se tait tant que le flux est sain ── */
function sseHealthy() { return !!(ui.sse.es && Date.now() - ui.sse.last < 12000); }
function connectEvents() {
  if (!("EventSource" in window)) return;   // navigateur sans SSE : polling seul
  const project = currentProject();
  const path = project ? project.path : null;
  const closed = ui.sse.es && ui.sse.es.readyState === 2;
  // même cible et flux non condamné : rien à faire (un flux condamné — 429, erreur
  // fatale — est retenté au plus toutes les 30 s, le polling assure entre-temps)
  if (ui.sse.es && ui.sse.path === path && (!closed || Date.now() - ui.sse.attemptAt < 30000)) return;
  if (ui.sse.es) ui.sse.es.close();
  const es = new EventSource(withLang("/api/events" + (path ? "?path=" + encodeURIComponent(path) : "")));
  ui.sse = { es, path, last: 0, attemptAt: Date.now() };
  es.addEventListener("state", (e) => { ui.sse.last = Date.now(); ingestState(JSON.parse(e.data)); });
  es.addEventListener("run", (e) => { ui.sse.last = Date.now(); ingestRun(JSON.parse(e.data)); });
  es.addEventListener("error", () => { ui.sse.last = 0; });  // repli : le polling reprend au tick suivant
}

/* ── polling (repli, et source unique si SSE indisponible) ── */
async function refreshState() {
  try { ingestState(await api("/api/state")); }
  catch (err) { toast(err.message, true); render(); }
}
async function refreshRun() {
  const project = currentProject();
  if (route() !== "run" || !project) return;
  try { ingestRun(await api("/api/run?path=" + encodeURIComponent(project.path))); }
  catch (err) { state.run = { error: err.message }; render(); }
}
async function loadGateDoc(path, file) {
  try {
    const doc = await api("/api/doc?path=" + encodeURIComponent(path) + "&file=" + encodeURIComponent(file));
    ui.gateDocHtml = doc.html;
  } catch (err) { ui.gateDocHtml = "<p>" + esc(err.message) + "</p>"; }
  render();
}

/* ── actions ── */
async function act(fn, okMsg) {
  if (ui.busy) return;
  ui.busy = true; render();
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg); }
  catch (err) { toast(err.message, true); }
  ui.busy = false;
  await refreshState(); await refreshRun();
}
function addProjectPath(path) {
  if (!path || !path.trim()) return;
  act(async () => { const r = await api("/api/project/add", { path });
                    ui.activeProject = r.path; localStorage.setItem("mm_active", r.path); });
}
function addProject() { const input = $("#newpath"); if (input) addProjectPath(input.value); }
const equip  = (h, eng, harness) => act(() => api("/api/project/equip",
                                 { path: P(h), engine: eng || undefined, harness: harness || undefined }),
                               (r) => I18N.equipped_msg(r, state.data.engines.length > 1));
const forget = (h) => act(() => api("/api/project/forget", { path: P(h) }));
/* ── nettoyage des artefacts de l'usine (spec, plan, blackboard, rapports, cartes) ──
      Le geste « repartir de zéro » : ces fichiers SONT l'état de reprise, et il fallait
      jusqu'ici un terminal pour les effacer. La liste est recalculée AU CLIC (l'état a
      pu bouger depuis le rendu), et le serveur refuse tant qu'un run est vivant. */
const runFor = (p) => (state.run && state.run.project && state.run.project.path === p.path) ? state.run : null;
/* Le serveur dit ce qu'il sait nettoyer ({name, dir}) : fichiers ET dossiers de constats
   des audits/doc (l'UI n'a pas à connaître ces derniers). Le payload run est plus frais
   que le résumé d'état quand il porte sur CE projet. */
const cleanableOf = (project, r) => ((r || project || {}).cleanable) || [];
const cleanLabel = (c) => c.dir ? c.name + "/" : c.name;
function cleanFiles(h, items, ask) {
  if (!items.length) { toast(I18N.clean_none); return; }
  if (!confirm(ask)) return;
  act(async () => {
    const r = await api("/api/project/clean", { path: P(h), files: items.map(c => c.name) });
    ui.gateMtime = null; ui.gateDocHtml = "";   // un aperçu ne survit pas à son fichier
    if (r.removed.includes("need.md")) {
      // Le besoin effacé ne doit pas RESSUSCITER : le brouillon du bloc besoin serait
      // réécrit dans need.md au prochain lancement (flushNeedDraft), et le textarea
      // afficherait encore l'ancien texte (loadNeedBlock ne recharge qu'au changement).
      delete ui.needDrafts[h];
      ui.need = { hash: null, mtime: null, saved: "" };
      const ta = $("#needta");
      if (ta) ta.value = "";
    }
    if (r.failed.length) toast(I18N.clean_failed(r.failed), true);
    else toast(I18N.cleaned_msg(r));
  });
}
function cleanAll(h) {
  const p = byHash(h); if (!p) return;
  const items = cleanableOf(p, runFor(p));
  cleanFiles(h, items, I18N.clean_confirm(items.map(cleanLabel)));
}
const cleanOne = (h, name, isDir) => cleanFiles(h, [{ name, dir: !!isDir }],
    I18N.clean_one_confirm(isDir ? name + "/" : name));
/* ── timeouts du projet (section 'timeouts' de .mm-equip.json) ──
      Deux réglages en secondes : 'verify' (compilation + tests) et 'phase' (garde-fou
      d'une passe d'agent). Champ vide = défaut de l'orchestrateur. Lus par les
      orchestrateurs AU DÉMARRAGE : le dialogue le dit, un run en cours ne bouge pas. */
function openTimeouts(h) {
  const p = byHash(h); if (!p) return;
  ui.tmoHash = h;
  const t = p.timeouts || {};
  $("#tmoverify").value = t.verify ?? "";
  $("#tmophase").value = t.phase ?? "";
  openDialog($("#tmodlg"));
}
function timeoutsSave() {
  const read = (sel) => {
    const raw = $(sel).value.trim();
    if (raw === "") return null;                       // vide = retour au défaut
    const n = Number(raw);
    return Number.isInteger(n) && n >= 60 && n <= 7200 ? n : undefined;
  };
  const verify = read("#tmoverify"), phase = read("#tmophase");
  if (verify === undefined || phase === undefined) { toast(I18N.tmo_invalid, true); return; }
  act(async () => {
    await api("/api/project/timeouts", { path: P(ui.tmoHash), timeouts: { verify, phase } });
    $("#tmodlg").close();
  }, I18N.tmo_saved);
}
const startRun = (h, o) => act(async () => {
  if (!(await flushNeedDraft(h))) return;   // brouillon du bloc besoin d'abord ; échec déjà signalé
  await api("/api/run/start", { path: P(h), orchestrator: o });
  location.hash = "#/run";
});
const answerGate = (h, a, kind) => {
  // La confirmation « arrêt propre » ne vaut que pour le n des portes y/n : dans une
  // porte à choix (triage r/e/o, questionnaire 1/2/3), « n » est une réponse comme
  // une autre, jamais un abandon du run.
  if (kind !== "choice" && kind !== "text" && a === "n" && !confirm(I18N.confirm_reject)) return;
  act(() => api("/api/run/gate", { path: P(h), answer: a }), I18N.gate_sent);
};
const answerGateText = (h) => {
  const field = $("#gateinput");
  const value = field ? field.value.trim() : "";
  if (!value) { if (field) field.focus(); return; }
  act(() => api("/api/run/gate", { path: P(h), answer: value }), I18N.gate_sent);
};

/* Boutons d'une porte selon son TYPE (contrat manifeste v1.1) : y/n historique,
   'choice' (un bouton par choix déclaré), 'text' (saisie libre d'une ligne — la
   valeur tapée survit aux re-rendus via data-fkey, cf. captureFocus). */
function gateControlsHtml(gate, h) {
  if (gate.kind === "choice" && Array.isArray(gate.choices) && gate.choices.length) {
    return gate.choices.map((c, i) =>
      `<button class="btn ${i ? "ghost" : ""}" ${ui.busy?"disabled":""} data-fkey="gate-${esc(c.key)}" data-action="gate-answer" data-arg="${h}" data-arg2="${esc(c.key)}" data-kind="choice">${esc(tr(c.label) || c.key)}</button>`).join("");
  }
  if (gate.kind === "text") {
    return `<input type="text" id="gateinput" data-fkey="gateinput" style="flex:1;min-width:220px" maxlength="200"
              aria-label="${esc(tr(gate.placeholder) || I18N.gate_send)}" placeholder="${esc(tr(gate.placeholder) || "")}">
      <button class="btn" ${ui.busy?"disabled":""} data-fkey="gate-send" data-action="gate-answer-text" data-arg="${h}">${I18N.gate_send}</button>`;
  }
  return `<button class="btn" ${ui.busy?"disabled":""} data-fkey="gate-y" data-action="gate-answer" data-arg="${h}" data-arg2="y">${esc(tr(gate.yes_label) || I18N.validate)}</button>
    <button class="btn ghost" ${ui.busy?"disabled":""} data-fkey="gate-n" data-action="gate-answer" data-arg="${h}" data-arg2="n">${esc(tr(gate.no_label) || I18N.reject)}</button>`;
}
const interruptRun = (h) => act(() => api("/api/run/interrupt", { path: P(h) }));
const killRun = (h) => { if (confirm(I18N.confirm_kill)) act(() => api("/api/run/kill", { path: P(h) })); };

/* Extinction depuis l'UI (lancement double-clic : pas de Ctrl+C). Le serveur meurt
   juste après la réponse ; l'onglet devient un écran d'adieu statique, flux SSE
   refermé d'abord (sinon il tenterait de se reconnecter en boucle sur un port mort). */
async function appQuit() {
  if (!confirm(I18N.quit_confirm)) return;
  if (ui.sse.es) { ui.sse.es.close(); ui.sse.es = null; }
  try { await api("/api/quit", {}); }
  catch (err) { /* le serveur peut se fermer avant que la réponse ne parte */ }
  document.body.innerHTML = `<div style="display:grid;place-items:center;min-height:100vh;padding:24px">
    <div style="max-width:56ch;text-align:center"><h1 style="font-size:22px">⏻ ${esc(I18N.quit_done_title)}</h1>
    <p style="color:var(--ink-soft);line-height:1.5">${esc(I18N.quit_done)}</p></div></div>`;
}
const openEditor = (h, f) => act(() => api("/api/open", { path: P(h), file: f }));
async function showDoc(h, file) {
  try {
    const doc = await api("/api/doc?path=" + encodeURIComponent(P(h)) + "&file=" + encodeURIComponent(file));
    $("#docttl").textContent = file;
    $("#docmeta").textContent = new Date(doc.mtime * 1000).toLocaleString();
    $("#docbody").innerHTML = doc.html;
    openDialog($("#docdlg"));
  } catch (err) { toast(err.message, true); }
}

/* ── sélection du dossier projet (natif puis explorateur intégré) ── */
async function browse() {
  try {
    const r = await api("/api/fs/pick", {});
    if (r.cancelled) return;
    if (r.native && r.path) { addProjectPath(r.path); return; }
  } catch (err) { /* natif indisponible : repli explorateur */ }
  fsGo(null);
  openDialog($("#fsdlg"));
}
async function fsGo(path) {
  try {
    const r = await api("/api/fs" + (path ? "?path=" + encodeURIComponent(path) : ""));
    ui.fs = { path: r.path, parent: r.parent, home: r.home };
    $("#fspath").textContent = r.path;
    $("#fsup").disabled = !r.parent;
    $("#fslist").innerHTML = r.dirs.map(d =>
      `<button class="fs-item" data-action="fs-go" data-arg="${esc(d.path)}">📁 ${esc(d.name)}${d.equipped ? `<span class="tag">${I18N.equipped}</span>` : ""}</button>`
    ).join("") + (r.truncated ? `<div class="hintline" style="padding:8px">${I18N.truncated}</div>` : "")
      + (!r.dirs.length ? `<div class="hintline" style="padding:8px">${I18N.no_subdirs}</div>` : "");
  } catch (err) { toast(err.message, true); }
}
function fsChoose() { $("#fsdlg").close(); addProjectPath(ui.fs.path); }

/* ── bloc besoin (étape 1 de la Bibliothèque) : le texte est écrit tel quel dans le
      need.md du projet — le fichier reste le contrat des binaires, l'utilisateur n'a
      plus à le connaître. Le textarea est rendu VIDE par libraryHtml et rempli ici,
      hors cycle de rendu : le HTML de la région reste stable pendant la frappe, et
      syncNeedTa() réinjecte brouillon ou contenu après chaque re-rendu. ── */
function needStateLabel(p) {
  const draft = ui.needDrafts[p.hash];
  if (draft != null && !(ui.need.hash === p.hash && draft === ui.need.saved)) return I18N.need_unsaved;
  if (p.need.ready) return I18N.need_ready;
  if (!p.need.present) return I18N.need_absent;
  return p.need.why || I18N.need_missing;   // gabarit ou vide : le motif serveur dit quoi faire
}
function syncNeedTa() {
  const ta = $("#needta"), p = currentProject();
  if (!ta || !p || ui.need.hash !== p.hash) return;
  const draft = ui.needDrafts[p.hash];
  const want = draft != null ? draft : ui.need.saved;
  if (ta.value !== want) ta.value = want;
}
async function loadNeedBlock(p, force) {
  if (!p || !p.exists) return;
  if (!force && ui.need.hash === p.hash) { syncNeedTa(); return; }
  ui.need = { hash: p.hash, mtime: null, saved: "" };
  try {
    const r = await api("/api/file?path=" + encodeURIComponent(p.path) + "&file=need.md");
    if (ui.need.hash !== p.hash) return;              // le projet actif a changé pendant le fetch
    ui.need.mtime = r.mtime;
    ui.need.saved = r.is_template ? "" : r.content;   // le gabarit s'affiche comme un champ vide
  } catch (err) { /* 404 : need.md pas encore créé — champ vide, création à l'enregistrement */ }
  syncNeedTa();
}
async function needSave(silent) {
  const p = currentProject(), ta = $("#needta");
  if (!p || !ta || ui.need.hash !== p.hash) return false;
  try {
    const r = await api("/api/file/save", { path: p.path, file: "need.md",
                                            content: ta.value, base_mtime: ui.need.mtime });
    ui.need.mtime = r.mtime; ui.need.saved = ta.value;
    delete ui.needDrafts[p.hash];
    if (!silent) { toast(I18N.saved); refreshState(); }
    return true;
  } catch (err) {
    if (err.status === 409 && confirm(I18N.conflict_reload)) {   // conflit du verrou optimiste
      delete ui.needDrafts[p.hash];
      await loadNeedBlock(p, true);
      refreshState();
    } else toast(err.message, true);
    return false;
  }
}
/* Appelé par startRun : un brouillon non enregistré est écrit dans need.md AVANT le
   lancement — le geste naturel « je décris, je lance » n'exige aucun bouton. */
async function flushNeedDraft(h) {
  const p = currentProject();
  if (!p || p.hash !== h || ui.need.hash !== h) return true;
  const draft = ui.needDrafts[h];
  if (draft == null || draft === ui.need.saved) return true;
  return needSave(true);
}

/* ── éditeur intégré (need.md, spec.md, plan.md, blackboard.yaml) ── */
async function openFileEditor(h, file, isGateFile) {
  try {
    const r = await api("/api/file?path=" + encodeURIComponent(P(h)) + "&file=" + encodeURIComponent(file));
    ui.edit = { hash: h, file, mtime: r.mtime, isGateFile: !!isGateFile };
    $("#editttl").textContent = file;
    $("#editmeta").textContent = (byHash(h) || {}).name || "";
    $("#editta").value = r.content;
    openDialog($("#editdlg"));
  } catch (err) { toast(err.message, true); }
}
async function editorSave() {
  try {
    const r = await api("/api/file/save", { path: P(ui.edit.hash), file: ui.edit.file,
                                            content: $("#editta").value, base_mtime: ui.edit.mtime });
    ui.edit.mtime = r.mtime;
    toast(I18N.saved);
    if (ui.edit.isGateFile) { ui.gateMtime = null; refreshRun(); }
    refreshState();
  } catch (err) {
    if (err.status === 409 && confirm(I18N.conflict_reload)) {   // conflit du verrou optimiste
      const cur = await api("/api/file?path=" + encodeURIComponent(P(ui.edit.hash)) + "&file=" + encodeURIComponent(ui.edit.file));
      ui.edit.mtime = cur.mtime; $("#editta").value = cur.content;
    } else { toast(err.message, true); }
  }
}

/* ── notifications (porte ouverte, fin de run) ── */
function toggleNotif() {
  if (!ui.notif) {
    if (!("Notification" in window)) { toast(I18N.notif_denied, true); return; }
    Notification.requestPermission().then(p => {
      if (p !== "granted") { toast(I18N.notif_denied, true); return; }
      ui.notif = true; localStorage.setItem("mm_notif", "1"); toast(I18N.notif_on); render();
    });
  } else { ui.notif = false; localStorage.setItem("mm_notif", "0"); render(); }
}
function sendNotif(text, projectHash) {
  if (!ui.notif || !("Notification" in window) || Notification.permission !== "granted") return;
  const n = new Notification("MAIsterMind", { body: text });
  n.addEventListener("click", () => { window.focus(); const p = byHash(projectHash);
    if (p) { ui.activeProject = p.path; localStorage.setItem("mm_active", p.path); location.hash = "#/run"; } });
}
function watchRunEvents(projects) {
  const snapshot = {};
  for (const p of projects) {
    snapshot[p.hash] = { gate: p.run && p.run.gate ? p.run.gate.id : null,
                         alive: !!(p.run && p.run.alive),
                         exit: p.run ? p.run.exit_code : null,
                         gateTitle: p.run && p.run.gate ? (tr(p.run.gate.title) || p.run.gate.id) : null,
                         name: p.name };
  }
  if (ui.prevRuns) {
    for (const [h, cur] of Object.entries(snapshot)) {
      const prev = ui.prevRuns[h];
      if (!prev) continue;
      if (cur.gate && cur.gate !== prev.gate) sendNotif(I18N.notif_gate(cur.gateTitle, cur.name), h);
      if (prev.alive && !cur.alive) sendNotif(I18N.notif_end(cur.exit, cur.name), h);
    }
  }
  ui.prevRuns = snapshot;
}

/* ── rendus par région ──────────────────────────────────────────────────────
   Chaque vue = un squelette fixe (slots data-region) + une fonction de rendu par
   région. mount() compare le HTML région par région et ne touche au DOM que là où
   ça a changé : le terminal (rafraîchi toutes les 2 s) ne re-rend plus jamais le
   panneau porte, et focus/saisie ne sont restaurés que si LEUR région a bougé. */

const SC_MAIN = `<div data-region="main"></div>`;
const SC_RUN = `<div>
  <div data-region="runhead"></div>
  <div class="sub" data-region="stepline" aria-live="polite"></div>
  <div class="visually-hidden" role="status" aria-live="polite" data-region="gatelive"></div>
  <div data-region="gate"></div>
  <div data-region="controls"></div>
  <div class="run-grid"><div><div data-region="phases"></div><div data-region="deliv"></div></div><div data-region="term"></div></div>
</div>`;

/* ── app shell : le contexte vit dans la topbar, le système dans un dialogue ──
   Le sélecteur de projet (#projsel, même id qu'avant : le handler change ne bouge
   pas) est hors des régions #view : ses options ne sont re-rendues que quand la
   liste change, pour ne jamais casser le focus ni la liste déroulée. */
function syncTopbar() {
  const d = state.data, sel = $("#projsel");
  sel.hidden = !d.projects.length;
  const options = d.projects.map(p =>
    `<option value="${esc(p.path)}"${p.path === (currentProject()||{}).path ? " selected" : ""}>${esc(p.name)}</option>`).join("");
  if (ui.projselOptions !== options) {
    ui.projselOptions = options;
    sel.innerHTML = options;
  }
  sel.value = (currentProject() || {}).path || "";
}

/* Une ligne de préflight. Les harness portent en plus leur authentification et un
   conseil actionnable : « présent » ne suffit pas à faire tourner un run. Un harness
   absent est affiché en info (pastille neutre), pas en erreur — l'autre peut suffire. */
function prereqRow(c) {
  if (!c.harness) {
    const dot = !c.found ? "ko" : (c.warn ? "warn" : "ok");
    const bits = [];
    if (c.version) bits.push(c.version + (c.path ? " · " + c.path : ""));
    if (c.warn) bits.push("⚠ " + c.warn);
    return `<div class="check"><span class="dot ${dot}"></span>${esc(c.name)}${bits.length?` <span class="hintline">${esc(bits.join(" · "))}</span>`:""}</div>`;
  }
  const bits = [];
  if (c.version) bits.push(c.version);
  if (c.found) bits.push(c.authed ? (c.auth_detail || "ok") : I18N.harness_unauth + (c.auth_detail ? " (" + c.auth_detail + ")" : ""));
  else bits.push(I18N.harness_absent);
  if (c.hint) bits.push("→ " + c.hint);
  const dot = !c.found ? "" : (c.authed ? "ok" : "ko");
  return `<div class="check"><span class="dot ${dot}"></span>${esc(c.label)} <span class="hintline">${esc(bits.join(" · "))}</span></div>`;
}

/* Dialogue Statut & réglages (bouton-LEDs) : prérequis, moteurs, dossier de l'app,
   avertissements — l'ancien panneau latéral, à la demande. La langue (#langsel) est
   statique dans le HTML du dialogue, jamais re-rendue. */
function renderSys(force) {
  const d = state.data, body = $("#sysbody");
  if (!d) return;
  const html = `${d.app.warnings.map(w => `<div class="banner warn">${esc(w)}</div>`).join("")}
    <div class="label">${I18N.prereqs}</div>
    ${d.prereqs.map(prereqRow).join("")}
    <div class="label" style="margin-top:14px">${I18N.engines}</div>
    ${d.engines.length
      ? d.engines.map(e => `<div class="check"><span class="dot ${e.found ? "ok" : "ko"}"></span>${esc(I18N.engine_name(e.label))} <span class="hintline">${e.distro_version ? "distro " + esc(e.distro_version) + " · " : ""}${e.found}/${e.declared} ${I18N.binaries_word}</span></div>`).join("")
      : `<div class="hintline">${I18N.no_engine}</div>`}
    <div class="label" style="margin-top:14px">${I18N.install_dir}</div>
    <div class="hintline" style="word-break:break-all">${esc(d.app.install_dir)}</div>
    <div class="label" style="margin-top:14px">${I18N.sys_quit}</div>
    <div class="hintline">${I18N.sys_quit_hint}</div>
    <button class="btn ghost" data-action="app-quit" style="margin-top:8px">⏻ ${I18N.sys_quit}</button>`;
  if (force || ui.sysHtml !== html) {
    ui.sysHtml = html;
    body.innerHTML = html;
  }
}

/* État d'un harness vu du préflight : sert à MARQUER un bouton Équiper sans l'interdire
   (on peut légitimement équiper un projet avant d'installer le CLI). Le détail vit dans
   Statut & réglages. */
function harnessState(key) {
  const c = (state.data.prereqs || []).find(x => x.harness && x.key === key);
  if (!c) return { ok: true, note: "" };
  if (!c.found) return { ok: false, note: I18N.harness_absent };
  if (c.authed === false) return { ok: false, note: I18N.harness_unauth };
  return { ok: true, note: c.version || "" };
}

/* Un bouton Équiper par HARNESS : le choix ne se devine pas — il décide des artefacts
   copiés (.opencode/ ou .codex/ + AGENTS.md) ET du harness que les orchestrateurs
   piloteront (marqueur d'équipement). Croisé avec les moteurs quand il y en a plusieurs
   (le choix des skills ne se devine pas non plus). Un seul moteur + un seul harness =
   bouton unique, l'écran ne change pas. */
function equipBtns(p, cls, fkey) {
  const engines = state.data.engines || [], harnesses = state.data.harnesses || [];
  const label = p.equipped ? I18N.reequip : I18N.equip;
  const combos = [];
  (engines.length ? engines : [null]).forEach(e =>
    (harnesses.length ? harnesses : [null]).forEach(h => combos.push([e, h])));
  return combos.map(([e, h]) => {
    const st = h ? harnessState(h.key) : { ok: true, note: "" };
    const bits = [];
    if (e && engines.length > 1) bits.push(I18N.engine_name(e.label));
    if (h && harnesses.length > 1) bits.push(h.label + (st.ok ? "" : " ⚠"));
    const suffix = bits.length ? " (" + bits.join(" · ") + ")" : "";
    const key = [fkey, e && e.label, h && h.key].filter(Boolean).join("-");
    const title = h ? `${h.label}${st.note ? " — " + st.note : ""}` : "";
    return `<button class="btn ${cls}" data-fkey="${esc(key)}" data-action="equip"
      data-arg="${p.hash}" ${e ? `data-arg2="${esc(e.label)}"` : ""} ${h ? `data-arg3="${esc(h.key)}"` : ""}
      title="${esc(title)}">${label}${esc(suffix)}</button>`;
  }).join(" ");
}

/* Bouton « Nettoyer » — partagé entre l'étape 1 de la Bibliothèque et le bloc Livrables
   de la vue Run. ABSENT quand il n'y a rien à nettoyer (l'écran ne montre que des gestes
   possibles, et le compteur du libellé dit d'où il sort) ; DÉSACTIVÉ avec son motif
   pendant un run, comme les cartes d'orchestrateur — jamais de suppression en vol. */
function cleanBtn(project, files, alive, fkey, cls) {
  if (!project || !project.exists || !files.length) return "";
  const why = alive ? I18N.clean_blocked : I18N.clean_hint;
  return `<button class="btn ${cls}" ${alive ? "disabled" : ""} data-fkey="${fkey}"
      title="${esc(why)}" data-action="clean" data-arg="${project.hash}">🧹 ${esc(I18N.clean(files.length))}</button>`;
}

/* Pastilles d'état d'un projet — partagées entre l'étape 1 de la Bibliothèque
   et les cartes de la vue Projets. */
function projectChips(p) {
  return [
    p.equipped ? `<span class="badge ok">${I18N.equipped_v(p.equip_version)}</span>` : `<span class="badge warn">${I18N.not_equipped}</span>`,
    // Le harness du projet : c'est lui qui décide de la session tmux et des artefacts.
    // Toujours visible — se tromper de harness est l'erreur la plus coûteuse à diagnostiquer.
    p.harness_label ? `<span class="badge" title="${esc(p.model ? I18N.harness_model(p.model) : I18N.harness_model_default)}">${esc(p.harness_label)}${p.model ? " · " + esc(p.model) : ""}</span>` : "",
    p.timeouts ? `<span class="badge steel" title="${esc(I18N.tmo_badge_title)}">${esc(I18N.tmo_badge(p.timeouts))}</span>` : "",
    p.update_available ? `<span class="badge warn">${I18N.update_prompts}</span>` : "",
    p.need.ready ? "" : `<span class="badge warn">${esc(p.need.why || I18N.need_missing)}</span>`,
    p.run ? (p.run.alive
        ? `<span class="badge">${I18N.running}${p.run.step ? " · " + esc(I18N.step_word(p.run.step.index, (p.run.step.labels || I18N.steps).length)) : ""}</span>`
        : `<span class="badge ${p.run.exit_code === 0 ? "ok" : "err"}">${I18N.finished} (code ${esc(p.run.exit_code ?? "?")})</span>`) : "",
    !p.exists ? `<span class="badge err">${I18N.missing_dir}</span>` : "",
  ].join("");
}

function addProjectRow() {
  return `<div class="addrow">
      <input type="text" id="newpath" aria-label="${I18N.add_project_ph}" placeholder="${I18N.add_project_ph}">
      <button class="btn ghost" data-fkey="add-btn" data-action="add-project">${I18N.add}</button>
      <button class="btn ghost" data-fkey="browse-btn" data-action="browse">📁 ${I18N.browse}</button>
    </div>`;
}

/* Étape 1 de la Bibliothèque : le projet de travail — son état (badges partagés avec
   la vue Projets), l'équipement, et l'ajout d'un nouveau projet. Remplace l'ancien
   panneau latéral ET la bannière « non équipé ». */
function projectStepHtml(project) {
  const inner = project
    ? `<div class="oname">${esc(project.name)}</div>
       <div class="path">${esc(project.path)}</div>
       <div class="chips" style="margin:6px 0 2px">${projectChips(project)}</div>
       <div class="actions">${equipBtns(project, project.equipped && !project.update_available ? "ghost" : "", "equip-step")}
         ${project.equipped ? `<button class="btn" data-fkey="tmo-step" data-action="timeouts" data-arg="${project.hash}" title="${esc(I18N.tmo_hint)}">⏱ ${I18N.tmo_btn}${project.timeouts ? " · " + esc(I18N.tmo_badge(project.timeouts).replace("⏱ ", "")) : ""}</button>` : ""}
         ${cleanBtn(project, cleanableOf(project, runFor(project)),
                    !!(project.run && project.run.alive), "clean-step", "ghost")}</div>`
    : `<div class="hintline">${I18N.project_step_hint}</div>`;
  return `<div class="label" style="margin:0 0 6px">${I18N.project_step}</div>
    <div class="card" style="margin-bottom:16px">${inner}${addProjectRow()}</div>`;
}

/* Étape 2 de la Bibliothèque : le bloc besoin. Le textarea est rendu vide (le contenu
   vit hors HTML, injecté par syncNeedTa) : la frappe ne fait jamais re-rendre la région,
   et un re-rendu pour toute autre raison ne perd jamais le brouillon. */
function needBlockHtml(project) {
  const off = !project || !project.exists ? "disabled" : "";
  const state = !project ? I18N.select_project_first
    : !project.exists ? I18N.missing_dir
    : needStateLabel(project);
  return `<div class="label" style="margin:0 0 6px">${I18N.need_step}</div>
    <div class="card" style="margin-bottom:16px">
      <textarea id="needta" data-fkey="needta" class="needta" spellcheck="false" ${off}
        placeholder="${esc(I18N.need_ph)}" aria-label="${esc(I18N.need_step)}"></textarea>
      <div style="display:flex;gap:10px;align-items:center;margin-top:8px">
        <span class="hintline" id="needstate" aria-live="polite">${esc(state)}</span>
        <button class="btn ghost" style="margin-left:auto;flex-shrink:0" ${off}
          data-fkey="needsave-btn" data-action="need-save">${I18N.need_save}</button>
      </div>
    </div>`;
}

function libraryHtml() {
  const d = state.data, project = currentProject();
  // Un binaire supprimé disparaît du menu (c'est le geste voulu : l'utilisateur ne garde
  // que ses orchestrateurs) ; un binaire présent mais non exécutable reste visible avec
  // le motif chmod — deux situations différentes, deux traitements.
  const kept = d.orchestrators.filter(o => o.binary_found);
  const card = (o) => {
    const reasons = [];
    if (!o.binary_executable) reasons.push(I18N.binary_chmod(o.binary));
    if (!project) reasons.push(I18N.select_project_first);
    else {
      if (!project.equipped) reasons.push(I18N.not_equipped + " (« " + I18N.equip + " »)");
      if (o.needs_need_md && project.equipped && !project.need.ready)
        reasons.push(project.need.why || I18N.need_missing);   // le remède est l'étape 1, juste au-dessus
      if (project.run && project.run.alive) reasons.push(I18N.running);
    }
    const disabled = reasons.length ? "disabled" : "";
    return `<div class="ocard">
      <span style="display:flex;gap:6px"><span class="badge ${o.family === "production" ? "" : "steel"}">${esc(o.family || "")}</span>${o.recommended ? `<span class="badge ok">${I18N.recommended}</span>` : ""}${o.beta ? `<span class="badge warn">${I18N.beta}</span>` : ""}</span>
      <span class="oname">${esc(tr(o.title) || o.id)}</span>
      <span class="odesc">${esc(tr(o.description))}</span>
      ${reasons.length ? `<span class="hintline">⛔ ${esc(reasons[0])}</span>` : ""}
      <div style="display:flex;gap:6px">
        <button class="btn" ${disabled} data-fkey="launch-${esc(o.id)}" data-action="start-run" data-arg="${project ? project.hash : ""}" data-arg2="${esc(o.id)}">${I18N.launch}</button>
      </div>
    </div>`;
  };
  const grid = (list) => `<div class="grid">${list.map(card).join("")}</div>`;
  // Groupes par moteur seulement s'il y en a plusieurs : à un seul moteur, rien ne change.
  const body = d.engines.length > 1
    ? d.engines.map(e => {
        const own = kept.filter(o => o.engine === e.label);
        return own.length ? `<div class="label" style="margin:12px 0 6px">${esc(I18N.engine_name(e.label))}${e.distro_version ? ` <span class="hintline">distro ${esc(e.distro_version)}</span>` : ""}</div>${grid(own)}` : "";
      }).join("")
    : (kept.length ? grid(kept) : "");
  const empty = d.orchestrators.length ? I18N.no_binaries : I18N.no_manifest;
  // Les harness ne comptent pas comme « manquants » un par un : ils s'excluent. Seul
  // le cas « aucun des deux » est bloquant, et il a son propre message.
  const missing = d.prereqs.filter(c => !c.harness && !c.found).map(c => c.name);
  const noHarness = d.prereqs.some(c => c.harness) && !d.prereqs.some(c => c.harness && c.found);
  return `<div class="flow"><h1 class="page">${I18N.library_title}</h1>
    <div class="sub">${I18N.library_sub(kept.length)}</div>
    ${missing.length ? `<div class="banner warn">${esc(I18N.prereq_missing(missing.join(", ")))}</div>` : ""}
    ${noHarness ? `<div class="banner warn">${esc(I18N.no_harness)}</div>` : ""}
    ${d.app.warnings.map(w => `<div class="banner warn">${esc(w)}</div>`).join("")}
    ${projectStepHtml(project)}
    ${needBlockHtml(project)}
    <div class="label" style="margin:0 0 6px">${I18N.orch_step}</div>
    ${kept.length ? body : `<div class="empty">${empty}</div>`}</div>`;
}

/* Ligne d'étapes signature : piste + fil en dégradé de lever + points done/now/todo.
   Positions en % (8 → 92) comme les maquettes ; le fil s'arrête au point courant. */
function pipeHtml(step) {
  /* Libellés du payload (timeline déclarée au manifeste) ; I18N.steps = repli usine. */
  const labels = step.labels || I18N.steps;
  const total = labels.length;
  const pos = (n) => total > 1 ? 8 + (n - 1) * (84 / (total - 1)) : 50;
  const items = labels.map((name, i) => {
    const n = i + 1;
    const done = step.completed || n < step.index;
    const cls = done ? "done" : (n === step.index ? "now" : "");
    return `<div class="st ${cls}" ${n === step.index && !step.completed ? 'aria-current="step"' : ""} style="left:${pos(n)}%"><span class="d" aria-hidden="true"></span><span class="l">${esc(name)}</span></div>`;
  }).join("");
  const fill = step.completed ? pos(total) : pos(step.index);
  return `<div class="steps"><span class="trk" aria-hidden="true"></span><span class="fil" aria-hidden="true" style="right:${100 - fill}%"></span>${items}</div>`;
}

function phaseRow(p) {
  const map = { DONE: "done", PENDING: "run", REJECTED: "rej", TODO: "todo" };
  const cls = map[p.state] || "todo";
  const label = I18N.phase_states[p.state] || (p.state || "?").toLowerCase();
  const covers = p.covers && p.covers.length ? I18N.covers_word + p.covers.join(", ") : "";
  const tasks = p.tasks != null ? I18N.tasks_word(p.tasks) : "";
  // Le feedback du vérificateur : bloc dépliable sous la phase rejetée (UC4), ou sous la
  // phase en cours quand une tentative précédente a échoué (cas retry).
  const fb = p.feedback ? `<details class="fb ${p.state==="PENDING"?"retry":""}" data-fkey="fb-${esc(p.id)}" ${p.state==="REJECTED"?"open":""}>
      <summary>${p.state==="REJECTED" ? I18N.fb_rejected : I18N.fb_retry}</summary>
      <pre>${esc(p.feedback)}</pre></details>` : "";
  return `<div class="phase ${p.state==="PENDING"?"active":""} ${p.state==="REJECTED"?"rejected":""}">
    <span class="num">${esc(p.id ?? "·")}</span>
    <span class="nm">${esc(p.name)}<small>${esc([tasks, p.nature].filter(Boolean).join(" · "))}${esc(covers)}</small></span>
    <span class="pstate ${cls}">${label}</span></div>${fb}`;
}

function runMount() {
  const project = currentProject();
  const bare = (main) => ({ scaffold: SC_MAIN, regions: { main } });
  if (!project) return bare(`<div class="empty">${I18N.no_project_selected}</div>`);
  const r = state.run;
  if (!r) { refreshRun(); return bare(`<div class="empty">…</div>`); }
  if (r.error) return bare(`<div class="banner err">${esc(r.error)}</div>`);
  if (!r.session.exists) return bare(
    `<p class="kicker">${I18N.run_title}</p>
     <h1 class="page">${esc(project.name)}</h1>
     <div class="empty">${I18N.no_run}</div>
     ${deliverables(project, r)}`);

  const dead = !r.session.alive;
  const gate = r.gate;
  const h = project.hash;
  let banner = "";
  if (dead) {
    const code = r.session.exit_code;
    const failBtn = (code !== 0 && r.files["failReport.md"] && r.files["failReport.md"].exists)
      ? `<button class="btn ghost" data-fkey="failrep" data-action="show-doc" data-arg="${h}" data-arg2="failReport.md">📄 failReport.md</button>` : "";
    banner = `<div class="banner ${code === 0 ? "ok" : "err"}">${code === 0 ? I18N.run_dead_ok(code) : I18N.run_dead_err(code ?? "?")}
      <span style="margin-left:10px">${failBtn}
      <button class="btn ghost" data-fkey="kill-banner" data-action="kill" data-arg="${h}">${I18N.kill}</button></span></div>`;
  }
  // Une porte sans fichier (file: null au manifeste) valide un AFFICHAGE écran
  // (périmètre d'audit, carte…) : pas d'aperçu de livrable ni de boutons d'édition.
  const editable = gate && gate.file && EDITABLE.has(gate.file);
  const gateBlock = gate ? `<div class="gate">
      <p class="gate-k">${I18N.gate_kicker(r.step.index, (r.step.labels || I18N.steps).length)}</p>
      <h2>${esc(tr(gate.title) || gate.id)}</h2>
      <div class="hint">${esc(tr(gate.hint))}</div>
      ${gate.file ? `<div class="gate-doc" id="gatedoc">${ui.gateDocHtml || "…"}</div>`
                  : `<div class="hintline">${I18N.gate_screen}</div>`}
      <div class="gate-bar">
        ${gateControlsHtml(gate, h)}
        ${editable ? `<button class="btn ghost" data-fkey="gate-edit" data-action="edit-file" data-arg="${h}" data-arg2="${esc(gate.file)}" data-gate="1">${I18N.edit} ${esc(gate.file)}</button>` : ""}
        ${gate.file ? `<button class="btn ghost" data-fkey="gate-open" data-action="open-editor" data-arg="${h}" data-arg2="${esc(gate.file)}">${I18N.open_editor}</button>` : ""}
        ${gate.file ? `<span class="hintline" style="margin-left:auto">${I18N.gate_watch(esc(gate.file))}</span>` : ""}
      </div></div>` : "";

  const meta = `<div class="runmeta">
      <span>${I18N.binary_label} : <b>${esc(r.session.binary || "—")}</b></span>
      ${r.blackboard.project ? `<span>${I18N.project_label} : <b>${esc(r.blackboard.project)}</b></span>` : ""}
      ${r.blackboard.verify_cmd ? `<span>${I18N.verify_cmd} : <b>${esc(r.blackboard.verify_cmd)}</b></span>` : ""}
      ${r.blackboard.last_test_count != null ? `<span>${I18N.last_green} : <b>${esc(r.blackboard.last_test_count)}</b></span>` : ""}
      <span>${I18N.sessions} : <b>${esc(r.session.name)}${r.agent_session.alive ? " · " + esc(r.agent_session.name) : ""}</b></span>
      <span>${I18N.harness_word} : <b>${esc(r.harness.label)}${r.harness.model ? " · " + esc(r.harness.model) : ""}</b></span>
      <span><button class="btn small" data-fkey="tmo-run" data-action="timeouts" data-arg="${h}" title="${esc(I18N.tmo_hint)}">⏱ ${I18N.tmo_btn}${project.timeouts ? " · " + esc(I18N.tmo_badge(project.timeouts).replace("⏱ ", "")) : ""}</button></span>
    </div>`;

  const phases = r.phases.length ? r.phases.map(phaseRow).join("")
      : `<div class="hintline">${I18N.no_blackboard}</div>`;
  // screen_html : capture -e convertie côté serveur (texte échappé avant habillage) ;
  // repli sur la capture brute échappée si absent.
  const screenHtml = ui.termTab === "orch"
      ? (r.screen_html ?? esc(r.screen || I18N.no_capture))
      : (r.agent_screen_html ?? esc(r.agent_screen || "(pas de capture)"));
  // Écran alternatif = TUI du harness (OpenCode, Codex) : grille fixe ajustée à la largeur
  // (classe .tui, fitTerm) ; sinon log défilant replié. ⛶ bascule le pane en plein écran,
  // Échap referme.
  const shown = ui.termTab === "orch" ? r.session : r.agent_session;
  const zoomLabel = ui.termZoom ? I18N.term_unzoom : I18N.term_zoom;
  const term = `<div class="termwrap${ui.termZoom ? " zoom" : ""}">
      <div class="termtabs">
        <button class="${ui.termTab==="orch"?"active":""}" data-fkey="tab-orch" aria-pressed="${ui.termTab==="orch"}" data-action="term-tab" data-arg="orch">${I18N.tab_orch}</button>
        <button class="${ui.termTab==="agent"?"active":""}" ${r.agent_session.alive?"":"disabled"} data-fkey="tab-agent" aria-pressed="${ui.termTab==="agent"}" data-action="term-tab" data-arg="agent">${I18N.tab_agent(r.harness.label)}</button>
        <button class="zoombtn" data-fkey="term-zoom" data-action="term-zoom" aria-pressed="${ui.termZoom}" title="${zoomLabel}" aria-label="${zoomLabel}">⛶</button>
      </div>
      <pre class="term${shown.alt ? " tui" : ""}" id="termpane" data-cols="${shown.cols || 220}" data-rows="${shown.rows || 0}" tabindex="0" aria-label="${ui.termTab==="orch" ? I18N.tab_orch : I18N.tab_agent(r.harness.label)}">${screenHtml}</pre>
      <div class="hintline" style="margin-top:6px">${I18N.attach_hint(esc(ui.termTab==="orch" ? r.session.name : r.agent_session.name))}</div></div>`;

  const controls = dead ? "" : `<div class="actions" style="margin-bottom:14px">
      <button class="btn ghost" data-fkey="interrupt" data-action="interrupt" data-arg="${h}">${I18N.interrupt}</button>
      <button class="btn danger" data-fkey="kill" data-action="kill" data-arg="${h}">${I18N.kill}</button>
    </div>`;

  return { scaffold: SC_RUN, regions: {
    runhead: `<p class="kicker">${I18N.run_title}</p><h1 class="page">${esc(project.name)}${gate ? `<span class="statchip">${I18N.gate_open}</span>` : ""}</h1>${meta}${banner}${pipeHtml(r.step)}`,
    stepline: r.step.detail ? `→ ${esc(r.step.detail)}` : "",
    gatelive: gate ? esc(I18N.gate_live(tr(gate.title) || gate.id)) : "",
    gate: gateBlock,
    controls,
    phases,
    deliv: deliverables(project, r),
    term,
  } };
}

function deliverables(project, r) {
  const h = project.hash;
  // vrais <button> (un <a> sans href n'est pas focusable au clavier)
  const docBtn = (n) => `<button class="linklike" data-fkey="doc-${esc(n)}" data-action="show-doc" data-arg="${h}" data-arg2="${esc(n)}">${esc(n)}</button>`;
  const pencil = (n) => EDITABLE.has(n)
      ? ` <button class="linklike" data-fkey="edit-${esc(n)}" title="${I18N.edit} ${esc(n)}" aria-label="${I18N.edit} ${esc(n)}" data-action="edit-file" data-arg="${h}" data-arg2="${esc(n)}">✏️</button>` : "";
  // Corbeille par livrable, au bout de la ligne : supprimer CE fichier (le nom accessible
  // reste stable, seul le motif de l'infobulle change quand un run tient les fichiers).
  const alive = r ? r.session.alive : !!(project.run && project.run.alive);
  const trash = (n, isDir) => ` <button class="linklike" ${alive ? "disabled" : ""} data-fkey="del-${esc(n)}"
      title="${esc(alive ? I18N.clean_blocked : I18N.clean_one(isDir ? n + "/" : n))}"
      aria-label="${esc(I18N.clean_one(isDir ? n + "/" : n))}" data-action="clean-file"
      data-arg="${h}" data-arg2="${esc(n)}"${isDir ? ' data-dir="1"' : ""}>🗑️</button>`;
  // need.md tient sa propre ligne (📝, l'entrée de l'humain) UNIQUEMENT quand il porte un
  // vrai besoin : le serveur en décide (cleanable), sinon le compteur du bouton et la liste
  // affichée diraient deux choses différentes sur un projet fraîchement équipé.
  const cleanables = cleanableOf(project, r);
  const needShown = cleanables.some(c => c.name === "need.md");
  const icon = (n) => n === "need.md" ? "📝" : "📄";
  const files = r ? r.files : null;
  const items = files
    ? Object.entries(files).filter(([n, m]) => m.exists && (n !== "need.md" || needShown))
        .map(([n, m]) => `<div class="dlv">${icon(n)} ${docBtn(n)}${pencil(n)}
             <span class="hintline">${new Date(m.mtime*1000).toLocaleString()}</span>${trash(n)}</div>`)
    : (project.deliverables || []).map(d => `<div class="dlv">📄 ${docBtn(d.file)}${pencil(d.file)}${trash(d.file)}</div>`);
  // Dossiers de constats des audits/doc : pas d'aperçu (ce sont des dossiers), mais ils se
  // voient et se suppriment — sinon le compteur du bouton annoncerait plus que la liste.
  const dirs = cleanables.filter(c => c.dir);
  items.push(...dirs.map(c => `<div class="dlv">📁 ${esc(c.name)}/
      <span class="hintline">${I18N.clean_dir_hint}</span>${trash(c.name, true)}</div>`));
  if (!items.length) return "";
  return `<div class="card" style="margin-top:12px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <div class="label" style="margin:0">${I18N.deliverables}</div>
        <span style="margin-left:auto">${cleanBtn(project, cleanables, alive, "clean-deliv", "ghost")}</span>
      </div>${items.join("")}</div>`;
}

function projectsHtml() {
  const d = state.data;
  const cards = d.projects.map(p => {
    return `<div class="pcard">
      <span class="oname">${esc(p.name)}</span>
      <span class="path">${esc(p.path)}</span>
      <div class="chips">${projectChips(p)}</div>
      ${p.run && p.run.gate ? `<div class="hintline">🚪 ${esc(tr(p.run.gate.title) || I18N.gate_open)}</div>` : ""}
      <div class="actions">
        ${equipBtns(p, p.update_available ? "" : "ghost", "equip-" + p.hash)}
        ${p.equipped ? `<button class="btn ghost" data-fkey="editneed-${p.hash}" data-action="edit-file" data-arg="${p.hash}" data-arg2="need.md">${I18N.edit_need}</button>` : ""}
        ${p.equipped ? `<button class="btn ghost" data-fkey="tmo-${p.hash}" data-action="timeouts" data-arg="${p.hash}" title="${esc(I18N.tmo_hint)}">⏱ ${I18N.tmo_btn}</button>` : ""}
        ${p.run ? `<button class="btn" data-fkey="openrun-${p.hash}" data-action="open-run" data-arg="${p.hash}">${I18N.open_run}</button>` : ""}
        <button class="btn ghost" data-fkey="forget-${p.hash}" data-action="forget" data-arg="${p.hash}">${I18N.forget}</button>
      </div>
      ${(p.deliverables||[]).length ? `<div>${p.deliverables.map(x => `<span class="dlv">📄 <button class="linklike" data-fkey="doc-${p.hash}-${esc(x.file)}" data-action="show-doc" data-arg="${p.hash}" data-arg2="${esc(x.file)}">${esc(x.file)}</button></span>`).join(" · ")}</div>` : ""}
    </div>`;
  }).join("");
  return `<h1 class="page">${I18N.projects_title}</h1>
    <div class="sub">${I18N.projects_sub}</div>
    <div class="card" style="margin-bottom:16px">${addProjectRow()}</div>
    ${d.projects.length ? `<div class="grid">${cards}</div>` : `<div class="empty">${I18N.no_projects}</div>`}`;
}
function openRun(h) {
  const p = byHash(h); if (!p) return;
  ui.activeProject = p.path; localStorage.setItem("mm_active", p.path);
  state.run = null; location.hash = "#/run";
}

/* ── montage par régions ── */
function mount(scaffold, regions) {
  const view = $("#view");
  if (ui.scaffold !== scaffold) {
    // squelette différent (changement de vue) : reconstruction complète, une seule fois
    const focus = captureFocus(view);
    view.innerHTML = scaffold;
    ui.scaffold = scaffold; ui.regions = {};
    for (const [key, html] of Object.entries(regions)) {
      const slot = view.querySelector(`[data-region="${key}"]`);
      if (slot) { slot.innerHTML = html; ui.regions[key] = html; }
    }
    restoreFocus(view, focus);
    fitTerm();
    const pane = $("#termpane");
    if (pane) pane.scrollTop = pane.scrollHeight;   // premier affichage : collé au bas
    return;
  }
  for (const [key, html] of Object.entries(regions)) {
    if (ui.regions[key] === html) continue;         // région intacte : DOM intact
    const slot = view.querySelector(`[data-region="${key}"]`);
    if (!slot) continue;
    const focus = captureFocus(slot);
    const folds = captureFolds(slot);
    const scroll = captureScrolls(slot);
    slot.innerHTML = html;
    ui.regions[key] = html;
    fitTerm();                                      // avant la restauration : la police change scrollHeight
    restoreFolds(slot, folds);
    restoreFocus(slot, focus);
    restoreScrolls(slot, scroll);
  }
}

/* Une grille TUI se rend à géométrie fixe : la police est calculée pour que les N
   colonnes du pane tmux tiennent dans la largeur disponible. La largeur de glyphe
   est mesurée (les monospaces varient entre 0.55 et 0.62 em), pas estimée. Clamp :
   sous 5px c'est illisible (léger défilement horizontal accepté) ; le plein écran
   (⛶) redonne une taille confortable. */
let _glyphRatio = null;
function fitTerm() {
  const pane = $("#termpane");
  if (!pane) return;
  if (!pane.classList.contains("tui")) { pane.style.fontSize = ""; return; }
  if (_glyphRatio === null) {
    const ctx = document.createElement("canvas").getContext("2d");
    ctx.font = "100px " + getComputedStyle(pane).fontFamily;
    _glyphRatio = ctx.measureText("M".repeat(50)).width / 50 / 100;
  }
  const cs = getComputedStyle(pane);
  const usable = pane.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const cols = parseInt(pane.dataset.cols, 10) || 220;
  // Grille entière visible : cale largeur ET hauteur (line-height:1 → rangée = font-size).
  // Plancher 9px : en dessous c'est illisible, on laisse alors scroller (overflow:auto).
  let size = usable / cols / _glyphRatio;
  const rows = parseInt(pane.dataset.rows, 10) || 0;
  if (rows) {
    const usableH = pane.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    size = Math.min(size, usableH / rows);
  }
  pane.style.fontSize = Math.min(16, Math.max(9, size)) + "px";
}
window.addEventListener("resize", fitTerm);

/* Remplacer l'innerHTML d'une région détruit son DOM : on mémorise le focus (avec la
   saisie et la sélection s'il est dans un champ), les blocs dépliés et les positions de
   scroll, puis on restaure — sinon Tab devient inutilisable à chaque rafraîchissement. */
function captureFocus(root) {
  const el = document.activeElement;
  if (!el || !root.contains(el)) return null;
  const key = el.dataset.fkey || el.id || null;
  if (!key) return null;
  const isField = el.tagName === "INPUT" || el.tagName === "TEXTAREA";
  return { key, isField, value: isField ? el.value : null,
           selStart: isField ? el.selectionStart : null, selEnd: isField ? el.selectionEnd : null };
}
function restoreFocus(root, saved) {
  if (!saved) return;
  const el = root.querySelector(`[data-fkey="${CSS.escape(saved.key)}"]`) || document.getElementById(saved.key);
  if (!el) return;
  el.focus({ preventScroll: true });
  if (saved.isField && "value" in el) {
    el.value = saved.value;
    try { el.setSelectionRange(saved.selStart, saved.selEnd); } catch (e) {}
  }
}
function captureFolds(root) {
  return { open: new Set([...root.querySelectorAll("details[data-fkey][open]")].map(d => d.dataset.fkey)),
           closed: new Set([...root.querySelectorAll("details[data-fkey]:not([open])")].map(d => d.dataset.fkey)) };
}
function restoreFolds(root, folds) {
  root.querySelectorAll("details[data-fkey]").forEach(d => {
    if (folds.open.has(d.dataset.fkey)) d.open = true;
    else if (folds.closed.has(d.dataset.fkey)) d.open = false;  // choix de l'utilisateur > défaut
  });
}
function captureScrolls(root) {
  const out = {};
  const pane = root.querySelector("#termpane");
  if (pane) out.term = { nearBottom: pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60,
                         top: pane.scrollTop };
  const doc = root.querySelector("#gatedoc");
  if (doc) out.gate = doc.scrollTop;
  return out;
}
function restoreScrolls(root, saved) {
  const pane = root.querySelector("#termpane");
  if (pane) {
    // colle au bas (suivi du flux), sauf si l'utilisateur est remonté dans l'historique
    if (!saved.term || saved.term.nearBottom) pane.scrollTop = pane.scrollHeight;
    else pane.scrollTop = saved.term.top;
  }
  const doc = root.querySelector("#gatedoc");
  if (doc && saved.gate != null) doc.scrollTop = saved.gate;
}

function render() {
  const view = $("#view");
  if (!state.data) { view.innerHTML = `<div class="empty">${I18N.connecting}</div>`; ui.scaffold = null; ui.regions = {}; return; }
  $("#appver").textContent = "v" + state.data.app.version;
  syncTopbar();
  const tab = route();
  document.querySelectorAll("nav.tabs a").forEach(a => {
    const active = a.dataset.tab === tab;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const anyRun = state.data.projects.some(p => p.run && p.run.alive);
  const anyGate = state.data.projects.some(p => p.run && p.run.gate);
  $("#leds").innerHTML = `<span class="led on"></span><span class="led ${anyRun?"busy":""}"></span><span class="led ${anyGate?"on":""}"></span>`;
  const bell = $("#bell");
  bell.classList.toggle("on", ui.notif);
  bell.setAttribute("aria-pressed", String(ui.notif));
  bell.title = ui.notif ? I18N.notif_title_on : I18N.notif_title_off;
  bell.setAttribute("aria-label", bell.title);
  // le titre d'onglet reflète l'état sans aucune permission : porte en attente > run actif
  document.title = (anyGate ? "🚪 " : anyRun ? "⚙️ " : "") + "MAIsterMind — cockpit";

  if (tab === "run") { const m = runMount(); mount(m.scaffold, m.regions); }
  else {
    mount(SC_MAIN, { main: tab === "projets" ? projectsHtml() : libraryHtml() });
    // Bibliothèque : le textarea du bloc besoin est rendu vide, son contenu (fichier ou
    // brouillon) est injecté après chaque montage — jamais dans le HTML de la région.
    if (tab !== "projets") loadNeedBlock(currentProject());
  }
  connectEvents();   // suit le projet actif ; sans objet si la cible n'a pas changé
}

/* ── délégation d'événements : un listener par type, actions déclarées en data-action ── */
const ACTIONS = {
  "toggle-notif": () => toggleNotif(),
  "toggle-theme": () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true),
  "sys-open":     () => { renderSys(true); openDialog($("#sysdlg")); },
  "app-quit":     () => appQuit(),
  "dlg-close":    (el) => el.closest("dialog").close(),
  "fs-up":        () => fsGo(ui.fs.parent),
  "fs-home":      () => fsGo(ui.fs.home),
  "fs-go":        (el) => fsGo(el.dataset.arg),
  "fs-choose":    () => fsChoose(),
  "editor-save":  () => editorSave(),
  "need-save":    () => needSave(),
  "add-project":  () => addProject(),
  "browse":       () => browse(),
  "equip":        (el) => equip(el.dataset.arg, el.dataset.arg2, el.dataset.arg3),
  "timeouts":     (el) => openTimeouts(el.dataset.arg),
  "timeouts-save": () => timeoutsSave(),
  "forget":       (el) => forget(el.dataset.arg),
  "clean":        (el) => cleanAll(el.dataset.arg),
  "clean-file":   (el) => cleanOne(el.dataset.arg, el.dataset.arg2, el.dataset.dir === "1"),
  "start-run":    (el) => startRun(el.dataset.arg, el.dataset.arg2),
  "edit-file":    (el) => openFileEditor(el.dataset.arg, el.dataset.arg2, el.dataset.gate === "1"),
  "open-editor":  (el) => openEditor(el.dataset.arg, el.dataset.arg2),
  "show-doc":     (el) => showDoc(el.dataset.arg, el.dataset.arg2),
  "gate-answer":  (el) => answerGate(el.dataset.arg, el.dataset.arg2, el.dataset.kind),
  "gate-answer-text": (el) => answerGateText(el.dataset.arg),
  "interrupt":    (el) => interruptRun(el.dataset.arg),
  "kill":         (el) => killRun(el.dataset.arg),
  "term-tab":     (el) => { ui.termTab = el.dataset.arg;
                            // La TUI de l'agent se lit écran entier : son onglet ouvre en ⛶ (Échap referme).
                            if (el.dataset.arg === "agent") ui.termZoom = true;
                            render(); },
  "term-zoom":    () => { ui.termZoom = !ui.termZoom; render(); },
  "open-run":     (el) => openRun(el.dataset.arg),
};
document.addEventListener("click", (e) => {
  const el = e.target instanceof Element ? e.target.closest("[data-action]") : null;
  if (!el || el.disabled) return;
  const handler = ACTIONS[el.dataset.action];
  if (handler) { e.preventDefault(); handler(el); }
});
document.addEventListener("input", (e) => {
  if (e.target.id !== "needta") return;
  const p = currentProject();
  if (!p) return;
  // Le brouillon vit par projet (hash) : il survit aux re-rendus de région et aux
  // allers-retours entre projets. Revenir au texte enregistré efface le brouillon.
  if (ui.need.hash === p.hash && e.target.value === ui.need.saved) delete ui.needDrafts[p.hash];
  else ui.needDrafts[p.hash] = e.target.value;
  const st = $("#needstate");
  if (st) st.textContent = needStateLabel(p);   // état mis à jour en direct, hors cycle de rendu
});
document.addEventListener("change", (e) => {
  if (e.target.id === "projsel") {
    ui.activeProject = e.target.value; localStorage.setItem("mm_active", e.target.value);
    state.run = null; refreshRun(); render();
  }
  if (e.target.id === "langsel") {
    applyLang(e.target.value);
    if ($("#sysdlg").open) renderSys(true);       // le dialogue hôte suit immédiatement
    ui.scaffold = null; ui.regions = {};          // tout re-rendre dans la nouvelle langue
    if (ui.sse.es) { ui.sse.es.close(); ui.sse = { es: null, path: undefined, last: 0, attemptAt: 0 }; }
    render(); refreshState(); refreshRun();       // les messages serveur suivent (&lang=)
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.id === "newpath") addProject();
  if (e.key === "Enter" && e.target.id === "gateinput") {
    const p = currentProject();
    if (p) answerGateText(p.hash);
  }
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && $("#editdlg").open) { e.preventDefault(); editorSave(); }
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && !$("#editdlg").open && e.target.id === "needta") { e.preventDefault(); needSave(); }
  if (e.key === "Escape" && ui.termZoom && !document.querySelector("dialog[open]")) { ui.termZoom = false; render(); }
});

window.addEventListener("hashchange", () => { state.run = null; render(); refreshRun(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refreshState(); refreshRun(); } });
applyLang(ui.lang);   // estampille le texte statique (nav, dialogues) dans la langue active
render();
refreshState();
setInterval(() => { if (!document.hidden && !sseHealthy()) refreshState(); }, 4000);
setInterval(() => { if (!document.hidden && !sseHealthy()) refreshRun(); }, 2000);
</script>
</body>
</html>"""


# ─── INSTANCE UNIQUE + DÉMARRAGE ──────────────────────────────────────────────

def existing_instance_url() -> str | None:
    """Deux instances = confusion. Si une app répond déjà sur le port avec le jeton
    du verrou, on rouvre simplement son URL au lieu d'en démarrer une seconde."""
    lock = read_json(LOCK_FILE, None)
    if not lock or not lock.get("token"):
        return None
    url = f"http://{HOST}:{lock.get('port', PORT)}/api/ping?t={lock['token']}"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if json.load(response).get("app") == "maistermind":
                return f"http://{HOST}:{lock.get('port', PORT)}/?t={lock['token']}"
    except Exception:
        return None
    return None


def refuse_native_windows():
    """La variante Windows tourne DANS WSL, jamais sous Windows natif (binaires
    Linux, tmux inexistant côté Windows). Ce garde vise le seul contournement
    possible : un double-clic sur la source .py avec un Python Windows installé.
    Sans lui, l'app mourait sur « tmux est requis » dans une console refermée
    aussitôt — ici, elle explique le bon geste et attend avant de se fermer."""
    if os.name != "nt":
        return
    try:
        print("=" * 64)
        print("MAIsterMind ne tourne pas sous Windows natif : tout vit dans WSL 2.")
        print("Double-clique sur MAIsterMind.bat (dans ce dossier) — il lance")
        print("l'app dans WSL. Ou, depuis le terminal WSL : ./MAIsterMind_App")
        print("MAIsterMind does not run on native Windows: everything lives in")
        print("WSL 2. Double-click MAIsterMind.bat (in this folder), or run")
        print("./MAIsterMind_App from the WSL terminal. See INSTALL.md.")
        print("=" * 64)
        input("Entrée pour fermer · Press Enter to close ")
    except Exception:
        pass  # lancé sans console (pythonw) : rien à afficher, on sort quand même
    sys.exit(1)


def enrich_path():
    """Lancée par double-clic (bundle .app macOS, .desktop), l'app hérite du PATH
    de launchd ou du bureau — sans Homebrew, node@22 (keg-only), npm global ni
    harness d'agent. On AJOUTE les emplacements standards existants, sans jamais rien
    retirer : le mode terminal garde son PATH intact, et tmux (lancé par l'app)
    transmet ce PATH enrichi aux orchestrateurs, qui y trouvent leur harness. Les DEUX
    emplacements de harness sont ajoutés : le choix se fait par projet, au runtime."""
    home = os.path.expanduser("~")
    candidates = [
        "/opt/homebrew/bin", "/opt/homebrew/opt/node@22/bin",   # Homebrew Apple Silicon
        "/usr/local/bin", "/usr/local/opt/node@22/bin",         # Homebrew Intel
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".opencode", "bin"),
        os.path.join(home, ".codex", "bin"),
        os.path.join(home, ".npm-global", "bin"),
    ]
    current = os.environ.get("PATH", "").split(os.pathsep)
    added = [c for c in candidates if os.path.isdir(c) and c not in current]
    if added:
        os.environ["PATH"] = os.pathsep.join(current + added)
    # PATH du shell de login INTERACTIF en TÊTE : c'est celui que tmux donne à l'agent dans
    # son pane (nvm/fnm/volta chargés par les rc). Sans lui, l'app lancée sans terminal — et
    # les orchestrateurs, qui héritent de son PATH via le serveur tmux — exécutaient les
    # verdicts avec le Node SYSTÈME (v18) pendant que l'agent voyait Node 22 (incident du
    # 23/08/2026 : « styleText », suite verte côté agent, rouge côté orchestrateur).
    login_path = probe_login_path()
    if login_path:
        head = [p for p in login_path.split(os.pathsep) if p]
        rest = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in head]
        os.environ["PATH"] = os.pathsep.join(head + rest)


_LOGIN_PATH = {"probed": False, "path": None}


def probe_login_path(timeout: int = 10) -> str | None:
    """PATH du shell de login interactif de l'utilisateur ($SHELL -lic), mémoïsé ; None si
    la sonde échoue ou si MM_TOOLCHAIN_PROBE=0. Même sonde que mm_core côté moteur (l'app
    reste mono-fichier : elle n'importe pas le moteur)."""
    if _LOGIN_PATH["probed"]:
        return _LOGIN_PATH["path"]
    _LOGIN_PATH["probed"] = True
    if os.environ.get("MM_TOOLCHAIN_PROBE", "").strip() == "0":
        return None
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not (os.path.isfile(shell) and os.access(shell, os.X_OK)):
        shell = "/bin/bash"
    script = "printf '\\n__MM_PATH_B__\\n%s\\n__MM_PATH_E__\\n' \"$PATH\""
    try:
        proc = subprocess.run([shell, "-lic", script], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, TERM="dumb"))
        match = re.search(r"__MM_PATH_B__\n(.*?)\n__MM_PATH_E__", proc.stdout or "", re.S)
        _LOGIN_PATH["path"] = (match.group(1).strip() or None) if match else None
    except Exception:
        _LOGIN_PATH["path"] = None
    return _LOGIN_PATH["path"]


def main():
    global SESSION_TOKEN
    refuse_native_windows()   # variante Windows = WSL uniquement, avant tout effet de bord
    enrich_path()
    fallback = resolve_app_dir()
    if sys.stdout is None or not sys.stdout.isatty():
        # Lancement par double-clic (bundle .app, .desktop, .bat sans console) : les
        # messages partiraient dans le néant — on les journalise, et l'extinction
        # passe par le bouton ⏻ de l'UI (/api/quit) au lieu de Ctrl+C.
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            stream = open(os.path.join(APP_DIR, "launcher.log"), "a", buffering=1,
                          encoding="utf-8", errors="replace")
            stream.write(f"\n── {time.strftime('%Y-%m-%d %H:%M:%S')} · lancement sans terminal"
                         " · headless launch ──\n")
            sys.stdout = sys.stderr = stream
        except OSError:
            pass
    if shutil.which("tmux") is None:
        print("❌ tmux est requis (voir INSTALL.md). Installe-le puis relance.")
        sys.exit(1)

    if fallback:
        print(f"⚠️  Impossible d'écrire dans le dossier d'installation : l'app rangera ses fichiers dans {APP_DIR}")
        print("   (la liste des projets, le verrou d'instance et le journal y seront rangés).")

    running = existing_instance_url()
    if running:
        print(f"♻️  Une instance de l'app tourne déjà : {running}")
        webbrowser.open(running)
        return

    adopted = adopt_legacy_registry()
    if adopted:
        print(f"📦 Liste de projets récupérée (copiée) depuis {adopted}")

    healed = heal_engine_binaries()
    if healed:
        print("🔧 Moteur réparé (droits d'exécution / quarantaine macOS) · engine repaired: "
              + ", ".join(healed))

    SESSION_TOKEN = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
    try:
        server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    except OSError as err:
        print(f"❌ Impossible d'écouter sur {HOST}:{PORT} ({err}).")
        print("   Un autre programme occupe ce port ? Relance avec MM_APP_PORT=8749 par exemple.")
        sys.exit(1)
    _SERVER["instance"] = server

    write_json_atomic(LOCK_FILE, {"port": PORT, "token": SESSION_TOKEN, "pid": os.getpid()})
    try:
        os.chmod(LOCK_FILE, 0o600)  # le jeton ne doit être lisible que par l'utilisateur
    except OSError:
        pass

    url = f"http://{HOST}:{PORT}/?t={SESSION_TOKEN}"
    print("🏭 MAIsterMind — cockpit des orchestrateurs · orchestrator cockpit")
    print(f"   Dossier de l'app · App folder : {INSTALL_DIR}")
    engines = discover_engine_dirs()
    for label, home in engines:
        print(f"   Moteur · Engine « {label} » : {os.path.join(home, MANIFEST_NAME)}")
    if not engines:
        print(f"   ⚠️  Aucun moteur ({MANIFEST_NAME} introuvable) · No engine ({MANIFEST_NAME} not found).")
    print(f"   URL (avec jeton · with token) : {url}")
    print("   Les runs vivent dans tmux : fermer cette app ne tue AUCUN run en cours.")
    print("   Runs live in tmux: closing this app doesn't kill anything that's running.")
    print("   Arrêt · Stop : Ctrl + C")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
        # Retour normal = shutdown() déclenché par /api/quit (bouton ⏻ de l'UI).
        print("⏹️  App éteinte depuis l'UI. Sessions tmux 'mm-run-*' laissées intactes "
              "(elles réapparaîtront au prochain lancement).")
    except KeyboardInterrupt:
        print("\n⏹️  App arrêtée. Sessions tmux 'mm-run-*' laissées intactes "
              "(elles réapparaîtront au prochain lancement).")
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
