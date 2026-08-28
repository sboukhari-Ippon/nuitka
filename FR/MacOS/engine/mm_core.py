#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_core — les fonctions PARTAGÉES des orchestrateurs (plan-big-last, Lot 4a)
─────────────────────────────────────────────────────────────────────────────
Module embarqué (JAMAIS un point d'entrée : exclu de la boucle Nuitka de build.yml,
comme mm_runner et mm_audit). Chaque fonction de ce fichier était dupliquée à
l'identique — AST ET CHAÎNES — dans plusieurs orchestrateurs : l'extraction est une
RECOPIE, générée et vérifiée par tools/migrate_mm_core.py, jamais une réécriture.
Un correctif de logique se fait désormais ICI, une fois (× 2 langues), au lieu de
N fichiers × 6 variantes.

Contrat de configuration : les fonctions référencent des constantes et objets de
l'orchestrateur (RUNNER, BLACKBOARD_FILE, _GIT…). Chaque orchestrateur appelle UNE
fois, en fin de module (tous ses noms sont alors définis, rien n'est encore exécuté) :

    mm_core.configure(RUNNER=RUNNER, BLACKBOARD_FILE=BLACKBOARD_FILE, ...)

Un processus = un orchestrateur : cet état module-level ne peut pas entrer en
conflit. Les objets MUTABLES (_GIT, _TEST_COUNT…) sont PARTAGÉS par référence :
les deux côtés voient les mêmes mutations, comme avant l'extraction.
"""

import os
import re
import sys
import json
import time
import signal
import subprocess
import shlex
import shutil
import yaml

from mm_runner import resolve_timeout

import mm_audit

# Constantes canoniques utilisées dans des arguments PAR DÉFAUT (liées au def) —
# mêmes valeurs que dans tous les orchestrateurs, calculées au même moment (import).
MAX_PHASE_TIMEOUT = resolve_timeout("phase", 600)
VERIFY_TIMEOUT = resolve_timeout("verify", 300)
VERIFY_FEEDBACK_LIMIT = 4000
MUTATION_TIMEOUT = 300

# ─── ENVIRONNEMENT D'OUTILLAGE (PATH DU SHELL DE LOGIN) ──────────────────────
# L'agent tourne dans un pane tmux ouvert SANS commande : tmux y démarre un shell de
# login INTERACTIF, qui charge nvm/fnm/volta et le reste des fichiers rc. L'orchestrateur,
# lui, hérite du PATH du processus qui a créé le serveur tmux — l'app, parfois lancée
# sans terminal (PATH du bureau : Node système). Deux PATH, deux Node : un verdict rendu
# sous Node 18 alors que l'agent voyait Node 22 (incident du 23/08/2026, « styleText »).
# On sonde donc UNE fois le PATH du shell de login interactif de l'utilisateur et on le
# place EN TÊTE de celui du processus : le verdict s'exécute avec la même toolchain que
# l'agent. Désactivable (MM_TOOLCHAIN_PROBE=0) ; court-circuité sous harness mock.
TOOLCHAIN_PROBE_ENV = "MM_TOOLCHAIN_PROBE"
TOOLCHAIN_PROBE_TIMEOUT = 10
_TOOLCHAIN = {"probed": False, "login_path": None, "preflight_done": False}
_JS_TOOLCHAIN_RE = re.compile(r"\b(node|npx|npm|pnpm|yarn|bun|vitest|jest|tsc|vite|next|"
                              r"mocha|playwright|cypress|eslint|ng|nx|turbo)\b")
# Signatures d'une INCOMPATIBILITÉ DE RUNTIME dans la sortie d'un verdict (Node trop
# ancien pour l'outil, moteur refusé…) : ce n'est pas un code rouge, c'est l'environnement
# de l'orchestrateur qu'il faut corriger — un agent scaffold n'y changerait rien.
_RUNTIME_MISMATCH_MARKERS = (
    "does not provide an export named",      # API Node absente (util.styleText < 20.12…)
    "EBADENGINE", "Unsupported engine", "engine \"node\" is incompatible",
    "requires Node.js", "requires node version", "Node.js version",
    "ERR_REQUIRE_ESM", "ERR_UNSUPPORTED_ESM_URL_SCHEME", "ERR_UNKNOWN_FILE_EXTENSION",
)
# Priorités des répertoires pour l'ÉCHANTILLON du prompt de cartographie (voir
# select_carto_sample) : le code applicatif d'abord, les assets/migrations/outillage
# en dernier — les N premiers fichiers par ordre alphabétique étaient, sur un monorepo,
# 300 feuilles de style d'icônes et zéro fichier de src/.
_HIGH_PRIORITY_DIR_RE = re.compile(r"(^|/)(src|app|apps|lib|libs|pages|components|modules|"
                                   r"features|domain|core|server|client|api|routes|views|"
                                   r"controllers|services|hooks|store|stores)(/|$)", re.I)
_LOW_PRIORITY_DIR_RE = re.compile(r"(^|/)(public|static|assets?|migrations?|drizzle|prisma|"
                                  r"docs?|scripts?|fixtures?|mocks?|__mocks__|storybook|"
                                  r"stories|i18n|locales?|generated|gen|vendor|fonts?|"
                                  r"icons?|images?|img)(/|$)", re.I)
# Marqueurs d'une TUI figée sur une demande de permission : l'agent attend un clic humain
# qui ne viendra jamais (usine sans surveillance) — inutile d'attendre le timeout.
_PERMISSION_PROMPT_MARKERS = ("Permission required", "Allow once", "Allow always",
                              "Do you trust", "Approve this")
# Caractères d'animation (spinners braille, points de progression) retirés avant de
# comparer deux écrans : une TUI qui « respire » sans travailler n'est pas active.
_SCREEN_NOISE_RE = re.compile(r"[⠀-⣿⬝■-◿░-▓\s]+")


def probe_login_path(timeout: int = TOOLCHAIN_PROBE_TIMEOUT):
    """PATH du shell de login INTERACTIF de l'utilisateur ($SHELL -lic), ou None.

    Exactement ce que tmux fait pour le pane de l'agent (default-command vide → shell de
    login, interactif puisque attaché à un pty) : bash sur Ubuntu/WSL, zsh sur macOS. Les
    marqueurs isolent le PATH des bannières/motd ; stdin fermé et TERM=dumb neutralisent
    les rc bavards. Toute anomalie → None (l'appelant garde son PATH).
    """
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not (os.path.isfile(shell) and os.access(shell, os.X_OK)):
        shell = "/bin/bash"
    script = "printf '\\n__MM_PATH_B__\\n%s\\n__MM_PATH_E__\\n' \"$PATH\""
    try:
        proc = subprocess.run([shell, "-lic", script], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, TERM="dumb"))
    except Exception:
        return None
    match = re.search(r"__MM_PATH_B__\n(.*?)\n__MM_PATH_E__", proc.stdout or "", re.S)
    if not match:
        return None
    return match.group(1).strip() or None


def unify_toolchain_env():
    """Place le PATH du shell de login EN TÊTE de os.environ['PATH'] (une fois par
    processus, mémoïsé). Renvoie le PATH sondé, ou None si la sonde est désactivée,
    court-circuitée (harness mock) ou en échec — le PATH courant est alors conservé."""
    if _TOOLCHAIN["probed"]:
        return _TOOLCHAIN["login_path"]
    _TOOLCHAIN["probed"] = True
    if os.environ.get(TOOLCHAIN_PROBE_ENV, "").strip() == "0" \
            or os.environ.get("MM_AGENT_HARNESS", "").strip().lower() == "mock":
        return None
    login_path = probe_login_path()
    if login_path:
        current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        head = [p for p in login_path.split(os.pathsep) if p]
        os.environ["PATH"] = os.pathsep.join(head + [p for p in current if p not in head])
    _TOOLCHAIN["login_path"] = login_path
    return login_path


def verify_env() -> dict:
    """Environnement des commandes de VERDICT : PATH unifié avec le shell de login, puis
    node_modules/.bin en tête (les outils JS/TS sont souvent installés en local et
    absents du PATH global : sous shell=True, /bin/sh ne les trouve pas — « tsc: not
    found »). Inoffensif hors écosystème Node (le dossier est simplement absent)."""
    unify_toolchain_env()
    env = os.environ.copy()
    local_bin = os.path.abspath(os.path.join("node_modules", ".bin"))
    if os.path.isdir(local_bin):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return env


def node_expected_version() -> str:
    """Version de Node attendue par le projet ('.nvmrc', '.node-version', puis
    package.json engines.node), ou '' si rien n'est déclaré."""
    for name in (".nvmrc", ".node-version"):
        try:
            with open(name, "r", encoding="utf-8") as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            return value
    try:
        with open("package.json", "r", encoding="utf-8") as f:
            engines = (json.load(f) or {}).get("engines") or {}
        return str(engines.get("node") or "").strip()
    except Exception:
        return ""


def node_version_matches(version: str, expected: str) -> bool:
    """La MAJEURE de Node vue satisfait-elle la contrainte attendue ? Sous-ensemble de
    semver suffisant pour .nvmrc / engines.node : '22', 'v22.1.0', '>=20.19', '^20 ||
    >=22.12', '20 - 22'. Une contrainte non comparable ('lts/*', 'node') → True (on ne
    crie jamais au loup sur ce qu'on ne sait pas lire)."""
    match = re.match(r"v?(\d+)", (version or "").strip())
    if not match:
        return True
    major = int(match.group(1))
    comparable, satisfied = False, False
    for clause in (expected or "").split("||"):
        clause_ok, found = True, False
        for op, num in re.findall(r"(>=|<=|>|<|=|\^|~)?\s*v?(\d+)(?:\.\d+)*", clause):
            found = True
            n = int(num)
            if op in (">=", ">"):
                clause_ok = clause_ok and major >= n
            elif op in ("<=",):
                clause_ok = clause_ok and major <= n
            elif op == "<":
                clause_ok = clause_ok and major < n
            else:
                clause_ok = clause_ok and major == n
        if found:
            comparable = True
            satisfied = satisfied or clause_ok
    return satisfied if comparable else True


def _node_seen(env: dict) -> tuple:
    """(chemin du node résolu dans cet environnement ou '', version ou '')."""
    node_path = shutil.which("node", path=env.get("PATH")) or ""
    version = ""
    if node_path:
        try:
            proc = subprocess.run([node_path, "--version"], capture_output=True, text=True,
                                  timeout=10, env=env)
            version = (proc.stdout or "").strip()
        except Exception:
            version = ""
    return node_path, version


def toolchain_preflight(cmd: str, env: dict):
    """Une ligne, UNE fois par run, dès qu'une commande de verdict relève de l'écosystème
    Node : quel Node l'orchestrateur exécute réellement (chemin + version), ce que le
    projet attend, et l'écart s'il y en a. Journalisé dans .mm-runs. Muet pour les autres
    écosystèmes (python, java, go…)."""
    if _TOOLCHAIN["preflight_done"] or not _JS_TOOLCHAIN_RE.search(cmd or ""):
        return
    _TOOLCHAIN["preflight_done"] = True
    node_path, version = _node_seen(env)
    expected = node_expected_version()
    mm_audit.event("toolchain", node_path=node_path, node_version=version, expected=expected,
                   login_path_probed=bool(_TOOLCHAIN["login_path"]))
    if not node_path:
        print("   🧭 Toolchain : aucun 'node' dans le PATH de l'orchestrateur (commande JS/TS détectée).")
        return
    line = f"   🧭 Toolchain : Node {version or '?'} ({node_path})"
    if expected:
        line += f" · attendu par le projet : {expected}"
        if not node_version_matches(version, expected):
            line += " ⚠️  divergence"
    print(line)


def toolchain_failure_hint(output: str) -> str:
    """Indice d'ENVIRONNEMENT si la sortie d'un verdict porte la signature d'une
    incompatibilité de runtime plutôt que d'un code rouge ; '' sinon. Sert à distinguer
    « la chaîne de vérification est cassée » (le code) de « l'orchestrateur exécute la
    commande avec le mauvais Node » (l'environnement) — deux rapports d'échec différents."""
    text = output or ""
    if not any(marker in text for marker in _RUNTIME_MISMATCH_MARKERS):
        return ""
    node_path, version = _node_seen(verify_env())
    expected = node_expected_version()
    return (f"Signature d'une incompatibilité de RUNTIME (pas d'un code rouge) : la commande a "
            f"tourné avec Node {version or '?'} ({node_path or 'absent du PATH'})"
            + (f", le projet attend {expected}" if expected else "")
            + ". Cause probable : PATH de l'orchestrateur différent de celui de ton terminal "
              "(app lancée sans shell de login : nvm/fnm/volta non chargés). Vérifie "
              "'node --version' dans le pane de l'orchestrateur, corrige, puis relance : "
              "aucune phase n'a été consommée.")


ENV_FAIL_ACTION = ("Corrige l'environnement de l'orchestrateur (Node/PATH), puis relance : "
                   "aucune phase n'a été consommée.")


def fail_if_toolchain_environment_broken(verify_cmd: str, output: str, blackboard: dict):
    """Arrêt net + rapport d'échec « environnement » si le pré-contrôle du scaffold a échoué
    sur une incompatibilité de RUNTIME : solliciter un agent scaffold serait inutile (il
    tourne dans un AUTRE environnement — sa suite passe — et le squelette qu'il produirait
    échouerait pareil sous l'orchestrateur). Ne fait rien si la sortie ne porte pas cette
    signature : le flux scaffold habituel continue."""
    env_hint = toolchain_failure_hint(output)
    if not env_hint:
        return
    print(f"""
{'='*60}
❌ Environnement de vérification défaillant AVANT toute production.
   La commande « {verify_cmd} » échoue dans l'environnement de l'ORCHESTRATEUR, et sa
   sortie porte la signature d'une incompatibilité de runtime : un agent scaffold n'y
   changerait rien.

   {env_hint}

   Sortie (tronquée) :
{output}
{'='*60}
""")
    write_fail_report(
        "Environnement de vérification défaillant (orchestrateur)",
        f"La commande « {verify_cmd} » échoue dans l'environnement de l'orchestrateur, avant "
        f"toute production. {env_hint}",
        blackboard, details=output, action=ENV_FAIL_ACTION)
    RUNNER.kill()
    sys.exit(1)


# ─── CARTOGRAPHIE : ÉCHANTILLON, RÉPERTOIRES, LIVRABLES RÉSIDUELS ────────────

def select_carto_sample(files: list, limit: int) -> list:
    """Échantillon de `limit` fichiers REPRÉSENTATIF de toute l'arborescence pour le prompt
    de cartographie (à la place des `limit` premiers par ordre alphabétique) : tourniquet
    par répertoire, répertoires de code d'abord (src/, app/, lib/…), assets/migrations/
    outillage en dernier. Ordre de sortie : celui du périmètre. Déterministe."""
    if len(files) <= limit:
        return list(files)
    by_dir = {}
    for f in files:
        by_dir.setdefault(os.path.dirname(f) or ".", []).append(f)

    def rank(directory: str) -> int:
        if _LOW_PRIORITY_DIR_RE.search(directory):
            return 2
        if _HIGH_PRIORITY_DIR_RE.search(directory):
            return 0
        return 1

    tiers = {0: [], 1: [], 2: []}
    for directory in sorted(by_dir):
        tiers[rank(directory)].append(list(by_dir[directory]))
    chosen = set()
    for tier in (0, 1, 2):
        queues = tiers[tier]
        while queues and len(chosen) < limit:
            for queue in list(queues):
                if len(chosen) >= limit:
                    break
                chosen.add(queue.pop(0))
                if not queue:
                    queues.remove(queue)
    return [f for f in files if f in chosen]


def expand_dir_entry(entry: str, candidates: list, taken) -> list:
    """Fichiers du périmètre couverts par une entrée RÉPERTOIRE de la carte (chemin
    terminé par '/'), récursivement, hors ceux déjà assignés. [] si l'entrée n'est pas
    un répertoire ou ne couvre rien : l'appelant la traite alors comme un chemin inconnu.
    C'est ce qui permet au cartographe d'assigner un monorepo entier sans recopier des
    milliers de chemins (et sans que le surplus tombe mécaniquement en « Divers »)."""
    if not entry.endswith("/"):
        return []
    prefix = entry.lstrip("/")
    return [f for f in candidates if f.startswith(prefix) and f not in taken]


def residual_deliverable_warning(path: str, orchestrator_id: str) -> str:
    """Avertissement si `path` a été écrit APRÈS la dernière trace du dernier run de cet
    orchestrateur resté SANS clôture (pas de run.json) : signature d'un agent orphelin
    qui a fini d'écrire après l'arrêt de l'orchestrateur — un livrable à relire avant de
    le reprendre comme valide. '' sinon. Best-effort : lit .mm-runs, n'échoue jamais."""
    try:
        runs_root = os.path.join(os.getcwd(), mm_audit.RUNS_DIR)
        runs = sorted(d for d in os.listdir(runs_root)
                      if d.split("-", 2)[-1].startswith(orchestrator_id)
                      and os.path.isdir(os.path.join(runs_root, d)))
        if not runs:
            return ""
        last = os.path.join(runs_root, runs[-1])
        if os.path.exists(os.path.join(last, "run.json")):
            return ""
        events = os.path.join(last, "events.jsonl")
        if not os.path.exists(events) or os.path.getmtime(path) <= os.path.getmtime(events):
            return ""
        return (f"'{path}' a été écrit APRÈS la dernière trace du run '{runs[-1]}', resté sans "
                f"clôture : livrable d'un agent orphelin (run tué pendant sa passe) ? Relis-le "
                f"avant de le valider, ou supprime-le pour rejouer la cartographie.")
    except Exception:
        return ""


def agent_screen_fingerprint() -> str:
    """Empreinte de l'écran de l'agent, animations retirées : deux captures identiques à
    quelques secondes d'écart signifient un agent qui n'avance plus (bloqué, ou fini)."""
    try:
        screen = RUNNER.capture() or ""
    except Exception:
        return ""
    return _SCREEN_NOISE_RE.sub("", screen)


def agent_blocked_on_permission() -> bool:
    """La TUI de l'agent affiche-t-elle une demande de permission (dialogue bloquant) ?"""
    try:
        screen = RUNNER.capture() or ""
    except Exception:
        return False
    return any(marker in screen for marker in _PERMISSION_PROMPT_MARKERS)


ACTIVITY_GRACE = 120             # s : prolongation tant que l'écran de l'agent change encore
WAIT_EXTENSION_FACTOR = 3        # plafond dur de l'attente : 3 × le budget nominal
PERMISSION_POLLS_BEFORE_STOP = 3  # dialogue de permission vu N fois de suite → arrêt


def wait_should_continue(start: float, timeout: int, activity: dict) -> bool:
    """Décide, à chaque tour de scrutation d'un livrable, si l'attente continue.

    Budget nominal `timeout` ; au-delà, PROLONGATION tant que l'écran de l'agent change
    encore (il travaille : une cartographie de 1 600 fichiers ne tient pas en 10 min),
    par tranches de ACTIVITY_GRACE s, jusqu'à WAIT_EXTENSION_FACTOR × timeout. Un agent
    figé sur une DEMANDE DE PERMISSION de sa TUI arrête l'attente tout de suite :
    personne ne cliquera (usine sans surveillance), et 3 × 600 s d'attente pour rien
    était le premier poste de lenteur observé sur un audit. `activity` porte l'état
    entre deux appels ({fingerprint, last_change, permission_polls, extended_warned,
    stop}) ; 'stop' ∈ {'permission', 'timeout'} une fois l'attente arrêtée.
    """
    now = time.time()
    fingerprint = agent_screen_fingerprint()
    if "fingerprint" not in activity:
        # Première observation : point de référence, pas une activité (last_change reste
        # absent tant qu'aucun CHANGEMENT d'écran n'a été observé).
        activity["fingerprint"] = fingerprint
    elif fingerprint != activity["fingerprint"]:
        activity["fingerprint"] = fingerprint
        activity["last_change"] = now
    if agent_blocked_on_permission():
        activity["permission_polls"] = activity.get("permission_polls", 0) + 1
        if activity["permission_polls"] >= PERMISSION_POLLS_BEFORE_STOP:
            print("   ⛔ L'agent est figé sur une DEMANDE DE PERMISSION de sa TUI (aucun humain ne "
                  "cliquera) : attente interrompue. Vérifie le bloc 'permission' de l'agent "
                  "d'usine (ex. 'external_directory: allow' pour un accès hors projet), puis "
                  "relance.")
            activity["stop"] = "permission"
            return False
    else:
        activity["permission_polls"] = 0
    elapsed = now - start
    if elapsed < timeout:
        return True
    last_change = activity.get("last_change")
    if elapsed < timeout * WAIT_EXTENSION_FACTOR \
            and last_change is not None and now - last_change < ACTIVITY_GRACE:
        if not activity.get("extended_warned"):
            print(f"   ⏳ Budget nominal ({timeout}s) atteint mais l'agent travaille encore (écran "
                  f"actif) : attente prolongée, au plus {timeout * WAIT_EXTENSION_FACTOR:g}s.")
            activity["extended_warned"] = True
        return True
    activity["stop"] = "timeout"
    return False

# Noms injectés par configure() — placeholders écrasés par l'orchestrateur au chargement :
AGENT_CONFIG_FILE = None
BLACKBOARD_FILE = None
CYCLE_REFACTO_PHASE_ID = None
GITIGNORE_BODY = None
IMPACT_DONE_SENTINEL = None
IMPACT_FILE = None
IMPACT_PHASE_PREFIX = None
IMPL_NATURE = None
MAX_ATTEMPTS = None
MAX_VERIFY_RETRIES_ON_TIMEOUT = None
PIPELINE_SKILLS = None
PLAN_FILE = None
POLL_INTERVAL = None
REFACTO_DONE_SENTINEL = None
REFACTO_FIX_PHASE_ID = None
REFACTO_REPORT_FILE = None
REQUIRED_GLOBAL_RULES = None
RUNNER = None
SCAFFOLD_TIMEOUT = None
SKILLS_DIR = None
SPEC_FILE = None
TMP_ARCHITECT_FILE = None
TMP_CODER_FILE = None
TMP_IMPACT_FILE = None
TMP_PLAN_FILE = None
TMP_PO_FILE = None
TMP_PROMPT_BUFFER = None
TMP_REFACTO_FILE = None
TMP_REPAIR_FILE = None
TMP_TRIAGE_FILE = None
TMP_VERIFIER_FILE = None
TMUX_SESSION = None
UI_EXTENSIONS = None
US_HEADING_RE = None
_GIT = None
_ORCH_BASENAMES = None
_PHASE_STATUS_SEEN = None
_TEST_COUNT = None
cleanup_pipeline_sentinel = None
parse_skill_frontmatter = None
run_git = None
wait_for_pipeline_file = None
write_fail_report = None


def configure(**names):
    """Injecte les constantes et objets de l'orchestrateur (appelée UNE fois par
    orchestrateur, en fin de module). Volontairement brutal : l'extraction est une
    recopie à l'identique, les fonctions lisent les mêmes NOMS qu'avant."""
    globals().update(names)
    # Arrêts EXTERNES (kill de la session tmux par l'app → SIGHUP, kill → SIGTERM) : même
    # nettoyage que Ctrl-C — journal clos ('interrupted'), session d'agent tuée. Sans
    # cela, l'agent survivait à l'orchestrateur et finissait d'écrire son livrable dans
    # un projet que plus personne ne pilotait (carte résiduelle trompeuse au relancement).
    for sig_name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal_handler)
        except (ValueError, OSError):
            pass


