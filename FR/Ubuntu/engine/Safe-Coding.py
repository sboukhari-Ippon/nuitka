#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à code avec un harness d'agent + tmux (Version Full TUI Data Center)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « VERDICT UNIVERSEL » (split feature / tests piloté par le plan).

Différence avec les variantes Agnostic Safe-Coding.py et Coding-Without-Tests.py (qui restent en Palier 1) :
  - Le verdict de TOUTE phase est la commande de vérification globale 'verify_cmd'
    (compilation + SUITE COMPLÈTE, déclarée par l'Architecte dans le plan). Le scaffold
    garantissant une suite non vide dès la phase 1, une régression introduite par
    N'IMPORTE QUELLE phase est détectée à CETTE phase, avec un feedback frais — plus
    besoin d'attendre la phase de tests finale. Une phase peut déclarer sa propre
    commande ('phases[].verify_cmd') en EXCEPTION rare.
  - Le prompt codeur est NEUTRE, piloté par le plan : il n'impose NI n'interdit les tests.
    L'agent ne fait que les tâches de SA phase (une autre phase peut être dédiée aux tests).

Pipeline PO → Architecte (nouveau) :
  - Étape 1 : un Agent PO affine 'need.md' en spécification métier 'spec.md' (user stories,
    critères d'acceptation testables, hors-périmètre, hypothèses), VALIDÉE par l'humain.
    Corriger le besoin coûte le moins cher ICI, avant de payer plan + blackboard + production.
  - Étape 2 : un Agent Architecte convertit 'spec.md' en plan d'implémentation où chaque
    phase déclare EXPLICITEMENT sa nature (feature/tests) et sa commande de vérification.
  - Étape 3 : la conversion en blackboard devient une RECOPIE mécanique de ces décisions
    (zéro inférence demandée au petit modèle, qui se contente de compiler le format).

Stratégie Data Center & TUI (inchangée) :
  - La session tmux est initialisée DIRECTEMENT au démarrage.
  - On lance directement le TUI du harness choisi (Modèle Cloud / Data Center).
  - Les étapes 1 (Spec PO), 2 (Plan) et 3 (Blackboard) sont exécutées directement dans le TUI.
  - Production : chaque phase passe par un Agent Codeur, puis l'orchestrateur EXÉCUTE
    lui-même la commande de vérification de la phase ; le code de sortie EST le verdict
    (brique A). Le codeur communique par fichier sentinelle ('.phase_<id>.attemptN.done') ;
    le seul maître du blackboard est l'orchestrateur Python (aucune écriture concurrente).

Risque résiduel assumé : le verdict prouve « rien n'est cassé », pas « la phase a fait
son travail » (un codeur sans effet passerait au vert). Garde-fous : contrôle anti
« codeur fantôme » (au moins un fichier déclaré dans la sentinelle doit avoir réellement
changé pendant la tentative) et preuve fonctionnelle a posteriori par les phases de
tests, dérivées des critères d'acceptation de la spec.
"""

import os
import re
import sys
import time
import signal
import subprocess
import shlex
import yaml

from mm_runner import resolve_runner, resolve_timeout

# Journal de run (boîte noire .mm-runs/, plan-big-last Lot 2) : purement additif,
# no-op intégral si MM_AUDIT=0, ne fait JAMAIS échouer un run.
import mm_audit

# Fonctions partagées extraites au Lot 4a (plan-big-last) : voir mm_core.py.
# La configuration (constantes/objets de CE module) est injectée en fin de
# fichier via mm_core.configure(...) — tous les noms y sont alors définis.
import mm_core
from mm_core import (
    build_coder_prompt, build_mutation_targets, build_skills_dictionary, cleanup_all_sentinels,
    cleanup_sentinels, collect_spec_us_ids, commit_phase, done_sentinel,
    ensure_executable_scaffold, ensure_phase_repo, fail_pipeline, files_changed_since_phase_start,
    git_head_sha, inject_skills_dictionary, is_test_file, load_blackboard,
    load_skills, mutation_tool_available, no_declared_file_touched, print_failure_message,
    read_touched_files, record_test_count, resolve_mutation_cmd, resolve_verify_cmd,
    run_mutation, run_verify_resilient, save_blackboard, signal_handler,
    test_count_regression, truncate_output, validate_all_skills, validate_blackboard_schema,
    verify_and_fix_after_refacto, wait_for_file_creation,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="factory")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
FAIL_REPORT_FILE      = "failReport.md"   # rapport d'arrêt persistant (volet D, §6.8)
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Skills système du pipeline : jamais routés vers les phases de production.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-to-blackboard", "refacto"}

# Fichiers temporaires de routage de contexte
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_REFACTO_FILE      = RUNNER.tmp_file("refacto")
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : c'est le
# seul choix valable sur les 3 OS (Windows n'a pas de /tmp), et l'unifier supprime le
# dernier diff de CODE entre les variantes d'une même langue (un script = une langue).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Sentinelles de fin des livrables du pipeline (étapes 1 à 3) : même contrat que la
# production (l'agent crée le .done APRÈS avoir sauvegardé le livrable). Remplace la
# détection « taille stable 1,5 s », qui pouvait lire un fichier à moitié écrit si
# l'agent marquait une pause entre deux écritures.
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"
PLAN_DONE_SENTINEL       = ".pipeline_plan.done"
BLACKBOARD_DONE_SENTINEL = ".pipeline_blackboard.done"
REFACTO_DONE_SENTINEL    = ".pipeline_refacto.done"

# Approbation HUMAINE de la spec, matérialisée : la simple EXISTENCE de spec.md ne prouve
# rien (un timeout peut laisser derrière lui une spec jamais validée, cf. fail_pipeline).
# Volontairement hors du motif '.pipeline_*.done' purgé par cleanup_all_sentinels :
# l'approbation doit survivre à une reprise.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Nom de la session tmux, suffixé d'une empreinte du répertoire du projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session (les prompts du
# projet B atterriraient dans l'agent du projet A). Reprendre le MÊME projet réutilise
# sa session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
REFACTO_FIX_PHASE_ID  = -1             # id de sentinelle dédié à la correction de régression post-refacto (≠ phases ≥1, ≠ scaffold 0)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par phase (filet de sécurité)
VERIFY_TIMEOUT        = resolve_timeout("verify", 300)            # 5 min max pour l'exécution de la commande de vérification
MAX_VERIFY_RETRIES_ON_TIMEOUT = 2      # re-vérifications immédiates sur timeout d'infra (le code n'a pas changé)
MAX_PHASE_VERIFY_TIMEOUTS     = 3      # timeouts persistants tolérés sur une phase avant arrêt « infra cassée »
MUTATION_TIMEOUT      = 300            # PRUDENT : budget borné du mutation testing (brique B). La brique B
                                       # ne rallonge JAMAIS le run sans borne ; tout dépassement dégrade en warn
MAX_PHASE_MUTATION_TIMEOUTS   = 2      # backstop anti-coût si l'outil/infra de mutation est durablement lent
SCAFFOLD_TIMEOUT      = 300            # 5 min : le scaffold est la tâche la plus courte du run — s'il
                                       # n'aboutit pas, c'est presque toujours le tool calling du modèle
                                       # qui est en cause, et un diagnostic rapide vaut mieux qu'une longue attente
VERIFY_FEEDBACK_LIMIT = 4000           # taille max du feedback de vérification renvoyé au codeur
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable pipeline accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes).
                                       # 30 s : un modèle local lent qui marque une pause entre deux écritures
                                       # ne doit pas voir son livrable à moitié écrit accepté (cf. structural_check aussi)

# PAS de bascule test/code dans ce script (contrairement à ses variantes Agnostic Safe-Coding.py /
# Coding-Without-Tests.py). La commande de vérification est TOUJOURS lue dans le champ
# "verify_cmd" : celui de la phase s'il est déclaré (exception rare), sinon le global.
# Le VERDICT UNIVERSEL (compilation + suite complète) est porté par le 'verify_cmd' global,
# déclaré par l'Agent Architecte et recopié par le compilateur blackboard, jamais par ce script.


# ─── SENTINELLES DE PHASE (CANAL CODEUR → ORCHESTRATEUR) ────────

def cleanup_pipeline_sentinel(sentinel: str):
    """Supprime une sentinelle de pipeline résiduelle (run précédent interrompu)."""
    try:
        os.remove(sentinel)
    except OSError:
        pass


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def spec_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'une spec acceptée SANS sentinelle : sa section
    obligatoire « Hors périmètre » doit être présente (une spec à moitié écrite s'arrête avant)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "hors périmètre" in f.read().lower()
    except OSError:
        return False


def plan_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'un plan accepté SANS sentinelle : le bloc d'en-tête
    obligatoire « Stack & Vérification » doit être présent."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "stack & vérification" in f.read().lower()
    except OSError:
        return False


def blackboard_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'un blackboard accepté SANS sentinelle : le YAML
    doit au moins se parser (un mapping à moitié écrit n'y parvient presque jamais)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) is not None
    except (OSError, yaml.YAMLError):
        return False


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Attend un livrable du pipeline (spec/plan/blackboard) signalé par SENTINELLE.

    Même contrat que la production : l'agent crée un fichier .done APRÈS avoir sauvegardé
    le livrable — signal sans ambiguïté, robuste aux pauses d'écriture (l'heuristique
    « taille stable 1,5 s » seule pouvait accepter un fichier à moitié écrit si l'agent
    marquait une pause entre deux écritures). FILET pour un agent qui oublie la sentinelle :
    si le livrable existe, est non vide et n'a plus bougé depuis STABLE_POLLS_FALLBACK
    contrôles consécutifs, on l'accepte avec avertissement (dégradation gracieuse — ne
    jamais bloquer 10 minutes pour un simple oubli de signal). Le 'structural_check'
    optionnel ne durcit QUE ce filet : un livrable stable mais structurellement incomplet
    continue d'attendre (l'agent peut marquer une pause plus longue que la fenêtre de
    stabilité) jusqu'au timeout global.
    """
    start = time.time()
    print(f"   ⏳ En attente de '{filepath}' (signal de fin : '{sentinel}')...")
    stable_streak = 0
    last_size = -1
    structural_warned = False
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        file_ready = os.path.exists(filepath) and os.path.getsize(filepath) > 0
        if file_ready and os.path.exists(sentinel):
            cleanup_pipeline_sentinel(sentinel)
            return True
        if file_ready:
            size = os.path.getsize(filepath)
            stable_streak = stable_streak + 1 if size == last_size else 0
            last_size = size
            if stable_streak >= STABLE_POLLS_FALLBACK:
                if structural_check is not None and not structural_check(filepath):
                    if not structural_warned:
                        print(f"   ⏳ '{filepath}' est stable mais structurellement incomplet : "
                              f"on continue d'attendre (l'agent écrit peut-être encore).")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinelle '{sentinel}' absente mais '{filepath}' est stable depuis "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s : livrable accepté (filet de secours).")
                return True
    return False


# ─── LECTURE / ÉCRITURE BLACKBOARD ────────────────────────────────────────────

# Derniers statuts de phase journalisés (détection des TRANSITIONS par save_blackboard).
_PHASE_STATUS_SEEN = {}


def check_need_file():
    if not os.path.exists(NEED_FILE):
        print(f"❌ Erreur critique : '{NEED_FILE}' est manquant.")
        write_fail_report("Fichier de besoins manquant", f"'{NEED_FILE}' est manquant à la racine du projet.")
        sys.exit(1)
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print(f"❌ Erreur critique : '{NEED_FILE}' est vide.")
        write_fail_report("Fichier de besoins vide", f"'{NEED_FILE}' est présent mais vide.")
        sys.exit(1)
    print("✓ Validation du fichier de besoins (need.md) : OK")


# ─── GARDE-FOUS GIT (BEST-EFFORT) ─────────────────────────────────────────────
# Tout ici est BEST-EFFORT : sans git (binaire absent, échec d'init), l'usine tourne
# à l'identique mais SANS garde-fous mécaniques — dégradation gracieuse, ne jamais
# bloquer le run pour de l'outillage. Ce que git apporte, dans l'esprit « Python
# vérifie ce qui est vérifiable » : un commit par phase verte (diff par phase →
# détection mécanique du sabotage de tests), un point de rollback pour le refacto
# final, et une piste d'audit (spec/plan/blackboard SONT committés).

_GIT = {"enabled": False}

# Identité passée à chaque commande : l'usine ne doit pas dépendre de la config git de la machine.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

GITIGNORE_BODY = f"""# Artefacts d'orchestration MAIster-Mind (éphémères)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.fix_*
.spec_approved
.mm-runs/
__pycache__/
"""


def run_git(args: list, timeout: int = 60) -> tuple:
    """Exécute une commande git. Renvoie (ok, stdout strippé). Ne lève jamais."""
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


# ─── INTÉGRITÉ DE LA SUITE DE TESTS (GARDE-FOUS §1.3 BEST-EFFORT) ─────────────

_TEST_COUNT = {"warned": False}


# ─── EXÉCUTION DE LA VÉRIFICATION (BRIQUE A : EXÉCUTION = VERDICT) ─────────────

# ─── BRIQUE B : MUTATION TESTING CIBLÉ (LA SUITE MORD-ELLE ?) ─────────────────
# Extension de la brique A : le verdict universel prouve « rien n'est cassé » ; la brique B
# prouve « la suite ROUGIT quand le code est faux » (tests falsifiables). Mécanique de bout en
# bout — le code de sortie de l'outil de mutation EST le verdict, aucun LLM ne juge. Pilotée par
# l'Architecte via un champ 'mutation_cmd' OPTIONNEL ; absente → brique inactive (run identique à
# aujourd'hui). Dégradation gracieuse partout (outil absent / timeout → warn, jamais de blocage).

# Fichiers appartenant à l'ORCHESTRATEUR lui-même (jamais du code produit par le codeur) :
# prompts tampons, livrables du pipeline, blackboard, sentinelles, caches Python, venv, configs
# d'agents et le script MAIsterMind. Ils sont réécrits à chaque phase ; AUCUNE garde basée sur
# 'git diff' ne doit les compter comme « code de production modifié » ni les restaurer
# (git checkout) — sinon l'usine sabote son propre état, voire son propre script, et aucune
# phase 'tests' ne converge (cause d'un rejet systématique quand ces artefacts sont suivis par
# git, p. ex. un dépôt humain dont le .gitignore ne les couvrait pas). Volontairement LARGE :
# en cas de doute on protège (au pire on rate un faux « code touché » sur un fichier
# d'orchestration, jamais sur du vrai code produit).
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE, FAIL_REPORT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


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
    # Sentinelles et tampons éphémères, où qu'ils se trouvent dans l'arbre ('.fix_*' :
    # sentinelles de Guided-Fix.py, même famille).
    if base.startswith(".phase_") or base.startswith(".pipeline_") or base.startswith(".fix_"):
        return True
    # Rapports d'arbitrage de Guided-Fix.py : livrables d'orchestration COMMITTÉS
    # comme piste d'audit (même statut que spec/plan/blackboard) — jamais du code
    # produit. Sans ce motif, ils entreraient dans le périmètre du refacto final et
    # seraient comptés « code touché » par les gardes en diff.
    if base.startswith("fix_report-") and base.endswith(".md"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Caches Python, environnement virtuel et répertoires d'outillage : jamais du code produit.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


# ─── VALIDATION DU SCHÉMA DU BLACKBOARD (PRODUIT PAR UN PETIT LLM FAILLIBLE) ───

REQUIRED_GLOBAL_RULES = ["target", "styling", "constraints", "accessibility"]


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
            # Décisions de l'architecte transportées depuis le plan (vides sur les anciens
            # blackboards : le prompt codeur retombe alors sur sa formulation neutre).
            phase.setdefault("nature", "")
            phase.setdefault("context", "")
            phase.setdefault("files_to_read", [])


# ─── DÉCOUPE DE LA SPEC PAR PHASE (FENÊTRE DE CONTEXTE) ───────────────────────
# L'ancienne heuristique « couverture anti-régression » (deviner sur chaîne libre si la
# dernière phase de tests lançait la suite complète) est SUPPRIMÉE : le verdict universel
# (toute phase = compilation + suite complète) rend la couverture structurelle.

# En-tête d'une user story dans la spec PO (ex. « ### US-1 : Calcul du solde »).
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Tranche de la spec limitée aux US couvertes par la phase (+ tout le hors-US).

    Le prompt codeur embarquait la spec ENTIÈRE à chaque phase : sur une grosse spec,
    chaque phase payait tout le contexte. On ne garde ici que les sections '### US-n'
    listées dans 'covers' (champ recopié du plan par le compilateur blackboard), plus
    tout ce qui n'est pas une section d'US (objectif métier, contraintes, hors-périmètre,
    hypothèses). Prudence de petit modèle : si 'covers' est vide, si la spec ne suit pas
    le format à US, ou si aucune US couverte n'y est trouvée, on renvoie la spec ENTIÈRE
    (dégradation gracieuse — ne jamais priver le codeur de contexte par excès de zèle).
    """
    wanted = {c.strip().upper() for c in (covers or [])
              if isinstance(c, str) and c.strip()}
    if not wanted:
        return spec_text
    spec_us_ids = collect_spec_us_ids(spec_text)
    if not spec_us_ids or not (wanted & spec_us_ids):
        return spec_text
    kept = []
    current_us = None  # id de la section US en cours, None = tronc commun
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            current_us = match.group(1).upper()
        elif current_us is not None and line.startswith("## "):
            current_us = None  # fin de la zone US : retour au tronc commun
        if current_us is None or current_us in wanted:
            kept.append(line)
    return "\n".join(kept)


def check_spec_coverage(blackboard: dict, spec_text: str) -> list:
    """AVERTISSEMENTS (non bloquants) de traçabilité spec → phases via 'covers'.

    Deux directions :
      - une phase référence une US absente de la spec : hallucination probable du
        compilateur blackboard (même famille que les skills hallucinés) ;
      - une US de la spec n'est couverte par aucune phase : exigence potentiellement
        OUBLIÉE par l'Architecte — c'est l'avertissement le plus précieux.
    Warn-only : 'covers' est optionnel et la spec peut ne pas suivre le format à US ;
    c'est l'œil humain au y/n qui tranche.
    """
    spec_us = collect_spec_us_ids(spec_text)
    if not spec_us:
        return []
    referenced = set()
    for phase in blackboard.get("phases", []) or []:
        if not isinstance(phase, dict):
            continue
        for item in phase.get("covers", []) or []:
            if isinstance(item, str) and item.strip():
                referenced.add(item.strip().upper())
    warnings = []
    unknown = sorted(referenced - spec_us)
    if unknown:
        warnings.append(f"US référencées par des phases mais ABSENTES de la spec : "
                        f"{', '.join(unknown)} (hallucination probable du compilateur).")
    uncovered = sorted(spec_us - referenced)
    if uncovered:
        warnings.append(f"US de la spec couvertes par AUCUNE phase : {', '.join(uncovered)} "
                        f"(exigence oubliée par l'Architecte ? Vérifie le plan).")
    return warnings


# ─── DICTIONNAIRE DYNAMIQUE DES SKILLS ────────────────────────────────────────

def parse_skill_frontmatter(skill_path: str) -> tuple:
    """Extrait (name, description) du frontmatter YAML d'un SKILL.md."""
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None, None
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return meta.get("name"), meta.get("description")
            except yaml.YAMLError:
                pass
    return None, None


# ─── ETAPES INTERACTIVES 1 À 3 DANS LE TUI (CLOUD) ────────────────────────────

def generate_spec_from_need_tui():
    print("\n📖 [ETAPE 1 : AGENT PO] Affinage du besoin en spécification métier dans le TUI Cloud...")

    if not os.path.exists(PO_SKILL_FILE):
        fail_pipeline(f"❌ Skill PO manquant : '{PO_SKILL_FILE}'")
    with open(PO_SKILL_FILE, "r", encoding="utf-8") as f:
        po_spec = f.read()
    with open(TMP_PO_FILE, "w", encoding="utf-8") as f:
        f.write(po_spec)

    po_prompt = f"""Lis le fichier '{NEED_FILE}' à la racine de notre projet, ainsi que les consignes de Product Owner du fichier '{TMP_PO_FILE}'.
Tu es un Product Owner Senior. En appliquant SCRUPULEUSEMENT les consignes de '{TMP_PO_FILE}', affine le besoin brut en une spécification métier et sauvegarde-la DIRECTEMENT dans un nouveau fichier nommé '{SPEC_FILE}' à la racine du projet.

Directives pour le fichier '{SPEC_FILE}' :
- Zéro invention : chaque exigence doit découler du besoin exprimé dans '{NEED_FILE}'.
- Chaque user story porte des critères d'acceptation TESTABLES (Étant donné / Quand / Alors).
- Toute ambiguïté du besoin devient une hypothèse explicite dans « Hypothèses & Questions ».
- La section « Hors périmètre » est obligatoire (verrou anti sur-ingénierie).
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.
En toute DERNIÈRE action, après avoir sauvegardé '{SPEC_FILE}', crée le fichier sentinelle '{SPEC_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le signal de fin pour l'orchestrateur.
"""
    cleanup_pipeline_sentinel(SPEC_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(po_prompt))
    RUNNER.send_task(po_prompt)

    if wait_for_pipeline_file(SPEC_FILE, SPEC_DONE_SENTINEL, structural_check=spec_structural_check):
        print(f"✅ [ETAPE 1] Spécification '{SPEC_FILE}' créée avec succès !")
    else:
        fail_pipeline(f"❌ [ETAPE 1] Timeout ou échec de création de '{SPEC_FILE}'.")


def confirm_spec_with_human():
    """Validation humaine de la spec (human-in-the-loop AMONT).

    C'est ici que corriger coûte le moins cher : une exigence mal comprise rejetée à ce
    stade évite de payer (et de refaire) un plan, un blackboard et des phases de production.
    L'humain peut éditer la spec dans un autre terminal avant de valider.
    """
    print(f"\n{'='*50}")
    print(f"📋 SPÉCIFICATION PRÊTE : relis '{SPEC_FILE}' (hypothèses et hors-périmètre en priorité).")
    print(f"   Tu peux la modifier directement dans un autre terminal avant de valider.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Valider la spécification et lancer l'architecture ? (y/n) : ")
    mm_audit.event("gate", id="spec", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Annulé par l'utilisateur. Précise '{NEED_FILE}', supprime '{SPEC_FILE}', puis relance.")
        RUNNER.kill()
        sys.exit(0)
    # L'approbation est MATÉRIALISÉE (pas déduite de l'existence du fichier) : à la reprise,
    # une spec sans cette sentinelle repasse par le y/n au lieu d'être tenue pour validée.
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # copie figée de la spec TELLE QU'APPROUVÉE


def generate_plan_from_need_tui():
    print("\n📖 [ETAPE 2 : AGENT ARCHITECTE] Génération du plan d'implémentation dans le TUI Cloud...")

    if not os.path.exists(PLAN_SKILL_FILE):
        fail_pipeline(f"❌ Skill de planification manquant : '{PLAN_SKILL_FILE}'")
    with open(PLAN_SKILL_FILE, "r", encoding="utf-8") as f:
        plan_spec = f.read()
    # Le catalogue RÉEL des skills va à l'Architecte : la décision de routage (le champ
    # Skill de chaque phase) appartient à l'agent qui a tout le contexte du plan, puis
    # est recopiée mécaniquement en aval par le compilateur blackboard.
    plan_spec = inject_skills_dictionary(plan_spec)
    with open(TMP_PLAN_FILE, "w", encoding="utf-8") as f:
        f.write(plan_spec)

    print("   📚 Skills détectés et proposés à l'architecte :")
    for line in (build_skills_dictionary().splitlines() or ["(aucun skill de phase détecté)"]):
        print(f"      {line}")

    planning_prompt = f"""Lis le fichier '{SPEC_FILE}' à la racine de notre projet (spécification métier validée), ainsi que les consignes d'architecture du fichier '{TMP_PLAN_FILE}'.
Tu es un Architecte Logiciel senior. En appliquant SCRUPULEUSEMENT les consignes de '{TMP_PLAN_FILE}', génère un plan d'implémentation séquentiel au format Markdown et sauvegarde-le DIRECTEMENT dans un nouveau fichier nommé '{PLAN_FILE}' à la racine du projet.

Directives pour le fichier '{PLAN_FILE}' :
- Le plan DOIT commencer par le bloc « Stack & Vérification » (avec la commande de vérification du VERDICT UNIVERSEL : compilation + suite complète) et CHAQUE phase DOIT déclarer sa Nature (feature/tests) et son champ « Couvre » (US-x) : les étapes suivantes du pipeline recopient ces décisions sans les déduire.
- Découpe la spécification en micro-phases BORNÉES (1 à 5 tâches, au plus 5 fichiers créés/modifiés, au plus 3 fichiers à lire par phase) ; la fourchette indicative de 3 à 12 phases cède toujours devant ces bornes de taille. N'ajoute aucune phase pour une exigence absente de '{SPEC_FILE}'.
- Principe YAGNI : ne planifie QUE ce que la spécification demande ; sa section « Hors périmètre » est une interdiction.
- Checklists unitaires précises, stack claire.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.
En toute DERNIÈRE action, après avoir sauvegardé '{PLAN_FILE}', crée le fichier sentinelle '{PLAN_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le signal de fin pour l'orchestrateur.
"""
    cleanup_pipeline_sentinel(PLAN_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(planning_prompt))
    RUNNER.send_task(planning_prompt)

    if wait_for_pipeline_file(PLAN_FILE, PLAN_DONE_SENTINEL, structural_check=plan_structural_check):
        print(f"✅ [ETAPE 2] Plan '{PLAN_FILE}' créé avec succès !")
    else:
        fail_pipeline(f"❌ [ETAPE 2] Timeout ou échec de création de '{PLAN_FILE}'.")


def transform_plan_to_blackboard_tui():
    if not os.path.exists(BLACKBOARD_SKILL_FILE):
        fail_pipeline(f"❌ Skill de compilateur blackboard manquant : '{BLACKBOARD_SKILL_FILE}'")

    print("\n📖 [ETAPE 3 : COMPILATEUR BLACKBOARD] Génération du Blackboard YAML dans le TUI Cloud...")

    # Le compilateur RECOPIE les décisions du plan (dont le Skill de chaque phase) : le
    # dictionnaire des skills va désormais à l'Architecte (étape 2), pas ici. Le filet
    # Python validate_all_skills attrape toujours les mots-clés hallucinés en aval.
    with open(BLACKBOARD_SKILL_FILE, "r", encoding="utf-8") as f:
        compiler_spec = f.read()
    with open(TMP_ARCHITECT_FILE, "w", encoding="utf-8") as f:
        f.write(compiler_spec)

    prompt = f"""Tu es un Compilateur Blackboard : tu RECOPIES les décisions du plan, tu n'en prends aucune. Lis le plan qui vient d'être généré dans '{PLAN_FILE}' ainsi que les consignes de structure du fichier '{TMP_ARCHITECT_FILE}'.
Génère le fichier '{BLACKBOARD_FILE}' à la racine de notre projet en respectant scrupuleusement le format YAML demandé.

Écris directement le YAML propre dans le fichier '{BLACKBOARD_FILE}', sans l'enrober de balises markdown de type ```yaml.
En toute DERNIÈRE action, après avoir sauvegardé '{BLACKBOARD_FILE}', crée le fichier sentinelle '{BLACKBOARD_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le signal de fin pour l'orchestrateur.
"""
    cleanup_pipeline_sentinel(BLACKBOARD_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)

    if wait_for_pipeline_file(BLACKBOARD_FILE, BLACKBOARD_DONE_SENTINEL,
                              structural_check=blackboard_structural_check):
        try:
            parsed_data = load_blackboard()
            print(f"🏆 [ETAPE 3] Blackboard initialisé et validé dans '{BLACKBOARD_FILE}' !\n")
            return parsed_data
        except Exception as err:
            fail_pipeline(f"❌ [ETAPE 3] Échec du parsing du YAML : {err}")
    else:
        fail_pipeline(f"❌ [ETAPE 3] Timeout ou échec de création de '{BLACKBOARD_FILE}'.")


# ─── ÉTAPES 4 & 5 : PROMPTS DÉPORTÉS PAR FICHIER ──────────────────────────────

# ─── MESSAGE D'ÉCHEC ──────────────────────────────────────────────────────────


def write_fail_report(title: str, reason: str, blackboard: dict = None, details: str = ""):
    """Écrit un rapport d'arrêt persistant à la racine (volet D, §6.8). Best-effort : ne lève JAMAIS.

    Tout arrêt NON nominal du run (chaque sys.exit(1)) en produit un : cause, avancement
    (phases validées vs restantes) et action recommandée survivent au message console (volatile),
    ce qui est précieux pour un run long et non surveillé. Réservé aux arrêts RÉELS : les
    dégradations gracieuses de la brique B (outil de mutation absent, timeout, mutants restants)
    n'arrêtent PAS le run et n'écrivent donc PAS de rapport.
    """
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind", "", f"## {title}", "", "### Cause", reason.strip(), ""]
        if isinstance(blackboard, dict) and isinstance(blackboard.get("phases"), list):
            phases = blackboard["phases"]
            done = sum(1 for p in phases if isinstance(p, dict)
                       and p.get("status") == "DONE" and p.get("verdict") == "OK")
            lines.append("### Avancement")
            lines.append(f"- Phases validées : {done}/{len(phases)}")
            for p in phases:
                if not isinstance(p, dict):
                    continue
                ok = p.get("status") == "DONE" and p.get("verdict") == "OK"
                mark = "✅" if ok else "⏳"
                lines.append(f"  - {mark} Phase {p.get('id', '?')} : {p.get('name', '(sans nom)')} "
                             f"[{p.get('status', '?')}/{p.get('verdict', '?')}]")
            lines.append("")
        if details.strip():
            lines.append("### Détails")
            lines.append(truncate_output(details))
            lines.append("")
        lines.append("### Action recommandée")
        lines.append("Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model ou "
                     f"'{AGENT_CONFIG_FILE}'), puis relance : les phases déjà validées seront "
                     "reprises automatiquement.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Rapport d'échec écrit dans '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── ÉTAPE DE SCAFFOLD (SQUELETTE EXÉCUTABLE + TEST SANTÉ) ────────────────────

# ─── BOUCLE DE PRODUCTION PRINCIPALE ──────────────────────────────────────────

def run_production_phases(blackboard: dict, user_need: str, need_is_spec: bool = False):
    total = len(blackboard["phases"])

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} déjà validée : {phase['name']}")
            continue

        print(f"\n{'='*50}\n🛠️  PHASE {phase['id']}/{total} : {phase['name']}\n{'='*50}")

        skills_context = load_skills(phase.get("skills_required", []))
        loaded = [s for s in phase.get("skills_required", []) if os.path.exists(os.path.join(SKILLS_DIR, s, "SKILL.md"))]
        if loaded:
            print(f"   📦 Skills chargés : {', '.join(loaded)}")

        # Fenêtre de contexte : le codeur ne reçoit que la tranche de spec couverte par
        # SA phase (champ 'covers'), jamais la spec entière — sauf dégradation gracieuse.
        phase_need = extract_spec_slice(user_need, phase.get("covers")) if need_is_spec else user_need
        if need_is_spec and len(phase_need) < len(user_need):
            print(f"   ✂️  Spec tranchée pour la phase : {len(phase_need)}/{len(user_need)} caractères "
                  f"(couvre {', '.join(phase.get('covers', []))}).")

        verify_cmd = resolve_verify_cmd(phase, blackboard)
        if not verify_cmd:
            print(f"❌ Phase {phase['id']} : aucune commande de vérification "
                  f"('verify_cmd' de phase ou globale). Corrige '{BLACKBOARD_FILE}' puis relance.")
            write_fail_report(
                f"Phase {phase['id']} « {phase['name']} » sans commande de vérification",
                f"Ni la phase ni le blackboard ne déclarent de 'verify_cmd' : impossible de vérifier "
                f"cette phase. Corrige '{BLACKBOARD_FILE}' puis relance.",
                blackboard)
            RUNNER.kill()
            sys.exit(1)

        # ── REVALIDATION POST-FIX (handshake avec Guided-Fix.py) ── : une phase
        # 'FIXED' a été réparée et amenée au vert par Guided-Fix.py après arbitrage
        # humain (régression corrigée ou évolution entérinée). fix.py ne tamponne JAMAIS
        # DONE/OK lui-même — c'est une RÉCLAMATION, pas un verdict : l'orchestrateur
        # reste l'unique autorité et RE-EXÉCUTE la vérification ici. Vert → validée SANS
        # relancer de codeur (rejouer une phase déjà complète pousserait l'agent à des
        # modifications gratuites pour satisfaire la garde anti-fantôme). Rouge → la
        # phase retombe dans la boucle normale ci-dessous, la sortie fraîche en premier
        # feedback. La comptabilité annexe (last_test_count, protected_test_files d'une
        # phase 'tests') et le COMMIT du travail réparé sont déjà tenus par fix.py (un
        # fix non committé serait pris pour le travail de la phase suivante par les
        # gardes en diff HEAD, et restauré).
        fix_recheck_feedback = ""
        if str(phase.get("status") or "").strip().upper() == "FIXED":
            print(f"🔁 Phase {phase['id']} marquée 'FIXED' par Guided-Fix.py : revalidation par exécution...")
            recheck_ok, recheck_output, recheck_timed_out = run_verify_resilient(verify_cmd)
            if recheck_ok:
                record_test_count(recheck_output, blackboard)
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCÈS] Phase {phase['id']} revalidée : la vérification passe (code de sortie 0).")
                commit_phase(f"phase {phase['id']}: {phase['name']} (revalidee post-fix)")
                continue
            if recheck_timed_out:
                print(f"🛑 [TIMEOUT INFRA] La revalidation de la phase {phase['id']} expire de façon "
                      f"répétée : incident d'INFRASTRUCTURE, pas un échec du code. Le marqueur "
                      f"'FIXED' est conservé : vérifie la machine ou la commande, puis relance.")
                write_fail_report(
                    f"Revalidation post-fix de la phase {phase['id']} expirée",
                    f"La commande « {verify_cmd} » expire de façon répétée pendant la revalidation "
                    f"post-fix : incident d'infrastructure, pas un échec du code. Le marqueur "
                    f"'FIXED' est conservé — corrige l'environnement puis relance.",
                    blackboard)
                RUNNER.kill()
                sys.exit(1)
            print(f"⚠️  La revalidation de la phase {phase['id']} échoue : l'état réparé ne passe "
                  f"plus (environnement différent ou code modifié depuis la réparation). La phase "
                  f"repart en boucle de production normale avec cette sortie comme premier feedback.")
            fix_recheck_feedback = recheck_output

        attempts = 0
        verify_timeouts = 0
        mutation_timeouts = 0       # brique B : timeouts de mutation sur cette phase (backstop coût)
        mutation_hardening_used = 0 # brique B : passes de durcissement consommées (bornées à 1)
        success  = False
        critic_feedback = fix_recheck_feedback or "Premier jet — aucune critique précédente."
        # Décision de l'Architecte (copiée depuis le plan) : pilote la garde sur les fichiers de test ci-dessous.
        nature = str(phase.get("nature") or "").strip().lower()
        # Jalon pour le diff par phase (3c) : vide sans git.
        phase_start_sha = git_head_sha()
        # Référentiel temporel de la PHASE (fallback mtime de la garde anti-fantôme), capturé
        # UNE seule fois ici. Surtout pas par tentative : un référentiel par-tentative
        # reclassait à tort en « fantôme » un fichier écrit lors d'une tentative précédente.
        phase_started = time.time()

        phase["status"]  = "IN_PROGRESS"
        phase["verdict"] = "PENDING"
        save_blackboard(blackboard)
        cleanup_sentinels(phase["id"])

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1
            cleanup_sentinels(phase["id"])
            print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Phase {phase['id']} — lancement de l'Agent Codeur...")

            coder_prompt = build_coder_prompt(phase, blackboard, phase_need, skills_context, critic_feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(coder_prompt))
            RUNNER.send_task(coder_prompt)

            if not wait_for_file_creation(done_sentinel(phase["id"], attempts)):
                print(f"⏱️  Le codeur n'a pas signalé la fin (sentinelle '{done_sentinel(phase['id'], attempts)}' absente). Nouvelle tentative.")
                RUNNER.new_context()
                continue

            touched_files = read_touched_files(phase["id"], attempts)

            # ── GARDE ANTI « CODEUR FANTÔME » ── : la suite complète reste verte si l'agent
            # n'a RIEN fait ; le verdict seul ne peut donc pas distinguer « rien cassé » de
            # « rien fait ». Si aucun fichier déclaré n'a changé DEPUIS LE DÉBUT DE LA PHASE,
            # on rejette AVANT de payer une vérification. Référentiel = phase (pas tentative) :
            # un fichier produit à une tentative et re-déclaré inchangé ensuite reste du travail réel.
            changed_in_phase = files_changed_since_phase_start(phase_start_sha)
            if no_declared_file_touched(touched_files, phase_started, changed_in_phase):
                critic_feedback = (
                    f"Ta sentinelle déclare {len(touched_files)} fichier(s), mais AUCUN n'a "
                    "réellement été créé ou modifié depuis le début de cette phase. Réalise "
                    "CONCRÈTEMENT les tâches de la checklist (crée/modifie les fichiers), puis "
                    "seulement recrée la sentinelle avec la liste réelle des fichiers touchés."
                )
                phase["critic_feedback"] = critic_feedback
                save_blackboard(blackboard)
                print(f"👻 [REJET] Tentative {attempts} : sentinelle écrite mais aucun fichier "
                      f"déclaré n'a été touché (codeur fantôme).")
                mm_audit.event("guard", name="codeur_fantome", action="rejet")
                RUNNER.new_context()
                continue

            # ── PROTECTION DES FICHIERS DE TEST (garde mécanique §1.3, best-effort) ── :
            # les fichiers produits par les phases 'tests' vertes sont hors limites pendant
            # les phases 'feature'. L'interdiction par prompt seul est invérifiable ; ce
            # diff ne l'est pas. Faux positif connu (un helper de test légitimement
            # partagé) : le feedback nomme les fichiers, l'humain arbitre.
            if nature == "feature" and _GIT["enabled"]:
                protected = set(blackboard.get("protected_test_files") or [])
                if protected:
                    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                    touched_protected = sorted(set(diff_out.splitlines()) & protected) if ok_diff else []
                    if touched_protected:
                        run_git(["checkout", "--"] + touched_protected)
                        critic_feedback = (
                            f"Tu as modifié des fichiers de test PROTÉGÉS pendant une phase 'feature' : "
                            f"{', '.join(touched_protected)}. Ils ont été restaurés. Les fichiers de test "
                            f"sont hors limites en phase feature : implémente la checklist de cette "
                            f"phase sans y toucher."
                        )
                        phase["critic_feedback"] = critic_feedback
                        save_blackboard(blackboard)
                        print(f"🛡️  [REJET] Tentative {attempts} : fichiers de test protégés modifiés "
                              f"({', '.join(touched_protected)}) — restaurés.")
                        mm_audit.event("guard", name="tests_proteges",
                                       action="restauration", files=len(touched_protected))
                        RUNNER.new_context()
                        continue

            # ── GARDE TESTS-ONLY (miroir de protected_test_files, best-effort, §6.6) ── :
            # une phase 'tests' ne modifie QUE des fichiers de test ; le code de production est
            # GELÉ. Tout fichier de prod touché est restauré (git checkout) et la tentative
            # rejetée. Placée AVANT la vérification (comme protected_test_files) : on attrape la
            # triche que la tentative finisse verte OU rouge, et on évite un verify gaspillé sur
            # un état qu'on va rejeter. Anti-triche (le testeur ne peut pas bidouiller la prod pour
            # faire passer ses tests) ET socle de la brique B (on mute une prod stable). Caveat
            # tranché : un vrai bug de prod révélé par un test fait CALER la phase, à charge de
            # l'humain (pas de correction de prod en douce par le testeur).
            if nature == "tests" and _GIT["enabled"]:
                ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                # Exclut les fichiers de l'orchestrateur lui-même (prompts, blackboard,
                # sentinelles, .pyc, son propre script…), qu'il réécrit à chaque phase : les
                # compter comme « code de prod modifié » rejetterait TOUTE tentative tests et,
                # pire, leur restauration (git checkout ci-dessous) saboterait l'état — voire le
                # script — de l'orchestrateur. Cf. is_orchestration_file.
                touched_prod = sorted(f for f in diff_out.splitlines()
                                      if f.strip() and not is_test_file(f.strip())
                                      and not is_orchestration_file(f.strip())) if ok_diff else []
                if touched_prod:
                    run_git(["checkout", "--"] + touched_prod)
                    critic_feedback = (
                        f"En phase 'tests', tu ne touches QU'AUX fichiers de test. Tu as modifié du "
                        f"code de production : {', '.join(touched_prod)}. Ces fichiers ont été "
                        f"restaurés. Si un test révèle un vrai bug de production, NE le corrige pas : "
                        f"laisse la vérification échouer (un humain tranchera)."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJET] Tentative {attempts} : code de production modifié en phase "
                          f"'tests' ({', '.join(touched_prod)}) — restauré.")
                    RUNNER.new_context()
                    continue

            print(f"  → Codeur terminé ({len(touched_files)} fichier(s) déclaré(s)). Vérification par EXÉCUTION...")

            # ── BRIQUE A : le verdict EST le code de sortie. ──
            # Python exécute lui-même la commande ; aucun LLM ne juge la complétude
            # fonctionnelle. Signal objectif que ni le codeur ni un vérificateur ne
            # peuvent halluciner. Un TIMEOUT n'est PAS un verdict rouge (cf. branche dédiée).
            is_ok, output, verify_timed_out = run_verify_resilient(verify_cmd)

            if is_ok:
                # ── COMPTE DE TESTS NON DÉCROISSANT (garde mécanique §1.3, best-effort) ── :
                # une suite verte qui a PERDU des tests est une suite affaiblie, pas un succès.
                count_regression = test_count_regression(output, blackboard)
                if count_regression:
                    critic_feedback = count_regression
                    phase["critic_feedback"] = count_regression
                    save_blackboard(blackboard)
                    print(f"🛡️  [REJET] Tentative {attempts} : suite verte mais le compte de tests "
                          f"passants a DIMINUÉ.")
                    RUNNER.new_context()
                    continue

                # ── BRIQUE B : la suite MORD-elle ? (mutation testing ciblé, §6.4) ── :
                # la suite est verte ; on prouve maintenant qu'elle ROUGIT quand le code est faux.
                # N'agit que sur 'tests', et seulement après une suite verte (muter un code aux
                # tests rouges n'a aucun sens). Verdict = code de sortie de l'outil ; aucun LLM ne
                # juge. Dégradation gracieuse partout (outil absent / timeout → warn, jamais de rejet
                # ni d'arrêt). UNE seule passe de durcissement par phase (§5 pt 6) : au-delà, on
                # valide et on signale (ne pas s'acharner sur un petit modèle qui ne sait pas durcir).
                if nature == "tests":
                    mcmd = resolve_mutation_cmd(phase, blackboard)
                    targets = build_mutation_targets(phase)
                    if not mcmd:
                        print("ℹ️  Brique B inactive (pas de 'mutation_cmd' déclarée).")
                    elif "{targets}" in mcmd and not targets:
                        print("⚠️  Brique B : aucune cible mutable (files_to_read vide ou introuvable) — sautée.")
                    elif not mutation_tool_available(mcmd):
                        print("⚠️  Brique B : outil de mutation introuvable — sautée (dégradation gracieuse).")
                    else:
                        run_cmd = mcmd.replace("{targets}", " ".join(shlex.quote(t) for t in targets)) if "{targets}" in mcmd else mcmd
                        print("🧬 Brique B : la suite passe — on vérifie qu'elle MORD (mutation ciblée)...")
                        mut_started = time.time()
                        ok_mut, mout, mut_timed_out = run_mutation(run_cmd)
                        print(f"   ⏱️  Brique B : mutation terminée en {time.time() - mut_started:.0f}s.")
                        if mut_timed_out:
                            mutation_timeouts += 1
                            print(f"⏱️  Brique B : mutation expirée ({MUTATION_TIMEOUT}s) — ignorée "
                                  f"({mutation_timeouts}/{MAX_PHASE_MUTATION_TIMEOUTS}), phase validée sur "
                                  f"le verdict universel. On NE relance PAS le codeur (la suite est verte, "
                                  f"seul l'outil a calé) : dégradation gracieuse, run jamais rallongé sans borne.")
                        elif not ok_mut and mutation_hardening_used < 1:
                            mutation_hardening_used += 1
                            critic_feedback = (
                                "La suite PASSE mais ne MORD pas : des mutants ont survécu (tests creux). "
                                "Renforce les ASSERTIONS pour tuer ces mutations (teste les bornes, les "
                                "valeurs de retour, les branches), sans ajouter d'I/O ni de test trivial :\n"
                                + truncate_output(mout))
                            phase["critic_feedback"] = critic_feedback
                            save_blackboard(blackboard)
                            print(f"🧬 [REJET] Tentative {attempts} : des mutants survivent — "
                                  f"durcissement des tests demandé (passe unique).")
                            RUNNER.new_context()
                            continue
                        elif not ok_mut:
                            print("⚠️  Brique B : des mutants survivent encore après 1 passe — on valide et "
                                  "on signale (le modèle ne sait pas renforcer ; ne pas bloquer le run).")
                        else:
                            print("🧬 Brique B : la suite MORD (mutants tués). Phase réellement validée.")

                record_test_count(output, blackboard, expect_growth=(nature == "tests"))
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCÈS] Phase {phase['id']} : la vérification passe (code de sortie 0).")
                commit_phase(f"phase {phase['id']}: {phase['name']}")
                # Enregistre les livrables de cette phase tests comme PROTÉGÉS pour les
                # phases feature ultérieures (le diff couvre toute la phase, chaque tentative).
                if nature == "tests" and _GIT["enabled"] and phase_start_sha:
                    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"])
                    if ok_diff:
                        protected = set(blackboard.get("protected_test_files") or [])
                        # N'enregistre PAS les artefacts d'orchestration committés pendant la
                        # phase (blackboard, prompts…) : protégés, ils feraient ensuite caler
                        # toute phase 'feature' via la garde protected_test_files.
                        protected.update(line.strip() for line in diff_out.splitlines()
                                         if line.strip() and not is_orchestration_file(line.strip()))
                        blackboard["protected_test_files"] = sorted(protected)
                        save_blackboard(blackboard)
            elif verify_timed_out:
                # Timeout d'INFRA, pas un échec du code : on NE consomme PAS la tentative
                # (sinon quelques lenteurs machine épuiseraient les MAX_ATTEMPTS du codeur).
                # On rejoue la même tentative après reset, sous garde-fou anti-boucle si
                # l'infra est durablement cassée.
                verify_timeouts += 1
                if verify_timeouts >= MAX_PHASE_VERIFY_TIMEOUTS:
                    critic_feedback = (
                        f"La vérification « {verify_cmd} » a expiré (timeout {VERIFY_TIMEOUT}s) "
                        f"de façon répétée ({verify_timeouts}×) : incident d'INFRASTRUCTURE, pas "
                        f"un échec du code. Vérifie la machine ou la commande, puis relance."
                    )
                    print(f"🛑 [TIMEOUT INFRA] Abandon de la phase {phase['id']} après {verify_timeouts} "
                          f"timeouts persistants (et non {MAX_ATTEMPTS} échecs de code).")
                    break
                attempts -= 1  # tentative non décomptée : ce n'était pas un rouge du code
                print(f"⏱️  [TIMEOUT INFRA] Vérification non concluante (délai dépassé). Tentative NON "
                      f"décomptée ({verify_timeouts}/{MAX_PHASE_VERIFY_TIMEOUTS}) — relance après reset.")
                RUNNER.new_context()
            else:
                critic_feedback = output
                phase["critic_feedback"] = output
                save_blackboard(blackboard)
                print(f"⚠️  [REJET] Tentative {attempts} : la vérification échoue. Sortie retransmise au codeur :\n{output}")
                RUNNER.new_context()

        if not success:
            phase["status"]  = "TODO"
            phase["verdict"] = "REJECTED"
            phase["critic_feedback"] = critic_feedback
            save_blackboard(blackboard)
            cleanup_all_sentinels()
            print_failure_message(phase, blackboard, critic_feedback)
            write_fail_report(
                f"Phase {phase['id']} « {phase['name']} » non convergée après {MAX_ATTEMPTS} tentatives",
                f"Dernier point bloquant relevé par la vérification :\n{critic_feedback}",
                blackboard, details=critic_feedback)
            RUNNER.kill()
            sys.exit(1)

        RUNNER.new_context()


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


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)