def append_arbitration(phase_id, accepted: bool):
    """Consigne la décision humaine dans le rapport d'arbitrage (piste d'audit, best-effort)."""
    try:
        with open(impact_phase_file(phase_id), "a", encoding="utf-8") as f:
            f.write("\n## Décision de l'humain\n")
            f.write("ACCEPTÉ : l'impact est entériné, les tests concernés sont supprimés par l'orchestrateur.\n"
                    if accepted else
                    "REFUSÉ : le comportement historique fait foi, la phase est corrigée en le préservant.\n")
    except OSError:
        pass

def apply_blackboard_defaults(blackboard: dict):
    """Comble les champs non critiques absents pour éviter tout KeyError en production.

    Les manques structurants ont déjà été signalés par validate_blackboard_schema ;
    ici on garantit seulement que les accès directs ultérieurs ne lèvent pas d'exception.
    """
    if not isinstance(blackboard, dict):
        return
    blackboard.setdefault("status", "IN_PROGRESS")
    global_rules = blackboard.setdefault("global_rules", {})
    if isinstance(global_rules, dict):
        for key in REQUIRED_GLOBAL_RULES:
            global_rules.setdefault(key, "(non spécifié)")
    for phase in blackboard.get("phases", []) or []:
        if isinstance(phase, dict):
            phase.setdefault("status", "TODO")
            phase.setdefault("verdict", "PENDING")
            phase.setdefault("critic_feedback", "")
            phase.setdefault("skills_required", [])
            phase.setdefault("tasks", [])
            phase.setdefault("covers", [])
            # Décisions de l'architecte transportées depuis le plan. 'nature' et 'cycle' sont
            # OBLIGATOIRES en mode TDD (validés en fatal AVANT cet appel) : les défauts ici ne
            # sont que des filets anti-KeyError, jamais un mode dégradé.
            phase.setdefault("nature", "")
            phase.setdefault("cycle", "")
            phase.setdefault("context", "")
            phase.setdefault("files_to_read", [])
            phase.setdefault("tests_to_remove", [])
            phase.setdefault("tests_to_update", [])

PLANNED_TEST_FIELDS = ("tests_to_remove", "tests_to_update")


def planned_test_changes(phase: dict) -> tuple:
    """(tests à SUPPRIMER, tests MODIFIABLES) déclarés par l'architecte sur la phase —
    chemins normalisés (relatifs, séparateur '/', sans './'). Champs absents → ([], [])."""
    def norm(values):
        out = []
        for raw in (values if isinstance(values, list) else []):
            p = str(raw).strip().strip("'\"`").replace("\\", "/")
            if p.startswith("./"):
                p = p[2:]
            if p:
                out.append(p)
        return out
    return norm(phase.get("tests_to_remove")), norm(phase.get("tests_to_update"))


def allowed_test_edits(phase: dict, blackboard: dict) -> set:
    """Fichiers de test que les gardes de gel ne doivent PAS restaurer pendant cette phase :
    ceux que le plan déclare modifiables, ceux qu'il déclare obsolètes, et ceux déjà
    supprimés par l'orchestrateur (arbitrage humain ou plan). Tout le reste reste gelé."""
    to_remove, to_update = planned_test_changes(phase)
    return set(to_remove) | set(to_update) | set(blackboard.get("_yolo_deleted_tests") or [])


def planned_test_changes_policy(phase: dict) -> str:
    """Complément de la politique d'édition du prompt codeur quand le plan a déclaré des
    tests à supprimer ou à modifier pour cette phase ; '' sinon (aucun changement de
    consigne : le gel des tests reste la règle)."""
    to_remove, to_update = planned_test_changes(phase)
    if not to_remove and not to_update:
        return ""
    parts = [" EXCEPTION PLANIFIÉE par l'architecte pour cette phase :"]
    if to_remove:
        parts.append(f" les tests obsolètes suivants ont déjà été SUPPRIMÉS par l'orchestrateur "
                     f"(comportement retiré ou remplacé par la spec) — ne les recrée pas et n'en "
                     f"réécris pas l'équivalent : {', '.join(to_remove)}.")
    if to_update:
        parts.append(f" tu PEUX modifier ces fichiers de test existants, et eux seuls, parce que la "
                     f"spec fait évoluer le comportement qu'ils décrivent : {', '.join(to_update)}.")
    return "".join(parts)


def remove_planned_obsolete_tests(phase: dict, blackboard: dict) -> list:
    """Suppression MÉCANIQUE, au début de la phase, des tests que le PLAN déclare obsolètes
    (champ 'tests_to_remove', transporté depuis le plan par le compilateur).

    Le 23/08/2026, un plan déclarait noir sur blanc « exception à la règle de gel : l'US-1
    exige la suppression des tests d'incrémentation » et la garde de gel restaurait le test
    trois fois de suite. C'est l'ORCHESTRATEUR qui supprime, jamais un agent : chaque chemin
    est validé (existe, est un fichier de test, pas un artefact d'orchestration), retiré
    (git rm si suivi), committé aussitôt (les gardes en diff HEAD ne doivent pas prendre cet
    acte pour le travail d'un agent), retiré des protections, mémorisé dans
    _yolo_deleted_tests ; la garde de non-décroissance est re-baselinée."""
    to_remove, _to_update = planned_test_changes(phase)
    if not to_remove:
        return []
    print(f"🗂️  Tests déclarés obsolètes par le plan (phase {phase.get('id')}) : suppression "
          f"par l'orchestrateur...")
    deleted = []
    for p in to_remove:
        if not os.path.isfile(p):
            print(f"   ℹ️  '{p}' : déjà absent, rien à supprimer.")
            continue
        if not is_test_file(p):
            print(f"   ⚠️  '{p}' n'est pas un fichier de test : ignoré (le plan ne peut déclarer "
                  f"obsolète qu'un test, jamais du code de production).")
            continue
        tracked = False
        if _GIT["enabled"]:
            ok_tracked, tracked_out = run_git(["ls-files", "--", p])
            tracked = ok_tracked and bool(tracked_out.strip())
        if tracked:
            run_git(["rm", "-f", "--", p])
        else:
            try:
                os.remove(p)
            except OSError:
                continue
        deleted.append(p)
        print(f"   🗑️  Test obsolète supprimé par l'orchestrateur (plan) : {p}")
    if not deleted:
        return deleted
    if "last_test_count" in blackboard:
        blackboard.pop("last_test_count", None)
        print("   ℹ️  Garde de non-décroissance ré-initialisée (re-baseline au prochain vert).")
    protected = set(blackboard.get("protected_test_files") or [])
    if protected & set(deleted):
        blackboard["protected_test_files"] = sorted(protected - set(deleted))
    already = set(blackboard.get("_yolo_deleted_tests") or [])
    blackboard["_yolo_deleted_tests"] = sorted(already | set(deleted))
    save_blackboard(blackboard)
    mm_audit.event("guard", name="tests_obsoletes_plan", action="suppression", files=len(deleted))
    commit_phase(f"phase {phase.get('id')}: tests obsolètes retirés (déclarés par le plan)")
    return deleted


def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    verify_cmd = resolve_verify_cmd(phase, blackboard)

    # 'nature' est la décision de l'Architecte, recopiée par le compilateur : elle pilote
    # la ligne de politique de tests. Absente ou inconnue → formulation neutre (les anciens
    # blackboards restent valides).
    nature = str(phase.get("nature") or "").strip().lower()
    if nature == "feature":
        nature_line = ("La nature de cette phase est 'feature' : tu ne crées ni ne modifies "
                       "AUCUN test — une autre phase du plan est dédiée aux tests.")
    elif nature == "tests":
        nature_line = ("La nature de cette phase est 'tests' : ta mission est précisément "
                       "d'écrire les tests demandés par cette checklist.")
    else:
        nature_line = ("Le plan pilote la nature de ton travail : n'écris ou ne modifie des "
                       "tests QUE si une tâche de cette phase le demande explicitement.")

    # Politique d'édition du code de production, pilotée par la nature (garde tests-only §6.6) :
    # en phase 'tests' la prod est GELÉE (anti-triche + socle de la brique B) ; ailleurs, le
    # codeur peut corriger un bug de prod révélé par la suite. L'orchestrateur fait respecter
    # mécaniquement cette politique (restauration git en phase tests).
    if nature == "tests":
        prod_edit_policy = ("En phase 'tests', tu ne modifies QUE des fichiers de test : le code de "
                            "production est GELÉ. Si un test que tu écris révèle un vrai bug du code "
                            "de production, NE le corrige PAS — laisse la vérification échouer, un "
                            "humain tranchera (l'orchestrateur restaure d'office tout fichier de "
                            "production que tu modifierais).")
    else:
        prod_edit_policy = ("Tu PEUX modifier le code de production existant si c'est nécessaire pour "
                            "faire passer la vérification (la suite peut révéler un bug d'une feature "
                            "antérieure à corriger).") + planned_test_changes_policy(phase)

    # Contexte de l'architecte et liste de lecture, transportés depuis le plan : un GUIDAGE
    # qui épargne au codeur une ré-exploration libre du projet. Rien ne sandboxe ses
    # lectures, donc le gain de fenêtre de contexte est probabiliste, pas garanti.
    context_block = ""
    if str(phase.get("context") or "").strip():
        context_block = f"""--- TA PLACE DANS LE PLAN (contexte de l'architecte) ---
{str(phase.get("context")).strip()}

"""
    files_to_read = [str(p).strip() for p in (phase.get("files_to_read") or []) if str(p).strip()]
    files_block = ""
    if files_to_read:
        files_block = ("--- FICHIERS À LIRE EN PREMIER ---\n"
                       "Lis ces fichiers AVANT de coder (l'architecte les a sélectionnés pour "
                       "cette phase) ; n'explore pas le reste du projet sauf nécessité stricte :\n"
                       + "\n".join(files_to_read) + "\n\n")

    full_context = f"""--- SYSTEM RULES ---
Architecture: {blackboard['global_rules']['target']}
Design & CSS: {blackboard['global_rules']['styling']}
Interdictions: {blackboard['global_rules']['constraints']}
Accessibilité: {blackboard['global_rules']['accessibility']}

{skills_context}
--- CONTRAT COMPORTEMENTAL ---
Tu es un Agent Codeur ultra-spécialisé pour la Phase {phase['id']} UNIQUEMENT.
Tu réalises QUE les tâches de CETTE phase et tu t'arrêtes dès qu'elles sont faites.
Ne fais PAS le travail prévu pour d'autres phases : une autre phase du plan peut être
dédiée aux tests ou à une autre fonctionnalité. Principe YAGNI : rien qui ne soit pas
explicitement demandé par la checklist de cette phase.

--- VÉRIFICATION AUTOMATIQUE DE CETTE PHASE ---
{nature_line}
{prod_edit_policy}
Tu ne SUPPRIMES ni n'AFFAIBLIS JAMAIS un test existant pour faire passer la vérification :
si un test existant devient rouge, c'est le code qu'il faut corriger.
Si tu écris des tests, ils doivent être EXÉCUTABLES et RAPIDES : INTERDICTION de
Testcontainers, de Docker et de tout I/O réseau ou base de données.
Avant d'écrire des tests, LIS d'abord les fichiers source que tu testes pour connaître
leurs signatures réelles, et teste le COMPORTEMENT attendu (jamais une assertion toujours vraie).
L'orchestrateur lance automatiquement la commande de vérification de cette phase
« {verify_cmd} » (verdict universel : compilation + suite complète) : elle DOIT réussir
(code de sortie 0), sinon la phase est rejetée. C'est ton UNIQUE critère de réussite.

{context_block}{files_block}--- BESOIN (extrait de la spec couvert par cette phase) ---
{user_need}

--- OBJECTIF PHASE {phase['id']} : {phase['name']} ---
Checklist :
{chr(10).join([f'- [ ] {t}' for t in phase['tasks']])}

--- RETOUR DU VÉRIFICATEUR À CORRIGER (le cas échéant) ---
{critic_feedback}

--- INSTRUCTION DE FIN DE PHASE OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE} : c'est l'orchestrateur qui le gère.
Quand toutes les tâches de la phase sont RÉELLEMENT implémentées dans le code, et en toute
dernière action, crée le fichier sentinelle '{done_sentinel(phase['id'], attempt)}' à la racine du projet.
Il doit contenir la liste des fichiers que tu as créés ou modifiés (un chemin par ligne), et rien d'autre.
Ce fichier est le signal qui déclenche la vérification : ne le crée que lorsque tu as VRAIMENT terminé.
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier de consignes '{TMP_CODER_FILE}' à la racine du projet. Suis scrupuleusement ses instructions pour réaliser la Phase {phase['id']}."

def build_correction_prompt(phase: dict, blackboard: dict, failure_output: str,
                            phase_cmd: str, attempt: int) -> str:
    """Consignes du Correcteur, après REFUS humain d'un impact imprévu : l'ancien
    comportement fait foi, la phase s'y adapte, et l'arbitrage est consigné (décision 5)."""
    full_context = f"""--- RÈGLES SYSTÈME ---
Stack : {blackboard['global_rules']['target']}
Interdictions : {blackboard['global_rules']['constraints']}

--- CONTEXTE ---
L'humain a REFUSÉ l'impact décrit dans '{impact_phase_file(phase['id'])}' : le comportement HISTORIQUE (celui des tests qui échouent) fait foi et doit être préservé.

--- SORTIE DE LA VÉRIFICATION QUI ÉCHOUE ---
{truncate_output(failure_output)}

--- TA MISSION ---
1. Corrige le code de PRODUCTION pour que la commande « {phase_cmd} » réussisse (code de sortie 0) : les tests existants sont préservés et doivent repasser. Tu ne modifies, ne supprimes et ne désactives AUCUN fichier de test (gelés, vérifié par diff git).
2. Préserve autant que possible le travail de la phase {phase['id']} « {phase['name']} » tant qu'il ne contredit pas le comportement historique ; retire ou ajuste ce qui le contredit.
3. Consigne ton arbitrage : ajoute à la FIN de '{impact_phase_file(phase['id'])}' une section '## Correction appliquée' expliquant ce qui posait problème et ce que tu as fait pour corriger le tir.
En toute DERNIÈRE action, crée le fichier sentinelle '{correction_sentinel(phase['id'], attempt)}' à la racine (contenu : le seul mot done).
"""
    with open(TMP_REPAIR_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier de consignes '{TMP_REPAIR_FILE}' à la racine du projet et exécute-le scrupuleusement."

def build_mutation_targets(phase: dict) -> list:
    """Fichiers à muter pour une phase 'tests' = ses 'files_to_read' filtrés sur l'existant.

    'files_to_read' (Input requis) liste les sources de PRODUCTION que le testeur lit, donc ce
    qu'il est censé tester : c'est le ciblage naturel (aucun nouveau champ). On écarte les chemins
    introuvables (un fichier listé mais jamais créé ne se mute pas) et les fichiers de test
    eux-mêmes (on mute la production, pas les tests).
    """
    out = []
    for p in (phase.get("files_to_read") or []):
        clean = str(p).strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if clean and os.path.exists(clean) and not is_test_file(clean):
            out.append(clean)
    return out

def build_phase_verifier_prompt(phase: dict, blackboard: dict, user_need: str,
                                touched_files: list, attempt: int) -> str:
    """Consignes du Vérificateur LLM de phase (décision 2 du plan Yolo).

    La suite verte prouve « rien n'est cassé », pas « la phase a fait tout son travail » :
    un agent indépendant au contexte neuf confronte le code réellement produit à CHAQUE
    tâche de la phase du blackboard. Il ne peut que REJETER (renvoyer au codeur, tentative
    consommée) : le tampon DONE reste l'acte de l'orchestrateur, après CE verdict ET le
    verdict mécanique."""
    files_block = "\n".join(f"- {p}" for p in touched_files) if touched_files \
        else "(aucun fichier déclaré — explore le projet avec tes outils pour retrouver le travail du codeur)"

    full_context = f"""Tu es un Agent Vérificateur QA Senior, strict et indépendant. La suite de tests est DÉJÀ verte : ta mission n'est PAS de rejouer les tests, mais de vérifier que la Phase '{phase['name']}' a RÉELLEMENT livré tout ce que sa checklist demande.

--- RÈGLES GLOBALES À FAIRE RESPECTER ---
Architecture: {blackboard['global_rules']['target']}
Interdictions: {blackboard['global_rules']['constraints']}

--- BESOIN COUVERT PAR LA PHASE ---
{user_need}

--- CHECKLIST DE LA PHASE À VÉRIFIER ({BLACKBOARD_FILE}) ---
{chr(10).join([f'- {t}' for t in phase['tasks']])}

--- FICHIERS MODIFIÉS PAR LE CODEUR ---
{files_block}

--- MÉTHODE DE VÉRIFICATION OBLIGATOIRE ---
1. Ouvre et LIS réellement le contenu de chaque fichier ci-dessus avec tes outils de lecture. Ne te fie à aucun résumé.
2. Confronte le code réel à CHAQUE tâche de la checklist ET à CHAQUE règle globale.
3. Ne valide que ce que tu as effectivement constaté dans le code. Tu ne modifies AUCUN fichier du projet.

--- VERDICT ---
Écris ta conclusion dans le fichier sentinelle '{verdict_sentinel(phase['id'], attempt)}' à la racine du projet :
  - Si toutes les tâches sont réellement implémentées et conformes : la PREMIÈRE ligne contient EXACTEMENT le mot "OK" (rien d'autre).
  - Sinon : la PREMIÈRE ligne contient EXACTEMENT le mot "REJECTED", puis les lignes suivantes listent précisément les tâches manquantes ou non conformes.
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE} : l'orchestrateur le met à jour à partir de ton verdict.
"""
    with open(TMP_VERIFIER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier d'audit '{TMP_VERIFIER_FILE}' à la racine du projet. Suis ses instructions pour vérifier la Phase {phase['id']}."

def build_refacto_fix_prompt(blackboard: dict, user_need: str, failure_output: str,
                             verify_cmd: str, attempt: int) -> str:
    """Consignes de CORRECTION d'une régression révélée par la suite globale après le refacto.

    Même canal que le codeur (prompt déporté en fichier + sentinelle). L'agent corrige le code
    de PRODUCTION fautif, sans défaire le refacto ni affaiblir/supprimer des tests.
    """
    full_context = f"""--- RÈGLES SYSTÈME ---
Stack : {blackboard['global_rules']['target']}
Interdictions : {blackboard['global_rules']['constraints']}

--- CONTEXTE ---
Le projet a été produit phase par phase puis poli (refactoring final). En relançant la SUITE
COMPLÈTE de vérification, une RÉGRESSION est apparue : le refactoring a probablement cassé une
fonctionnalité validée plus tôt.

--- BESOIN INITIAL (référence) ---
{user_need}

--- SORTIE DE LA VÉRIFICATION QUI ÉCHOUE ---
{failure_output}

--- TA MISSION ---
Corrige UNIQUEMENT la régression ci-dessus pour que la commande « {verify_cmd} » réussisse
(code de sortie 0). NE défais PAS les améliorations du refactoring sans nécessité ; NE supprime
PAS et N'AFFAIBLIS PAS de tests pour faire passer la suite : corrige le code de production.
C'est ton UNIQUE critère de réussite.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}. En toute dernière action, crée le fichier
sentinelle '{done_sentinel(REFACTO_FIX_PHASE_ID, attempt)}' à la racine, contenant la liste des
fichiers modifiés (un chemin par ligne).
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)
    return f"Lis le fichier de consignes '{TMP_CODER_FILE}' à la racine et corrige la régression décrite."