def main():
    check_need_file()

    # Une sentinelle d'approbation orpheline (spec.md supprimée depuis) ne doit jamais
    # valider une spec FUTURE : on la purge avant toute chose.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du run
    # courant (volet D, §6.8) : on le purge au démarrage, comme le refactoring_report résiduel.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "universal", RUNNER.name,
                   model=RUNNER.configured_model())

    # 🚀 ÉTAPE ZÉRO : Boot immédiat du harness Data Center dans Tmux
    RUNNER.start()

    # Étape 1 : Affinage PO via le TUI (need.md → spec.md), validé par l'HUMAIN.
    # La spec validée devient la source de vérité de tout l'aval (plan, production).
    # Trois états de reprise : pas de spec → génération + confirmation ; spec SANS la
    # sentinelle d'approbation (run interrompu : timeout, Ctrl-C pendant le y/n) → on
    # redemande à l'humain au lieu de croire un fichier peut-être jamais validé ;
    # spec + sentinelle → étape passée.
    mm_audit.event("step_start", step="spec")
    if not os.path.exists(SPEC_FILE):
        generate_spec_from_need_tui()
        confirm_spec_with_human()
        RUNNER.new_context()
    elif not os.path.exists(SPEC_APPROVED_SENTINEL):
        print(f"🔄 '{SPEC_FILE}' existante trouvée mais JAMAIS approuvée (run interrompu ?).")
        confirm_spec_with_human()
    else:
        print(f"🔄 '{SPEC_FILE}' existante trouvée (approuvée par l'humain). Étape PO passée.")

    # Étape 2 : Plan d'implémentation via le TUI (spec.md → plan.md)
    mm_audit.event("step_start", step="plan")
    if not os.path.exists(PLAN_FILE):
        generate_plan_from_need_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 '{PLAN_FILE}' existant trouvé. Étape passée.")

    # Étape 3 : Configuration du Blackboard via le TUI
    mm_audit.event("step_start", step="blackboard")
    if not os.path.exists(BLACKBOARD_FILE):
        blackboard = transform_plan_to_blackboard_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 '{BLACKBOARD_FILE}' existant trouvé. Chargement...")
        try:
            blackboard = load_blackboard()
        except Exception as err:
            # Le blackboard est l'état de reprise : s'il est illisible (YAML corrompu, p. ex.
            # un kill pendant une ancienne écriture), on s'arrête NET avec un message clair
            # plutôt que de planter sur une trace brute ou de repartir d'un état douteux.
            print(f"❌ '{BLACKBOARD_FILE}' présent mais illisible (YAML invalide ou corrompu) : {err}")
            print(f"   → Corrige ou supprime '{BLACKBOARD_FILE}', puis relance "
                  f"(il sera régénéré depuis '{PLAN_FILE}').")
            write_fail_report(
                "Blackboard illisible au démarrage",
                f"'{BLACKBOARD_FILE}' est présent mais illisible (YAML invalide ou corrompu) : {err}. "
                f"Corrige ou supprime ce fichier puis relance.")
            RUNNER.kill()
            sys.exit(1)

    # Le contexte « besoin » injecté aux agents de production est la SPEC affinée et validée
    # (critères d'acceptation testables) ; need.md ne sert que de secours (anciens runs).
    # need_is_spec conditionne la découpe par US (extract_spec_slice) en production.
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Garde-fou : le blackboard est produit par un petit LLM faillible. On valide la structure
    # AVANT de payer un run entier. Les manques STRUCTURANTS (pas de phases, phase sans id/nom/
    # tâches, pas de 'verify_cmd' global) feraient planter la production ou la fausseraient au
    # vert : on s'arrête dessus (erreur BLOQUANTE). Les champs non critiques (global_rules & ses
    # clés, comblés par apply_blackboard_defaults ; 'project' d'affichage) sont seulement
    # signalés. La confirmation humaine y/n reste le filet sur le CONTENU des commandes.
    # La séquence validation → récap → y/n BOUCLE : l'humain peut éditer le blackboard dans un
    # autre terminal pendant que le prompt attend, or la production tourne sur ce dict en mémoire
    # et save_blackboard() réécrit le fichier depuis celui-ci — une édition non rechargée avant
    # le 'y' serait ignorée puis écrasée en silence. Tout changement du fichier pendant le prompt
    # déclenche donc un rechargement, une re-validation et une nouvelle confirmation.
    while True:
        fatal, soft = validate_blackboard_schema(blackboard)
        if soft:
            print("\nℹ️  Champs non critiques absents (comblés automatiquement) :")
            for problem in soft:
                print(f"   - {problem}")
        if fatal:
            print("\n❌ Le blackboard présente des anomalies STRUCTURANTES :")
            for problem in fatal:
                print(f"   - {problem}")
            print(f"   → Corrige '{BLACKBOARD_FILE}' puis relance : démarrer la production sur un "
                  f"blackboard incohérent garantit un échec ou un faux vert.")
            write_fail_report(
                "Blackboard structurellement invalide",
                "Le blackboard présente des anomalies STRUCTURANTES qui feraient échouer ou fausser "
                "le run.",
                blackboard, details="\n".join(f"- {p}" for p in fatal))
            RUNNER.kill()
            sys.exit(1)
        apply_blackboard_defaults(blackboard)

        # Avertissements NON bloquants de traçabilité spec → phases ('covers') : US hallucinées
        # par le compilateur, ou exigences de la spec qu'aucune phase ne couvre.
        if need_is_spec:
            coverage_warnings = check_spec_coverage(blackboard, user_need)
            if coverage_warnings:
                print("\n⚠️  Traçabilité spec → phases :")
                for warning in coverage_warnings:
                    print(f"   - {warning}")

        print(f"\n{'='*50}")
        print(f"📋 BLACKBOARD PRÊT — Récapitulatif :")
        print(f"   Projet : {blackboard.get('project', '(sans titre)')}")
        print(f"   Stack (global_rules.target) : {blackboard['global_rules']['target']}")
        print(f"   Verdict universel (verify_cmd) : {blackboard.get('verify_cmd') or '⚠️  ABSENT'}")
        print(f"   Phases : {len(blackboard['phases'])}")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers', []))
            own_cmd = (p.get('verify_cmd') or '').strip()
            extra = f" — vérif spécifique: {own_cmd}" if own_cmd else ""
            print(f"   Phase {p['id']}: {p['name']} [{skills}] "
                  f"({len(p.get('tasks', []))} tâche(s) ; couvre: {covers or '?'}){extra}")
        print(f"{'='*50}")
        print(f"   Tu peux modifier '{BLACKBOARD_FILE}' directement dans un autre terminal avant de valider.")

        with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
            raw_at_prompt = f.read()
        confirm = input("\n▶️  Valider le blackboard et lancer la production ? (y/n) : ")
        mm_audit.event("gate", id="blackboard", gate_kind="yn", answer=confirm.strip().lower())
        if confirm.strip().lower() != 'y':
            print("⏹️  Annulé par l'utilisateur.")
            RUNNER.kill()
            sys.exit(0)

        try:
            with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
                edited_during_prompt = f.read() != raw_at_prompt
            if not edited_during_prompt:
                break
            print(f"\n🔄 '{BLACKBOARD_FILE}' a été modifié pendant l'attente du prompt : rechargement...")
            blackboard = load_blackboard()
        except Exception as err:
            print(f"❌ '{BLACKBOARD_FILE}' a été modifié pendant le prompt mais est désormais illisible "
                  f"(YAML invalide ou corrompu) : {err}")
            print(f"   → Corrige '{BLACKBOARD_FILE}' puis relance.")
            write_fail_report(
                "Blackboard illisible après édition manuelle",
                f"'{BLACKBOARD_FILE}' a été modifié pendant le prompt mais est désormais illisible "
                f"(YAML invalide ou corrompu) : {err}. Corrige ce fichier puis relance.")
            RUNNER.kill()
            sys.exit(1)

    mm_audit.snapshot(BLACKBOARD_FILE)   # copie figée du blackboard TEL QU'APPROUVÉ
    validate_all_skills(blackboard)

    # Filet de sécurité git (best-effort) : référence AVANT le scaffold, puis un commit par
    # phase verte (diff par phase, protection des fichiers de test, rollback du refacto,
    # piste d'audit).
    ensure_phase_repo()

    # Référentiel du run : tout ce qui diffère de ce sha est l'œuvre de l'usine (scaffold +
    # phases), jamais le legacy préexistant. Persisté car une REPRISE recapturerait un HEAD
    # déjà avancé, et le refacto raterait alors les fichiers des phases antérieures.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    # Étape 0 : squelette exécutable (prérequis dur de la vérification par exécution).
    mm_audit.event("step_start", step="scaffold")
    ensure_executable_scaffold(blackboard, user_need)

    print(f"\n🚀 Démarrage de la production active : {blackboard.get('project', '')}")

    # Étape 4 : Boucle de production
    mm_audit.event("step_start", step="production")
    run_production_phases(blackboard, user_need, need_is_spec)

    # Étape 5 : Polish final
    mm_audit.event("step_start", step="refactoring")
    execute_final_refactoring(blackboard, user_need)

    # Fermeture propre
    RUNNER.kill()
    # Run réussi : aucun rapport d'échec ne doit subsister (volet D, §6.8).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)
    # Run réussi : plus rien à reprendre, on purge le marqueur d'approbation de la spec. Gardé
    # hors de cleanup_all_sentinels (qui tourne aussi en cours de route) car il doit survivre à
    # une INTERRUPTION ; ici on est sur le chemin succès, donc sa suppression est sûre.
    if os.path.exists(SPEC_APPROVED_SENTINEL):
        os.remove(SPEC_APPROVED_SENTINEL)
    print("\n🏁 [CONGRATULATIONS] L'usine de code Data Center a tout validé en un seul run !")
    # Clôture du journal de run (le chemin du dossier est capturé AVANT end, qui remet
    # l'état à zéro). Une seule ligne visible de plus — le Bilan n'est pas une porte.
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    AGENT_CONFIG_FILE=AGENT_CONFIG_FILE,
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    GITIGNORE_BODY=GITIGNORE_BODY,
    MAX_ATTEMPTS=MAX_ATTEMPTS,
    MAX_VERIFY_RETRIES_ON_TIMEOUT=MAX_VERIFY_RETRIES_ON_TIMEOUT,
    PIPELINE_SKILLS=PIPELINE_SKILLS,
    POLL_INTERVAL=POLL_INTERVAL,
    REFACTO_FIX_PHASE_ID=REFACTO_FIX_PHASE_ID,
    REQUIRED_GLOBAL_RULES=REQUIRED_GLOBAL_RULES,
    RUNNER=RUNNER,
    SCAFFOLD_TIMEOUT=SCAFFOLD_TIMEOUT,
    SKILLS_DIR=SKILLS_DIR,
    TMP_CODER_FILE=TMP_CODER_FILE,
    TMUX_SESSION=TMUX_SESSION,
    US_HEADING_RE=US_HEADING_RE,
    _GIT=_GIT,
    _PHASE_STATUS_SEEN=_PHASE_STATUS_SEEN,
    _TEST_COUNT=_TEST_COUNT,
    parse_skill_frontmatter=parse_skill_frontmatter,
    run_git=run_git,
    write_fail_report=write_fail_report,
)


if __name__ == "__main__":
    main()