def build_repair_prompt(phase: dict, blackboard: dict, failure_output: str,
                        phase_cmd: str, attempt: int) -> str:
    """Consignes du Réparateur : résorber un effet de bord IMPRÉVU sans toucher aux tests
    (gelés, vérifié par diff git) ni sacrifier le comportement de la phase — le miroir du
    mode 'régression' de Guided-Fix, déplacé dans le run."""
    full_context = f"""--- RÈGLES SYSTÈME ---
Stack : {blackboard['global_rules']['target']}
Interdictions : {blackboard['global_rules']['constraints']}

--- CONTEXTE ---
Pendant la phase {phase['id']} « {phase['name']} », la suite de vérification a révélé des tests EXISTANTS qui échouent, et cette cassure N'EST PAS couverte par la revue d'impact validée ('{IMPACT_FILE}') : c'est un EFFET DE BORD imprévu du travail de la phase.

--- SORTIE DE LA VÉRIFICATION QUI ÉCHOUE ---
{truncate_output(failure_output)}

--- TA MISSION ---
Fais coexister les deux : la commande « {phase_cmd} » doit réussir (code de sortie 0) SANS sacrifier le comportement que la phase vient d'implémenter (checklist : {'; '.join(phase['tasks'])}).
RÈGLES ABSOLUES :
1. Tu ne modifies, ne supprimes et ne désactives AUCUN fichier de test : ils sont GELÉS (toute modification sera détectée par diff git et annulée). Corrige le code de PRODUCTION.
2. Tu ne défais pas le travail de la phase : le comportement demandé par sa checklist doit rester implémenté.
3. CAS D'EXCEPTION — vraie incohérence : si tu constates que l'ancien comportement testé et le nouveau comportement demandé sont LOGIQUEMENT INCOMPATIBLES (ce n'est pas un bug de code : les deux ne peuvent pas coexister), n'écris AUCUN correctif bancal. Crée le fichier '{impact_phase_file(phase['id'])}' à la racine décrivant : l'ancien comportement (et ses tests, chemins réels), le nouveau comportement exigé par la phase, et pourquoi ils s'excluent. Puis écris dans la sentinelle ci-dessous le mot CONFLICT sur la première ligne, suivi d'une ligne 'TEST: <chemin>' par fichier de test concerné.
En toute DERNIÈRE action, crée le fichier sentinelle '{repair_sentinel(phase['id'], attempt)}' à la racine : le seul mot DONE si tu as réparé, ou le bloc CONFLICT décrit ci-dessus.
"""
    with open(TMP_REPAIR_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier de consignes '{TMP_REPAIR_FILE}' à la racine du projet et exécute-le scrupuleusement."

def build_skills_dictionary() -> str:
    """Construit dynamiquement le catalogue des skills affectables aux phases.

    Scanne ./.agents/skills, lit le frontmatter (name + description) de chaque
    SKILL.md et exclut les skills système du pipeline. Le résultat est injecté
    dans les consignes de plan de l'ARCHITECTE (étape 2) : l'architecte déclare
    le Skill de chaque phase, et le compilateur blackboard ne fait ensuite que
    RECOPIER cette décision. L'outil s'adapte aux skills présents, quels qu'ils
    soient, sans catalogue codé en dur.
    """
    lines = []
    if not os.path.isdir(SKILLS_DIR):
        return ""
    for entry in sorted(os.listdir(SKILLS_DIR)):
        if entry in PIPELINE_SKILLS:
            continue
        skill_path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.exists(skill_path):
            continue
        name, description = parse_skill_frontmatter(skill_path)
        keyword = name or entry
        desc = description or "(aucune description fournie)"
        lines.append(f'"{keyword}" : {desc}')
    return "\n".join(lines)

def build_triage_prompt(phase: dict, failure_output: str, attempt: int) -> str:
    """Consignes de l'Agent de Triage : chaque fichier de test en échec est-il couvert par
    la revue d'impact VALIDÉE ? Lecture seule, verdict ligne à ligne, doute = IMPRÉVU
    (le réparateur est le chemin sûr ; une suppression à tort ne se rattrape pas)."""
    impact_content = read_impact_review() or "(revue d'impact absente : considère toutes les cassures comme IMPRÉVUES)"

    full_context = f"""Tu es un Agent de Triage, mécanique et prudent. Pendant la phase {phase['id']} « {phase['name']} », la suite de vérification échoue. Ta mission : déterminer, pour CHAQUE fichier de test en échec, si sa cassure était PRÉVUE par la revue d'impact validée par l'humain, ou IMPRÉVUE.

--- REVUE D'IMPACT VALIDÉE PAR L'HUMAIN ({IMPACT_FILE}) ---
{impact_content}

--- SORTIE DE LA VÉRIFICATION QUI ÉCHOUE ---
{truncate_output(failure_output)}

--- MÉTHODE OBLIGATOIRE ---
1. Identifie les fichiers de test en échec (chemins RÉELS depuis la racine : vérifie leur existence avec tes outils de lecture).
2. Un fichier n'est PREVU que si TOUS ses tests en échec correspondent à un impact de la section « Comportements existants qui vont casser » ci-dessus. Un fichier mêlant échecs prévus et échecs imprévus est IMPREVU. Dans le doute : IMPREVU (un agent réparateur prendra le relais, c'est le chemin sûr — une suppression à tort est irréversible).
3. Tu ne modifies AUCUN fichier du projet : ton seul livrable est le verdict ci-dessous.

--- VERDICT ---
Écris ta conclusion dans le fichier sentinelle '{triage_sentinel(phase['id'], attempt)}' à la racine : UNE ligne par fichier de test en échec, au format EXACT :
PREVU: <chemin du fichier de test>
ou
IMPREVU: <chemin du fichier de test ou résumé de l'échec>
Aucune autre ligne.
"""
    with open(TMP_TRIAGE_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier de consignes '{TMP_TRIAGE_FILE}' à la racine du projet et exécute-le scrupuleusement."

def cleanup_all_sentinels():
    """Nettoyage final de toutes les sentinelles résiduelles (phases ET pipeline)."""
    for name in os.listdir("."):
        if (name.startswith(".phase_") or name.startswith(".pipeline_")) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass

def cleanup_sentinels(phase_id: int):
    """Supprime toutes les sentinelles (toutes tentatives) d'une phase."""
    prefix = f".phase_{phase_id}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass

def collect_spec_us_ids(spec_text: str) -> set:
    """Identifiants des user stories (US-n) déclarées dans la spec."""
    ids = set()
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            ids.add(match.group(1).upper())
    return ids

def commit_phase(label: str) -> bool:
    """Committe tout l'arbre de travail (best-effort ; échec → avertissement et poursuite).

    --allow-empty : une phase verte qui n'a rien changé reçoit quand même son commit
    jalon, pour que les shas par phase restent fiables pour les diffs et le rollback.
    """
    if not _GIT["enabled"]:
        return False
    ok_add, _ = run_git(["add", "-A"])
    ok_commit = False
    if ok_add:
        ok_commit, _ = run_git(["commit", "-q", "--allow-empty", "-m", label])
    if not ok_commit:
        print(f"⚠️  Échec du commit git pour « {label} » (poursuite sans ce jalon).")
    return ok_commit

def correction_sentinel(phase_id: int, attempt: int) -> str:
    """Fin de passe du Correcteur (après refus humain d'un impact imprévu)."""
    return f".phase_{phase_id}.attempt{attempt}.correction.done"

def delete_planned_tests(paths: list, blackboard: dict, phase: dict, reason: str) -> list:
    """Suppression MÉCANIQUE de fichiers de test dont la cassure est entérinée (décision 1).

    C'est l'ORCHESTRATEUR qui supprime, jamais un agent : chaque chemin est validé (existe,
    est un fichier de test, pas un artefact d'orchestration), supprimé (git rm si suivi),
    journalisé dans impact.md, retiré de protected_test_files et mémorisé dans
    _yolo_deleted_tests (les gardes de gel ne doivent pas le restaurer). La référence
    last_test_count est ré-initialisée : le prochain vert re-baseline la garde de
    non-décroissance (elle comparerait sinon à un compte incluant les tests supprimés).
    La suppression est COMMITTÉE aussitôt : les gardes en diff HEAD ne doivent pas prendre
    cet acte d'orchestrateur pour le travail d'un agent.
    """
    deleted = []
    for raw in paths:
        p = str(raw).strip().strip("'\"`").replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        if not p or not os.path.isfile(p):
            print(f"   ⚠️  Suppression : chemin introuvable, ignoré : '{raw}'")
            continue
        if not is_test_file(p) or is_orchestration_file(p):
            print(f"   ⚠️  Suppression : '{p}' n'est pas un fichier de test supprimable, "
                  f"ignoré (le réparateur prendra le relais).")
            continue
        tracked = False
        if _GIT["enabled"]:
            ok_tracked, tracked_out = run_git(["ls-files", "--", p])
            tracked = ok_tracked and bool(tracked_out.strip())
        if tracked:
            run_git(["rm", "-f", "--", p])
        else:
            try:
                os.remove(p)
            except OSError:
                continue
        deleted.append(p)
        print(f"   🗑️  Test supprimé par l'orchestrateur (cassure entérinée) : {p}")
    if not deleted:
        return deleted

    # Comptabilité : re-baseline de la garde de non-décroissance + retrait des protections
    # + mémoire des suppressions (exclusion des gardes de gel).
    if "last_test_count" in blackboard:
        blackboard.pop("last_test_count", None)
        print("   ℹ️  Garde de non-décroissance ré-initialisée (re-baseline au prochain vert).")
    protected = set(blackboard.get("protected_test_files") or [])
    if protected & set(deleted):
        blackboard["protected_test_files"] = sorted(protected - set(deleted))
    already = set(blackboard.get("_yolo_deleted_tests") or [])
    blackboard["_yolo_deleted_tests"] = sorted(already | set(deleted))
    save_blackboard(blackboard)

    # Journal d'audit dans impact.md (section « Journal des suppressions », en fin de fichier).
    try:
        with open(IMPACT_FILE, "a", encoding="utf-8") as f:
            for p in deleted:
                f.write(f"- Phase {phase['id']} : `{p}` supprimé — {reason}\n")
    except OSError:
        pass
    commit_phase(f"phase {phase['id']}: suppression de tests entérinée ({reason})")
    return deleted

def done_sentinel(phase_id: int, attempt: int) -> str:
    """Fichier écrit par le Codeur en toute fin de phase (signal 'j'ai terminé').

    Le numéro de tentative est inclus dans le nom : une sentinelle écrite
    tardivement par l'agent d'une tentative précédente ne peut pas être prise
    pour le signal de la tentative courante (pas de faux positif de fin de phase).
    """
    return f".phase_{phase_id}.attempt{attempt}.done"

def ensure_executable_scaffold(blackboard: dict, user_need: str):
    """Garantit un projet exécutable AVANT la production (prérequis dur de la brique A).

    Si la commande de vérification globale ne passe pas (toolchain/scaffold absents), un
    agent dédié crée le squelette minimal (build file + arborescence + un test santé
    trivial), puis on re-teste. Échec précoce et lisible plutôt que N phases rouges sans
    rapport avec leur logique.

    Idempotent : si la vérification passe déjà (reprise après crash, ou projet pré-amorcé),
    l'étape est sautée sans solliciter d'agent.
    """
    verify_cmd = (blackboard.get("verify_cmd") or "").strip()
    if not verify_cmd:
        print("⚠️  Aucune commande de vérification globale : étape de scaffold sautée "
              "(la vérification par exécution sera inopérante).")
        return

    print(f"\n{'='*50}\n🏗️  ÉTAPE 0 : SCAFFOLD EXÉCUTABLE\n{'='*50}")
    print("   ℹ️  Cette étape sert aussi de SMOKE TEST du modèle : c'est sa sollicitation la plus")
    print("      simple (créer 2-3 fichiers + une sentinelle). Si elle n'aboutit pas, suspecte en")
    print("      priorité les appels d'outils (tool calling) du modèle configuré.")
    print("   Contrôle préalable de la chaîne de vérification...")
    ok, output, _ = run_verify(verify_cmd)
    if ok:
        print("✓ La chaîne de vérification passe déjà : squelette présent, étape sautée.")
        record_test_count(output, blackboard)
        return
    fail_if_toolchain_environment_broken(verify_cmd, output, blackboard)

    print("   Chaîne non opérationnelle : génération du squelette par un agent dédié...")
    scaffold_done = done_sentinel(0, 1)
    cleanup_sentinels(0)

    scaffold_context = f"""Tu es un Ingénieur de plateforme. Crée UNIQUEMENT le squelette exécutable
minimal du projet, sans implémenter aucune fonctionnalité du besoin.

--- STACK CIBLE ---
{blackboard['global_rules']['target']}

--- BESOIN INITIAL (contexte uniquement, NE PAS implémenter) ---
{user_need}

--- OBJECTIF ---
1. Crée le fichier de build / gestion de dépendances adapté à la stack (ex. pom.xml,
   package.json, pyproject.toml) et l'arborescence source/test standard, vide.
2. Ajoute UN SEUL test santé trivial qui compile et passe à vide (ex. une assertion vraie).
3. Aucune logique métier, aucune fonctionnalité du besoin : strictement le squelette.
4. Une fois ton travail terminé, la commande suivante DOIT réussir : « {verify_cmd} ».

--- FIN DE TÂCHE OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}. En toute dernière action, crée le fichier
sentinelle '{scaffold_done}' à la racine, contenant la liste des fichiers créés (un par ligne).
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(scaffold_context)

    RUNNER.new_context()
    mm_audit.event("agent_task", prompt_bytes=len(f"Lis le fichier de consignes '{TMP_CODER_FILE}' à la racine et crée le squelette exécutable du projet."))
    RUNNER.send_task(f"Lis le fichier de consignes '{TMP_CODER_FILE}' à la racine et crée le squelette exécutable du projet.")

    if not wait_for_file_creation(scaffold_done, timeout=SCAFFOLD_TIMEOUT):
        print(f"""
{'='*60}
❌ L'agent n'a pas signalé la fin du scaffold (sentinelle absente après {SCAFFOLD_TIMEOUT}s).

   Le scaffold est la PREMIÈRE sollicitation du modèle et la plus simple. S'il échoue
   ici, suspecte EN PRIORITÉ un problème d'APPELS D'OUTILS (tool calling) du modèle
   configuré, avant tout problème de code : certains modèles (petits modèles locaux
   surtout) affichent l'appel d'outil en texte au lieu de l'exécuter, ou ne créent
   jamais les fichiers demandés.

   Diagnostic : attache-toi à la session ('tmux attach -t {TMUX_SESSION}') et regarde
   si le modèle écrit du texte au lieu d'utiliser ses outils d'édition. Si c'est le
   cas, bascule sur un modèle fiable en tool calling (/model dans le TUI ou
   '{AGENT_CONFIG_FILE}'), puis relance.
{'='*60}
""")
        # Diagnostic tool calling sans s'attacher : le dernier écran de la TUI montre
        # généralement si le modèle a affiché ses appels d'outils en texte au lieu de
        # les exécuter.
        tail = RUNNER.capture()[-1500:]
        if tail.strip():
            print(f"   Dernier écran de la TUI (diagnostic) :\n{tail}")
        write_fail_report(
            "Scaffold non abouti",
            f"L'agent n'a pas signalé la fin du scaffold (sentinelle absente après {SCAFFOLD_TIMEOUT}s). "
            f"Suspecte en priorité le tool calling du modèle configuré.",
            blackboard, details=tail)
        cleanup_sentinels(0)
        RUNNER.kill()
        sys.exit(1)

    RUNNER.new_context()
    ok, output, _ = run_verify(verify_cmd)
    cleanup_sentinels(0)
    if not ok:
        print(f"""
{'='*60}
❌ La chaîne d'exécution est cassée AVANT même la production.
   La commande « {verify_cmd} » échoue sur le squelette généré.

   Sortie (tronquée) :
{output}

💡 Corrige le squelette ou la commande de vérification dans '{BLACKBOARD_FILE}',
   puis relance. (On préfère cet arrêt net à N phases rouges sans rapport.)
{'='*60}
""")
        write_fail_report(
            "Chaîne de vérification cassée sur le scaffold",
            f"La commande « {verify_cmd} » échoue sur le squelette généré, avant toute production.",
            blackboard, details=output)
        RUNNER.kill()
        sys.exit(1)
    print("✓ Scaffold exécutable validé : la chaîne de vérification passe à vide.\n")
    record_test_count(output, blackboard)
    commit_phase("scaffold: executable skeleton")

def ensure_orchestration_ignored():
    """Sur un dépôt git HUMAIN préexistant, garantit que les artefacts ÉPHÉMÈRES de
    l'orchestrateur sont listés dans le .gitignore (append-only, idempotent, best-effort).

    Sans ça, MAIsterMind les réécrit à chaque phase et, s'ils finissent suivis par git, ses
    gardes basées sur 'git diff' les prendraient pour du code modifié. is_orchestration_file
    protège déjà les gardes en mémoire ; ceci évite en plus de salir le dépôt et les diffs.
    NE touche jamais aux fichiers déjà suivis (pas de 'git rm', décision laissée à l'humain)
    ni aux livrables d'audit (blackboard/spec/plan, volontairement committés).
    """
    if not os.path.exists(".gitignore"):
        return
    wanted = [ln for ln in GITIGNORE_BODY.splitlines() if ln.strip() and not ln.startswith("#")]
    try:
        with open(".gitignore", "r", encoding="utf-8") as f:
            present = {ln.strip() for ln in f.read().splitlines()}
    except OSError:
        return
    missing = [p for p in wanted if p not in present]
    if not missing:
        return
    try:
        with open(".gitignore", "a", encoding="utf-8") as f:
            f.write("\n# Artefacts d'orchestration MAIster-Mind (ajoutés automatiquement)\n")
            f.write("\n".join(missing) + "\n")
        print(f"✓ {len(missing)} motif(s) d'orchestration ajouté(s) au .gitignore existant.")
    except OSError:
        pass

def ensure_phase_repo():
    """Filet de sécurité git par phase, posé AVANT le scaffold (best-effort).

    Si le projet est déjà un dépôt git (géré par un humain), il est réutilisé TEL QUEL.
    Sinon 'git init' + un .gitignore minimal (fichiers d'orchestration éphémères
    uniquement) + un commit de référence. Sans git : avertir une fois et tourner en
    mode dégradé sans garde-fous.
    """
    if shutil.which("git") is None:
        print("⚠️  git introuvable : commits par phase, protection des fichiers de test et "
              "rollback du refacto sont désactivés pour ce run.")
        return
    if os.path.isdir(".git"):
        _GIT["enabled"] = True
        ensure_orchestration_ignored()
        print("✓ Dépôt git existant réutilisé (commits par phase activés).")
        return
    ok, _ = run_git(["init", "-q"])
    if not ok:
        print("⚠️  Échec de 'git init' : commits par phase, protection des fichiers de test et "
              "rollback du refacto sont désactivés pour ce run.")
        return
    if not os.path.exists(".gitignore"):
        with open(".gitignore", "w", encoding="utf-8") as f:
            f.write(GITIGNORE_BODY)
    _GIT["enabled"] = True
    commit_phase("baseline: factory start")

def execute_cycle_refactoring(blackboard: dict, phase: dict, verify_cmd: str, cycle,
                              phase_start_sha: str):
    """REFACTOR — 3e temps du cycle de Beck (red → green → refactor), joué après CHAQUE
    green validé.

    Le green vient d'être committé : la suite complète est VERTE et ce commit est le
    point de rollback. Un agent au contexte neuf polit le code de PRODUCTION posé par le
    cycle (duplication, noms, structure) SANS changer le comportement ; les tests restent
    GELÉS (même garde diff git qu'en green). Le verdict est mécanique : la suite doit
    RESTER verte (verify_cmd, code de sortie 0) sans perdre de tests. Tout écart — tests
    touchés, suite rouge, compte en baisse, timeout — déclenche le ROLLBACK au commit
    green : contrairement au polish final (étape 5), AUCUNE boucle de correction ici. Le
    refactor de cycle est OPPORTUNISTE : il ne bloque jamais le run, ne consomme aucune
    tentative, et un rollback laisse exactement l'état toutes-phases-vertes. Le rollback
    n'est pas qu'une commodité : le verdict inversé du PROCHAIN red suppose une suite
    verte au départ — un état de refactor non prouvé vert casserait son attribution.
    Sans git, pas de rollback possible : étape sautée (mode dégradé assumé, comme les
    autres gardes).
    """
    if not _GIT["enabled"]:
        print(f"ℹ️  Refactor du cycle {cycle} sauté : sans git, aucun rollback n'est possible.")
        return
    green_sha = git_head_sha()
    if not green_sha:
        return

    # Périmètre : les fichiers de PRODUCTION posés/modifiés par la phase green (le red
    # de ce cycle n'écrit que des tests). Diff commit-à-commit : tout le travail du green
    # vient d'être committé. Rien à polir → rien à payer.
    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"]) if phase_start_sha else (False, "")
    scope = sorted(f for f in (diff_out.splitlines() if ok_diff else [])
                   if f.strip() and not is_test_file(f.strip())
                   and not is_orchestration_file(f.strip()) and os.path.exists(f.strip()))
    if not scope:
        print(f"ℹ️  Refactor du cycle {cycle} sauté : aucun fichier de production à polir.")
        return

    print(f"\n🧹 REFACTOR — 3e temps du cycle {cycle} (opportuniste, re-vérifié, rollback au commit green)...")
    sentinel = done_sentinel(CYCLE_REFACTO_PHASE_ID, cycle)
    cleanup_sentinels(CYCLE_REFACTO_PHASE_ID)
    refacto_skills = load_skills(["refacto"])
    scope_block = "\n".join(f"   - {f}" for f in scope)

    full_context = f"""Tu es un Expert Craftsman, praticien du TDD. Le cycle {cycle} vient d'être refermé :
la suite complète est VERTE. Joue le TROISIÈME TEMPS du cycle de Beck (red → green →
REFACTOR) : améliore le design du code de production que ce cycle vient de poser, SANS
changer aucun comportement.

--- COMPÉTENCES SPÉCIALISÉES ---
{refacto_skills}
--- CONTRAINTES GLOBALES ---
Stack: {blackboard['global_rules']['target']}
Styling: {blackboard['global_rules']['styling']}
Interdictions: {blackboard['global_rules']['constraints']}
Accessibilité: {blackboard['global_rules']['accessibility']}

--- PÉRIMÈTRE (code de production posé par ce cycle) ---
{scope_block}
   Concentre-toi sur ces fichiers ; tu peux ajuster un AUTRE fichier de PRODUCTION
   uniquement si une duplication l'y relie directement. Tout le reste est HORS PÉRIMÈTRE.

--- RÈGLES ABSOLUES ---
1. Comportement STRICTEMENT inchangé : la commande « {verify_cmd} » doit rester verte.
2. Tu ne touches à AUCUN fichier de test (gelés : toute modification est détectée par
   diff git et annulée) ni au fichier {BLACKBOARD_FILE}.
3. Refactore UNIQUEMENT si utile (duplication à résorber, nom trompeur, structure
   alambiquée). Si rien ne vaut la peine, NE MODIFIE RIEN : un refactor cosmétique
   coûte plus qu'il ne rapporte.

--- FIN DE TÂCHE OBLIGATOIRE ---
En toute DERNIÈRE action, crée le fichier sentinelle '{sentinel}' à la racine : la liste
des fichiers modifiés (un par ligne), ou le seul mot NO_CHANGE si tu n'as rien modifié.
"""
    with open(TMP_REFACTO_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    RUNNER.new_context()
    mm_audit.event("agent_task", prompt_bytes=len(f"Lis le fichier de consignes '{TMP_REFACTO_FILE}' à la racine du projet et exécute le refactor du cycle."))
    RUNNER.send_task(f"Lis le fichier de consignes '{TMP_REFACTO_FILE}' à la racine du projet et exécute le refactor du cycle.")

    if not wait_for_file_creation(sentinel):
        print(f"⏱️  Le refactor du cycle {cycle} n'a pas signalé sa fin : l'arbre est vérifié tel quel (rollback au moindre doute).")
    cleanup_sentinels(CYCLE_REFACTO_PHASE_ID)

    # Le diff git est la seule vérité (la sentinelle n'est qu'un signal de fin) : les
    # tests touchés sont restaurés/supprimés comme en green, puis le reliquat de
    # production décide de la suite.
    touched = sorted(f for f in files_changed_since_phase_start(green_sha)
                     if f.strip() and not is_orchestration_file(f.strip()))
    touched_tests = [f for f in touched if is_test_file(f)]
    if touched_tests:
        ok_tracked, tracked_out = run_git(["ls-files", "--"] + touched_tests)
        tracked = set(tracked_out.splitlines()) if ok_tracked else set()
        to_restore = sorted(f for f in touched_tests if f in tracked)
        if to_restore:
            run_git(["checkout", "--"] + to_restore)
        for f in touched_tests:
            if f not in tracked:
                try:
                    os.remove(f)
                except OSError:
                    pass
        print(f"🛡️  Refactor du cycle {cycle} : fichiers de test touchés ({', '.join(touched_tests)}) — restaurés/supprimés (tests gelés, le refactor ne change que la production).")
    touched_prod = [f for f in touched if not is_test_file(f)]
    if not touched_prod:
        print(f"✓ Refactor du cycle {cycle} : aucun changement retenu (NO_CHANGE) — le commit green fait foi.")
        return

    is_ok, output, timed_out = run_verify_resilient(verify_cmd)
    count_regression = test_count_regression(output, blackboard) if is_ok else None
    if is_ok and not count_regression:
        record_test_count(output, blackboard)
        commit_phase(f"cycle {cycle} refactor: {phase['name']}")
        print(f"🧹 [SUCCÈS] Refactor du cycle {cycle} re-vérifié : la suite reste verte ({len(touched_prod)} fichier(s) de production poli(s)).")
        return

    # ── ROLLBACK MÉCANIQUE AU COMMIT GREEN ── : suite rouge, compte en baisse ou timeout
    # (un timeout ne prouve pas un vert, et seul un état PROUVÉ vert peut précéder le
    # prochain red). Les fichiers suivis reviennent via reset --hard ; les fichiers CRÉÉS
    # par le refactor (non suivis) sont supprimés — l'équivalent de la restauration pour
    # un fichier qui n'existait pas au commit green.
    ok_tracked, tracked_out = run_git(["ls-files", "--"] + touched_prod)
    tracked = set(tracked_out.splitlines()) if ok_tracked else set()
    run_git(["reset", "--hard", green_sha])
    for f in touched_prod:
        if f not in tracked:
            try:
                os.remove(f)
            except OSError:
                pass
    if timed_out:
        reason = "la re-vérification a expiré (timeout : un vert non prouvé ne suffit pas)"
    elif count_regression:
        reason = "le compte de tests passants a diminué"
    else:
        reason = "la suite ne reste pas verte"
    print(f"↩️  Refactor du cycle {cycle} ANNULÉ ({reason}) : retour au commit green {green_sha[:8]} — le run continue, rien n'est perdu.")

def execute_final_refactoring(blackboard: dict, user_need: str):
    print(f"\n{'='*50}\n🛡️  ETAPE 5 : AGENT RÉFACTORISATION & POLISH FINAL\n{'='*50}")

    # Point de rollback (3b) : le refacto est la dernière main posée sur une codebase
    # entièrement verte.
    pre_refacto_sha = git_head_sha()

    refacto_skills = load_skills(["refacto"])

    # Périmètre de l'usine : on ne refactore QUE ce que le run a produit ou modifié (diff
    # depuis la baseline du run), jamais le legacy préexistant. Sans git le périmètre est vide
    # → on retombe sur l'ancienne formulation (mode dégradé déjà assumé par tout le pipeline).
    baseline_sha = blackboard.get("_run_baseline_sha", "")
    scope = sorted(
        f for f in files_changed_since_phase_start(baseline_sha)
        if not is_orchestration_file(f) and os.path.exists(f)
    )
    if scope:
        scope_block = (
            "Analyse UNIQUEMENT les fichiers ci-dessous, produits ou modifiés par l'usine "
            "(tout le reste — legacy, dépendances — est HORS PÉRIMÈTRE : ne le lis ni ne le "
            "modifie) :\n"
            + "\n".join(f"   - {f}" for f in scope)
            + "\n   Procède fichier par fichier ; ne charge pas toute la codebase d'un coup."
        )
    else:
        scope_block = "Analyse tous les fichiers créés ou modifiés."

    full_context = f"""Tu es un Expert Craftsman, Ingénieur Senior de refactoring et Auditeur de Code.
Effectue un audit final et un polish sur l'ensemble de la codebase générée.

--- COMPÉTENCES SPÉCIALISÉES ---
{refacto_skills}
--- CONTRAINTES GLOBALES ---
Stack: {blackboard['global_rules']['target']}
Styling: {blackboard['global_rules']['styling']}
Interdictions: {blackboard['global_rules']['constraints']}
Accessibilité: {blackboard['global_rules']['accessibility']}

--- BESOIN INITIAL ---
{user_need}

--- OBJECTIFS ---
1. {scope_block}
2. Identifie les anomalies (imports orphelins, types obsolètes, etc.).
3. Corrige directement toutes les incohérences en modifiant les fichiers.
4. Tu ne SUPPRIMES ni n'AFFAIBLIS JAMAIS un test existant pour faire passer la suite :
   si un test devient rouge, c'est le code de production qu'il faut corriger.
5. Rédige un rapport technique récapitulant les optimisations appliquées dans {REFACTO_REPORT_FILE}.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{REFACTO_REPORT_FILE}', crée le fichier
sentinelle '{REFACTO_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le
signal de fin pour l'orchestrateur.
"""
    with open(TMP_REFACTO_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    # Un rapport résiduel d'un run précédent interrompu serait détecté IMMÉDIATEMENT
    # alors que l'agent modifie encore du code : on le purge pour que l'attente ci-dessous
    # n'observe que le rapport de CE run.
    if os.path.exists(REFACTO_REPORT_FILE):
        os.remove(REFACTO_REPORT_FILE)
        print(f"   🧹 '{REFACTO_REPORT_FILE}' résiduel d'un run précédent supprimé.")
    cleanup_pipeline_sentinel(REFACTO_DONE_SENTINEL)

    print("🤖 Envoi de l'ordre de refactoring via fichier...")
    RUNNER.new_context()
    mm_audit.event("agent_task", prompt_bytes=len(f"Lis le fichier '{TMP_REFACTO_FILE}' à la racine du projet et exécute l'audit final complet."))
    RUNNER.send_task(f"Lis le fichier '{TMP_REFACTO_FILE}' à la racine du projet et exécute l'audit final complet.")

    # Même contrat de sentinelle que les étapes 1 à 3 (avec le filet de stabilité hérité
    # de wait_for_pipeline_file) : la simple EXISTENCE du rapport n'est pas un signal de
    # fin — l'agent peut le créer puis continuer à modifier du code pendant qu'on re-vérifie.
    if wait_for_pipeline_file(REFACTO_REPORT_FILE, REFACTO_DONE_SENTINEL):
        print(f"✅ Rapport de refactoring généré dans '{REFACTO_REPORT_FILE}'.")
    else:
        print(f"⚠️  Timeout : '{REFACTO_REPORT_FILE}' non généré (le refacto a pu modifier du code malgré tout).")

    # Nettoyage des fichiers temporaires, quel que soit le sort du refacto.
    for tmp_f in [TMP_CODER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE, TMP_PLAN_FILE,
                  TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_sentinels()

    # ── REVÉRIFICATION POST-REFACTO + CORRECTION DE RÉGRESSION (brique A jusqu'au bout) ──
    # Le refacto MODIFIE le code : c'est la dernière main posée sur la codebase, et la seule
    # action de production qui échappait au verdict objectif. On re-exécute la SUITE GLOBALE ;
    # si le polish a introduit une régression, on ne s'arrête pas net : on lance une boucle de
    # CORRECTION (même logique que la production : feedback d'exécution → agent → re-vérif),
    # bornée à MAX_ATTEMPTS. Échec définitif seulement après (régression ou timeout d'infra).
    final_cmd = (blackboard.get("verify_cmd") or "").strip()
    if not final_cmd:
        print("⚠️  Pas de 'verify_cmd' global : revérification post-refacto impossible, étape sautée.")
        return

    ok, output, timed_out, fixes = verify_and_fix_after_refacto(blackboard, user_need, final_cmd)
    if ok:
        if fixes:
            print(f"✅ Régression post-refacto corrigée (tentative {fixes}) : la suite globale passe de nouveau.")
        else:
            print("✓ Revérification post-refacto OK : le polish n'a pas introduit de régression détectable.")
        commit_phase("refacto: final polish")
        return

    reason = ("a expiré de façon répétée (incident d'INFRASTRUCTURE, pas le code)"
              if timed_out else f"reste ROUGE après {MAX_ATTEMPTS} tentative(s) de correction")
    print(f"""
{'='*60}
❌ Après le refacto, la suite globale {reason}.
   Le polish modifie le code ; la suite « {final_cmd} » ne passe plus et la correction
   automatique n'a pas suffi.

   Dernière sortie (tronquée) :
{output}

💡 Inspecte les derniers changements (cf. '{REFACTO_REPORT_FILE}') ou corrige/relance la
   suite manuellement avant de livrer.
{'='*60}
""")
    # Rollback (3b) UNIQUEMENT sur une régression persistante PROUVÉE : un timeout d'infra
    # ne prouve rien contre le polish, donc le code est conservé dans ce cas. reset --hard
    # restaure tous les fichiers suivis ; les fichiers CRÉÉS par le refacto (non suivis,
    # rapport compris) survivent pour inspection.
    if _GIT["enabled"] and pre_refacto_sha and not timed_out:
        ok_rollback, _ = run_git(["reset", "--hard", pre_refacto_sha])
        if ok_rollback:
            print(f"↩️  Refacto annulé (retour à {pre_refacto_sha[:8]}) : le code livré est l'état "
                  f"toutes-phases-vertes. « {REFACTO_REPORT_FILE} » (non suivi) survit pour inspection.")
            mm_audit.event("guard", name="rollback_refacto", action="reset_hard")
    write_fail_report(
        "Régression post-refacto non résorbée",
        f"Après le refacto, la suite globale {reason}. La correction automatique n'a pas suffi.",
        blackboard, details=output)
    RUNNER.kill()
    sys.exit(1)

def fail_pipeline(message: str):
    """Point de sortie unique des échecs d'étape du pipeline (étapes 1 à 3).

    Tue toujours la session tmux AVANT de quitter : un exit qui laisse l'agent vivant
    le laisse finir d'écrire son livrable APRÈS que l'orchestrateur a abandonné — au
    relancement, ce fichier à moitié validé serait pris pour un état de reprise valide
    (c'est ainsi qu'une spec jamais approuvée devenait la source de vérité). RUNNER.kill()
    est sans effet quand aucune session n'existe : ce helper est donc sûr partout.
    """
    print(message)
    write_fail_report("Échec d'une étape du pipeline", message)
    RUNNER.kill()
    sys.exit(1)

def files_changed_since_phase_start(start_sha: str) -> set:
    """Ensemble des fichiers modifiés/créés depuis le début de la phase (signal robuste de
    la garde anti-fantôme, échelle PHASE). Vide sans git ou sans sha → l'appelant retombe
    sur le fallback mtime.

    Aucun commit intermédiaire n'est posé pendant une phase : le travail vit dans l'arbre de
    travail. On compare donc l'arbre au sha de début de phase ('git diff <sha>', fichiers
    suivis) et on ajoute les fichiers non suivis ('ls-files --others').
    """
    if not _GIT["enabled"] or not start_sha:
        return set()
    changed = set()
    ok_diff, diff_out = run_git(["diff", "--name-only", start_sha])
    if ok_diff:
        changed.update(line.strip() for line in diff_out.splitlines() if line.strip())
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        changed.update(line.strip() for line in others_out.splitlines() if line.strip())
    return changed

def generate_impact_review_tui():
    """Agent Revue d'Impact : croise le plan avec le code EXISTANT et matérialise dans
    'impact.md' les comportements actuels que l'évolution va casser. Placée APRÈS le plan
    (elle en a besoin) et AVANT le blackboard : l'humain arbitre les cassures au moment où
    corriger coûte le moins cher, et la liste validée pilote ensuite le triage du chemin
    rouge en production (une cassure entérinée ici ne re-bloquera jamais le run)."""
    print("\n🔎 [ETAPE 2BIS : AGENT REVUE D'IMPACT] Analyse de l'impact du plan sur l'existant...")

    impact_prompt = f"""Tu es un Agent de Revue d'Impact, indépendant et prudent. Le plan d'implémentation '{PLAN_FILE}' va faire évoluer ce projet : ta mission est d'identifier les COMPORTEMENTS EXISTANTS que ce plan va CASSER, pour que l'humain les valide AVANT la production (personne ne doit découvrir en cours de route que l'évolution pète l'application).

Méthode OBLIGATOIRE :
1. Lis '{PLAN_FILE}' (le plan) et '{SPEC_FILE}' (la spécification validée).
2. Explore le code EXISTANT du projet et sa suite de tests avec tes outils de lecture (si le projet est vierge, constate-le simplement).
3. Pour chaque comportement actuel que le plan va modifier ou supprimer, décris PRÉCISÉMENT : le comportement observable aujourd'hui, les fichiers de test qui le portent (chemins réels, vérifiés), et la partie du plan qui le casse.

Écris le résultat dans '{IMPACT_FILE}' à la racine, avec EXACTEMENT cette structure :
# Revue d'impact
## Comportements existants qui vont casser
(un bloc '### IMPACT-<n> — <titre court>' par comportement, contenant trois lignes : 'Comportement actuel : ...', 'Tests porteurs : <chemins réels>', 'Cause dans le plan : ...'. S'il n'y a AUCUN impact — projet vierge ou plan purement additif — écris à la place la seule ligne : 'Aucun impact : <justification courte>.')
## Journal des suppressions
(laisse cette section VIDE : elle est réservée à l'orchestrateur.)

Zéro invention : ne liste que ce que tu as constaté dans le code réel ; dans le doute, mentionne l'impact (l'humain tranchera). Tu ne modifies aucun autre fichier.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.
En toute DERNIÈRE action, après avoir sauvegardé '{IMPACT_FILE}', crée le fichier sentinelle '{IMPACT_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le signal de fin pour l'orchestrateur.
"""
    with open(TMP_IMPACT_FILE, "w", encoding="utf-8") as f:
        f.write(impact_prompt)
    cleanup_pipeline_sentinel(IMPACT_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(f"Lis le fichier de consignes '{TMP_IMPACT_FILE}' à la racine du projet et exécute-le scrupuleusement."))
    RUNNER.send_task(f"Lis le fichier de consignes '{TMP_IMPACT_FILE}' à la racine du projet et exécute-le scrupuleusement.")

    if wait_for_pipeline_file(IMPACT_FILE, IMPACT_DONE_SENTINEL):
        print(f"✅ [ETAPE 2BIS] Revue d'impact '{IMPACT_FILE}' créée avec succès !")
    else:
        fail_pipeline(f"❌ [ETAPE 2BIS] Timeout ou échec de création de '{IMPACT_FILE}'.")

def git_head_sha() -> str:
    """Sha du HEAD courant, ou chaîne vide sans git/commits."""
    ok, out = run_git(["rev-parse", "HEAD"])
    return out if ok else ""

def impact_phase_file(phase_id) -> str:
    """Rapport d'arbitrage mid-run d'un impact IMPRÉVU (impact-phase-<id>.md, committé)."""
    return f"{IMPACT_PHASE_PREFIX}{phase_id}.md"

def inject_skills_dictionary(text: str) -> str:
    """Substitue le catalogue RÉEL des skills dans les consignes d'un skill du pipeline.

    Le dictionnaire va à l'ARCHITECTE (étape 2), qui déclare le Skill de chaque phase
    dans le plan ; le compilateur blackboard ne fait ensuite que RECOPIER cette décision.
    Le routage est ainsi décidé par l'agent qui a le plus de contexte, jamais par le
    maillon le plus faible.
    """
    skills_dictionary = build_skills_dictionary()
    if "{{SKILLS_DICTIONARY}}" in text:
        return text.replace("{{SKILLS_DICTIONARY}}", skills_dictionary)
    return text + f"\n\nDICTIONNAIRE DES COMPÉTENCES AUTORISÉES :\n{skills_dictionary}\n"

def is_orchestration_file(path: str) -> bool:
    """'path' est-il un artefact de l'orchestrateur (et non du code produit) ? Cf. _ORCH_BASENAMES."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _ORCH_BASENAMES:
        return True
    # Sentinelles et tampons éphémères, où qu'ils se trouvent dans l'arbre.
    if base.startswith(".phase_") or base.startswith(".pipeline_"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Caches Python, environnement virtuel et répertoires d'outillage : jamais du code produit.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False

def is_test_file(path: str) -> bool:
    """Heuristique de nommage best-effort : 'path' ressemble-t-il à un fichier de test ?

    Multi-langages et agnostique (répertoires tests/__tests__/spec, conventions test_*.py,
    *_test.go, *.test.ts, *.spec.js, *Test.java/*Spec.kt). Volontairement LARGE côté test : en
    cas de doute on classe en test, pour NE PAS faire caler une phase tests légitime sur un faux
    « fichier de prod modifié » (la garde tests-only ne restaure que ce qui n'est PAS un test).
    Faux positif possible (helper hors convention) : le feedback nomme les fichiers, l'humain
    arbitre, exactement comme protected_test_files.
    """
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if any(s.lower() in ("test", "tests", "__tests__", "spec", "specs", "testing")
           for s in segments[:-1]):
        return True
    low = base.lower()
    if low.startswith("test_") or low.startswith("test."):
        return True
    if re.search(r"[._-](test|tests|spec|specs)\.[a-z0-9]+$", low):
        return True
    if re.search(r"(Test|Tests|Spec|Specs|IT)\.[A-Za-z0-9]+$", base):
        return True
    return False

def is_ui_file(name: str) -> bool:
    """'name' (nom de fichier nu) est-il une source d'interface à auditer ?

    Volontairement pragmatique : extensions UI connues, MOINS l'outillage qui partage ces
    extensions sans être de l'interface — bundles minifiés (illisibles, générés),
    déclarations TypeScript, fichiers de configuration (vite/webpack/tailwind…),
    stories Storybook (démo, pas produit), dotfiles.
    """
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext not in UI_EXTENSIONS:
        return False
    if low.startswith("."):
        return False
    if low.endswith(".d.ts") or ".min." in low or ".config." in low or ".stories." in low:
        return False
    return True

def load_blackboard() -> dict:
    with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_skills(skills_list: list) -> str:
    content = ""
    for skill in skills_list:
        skill_path = os.path.join(SKILLS_DIR, skill, "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                content += f"--- COMPÉTENCE : {skill.upper()} ---\n{f.read()}\n\n"
        else:
            print(f"   ⚠️  Skill manquant : '{skill}' (chemin attendu : {skill_path})")
    return content

def lot_closing_ids(phases: list) -> set:
    """Ids des phases qui REFERMENT leur lot ATDD (dernière phase du bloc de leur lot).

    La structure des lots (un bloc contigu par lot : une phase test puis ses phases
    d'implémentation, jamais de lot sans implémentation) est validée mécaniquement avant
    production : la dernière phase d'un bloc est donc toujours une phase 'atdd-impl'.
    C'est elle — et elle seule — qui porte le verdict universel (suite complète verte) ;
    les étapes d'implémentation intermédiaires sont validées par la compilation seule
    (build_cmd). Décision de POSITION, calculée ici par l'orchestrateur : jamais déclarée
    par un LLM, donc jamais hallucinable.
    """
    closing = set()
    for i, phase in enumerate(phases or []):
        if not isinstance(phase, dict):
            continue
        if str(phase.get("nature") or "").strip().lower() != IMPL_NATURE:
            continue
        nxt = phases[i + 1] if i + 1 < len(phases) else None
        if not isinstance(nxt, dict) or str(nxt.get("cycle")) != str(phase.get("cycle")):
            closing.add(phase.get("id"))
    return closing

def mutation_tool_available(cmd: str) -> bool:
    """Sonde best-effort : l'exécutable principal de la commande de mutation répond-il ? (§6.5)

    « Outil absent » ne doit JAMAIS être confondu avec « mutants survivants » : sans cette sonde,
    une stack sans l'outil ferait échouer un run pour rien. On extrait le premier exécutable utile
    (en sautant les affectations VAR=val de tête) et on teste sa présence (shutil.which / fichier).
    EN CAS DE DOUTE, on considère l'outil présent (best-effort) : la sonde ne doit jamais, à elle
    seule, désactiver une brique B déclarée.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return True  # commande non parsable : on n'empêche pas la brique B sur un doute
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1  # saute les affectations d'environnement de tête (ex. 'CI=1 mvn ...')
    if i >= len(tokens):
        return True
    exe = tokens[i]
    # 'npx <outil>' / 'npm exec <outil>' : ces lanceurs téléchargent au besoin, on les considère
    # disponibles dès que le lanceur lui-même est présent.
    if exe in ("npx", "npm", "pnpm", "yarn", "bunx"):
        return shutil.which(exe) is not None
    if "/" in exe or "\\" in exe:
        return os.path.exists(exe) or shutil.which(os.path.basename(exe)) is not None
    if shutil.which(exe) is not None:
        return True
    # Binaire JS local non préfixé (ex. 'stryker' installé dans node_modules/.bin).
    local_bin = os.path.join("node_modules", ".bin", exe)
    return os.path.exists(local_bin) or os.path.exists(local_bin + ".cmd")

def no_declared_file_touched(files: list, since_ts: float, changed_since_phase: set = None) -> bool:
    """True si AUCUN fichier déclaré n'a réellement changé DEPUIS LE DÉBUT DE LA PHASE.

    Signature du « codeur fantôme » : sentinelle écrite sans travail réel. Le verdict par
    suite complète ne peut PAS attraper ce cas (rien n'a changé → tout reste vert) : ce
    contrôle bon marché et agnostique s'en charge. Référentiel = la PHASE, pas la tentative :
    un fichier produit à une tentative et re-déclaré inchangé à la suivante reste reconnu
    comme du travail réel (LENIENT volontairement — il suffit d'UN fichier réellement touché
    DANS LA PHASE pour passer). Un référentiel par-tentative reclassait à tort en « fantôme »
    un fichier écrit à une tentative précédente. Deux signaux : 'changed_since_phase' (diff git
    depuis le début de phase, robuste et prioritaire — insensible aux mtimes tronqués de
    DrvFs/WSL2) puis, en fallback sans git, le mtime depuis le début de phase ('since_ts').
    """
    changed_since_phase = changed_since_phase or set()
    for path in files:
        clean = path.strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue
        if clean in changed_since_phase:
            return False
        try:
            if os.path.exists(clean) and os.path.getmtime(clean) >= since_ts:
                return False
        except OSError:
            continue
    return True

def parse_test_count(output: str):
    """Compte best-effort des tests PASSÉS dans la sortie d'un runner ; None quand aucun
    motif connu ne correspond (runner inconnu, sortie brouillée).

    Reconnus : Maven « Tests run: N » (la dernière ligne de synthèse gagne), vitest/jest
    « Tests: N passed », pytest/cargo « N passed », lignes « --- PASS: » de go test -v.
    """
    if not output:
        return None
    maven = re.findall(r"Tests run:\s*(\d+)", output)
    if maven:
        return int(maven[-1])
    vitest = re.findall(r"Tests:?\s+(\d+)\s+passed", output)
    if vitest:
        return int(vitest[-1])
    generic = re.findall(r"(\d+)\s+passed", output)
    if generic:
        return int(generic[-1])
    go_passes = len(re.findall(r"^--- PASS:", output, re.MULTILINE))
    if go_passes:
        return go_passes
    return None

def print_failure_message(phase: dict, blackboard: dict, critic_feedback: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    print(f"""
{'='*60}
❌ La phase {phase['id']} « {phase['name']} » n'a pas convergé après {MAX_ATTEMPTS} tentatives.

   Dernier point bloquant relevé par la vérification :
   « {critic_feedback} »

💡 Le modèle actuel ({model}) cale sur cette étape précise.
   Le plus efficace : relance après avoir amené un modèle un cran au-dessus,
   soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : les {done_count} phase(s) déjà validée(s) seront reprises
   automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
{'='*60}
""")

def read_impact_review() -> str:
    """Contenu de la revue d'impact validée (vide si absente : triage tout-IMPRÉVU)."""
    if not os.path.exists(IMPACT_FILE):
        return ""
    with open(IMPACT_FILE, "r", encoding="utf-8") as f:
        return f.read()

def read_repair_outcome(phase_id: int, attempt: int) -> tuple:
    """Lit la sentinelle du Réparateur. Retourne (is_conflict: bool, conflict_tests: list).

    DONE (ou tout contenu non CONFLICT) = réparation revendiquée, le verdict reste la
    re-vérification par exécution. CONFLICT = vraie incohérence déclarée : les lignes
    'TEST: <chemin>' listent les fichiers de test concernés (pour la suppression mécanique
    si l'humain entérine l'impact).
    """
    path = repair_sentinel(phase_id, attempt)
    if not os.path.exists(path):
        return False, []
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines or not lines[0].upper().startswith("CONFLICT"):
        return False, []
    tests = []
    for line in lines[1:]:
        m = re.match(r"^TEST\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            tests.append(m.group(1).strip())
    return True, tests

def read_touched_files(phase_id: int, attempt: int) -> list:
    """Lit la liste des fichiers déclarés par le Codeur dans son sentinelle .done.

    Les petits modèles formatent souvent la liste en puces ('- src/foo.ts', '* a.py',
    '1. b.go') : les marqueurs de liste en tête de ligne sont retirés, sinon chaque ligne
    échouerait au contrôle os.path.exists en aval (faux « codeur fantôme » avec un
    feedback trompeur).
    """
    path = done_sentinel(phase_id, attempt)
    if not os.path.exists(path):
        return []
    files = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            cleaned = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line.strip())
            if cleaned:
                files.append(cleaned)
    mm_audit.event("sentinel", path=path, declared_files=len(files))
    return files

def read_triage(phase_id: int, attempt: int) -> tuple:
    """Parse la sentinelle de triage. Retourne (prevu: list, imprevu: list).

    Toute ligne non conforme au format 'PREVU: ...' / 'IMPREVU: ...' est ignorée : un
    triage brouillé dégrade vers le réparateur (chemin sûr), jamais vers une suppression.
    """
    path = triage_sentinel(phase_id, attempt)
    prevu, imprevu = [], []
    if not os.path.exists(path):
        return prevu, imprevu
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^(PREVU|IMPREVU)\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
            if not m:
                continue
            (prevu if m.group(1).upper() == "PREVU" else imprevu).append(m.group(2).strip())
    return prevu, imprevu

def read_verdict(phase_id: int, attempt: int) -> tuple:
    """Lit le verdict du Vérificateur. Retourne (is_ok: bool, feedback: str).

    Parsing tolérant (repris de Coding-Without-Tests) : on ignore les lignes vides et les
    barrières markdown en tête, puis on lit le premier mot de la première ligne utile.
    'OK', 'OK.', 'OK, conforme'... valident ; tout le reste (dont 'REJECTED') rejette.
    """
    path = verdict_sentinel(phase_id, attempt)
    if not os.path.exists(path):
        return False, "Le vérificateur n'a produit aucun verdict."
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return False, "Verdict vide produit par le vérificateur."

    lines = raw.splitlines()
    idx = 0
    while idx < len(lines) and (not lines[idx].strip() or lines[idx].strip().startswith("```")):
        idx += 1
    head_line = lines[idx].strip() if idx < len(lines) else ""

    token = ""
    for ch in head_line.upper():
        if ch.isalpha():
            token += ch
        else:
            break

    if token == "OK":
        return True, ""
    body = "\n".join(lines[idx + 1:]).strip() if token == "REJECTED" else raw
    return False, body or "Le vérificateur a rejeté la phase sans en préciser le motif."

def record_test_count(output: str, blackboard: dict, expect_growth: bool = False):
    """Persiste dans le blackboard le dernier compte parsable de tests passés (survit aux reprises).

    Une phase 'tests' qui passe au vert SANS augmenter strictement le compte ne reçoit
    qu'un avertissement console : signal faible, délibérément pas un verdict (les
    réorganisations existent).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if expect_growth and isinstance(old_count, int) and new_count <= old_count:
        print(f"⚠️  Phase 'tests' passée au vert sans augmenter la suite "
              f"({old_count} → {new_count} passants) : tests faibles ou dupliqués ?")
    blackboard["last_test_count"] = new_count
    save_blackboard(blackboard)

def red_suite_damage(output: str, blackboard: dict):
    """Message de feedback quand une phase 'tdd-red' a ENDOMMAGÉ la suite existante, sinon None.

    Après un red légitime, la suite échoue à cause des NOUVEAUX tests : les tests
    préexistants, eux, doivent continuer de passer (le code de production est gelé et les
    tests des cycles précédents sont protégés). Si le compte de tests PASSANTS a diminué
    par rapport au dernier état vert enregistré, la phase a cassé de l'existant (fixture
    ou état partagé, édition d'un test hors protection comme le test santé du scaffold) :
    c'est un rouge pour la MAUVAISE raison, rejeté. Sortie non parsable → garde inactive —
    c'est le cas NORMAL d'un red qui casse la compilation (API pas encore créée) : aucun
    compte n'est émis, et ce rouge-là est légitime.
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return None
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count < old_count:
        return (f"Ta phase red a cassé des tests EXISTANTS : {old_count} passants avant, "
                f"{new_count} maintenant. Un red légitime AJOUTE des tests qui échouent, sans "
                f"toucher aux tests déjà verts : restaure ce que tu as cassé (test existant "
                f"modifié, fixture ou état partagé…) et fais échouer la suite UNIQUEMENT par "
                f"les nouveaux tests de ce cycle.")
    return None

def repair_sentinel(phase_id: int, attempt: int) -> str:
    """Fin de passe du Réparateur (DONE, ou CONFLICT + lignes TEST:)."""
    return f".phase_{phase_id}.attempt{attempt}.repair.done"

def repair_touched_tests(blackboard: dict, phase_start_sha: str, baseline: set) -> list:
    """Fichiers de test touchés par une passe de réparation/correction (violation du gel).

    'baseline' est l'instantané des fichiers déjà modifiés AVANT la passe (capturé par
    l'appelant) : seul ce qui apparaît EN PLUS est l'œuvre de la passe — l'attribution ne
    blâme jamais le travail légitime antérieur de la phase, ni les suppressions de
    l'orchestrateur (committées et mémorisées dans _yolo_deleted_tests).
    """
    deleted = set(blackboard.get("_yolo_deleted_tests") or [])
    return sorted(f for f in files_changed_since_phase_start(phase_start_sha) - set(baseline or ())
                  if f.strip() and is_test_file(f.strip())
                  and not is_orchestration_file(f.strip())
                  and f.strip() not in deleted)

def resolve_build_cmd(phase: dict, blackboard: dict) -> str:
    """Commande de COMPILATION SEULE d'une phase : 'build_cmd' de la phase, sinon le global.

    C'est le verdict des étapes d'implémentation INTERMÉDIAIRES d'un lot : l'arbre doit
    COMPILER, la suite d'acceptance du lot a le droit de rester rouge jusqu'à la clôture.
    Contrat porté par le plan (et validé par l'humain au y/n) : cette commande compile la
    PRODUCTION SEULE, jamais les fichiers de test — sinon elle resterait rouge tant que
    toute l'API attendue par les tests d'acceptance n'existe pas, et aucune étape
    intermédiaire ne passerait. Renvoie une chaîne vide si rien n'est défini (fatal en
    validation dès qu'un lot compte plusieurs phases d'implémentation).
    """
    return (phase.get("build_cmd") or blackboard.get("build_cmd") or "").strip()

def resolve_mutation_cmd(phase: dict, blackboard: dict) -> str:
    """Commande de mutation testing : 'mutation_cmd' de la phase, sinon le global.

    Optionnelle et non bloquante (même chemin que verify_cmd / build_cmd). Vide → brique B
    inactive. Peut contenir le placeholder '{targets}', substitué par les fichiers à muter.
    """
    return (phase.get("mutation_cmd") or blackboard.get("mutation_cmd") or "").strip()

def resolve_verify_cmd(phase: dict, blackboard: dict) -> str:
    """Commande de vérification d'une phase : 'verify_cmd' de la phase, sinon le global.

    Verdict UNIVERSEL : par défaut, toutes les phases sont validées par le 'verify_cmd'
    global (compilation + suite complète). Le champ de phase n'existe que comme EXCEPTION
    déclarée par l'Architecte dans le plan. Renvoie une chaîne vide si rien n'est défini
    (cas traité en amont).
    """
    return (phase.get("verify_cmd") or blackboard.get("verify_cmd") or "").strip()

def restore_test_files(paths: list):
    """Restaure des fichiers de test touchés en violation du gel (suivis : git checkout ;
    nouveaux : suppression — l'équivalent de la restauration pour un fichier qui n'existait pas)."""
    if not _GIT["enabled"] or not paths:
        return
    ok_tracked, tracked_out = run_git(["ls-files", "--"] + paths)
    tracked = set(tracked_out.splitlines()) if ok_tracked else set()
    to_restore = sorted(p for p in paths if p in tracked)
    if to_restore:
        run_git(["checkout", "--"] + to_restore)
    for p in paths:
        if p not in tracked:
            try:
                os.remove(p)
            except OSError:
                pass

def run_mutation(cmd: str, timeout: int = MUTATION_TIMEOUT) -> tuple:
    """Exécute la commande de mutation testing HORS tmux. Renvoie (ok, output, timed_out).

    Calqué sur run_verify : shell=True, PATH préfixé de node_modules/.bin (binaires JS locaux),
    sortie tronquée. ok = code de sortie 0 — l'outil encode LUI-MÊME son seuil de tolérance aux
    mutants survivants ; Python ne parse jamais le résultat pour DÉCIDER, seulement pour le
    feedback. timed_out distingue un dépassement de budget (incident de coût/infra, dégradé en
    warn) d'un vrai « la suite ne mord pas assez ».
    """
    print(f"   🧬 Mutation testing : {cmd}")
    env = verify_env()
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        return False, f"TIMEOUT après {timeout}s lors du mutation testing.\n{truncate_output(partial)}", True
    except Exception as exc:
        return False, f"Impossible d'exécuter la commande de mutation : {exc}", False
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, truncate_output(output), False

def run_verify(cmd: str, timeout: int = VERIFY_TIMEOUT) -> tuple:
    """Exécute la commande de vérification HORS tmux et renvoie (ok, output, timed_out).

    ok = (code de sortie 0). output = stdout+stderr tronqué, utile comme feedback de retry
    pour un petit modèle. timed_out distingue un DÉPASSEMENT DE DÉLAI (incident d'infra :
    machine/réseau lents, process figé) d'un vrai « rouge » sur le code — ce qui permet de
    NE PAS facturer une tentative du codeur à un simple timeout (cf. run_verify_resilient).
    L'orchestrateur Python reste SEUL juge du verdict : on ne délègue jamais l'interprétation
    du résultat à un LLM. La commande provient du blackboard validé par l'humain (y/n).
    """
    print(f"   🧪 Vérification par exécution : {cmd}")
    # PATH unifié avec le shell de login de l'utilisateur (même Node que l'agent) puis
    # node_modules/.bin en tête : voir verify_env(). Au premier verdict JS/TS du run, une
    # ligne dit quel Node exécute réellement la commande (et ce que le projet attend).
    env = verify_env()
    toolchain_preflight(cmd, env)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        return False, f"TIMEOUT après {timeout}s lors de l'exécution de la vérification.\n{truncate_output(partial)}", True
    except Exception as exc:
        return False, f"Impossible d'exécuter la commande de vérification : {exc}", False
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, truncate_output(output), False

def run_verify_resilient(cmd: str) -> tuple:
    """Vérification résiliente aux timeouts d'INFRA. Renvoie (ok, output, timed_out_persistant).

    Un dépassement de VERIFY_TIMEOUT n'est presque jamais un verdict « rouge » sur le code :
    c'est un incident d'environnement (machine/réseau lents, process figé). Le compter comme
    un échec consommerait une des MAX_ATTEMPTS du codeur (point ouvert de proposition.md). Le
    code n'ayant pas changé entre deux exécutions, on RE-EXÉCUTE la commande (sans relancer le
    codeur) jusqu'à MAX_VERIFY_RETRIES_ON_TIMEOUT fois pour obtenir un verdict ferme. Si tous
    les essais expirent, on remonte timed_out=True (l'appelant ne décomptera pas la tentative).
    """
    output = ""
    for i in range(MAX_VERIFY_RETRIES_ON_TIMEOUT + 1):
        verify_started = time.time()
        ok, output, timed_out = run_verify(cmd)
        if not timed_out:
            mm_audit.event("verdict", cmd=cmd, exit=0 if ok else 1,
                           duration_s=round(time.time() - verify_started, 1),
                           output_bytes=len(output or ""))
            return ok, output, False
        if i < MAX_VERIFY_RETRIES_ON_TIMEOUT:
            print(f"   ⏱️  Vérification expirée ({VERIFY_TIMEOUT}s) — incident d'infra probable, "
                  f"pas un échec du code. Re-vérification {i + 1}/{MAX_VERIFY_RETRIES_ON_TIMEOUT}...")
    return False, output, True

def save_blackboard(data: dict):
    """Écrit le blackboard de façon ATOMIQUE (fichier temporaire + os.replace).

    Le blackboard est l'UNIQUE état de reprise (quelles phases sont DONE/OK). Un kill pile
    pendant un dump en mode 'w' classique (qui tronque puis réécrit en place) laisserait un
    YAML à moitié écrit → reprise impossible, tout le run perdu. On écrit donc dans un fichier
    temporaire, on force le flush sur disque, puis on renomme atomiquement : le fichier final
    est TOUJOURS soit l'ancienne version complète, soit la nouvelle, jamais un état partiel.
    os.replace est atomique sur le même système de fichiers (POSIX comme Windows).
    """
    tmp_path = BLACKBOARD_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, BLACKBOARD_FILE)
    # Journal de run : une TRANSITION de statut de phase déclenche un événement + une
    # copie figée du blackboard (les sauvegardes sans transition ne journalisent rien).
    statuses = {str(p.get("id")): str(p.get("status"))
                for p in (data.get("phases") or []) if isinstance(p, dict)}
    if statuses != _PHASE_STATUS_SEEN:
        for pid, status in statuses.items():
            if _PHASE_STATUS_SEEN.get(pid) != status:
                mm_audit.event("phase_status", id=pid, status=status)
        _PHASE_STATUS_SEEN.clear()
        _PHASE_STATUS_SEEN.update(statuses)
        mm_audit.snapshot(BLACKBOARD_FILE)

def signal_handler(sig, frame):
    print("\n⚠️  Interruption détectée. Nettoyage...")
    # Journal clos AVANT de tuer l'agent : un run interrompu laisse un run.json et un
    # run_end ('interrupted'), jamais un events.jsonl tronqué indistinguable d'un crash.
    mm_audit.end("interrupted")
    RUNNER.kill()
    sys.exit(1)

def test_count_regression(output: str, blackboard: dict):
    """Message de feedback quand la suite verte a PERDU des tests vs le compte enregistré,
    sinon None.

    Une suite affaiblie passe trivialement son propre verdict : le compte non décroissant
    est le plancher mécanique bon marché. Sortie non parsable → garde inactive (averti
    une fois par run).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        if not _TEST_COUNT["warned"]:
            print("ℹ️  Compte de tests non parsable dans la sortie du runner : la garde de "
                  "non-décroissance est inactive pour ce run (runner inconnu).")
            _TEST_COUNT["warned"] = True
        return None
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count < old_count:
        mm_audit.event("guard", name="regression_compte_tests", action="rejet",
                       avant=old_count, apres=new_count)
        return (f"La suite de vérification a PERDU des tests : {old_count} passants avant, "
                f"{new_count} maintenant. Supprimer, désactiver ou affaiblir des tests est "
                f"interdit : restaure les tests manquants et fais-les passer en corrigeant le code.")
    return None

def test_phase_damage(output: str, blackboard: dict):
    """Message de feedback quand une phase 'atdd-test' a ENDOMMAGÉ la suite existante, sinon None.

    Après une phase test légitime, la suite échoue à cause des NOUVEAUX tests d'acceptance :
    les tests préexistants, eux, doivent continuer de passer (le code de production est gelé
    et les tests des lots précédents sont protégés). Si le compte de tests PASSANTS a diminué
    par rapport au dernier état vert enregistré, la phase a cassé de l'existant (fixture
    ou état partagé, édition d'un test hors protection comme le test santé du scaffold) :
    c'est un rouge pour la MAUVAISE raison, rejeté. Sortie non parsable → garde inactive —
    c'est le cas NORMAL d'une phase test qui casse la compilation (API pas encore créée) :
    aucun compte n'est émis, et ce rouge-là est légitime.
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return None
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count < old_count:
        return (f"Ta phase de tests d'acceptance a cassé des tests EXISTANTS : {old_count} "
                f"passants avant, {new_count} maintenant. Une phase test légitime AJOUTE des "
                f"tests qui échouent, sans toucher aux tests déjà verts : restaure ce que tu as "
                f"cassé (test existant modifié, fixture ou état partagé…) et fais échouer la "
                f"suite UNIQUEMENT par les nouveaux tests de ce lot.")
    return None

def triage_sentinel(phase_id: int, attempt: int) -> str:
    """Verdict de l'Agent de Triage (une ligne PREVU:/IMPREVU: par fichier de test en échec)."""
    return f".phase_{phase_id}.attempt{attempt}.triage"

def truncate_output(text: str, limit: int = VERIFY_FEEDBACK_LIMIT) -> str:
    """Tronque une sortie de vérification en conservant le DÉBUT ET la FIN.

    L'ancien comportement (garder les N derniers caractères) perdait souvent l'essentiel :
    sur la plupart des outils (compilateurs, pytest, Maven…), la PREMIÈRE erreur — la cause
    racine — apparaît au début de la sortie, la fin n'étant qu'un résumé de comptage. On
    garde donc moitié début / moitié fin, avec un marqueur explicite pour que le codeur
    sache qu'il manque un segment.
    """
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return (text[:head]
            + f"\n[... sortie tronquée ({len(text)} caractères au total) ...]\n"
            + text[-tail:])

def validate_all_skills(blackboard: dict):
    referenced = set()
    for phase in blackboard["phases"]:
        for skill in phase.get("skills_required", []):
            referenced.add(skill)

    available = set()
    if os.path.isdir(SKILLS_DIR):
        for entry in os.listdir(SKILLS_DIR):
            if os.path.exists(os.path.join(SKILLS_DIR, entry, "SKILL.md")):
                available.add(entry)

    hallucinated = sorted(referenced - available)
    if hallucinated:
        print(f"\n⚠️  Skills référencés dans le blackboard mais INTROUVABLES (hallucination probable de l'architecte) : {', '.join(hallucinated)}")
        usable = sorted(available - PIPELINE_SKILLS)
        print(f"   Skills réellement disponibles : {', '.join(usable) or '(aucun)'}")
        print("   → Les phases concernées s'exécuteront sans ces skills. Corrige 'blackboard.yaml' si besoin avant de continuer.\n")
    else:
        print(f"✅ Tous les skills référencés existent ({len(referenced)} référencé(s)).\n")

    if not os.path.exists(os.path.join(SKILLS_DIR, "refacto", "SKILL.md")):
        print("⚠️  Skill 'refacto' introuvable : l'étape de polish final sera dégradée.\n")

def validate_blackboard_schema(blackboard: dict) -> tuple:
    """Contrôle la structure du blackboard. Renvoie (fatal, soft).

    Le blackboard sort d'un petit LLM faillible ; deux classes de problèmes :
      - fatal : manques STRUCTURANTS sur lesquels la production planterait (accès direct
        `blackboard[...]` / `phase[...]`) ou tournerait à vide (checklist sans tâches, pas de
        'verify_cmd' global → scaffold sauté et fallback absent). L'orchestrateur DOIT s'arrêter
        dessus : payer plan + architecture puis lancer un run voué à l'échec n'a aucun intérêt.
      - soft : manques rattrapés par apply_blackboard_defaults (global_rules et ses clés,
        comblés en « (non spécifié) ») ou purement cosmétiques ('project', affichage seul) :
        on les signale sans bloquer.
    N'écrit rien et ne corrige rien : c'est l'orchestrateur (et l'humain au y/n) qui décide.
    """
    fatal, soft = [], []
    if not isinstance(blackboard, dict):
        return ["Le blackboard n'est pas un mapping YAML valide."], []
    if not blackboard.get("project"):
        soft.append("Champ 'project' manquant (titre d'affichage uniquement).")
    global_rules = blackboard.get("global_rules")
    if not isinstance(global_rules, dict):
        soft.append("Bloc 'global_rules' manquant ou invalide (sera comblé en « (non spécifié) »).")
    else:
        for key in REQUIRED_GLOBAL_RULES:
            if key not in global_rules:
                soft.append(f"Clé 'global_rules.{key}' manquante (sera comblée en « (non spécifié) »).")
    phases = blackboard.get("phases")
    if not isinstance(phases, list) or not phases:
        fatal.append("Bloc 'phases' manquant ou vide : rien à produire.")
    else:
        for idx, phase in enumerate(phases):
            if not isinstance(phase, dict):
                fatal.append(f"phases[{idx}] n'est pas un mapping.")
                continue
            if "id" not in phase:
                fatal.append(f"phases[{idx}].id manquant (accédé directement en production).")
            if not phase.get("name"):
                fatal.append(f"phases[{idx}].name manquant.")
            if not isinstance(phase.get("tasks"), list) or not phase.get("tasks"):
                fatal.append(f"phases[{idx}].tasks manquant ou vide : checklist sans contenu.")
            for field in PLANNED_TEST_FIELDS:
                if field not in phase:
                    continue
                entries = phase.get(field)
                if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                    fatal.append(f"phases[{idx}].{field} doit être une liste de chemins (chaînes).")
                    continue
                not_tests = [e for e in entries if e.strip() and not is_test_file(e.strip())]
                if not_tests:
                    fatal.append(f"phases[{idx}].{field} : {', '.join(not_tests)} — le plan ne peut "
                                 f"déclarer obsolète ou modifiable qu'un FICHIER DE TEST, jamais du "
                                 f"code de production.")
        ids = [str(phase.get("id")) for phase in phases if isinstance(phase, dict) and "id" in phase]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            fatal.append(
                f"phases[].id dupliqués ({', '.join(duplicated)}) : les sentinelles "
                f"'.phase_N.attemptM.done' seraient PARTAGÉES entre deux phases (faux signaux de fin)."
            )
        elif ids and ids != [str(i) for i in range(1, len(ids) + 1)]:
            soft.append(
                f"phases[].id n'est pas une séquence contiguë 1..N ({', '.join(ids)}) : toléré, "
                f"mais vérifie que le compilateur n'a pas sauté ou renuméroté une phase."
            )
        bad_nature = sorted({str(phase.get("nature")) for phase in phases
                             if isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip()
                             and str(phase.get("nature")).strip().lower() not in ("feature", "tests")})
        if bad_nature:
            soft.append(
                f"phases[].nature hors {{feature, tests}} : {', '.join(bad_nature)} "
                f"(les prompts codeur concernés retomberont sur la formulation neutre)."
            )
        if any(isinstance(phase, dict) and not str(phase.get("nature") or "").strip()
               for phase in phases):
            soft.append(
                "Certaines phases ne déclarent pas de 'nature' (ancien blackboard ?) : leur "
                "prompt codeur utilise la formulation neutre au lieu de celle pilotée par le plan."
            )
        # Volet A (informatif) : une phase 'tests' devrait couvrir AU PLUS une user story
        # (fenêtre de contexte du testeur, périmètre muté plus serré). Toléré, jamais bloquant.
        multi_cover_tests = sorted(
            str(phase.get("id")) for phase in phases
            if isinstance(phase, dict)
            and str(phase.get("nature") or "").strip().lower() == "tests"
            and isinstance(phase.get("covers"), list) and len(phase.get("covers")) > 1)
        if multi_cover_tests:
            soft.append(
                f"Phases 'tests' couvrant plusieurs user stories ({', '.join(multi_cover_tests)}) : "
                f"préfère une phase tests par US (fenêtre de contexte du testeur, périmètre muté). "
                f"Toléré, informatif."
            )
        # Brique B (informatif) : pas de 'mutation_cmd' alors que des phases 'tests' existent
        # → le contrôle que les tests MORDENT sera inactif. Toléré (la brique B est optionnelle).
        has_test_phase = any(isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower() == "tests"
                             for phase in phases)
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if has_test_phase and not has_mutation_cmd:
            soft.append(
                "Aucune 'mutation_cmd' déclarée alors que des phases 'tests' existent : la brique B "
                "(contrôle que les tests MORDENT) sera inactive. Toléré ; déclare-la dans le plan "
                "pour des tests falsifiables."
            )
    if not (blackboard.get("verify_cmd") or "").strip():
        fatal.append(
            "Commande de vérification globale 'verify_cmd' manquante : c'est le fallback des "
            "phases sans 'verify_cmd' propre ET le verrou de l'étape de scaffold. Sans elle, le "
            "scaffold est sauté et une phase sans commande dédiée ne peut pas être vérifiée."
        )
    return fatal, soft

def verdict_sentinel(phase_id: int, attempt: int) -> str:
    """Verdict du Vérificateur LLM de phase (première ligne : OK ou REJECTED)."""
    return f".phase_{phase_id}.attempt{attempt}.verdict"

def verify_and_fix_after_refacto(blackboard: dict, user_need: str, verify_cmd: str) -> tuple:
    """Re-vérifie la SUITE GLOBALE après le refacto ; en cas de régression, boucle de CORRECTION
    (feedback d'exécution → agent correcteur → re-vérification), bornée à MAX_ATTEMPTS.

    Renvoie (ok, output, timed_out, fixes) : 'fixes' = nombre de corrections tentées (0 si la
    suite passait déjà). Un timeout d'infra persistant n'est PAS traité comme une régression
    (aucune correction tentée) : il est remonté tel quel à l'appelant.
    """
    print("\n🧪 Revérification post-refacto (suite globale) : le polish ne doit pas avoir cassé le code...")
    ok, output, timed_out = run_verify_resilient(verify_cmd)
    # Une suite verte qui a PERDU des tests compte aussi comme une régression (même garde
    # §1.3 qu'en production) : le correcteur reçoit le feedback de compte au lieu d'une
    # sortie de runner.
    count_regression = test_count_regression(output, blackboard) if ok else None
    attempts = 0
    while (not ok or count_regression) and not timed_out and attempts < MAX_ATTEMPTS:
        attempts += 1
        cleanup_sentinels(REFACTO_FIX_PHASE_ID)
        print(f"\n🔧 [CORRECTION RÉGRESSION {attempts}/{MAX_ATTEMPTS}] Le refacto a cassé la suite — correction ciblée...")
        fix_prompt = build_refacto_fix_prompt(blackboard, user_need,
                                              count_regression or output, verify_cmd, attempts)
        RUNNER.new_context()
        mm_audit.event("agent_task", prompt_bytes=len(fix_prompt))
        RUNNER.send_task(fix_prompt)
        if not wait_for_file_creation(done_sentinel(REFACTO_FIX_PHASE_ID, attempts)):
            print("⏱️  Le correcteur n'a pas signalé la fin (sentinelle absente). Nouvelle tentative.")
            RUNNER.new_context()
            continue
        ok, output, timed_out = run_verify_resilient(verify_cmd)
        count_regression = test_count_regression(output, blackboard) if ok else None
    cleanup_sentinels(REFACTO_FIX_PHASE_ID)
    final_ok = ok and not count_regression
    if final_ok:
        record_test_count(output, blackboard)
    return final_ok, count_regression or output, timed_out, attempts

def wait_for_file_creation(filepath: str, timeout: int = MAX_PHASE_TIMEOUT) -> bool:
    """Attend qu'un fichier soit créé et stabilisé par l'agent dans le TUI."""
    start = time.time()
    print(f"   ⏳ En attente de la génération de '{filepath}'...")
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_init = os.path.getsize(filepath)
            time.sleep(1.5)  # Sécurité pour s'assurer que l'écriture est close
            if os.path.getsize(filepath) == size_init:
                return True
    return False
