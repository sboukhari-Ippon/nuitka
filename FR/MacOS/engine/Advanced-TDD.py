#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à code avec un harness d'agent + tmux (Version Full TUI Data Center)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « YOLO-TDD » (TDD + surcouche Yolo : revue d'impact, vérificateur LLM, triage des cassures).

Dérive de Safe-TDD.py (copie intégrale, la base n'est PAS modifiée), enrichie de
la surcouche Yolo — la logique d'arbitrage de Guided-Fix.py déplacée DANS le run :
  - Étape 2bis (amont) : un Agent Revue d'Impact croise 'plan.md' avec le code EXISTANT du
    projet et liste dans 'impact.md' les comportements actuels que le plan va CASSER ;
    l'HUMAIN les entérine à une porte dédiée AVANT la production (personne ne découvre en
    cours de route que l'évolution pète l'application).
  - Chemin vert (phases 'tdd-green' uniquement) : après le verdict mécanique, un
    Vérificateur LLM indépendant (contexte neuf) confronte le code réellement produit à la
    checklist de la phase du blackboard — la suite verte prouve « rien n'est cassé », pas
    « la phase a tout livré ». Il ne tamponne jamais DONE : un rejet consomme une tentative.
  - Chemin rouge (phases 'tdd-green' uniquement) : un Agent de Triage confronte les tests
    en échec à 'impact.md' — cassure ENTÉRINÉE → le test est supprimé par l'ORCHESTRATEUR
    (jamais par un agent, comptabilité ajustée) et le flux continue ; cassure IMPRÉVUE →
    un Agent Réparateur corrige l'effet de bord (tests en échec GELÉS, comportement de la
    phase exigé) ; VRAI conflit (les deux comportements s'excluent) → 'impact-phase-<id>.md'
    est arbitré par l'humain à une porte mid-run (accepté → suppression mécanique ; refusé
    → le comportement historique fait foi, correction consignée). Le filet REJECTED +
    failReport.md après MAX_ATTEMPTS reste inchangé — il devrait juste devenir rare.
  - Périmètre : les phases 'tdd-red' (verdict INVERSÉ : un rouge y est un succès) sont
    INCHANGÉES — le triage impact et le vérificateur n'y ont pas de sens.

Rappel de la base TDD (inchangée) — différence avec la variante « verdict universel » Safe-Coding.py :
  - Le plan n'est plus découpé en phases feature/tests mais en CYCLES TDD : pour chaque
    comportement, une phase 'tdd-red' (écrire des tests qui ÉCHOUENT, dérivés des critères
    d'acceptation) immédiatement suivie de sa phase 'tdd-green' (implémenter le code de
    production MINIMAL qui fait passer toute la suite). Ce découpage est décidé dès le
    PLAN (Agent Architecte TDD, skill 'plan-tdd') puis recopié dans le blackboard (champs
    'nature' et 'cycle' de chaque phase, skill 'plan-to-blackboard-tdd').
  - Le verdict d'une phase 'tdd-red' est INVERSÉ : l'orchestrateur exécute le verdict
    universel (compilation + suite complète) et VALIDE la phase quand il ÉCHOUE (code de
    sortie ≠ 0). Le code de production étant GELÉ pendant le red (garde tests-only), les
    tests des cycles précédents PROTÉGÉS, et la suite verte à la phase précédente
    (scaffold compris), un échec de la suite est mécaniquement attribuable aux nouveaux
    tests : c'est la preuve qu'ils sont falsifiables — le cœur du TDD.
  - Le verdict d'une phase 'tdd-green' est STANDARD (code de sortie 0 = suite complète
    verte). Les fichiers de test, TOUS gelés pendant le green (gardes git), sont la
    spécification exécutable : c'est le test qui commande, jamais l'inverse.
  - Le troisième temps du cycle (refactor) est joué APRÈS CHAQUE green validé, comme
    chez Beck : un agent au contexte neuf polit le code de PRODUCTION posé par le cycle
    (tests gelés, même garde qu'en green), la suite est re-vérifiée et tout écart —
    suite rouge, tests touchés, compte en baisse, timeout — déclenche un ROLLBACK
    mécanique au commit green (le refactor est opportuniste : il ne bloque JAMAIS le
    run et ne consomme aucune tentative). L'étape 5 (refactoring global re-vérifié,
    avec rollback git en cas de régression persistante) demeure en COMPLÉMENT : elle
    seule voit la duplication INTER-cycles.
  - La brique B (mutation testing) devient un SIGNAL warn-only sur les phases green :
    le seul agent relançable à ce stade (le green) n'a pas le droit de durcir les tests
    (gelés) — lui demander mènerait droit sur la garde de protection. Le red prouve déjà
    la falsifiabilité au niveau comportement ; les mutants survivants restent un signal
    qualité adressé à l'HUMAIN.

Pipeline PO → Architecte TDD :
  - Étape 1 : un Agent PO affine 'need.md' en spécification métier 'spec.md' (user stories,
    critères d'acceptation testables, hors-périmètre, hypothèses), VALIDÉE par l'humain.
    Corriger le besoin coûte le moins cher ICI, avant de payer plan + blackboard + production.
  - Étape 2 : un Agent Architecte TDD convertit 'spec.md' en plan par CYCLES où chaque
    phase déclare EXPLICITEMENT sa nature ('tdd-red'/'tdd-green') et son numéro de cycle.
  - Étape 3 : la conversion en blackboard devient une RECOPIE mécanique de ces décisions
    (zéro inférence demandée au petit modèle, qui se contente de compiler le format).
    L'appariement red → green (adjacence, même cycle) est VALIDÉ mécaniquement avant
    production : un blackboard qui le viole est REFUSÉ (le verdict inversé appliqué à la
    mauvaise phase fausserait tout le run).

Stratégie Data Center & TUI (inchangée) :
  - La session tmux est initialisée DIRECTEMENT au démarrage.
  - On lance directement le TUI du harness choisi (Modèle Cloud / Data Center).
  - Les étapes 1 (Spec PO), 2 (Plan) et 3 (Blackboard) sont exécutées directement dans le TUI.
  - Production : chaque phase passe par un Agent Codeur, puis l'orchestrateur EXÉCUTE
    lui-même la commande de vérification de la phase ; le code de sortie EST le verdict
    (brique A), interprété selon la nature (échec attendu en red, réussite exigée en green).
    Le codeur communique par fichier sentinelle ('.phase_<id>.attemptN.done') ; le seul
    maître du blackboard est l'orchestrateur Python (aucune écriture concurrente).

Risques résiduels assumés (en plus de ceux de la variante mère — codeur fantôme, etc.) :
le verdict rouge prouve que la SUITE échoue, pas que les tests échouent « pour la bonne
raison ». Un test red FABRIQUÉ (assertion toujours fausse, erreur d'écriture du test)
passe la porte du rouge mais bloque le green du cycle — les agents n'ayant pas le droit
de toucher aux tests, c'est l'HUMAIN qui arbitre (le rapport d'échec du green pointe
explicitement cette piste, cf. print_failure_message). La qualité des cas de test reste
portée par les consignes (critères d'acceptation) et le signal de la brique B.
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
    append_arbitration, apply_blackboard_defaults, build_correction_prompt, build_phase_verifier_prompt,
    build_refacto_fix_prompt, build_repair_prompt, build_skills_dictionary, build_triage_prompt,
    collect_spec_us_ids, commit_phase, correction_sentinel, done_sentinel,
    ensure_phase_repo, fail_pipeline, files_changed_since_phase_start, generate_impact_review_tui,
    git_head_sha, impact_phase_file, inject_skills_dictionary, load_blackboard,
    load_skills, mutation_tool_available, no_declared_file_touched, parse_test_count,
    read_repair_outcome, read_touched_files, read_triage, read_verdict,
    red_suite_damage, repair_sentinel, resolve_mutation_cmd, resolve_verify_cmd,
    restore_test_files, run_mutation, run_verify, run_verify_resilient,
    save_blackboard, signal_handler, test_count_regression, triage_sentinel,
    truncate_output, validate_all_skills, verdict_sentinel, wait_for_file_creation,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="yolo-tdd")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
FAIL_REPORT_FILE      = "failReport.md"   # rapport d'arrêt persistant (volet D, §6.8)
IMPACT_FILE           = "impact.md"       # Yolo : revue d'impact validée (committée, piste d'audit)
IMPACT_PHASE_PREFIX   = "impact-phase-"   # Yolo : impact-phase-<id>.md, arbitrage humain en cours de run
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard-tdd/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-tdd/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Skills système du pipeline : jamais routés vers les phases de production.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-proto", "plan-tdd",
                         "plan-to-blackboard", "plan-to-blackboard-proto",
                         "plan-to-blackboard-tdd", "refacto"}

# Natures de phase du mode TDD, décidées par l'Architecte dès le plan et recopiées par le
# compilateur blackboard. Elles pilotent TOUT : la mission du codeur (tests d'abord /
# implémentation minimale), les gardes git (prod gelée en red, tests gelés en green) et la
# SÉMANTIQUE du verdict (échec attendu en red, réussite exigée en green). Toute autre
# valeur est un blackboard invalide — validé mécaniquement avant production.
RED_NATURE            = "tdd-red"
GREEN_NATURE          = "tdd-green"

# Fichiers temporaires de routage de contexte
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_REFACTO_FILE      = RUNNER.tmp_file("refacto")
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")
# Yolo : consignes déportées des quatre nouveaux agents (revue d'impact, vérificateur de
# phase, triage des cassures, réparateur/correcteur d'effets de bord).
TMP_IMPACT_FILE       = RUNNER.tmp_file("impact")
TMP_VERIFIER_FILE     = RUNNER.tmp_file("verifier")
TMP_TRIAGE_FILE       = RUNNER.tmp_file("triage")
TMP_REPAIR_FILE       = RUNNER.tmp_file("repair")

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
IMPACT_DONE_SENTINEL     = ".pipeline_impact.done"    # Yolo : fin de l'étape 2bis (revue d'impact)

# Approbation HUMAINE de la spec, matérialisée : la simple EXISTENCE de spec.md ne prouve
# rien (un timeout peut laisser derrière lui une spec jamais validée, cf. fail_pipeline).
# Volontairement hors du motif '.pipeline_*.done' purgé par cleanup_all_sentinels :
# l'approbation doit survivre à une reprise.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Yolo : approbation HUMAINE de la revue d'impact, matérialisée — même contrat que la spec
# (l'existence d'impact.md ne prouve pas sa validation ; l'approbation survit à une reprise).
IMPACT_APPROVED_SENTINEL = ".impact_approved"

# Nom de la session tmux, suffixé d'une empreinte du répertoire du projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session (les prompts du
# projet B atterriraient dans l'agent du projet A). Reprendre le MÊME projet réutilise
# sa session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
REFACTO_FIX_PHASE_ID  = -1             # id de sentinelle dédié à la correction de régression post-refacto (≠ phases ≥1, ≠ scaffold 0)
CYCLE_REFACTO_PHASE_ID = -2            # id de sentinelle dédié au refactor par cycle (3e temps de Beck) ; le numéro de cycle sert de numéro de tentative — une sentinelle tardive d'un cycle précédent ne peut pas être prise pour celle du cycle courant
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par phase (filet de sécurité)
VERIFY_TIMEOUT        = resolve_timeout("verify", 300)            # 5 min max pour l'exécution de la commande de vérification
MAX_VERIFY_RETRIES_ON_TIMEOUT = 2      # re-vérifications immédiates sur timeout d'infra (le code n'a pas changé)
MAX_PHASE_VERIFY_TIMEOUTS     = 3      # timeouts persistants tolérés sur une phase avant arrêt « infra cassée »
MUTATION_TIMEOUT      = 300            # PRUDENT : budget borné du mutation testing (brique B). En mode TDD la
                                       # brique B est un SIGNAL warn-only (jamais de retry) : une exécution au
                                       # plus par phase green, tout dépassement dégrade en warn
SCAFFOLD_TIMEOUT      = 300            # 5 min : le scaffold est la tâche la plus courte du run — s'il
                                       # n'aboutit pas, c'est presque toujours le tool calling du modèle
                                       # qui est en cause, et un diagnostic rapide vaut mieux qu'une longue attente
VERIFY_FEEDBACK_LIMIT = 4000           # taille max du feedback de vérification renvoyé au codeur
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable pipeline accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes).
                                       # 30 s : un modèle local lent qui marque une pause entre deux écritures
                                       # ne doit pas voir son livrable à moitié écrit accepté (cf. structural_check aussi)

# UNE seule commande de vérification dans ce script : le VERDICT UNIVERSEL (compilation +
# suite complète), déclaré par l'Agent Architecte TDD et recopié par le compilateur
# blackboard, jamais par ce script. Ce qui change entre les phases n'est PAS la commande
# mais la SÉMANTIQUE de son code de sortie : une phase 'tdd-red' est validée quand la
# commande ÉCHOUE (les nouveaux tests doivent être rouges), une phase 'tdd-green' quand
# elle RÉUSSIT (suite complète verte). Le plan TDD ne déclare jamais de commande par phase.


# ─── SENTINELLES DE PHASE (CANAL CODEUR → ORCHESTRATEUR) ────────

# Yolo : sentinelles par tentative des nouveaux agents. Même contrat que le codeur (le
# numéro de tentative dans le nom rend impossible la confusion avec un signal tardif d'une
# tentative précédente), suffixes distincts pour que chaque canal reste identifiable.

# Yolo : les purges couvrent aussi les suffixes .verdict et .triage (les .done des nouveaux
# agents partagent déjà le motif du codeur).
_SENTINEL_SUFFIXES = (".done", ".verdict", ".triage")


def cleanup_sentinels(phase_id: int):
    """Supprime toutes les sentinelles (toutes tentatives, tous agents) d'une phase."""
    prefix = f".phase_{phase_id}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(_SENTINEL_SUFFIXES):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_sentinels():
    """Nettoyage final de toutes les sentinelles résiduelles (phases ET pipeline)."""
    for name in os.listdir("."):
        if (name.startswith(".phase_") or name.startswith(".pipeline_")) \
                and name.endswith(_SENTINEL_SUFFIXES):
            try:
                os.remove(name)
            except OSError:
                pass


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
.spec_approved
.impact_approved
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


def record_test_count(output: str, blackboard: dict, expect_growth: bool = False):
    """Persiste dans le blackboard le dernier compte parsable de tests passés (survit aux reprises).

    N'est appelée QUE sur des suites VERTES (scaffold, phases green, post-refacto) : le
    dernier état vert est la référence des gardes de non-décroissance — une phase red, dont
    la suite échoue par construction, n'enregistre jamais de compte. Un green qui passe SANS
    augmenter strictement le compte ne reçoit qu'un avertissement console : signal faible,
    délibérément pas un verdict (les réorganisations existent).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if expect_growth and isinstance(old_count, int) and new_count <= old_count:
        print(f"⚠️  Green passé au vert sans augmenter la suite ({old_count} → {new_count} "
              f"passants) : les tests ajoutés par le red de ce cycle sont-ils bien "
              f"découverts par le runner ?")
    blackboard["last_test_count"] = new_count
    save_blackboard(blackboard)


# ─── EXÉCUTION DE LA VÉRIFICATION (BRIQUE A : EXÉCUTION = VERDICT) ─────────────

# ─── BRIQUE B : MUTATION TESTING CIBLÉ (LA SUITE MORD-ELLE ?) ─────────────────
# Extension de la brique A : le verdict universel prouve « rien n'est cassé » ; la brique B
# prouve « la suite ROUGIT quand le code est faux » (tests falsifiables). Mécanique de bout en
# bout — le code de sortie de l'outil de mutation EST le verdict, aucun LLM ne juge. Pilotée par
# l'Architecte via un champ 'mutation_cmd' OPTIONNEL ; absente → brique inactive (run identique à
# aujourd'hui). Dégradation gracieuse partout (outil absent / timeout → warn, jamais de blocage).

def is_test_file(path: str) -> bool:
    """Heuristique de nommage best-effort : 'path' ressemble-t-il à un fichier de test ?

    Multi-langages et agnostique (répertoires tests/__tests__/spec, conventions test_*.py,
    *_test.go, *.test.ts, *.spec.js, *Test.java/*Spec.kt). Volontairement LARGE côté test : en
    cas de doute on classe en test, pour NE PAS faire caler une phase red légitime sur un faux
    « fichier de prod modifié » (le gel de la prod ne restaure que ce qui n'est PAS un test).
    Contrepartie assumée en green (gel des tests) : un helper hors convention peut être classé
    prod, un fichier de prod nommé comme un test peut être classé test — le feedback nomme les
    fichiers, l'humain arbitre, exactement comme protected_test_files.
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


# Fichiers appartenant à l'ORCHESTRATEUR lui-même (jamais du code produit par le codeur) :
# prompts tampons, livrables du pipeline, blackboard, sentinelles, caches Python, venv, configs
# d'agents et le script MAIsterMind. Ils sont réécrits à chaque phase ; AUCUNE garde basée sur
# 'git diff' ne doit les compter comme « code de production modifié » ni les restaurer
# (git checkout) — sinon l'usine sabote son propre état, voire son propre script, et aucune
# phase red ne converge (cause d'un rejet systématique quand ces artefacts sont suivis par
# git, p. ex. un dépôt humain dont le .gitignore ne les couvrait pas). Volontairement LARGE :
# en cas de doute on protège (au pire on rate un faux « code touché » sur un fichier
# d'orchestration, jamais sur du vrai code produit).
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE, FAIL_REPORT_FILE, IMPACT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_IMPACT_FILE, TMP_VERIFIER_FILE, TMP_TRIAGE_FILE, TMP_REPAIR_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, IMPACT_APPROVED_SENTINEL, ".gitignore",
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
    # Sentinelles et tampons éphémères, où qu'ils se trouvent dans l'arbre.
    if base.startswith(".phase_") or base.startswith(".pipeline_"):
        return True
    # Yolo : rapports d'arbitrage mid-run (impact-phase-<id>.md), écrits par les agents de
    # l'orchestrateur — jamais du code produit.
    if base.startswith(IMPACT_PHASE_PREFIX) and base.endswith(".md"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Caches Python, environnement virtuel et répertoires d'outillage : jamais du code produit.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def build_mutation_targets(phase: dict, phase_start_sha: str = "") -> list:
    """Fichiers de PRODUCTION à muter après une phase 'tdd-green' verte.

    Ciblage naturel du TDD : ce que le green vient d'écrire ou de modifier (diff git depuis
    le début de la phase, arbre de travail encore non committé à ce stade), filtré sur les
    fichiers de production existants — c'est exactement le code que les tests du cycle
    doivent mordre. Fallback sans git ou sans diff exploitable : les 'files_to_read' de la
    phase filtrés sur l'existant, comme la variante mère (mieux qu'une brique B
    silencieusement inactive — en green ils listent surtout les tests du cycle, d'où le
    filtre is_test_file).
    """
    out = sorted(
        f for f in files_changed_since_phase_start(phase_start_sha)
        if os.path.exists(f) and not is_test_file(f) and not is_orchestration_file(f)
    )
    if out:
        return out
    for p in (phase.get("files_to_read") or []):
        clean = str(p).strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if clean and os.path.exists(clean) and not is_test_file(clean):
            out.append(clean)
    return out


# ─── VALIDATION DU SCHÉMA DU BLACKBOARD (PRODUIT PAR UN PETIT LLM FAILLIBLE) ───

REQUIRED_GLOBAL_RULES = ["target", "styling", "constraints", "accessibility"]


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
        # ── APPARIEMENT DES CYCLES TDD (structurant) ── : tout le verdict repose sur la
        # nature ('tdd-red' → la suite doit échouer, 'tdd-green' → elle doit passer) et sur
        # l'appariement red → green (même cycle, adjacents, red d'abord). Un blackboard qui
        # les viole ne peut produire qu'un run FAUX (verdict inversé appliqué à la mauvaise
        # phase, ou run qui se termine suite rouge) : FATAL, jamais toléré.
        bad_nature = sorted({str(phase.get("nature") or "(absente)").strip() or "(absente)"
                             for phase in phases if isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower()
                             not in (RED_NATURE, GREEN_NATURE)})
        if bad_nature:
            fatal.append(
                f"phases[].nature hors {{{RED_NATURE}, {GREEN_NATURE}}} : {', '.join(bad_nature)}. "
                f"En mode TDD la nature pilote le VERDICT (échec attendu en red, réussite exigée "
                f"en green) : chaque phase doit déclarer l'une des deux."
            )
        missing_cycle = sorted(str(phase.get("id", "?")) for phase in phases
                               if isinstance(phase, dict)
                               and not str(phase.get("cycle") or "").strip())
        if missing_cycle:
            fatal.append(
                f"phases[].cycle manquant (phases {', '.join(missing_cycle)}) : l'appariement "
                f"red → green est vérifié par ce numéro, recopié du plan par le compilateur."
            )
        if not bad_nature and not missing_cycle:
            if len(phases) % 2 != 0:
                fatal.append(
                    f"Nombre de phases IMPAIR ({len(phases)}) : un cycle TDD = exactement une "
                    f"phase '{RED_NATURE}' immédiatement suivie de sa phase '{GREEN_NATURE}'. "
                    f"Un red sans green terminerait le run sur une suite rouge."
                )
            else:
                for i in range(0, len(phases), 2):
                    red, green = phases[i], phases[i + 1]
                    if not (isinstance(red, dict) and isinstance(green, dict)):
                        continue  # phase non-mapping : déjà signalée en fatal plus haut
                    red_nat = str(red.get("nature") or "").strip().lower()
                    green_nat = str(green.get("nature") or "").strip().lower()
                    if red_nat != RED_NATURE or green_nat != GREEN_NATURE:
                        fatal.append(
                            f"Phases {red.get('id', '?')}/{green.get('id', '?')} : ordre de cycle "
                            f"invalide (attendu '{RED_NATURE}' PUIS '{GREEN_NATURE}', trouvé "
                            f"'{red_nat}' puis '{green_nat}'). Chaque red précède immédiatement "
                            f"son green, sans phase intercalée."
                        )
                    elif str(red.get("cycle")) != str(green.get("cycle")):
                        fatal.append(
                            f"Phases {red.get('id', '?')} (cycle {red.get('cycle')}) et "
                            f"{green.get('id', '?')} (cycle {green.get('cycle')}) : les deux "
                            f"phases d'un cycle doivent porter le MÊME numéro de cycle."
                        )
                    else:
                        if (isinstance(red.get("covers"), list) and isinstance(green.get("covers"), list)
                                and red.get("covers") and green.get("covers")
                                and [str(c).strip().upper() for c in red["covers"]]
                                != [str(c).strip().upper() for c in green["covers"]]):
                            soft.append(
                                f"Cycle {red.get('cycle')} : « Couvre » différent entre le red et "
                                f"le green ({red.get('covers')} vs {green.get('covers')}) — les "
                                f"deux phases d'un cycle couvrent normalement la même user story."
                            )
                        if isinstance(red.get("covers"), list) and len(red.get("covers")) > 1:
                            soft.append(
                                f"Cycle {red.get('cycle')} : la phase red couvre plusieurs user "
                                f"stories ({', '.join(str(c) for c in red['covers'])}) — préfère "
                                f"un cycle par US (fenêtre de contexte du testeur, périmètre muté "
                                f"plus serré). Toléré, informatif."
                            )
        with_own_cmd = sorted(str(phase.get("id", "?")) for phase in phases
                              if isinstance(phase, dict) and (phase.get("verify_cmd") or "").strip())
        if with_own_cmd:
            soft.append(
                f"Phases avec 'verify_cmd' propre ({', '.join(with_own_cmd)}) : le mode TDD n'en "
                f"attend pas (verdict universel partout). Elle sera tout de même appliquée, avec "
                f"la sémantique de la nature (échec attendu en red) : vérifie que c'est voulu."
            )
        # Brique B (informatif) : sans 'mutation_cmd', le signal « la suite mord-elle encore
        # l'implémentation FINALE ? » (au-delà du rouge initial prouvé par chaque phase red)
        # sera inactif sur les phases green. Toléré (la brique B est optionnelle).
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if not has_mutation_cmd:
            soft.append(
                "Aucune 'mutation_cmd' déclarée : la brique B (signal warn-only « les tests "
                "mordent-ils l'implémentation finale ? ») sera inactive sur les phases green. "
                "Toléré ; déclare-la dans le plan pour des tests falsifiables de bout en bout."
            )
    if not (blackboard.get("verify_cmd") or "").strip():
        fatal.append(
            "Commande de vérification globale 'verify_cmd' manquante : c'est le fallback des "
            "phases sans 'verify_cmd' propre ET le verrou de l'étape de scaffold. Sans elle, le "
            "scaffold est sauté et une phase sans commande dédiée ne peut pas être vérifiée."
        )
    return fatal, soft


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
    print("\n📖 [ETAPE 2 : AGENT ARCHITECTE TDD] Génération du plan en cycles TDD dans le TUI Cloud...")

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
- Le plan DOIT commencer par le bloc « Stack & Vérification » (avec la commande de vérification du VERDICT UNIVERSEL : compilation + suite complète) et CHAQUE phase DOIT déclarer sa Nature (tdd-red/tdd-green), son Cycle et son champ « Couvre » (US-x) : les étapes suivantes du pipeline recopient ces décisions sans les déduire.
- Découpe la spécification en CYCLES TDD : pour chaque comportement, une micro-phase 'tdd-red' (tests dérivés des critères d'acceptation, qui doivent ÉCHOUER contre le code actuel) IMMÉDIATEMENT suivie de sa micro-phase 'tdd-green' (implémentation MINIMALE qui fait passer toute la suite), portant le même numéro de Cycle.
- Chaque micro-phase reste BORNÉE (1 à 5 tâches, au plus 5 fichiers créés/modifiés, au plus 3 fichiers à lire) ; divise un CYCLE trop gros en deux cycles, jamais une phase seule. N'ajoute aucun cycle pour une exigence absente de '{SPEC_FILE}'.
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


# ─── YOLO · ÉTAPE 2BIS : REVUE D'IMPACT (AMONT, ENTRE PLAN ET BLACKBOARD) ─────

def confirm_impact_with_human():
    """Validation humaine de la revue d'impact (human-in-the-loop AMONT, porte Yolo).

    L'humain entérine ICI les comportements existants que l'évolution va casser : en
    production, un test rouge couvert par cette revue sera supprimé MÉCANIQUEMENT par
    l'orchestrateur, sans nouvel arrêt. Il peut éditer 'impact.md' dans un autre terminal
    avant de valider (retirer un impact = exiger sa préservation).
    """
    print(f"\n{'='*50}")
    print(f"🔎 REVUE D'IMPACT PRÊTE : relis '{IMPACT_FILE}' — chaque impact validé ici sera "
          f"cassé SANS nouvel arrêt en production (test supprimé par l'orchestrateur).")
    print(f"   Tu peux la modifier directement dans un autre terminal avant de valider.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Valider la revue d'impact et continuer ? (y/n) : ")
    mm_audit.event("gate", id="impact", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Annulé par l'utilisateur. Ajuste '{PLAN_FILE}' (ou '{SPEC_FILE}') pour "
              f"préserver ces comportements, supprime '{IMPACT_FILE}', puis relance.")
        RUNNER.kill()
        sys.exit(0)
    # Approbation MATÉRIALISÉE (même contrat que la spec) : à la reprise, une revue sans
    # cette sentinelle repasse par le y/n au lieu d'être tenue pour validée.
    with open(IMPACT_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")


# ─── ÉTAPES 4 & 5 : PROMPTS DÉPORTÉS PAR FICHIER ──────────────────────────────

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    verify_cmd = resolve_verify_cmd(phase, blackboard)

    # 'nature' est la décision de l'Architecte TDD, recopiée par le compilateur et validée
    # mécaniquement avant production (uniquement RED_NATURE ou GREEN_NATURE ici) : elle
    # pilote la mission, la politique d'édition ET la sémantique du verdict.
    nature = str(phase.get("nature") or "").strip().lower()
    cycle = phase.get("cycle", "?")
    if nature == RED_NATURE:
        nature_line = (f"Cette phase est le RED du cycle TDD {cycle} : tu écris UNIQUEMENT des "
                       "tests, AVANT toute implémentation. Dérive chaque cas de test d'un critère "
                       "d'acceptation du besoin ci-dessous ; nomme et place les fichiers selon les "
                       "conventions du runner pour qu'ils soient réellement DÉCOUVERTS et exécutés. "
                       "Tes tests doivent ÉCHOUER contre le code actuel parce que le comportement "
                       "n'existe pas encore — jamais par un échec fabriqué (assertion toujours "
                       "fausse, fail() volontaire, erreur d'écriture du test) : un test fabriqué "
                       "bloquerait la phase d'implémentation qui suit, qui n'a pas le droit de le "
                       "corriger.")
    else:
        nature_line = (f"Cette phase est le GREEN du cycle TDD {cycle} : les tests écrits par la "
                       "phase red de ce cycle échouent et DÉCRIVENT le comportement attendu — ils "
                       "sont ta spécification exécutable. Lis-les d'abord, puis implémente le code "
                       "de production MINIMAL qui fait passer TOUTE la suite (YAGNI strict : rien "
                       "au-delà de ce que les tests et la checklist exigent). Tu n'écris ni ne "
                       "modifies AUCUN test.")

    # Politique d'édition, pilotée par la nature (gardes mécaniques git) : en red le code de
    # production est GELÉ (le rouge doit venir des tests, pas d'un sabotage de la prod) ; en
    # green ce sont les fichiers de TEST qui sont GELÉS (c'est le test qui commande, jamais
    # l'inverse). L'orchestrateur fait respecter ces politiques par restauration git.
    if nature == RED_NATURE:
        prod_edit_policy = ("En phase red, tu ne crées et ne modifies QUE des fichiers de test : le "
                            "code de production est GELÉ (l'orchestrateur restaure d'office tout "
                            "fichier de production que tu modifierais). Tu ne touches pas non plus "
                            "aux tests des cycles précédents (protégés, restaurés d'office) : tu "
                            "AJOUTES les tests de CE cycle.")
    else:
        prod_edit_policy = ("En phase green, tu ne crées et ne modifies AUCUN fichier de test "
                            "(l'orchestrateur restaure ou supprime d'office toute édition de test "
                            "et rejette la tentative). Tu PEUX modifier le code de production "
                            "existant si c'est nécessaire pour faire passer la suite (elle peut "
                            "révéler un bug d'un cycle antérieur à corriger).")

    # Consignes de qualité et verdict, par nature : en red le verdict est INVERSÉ (la suite
    # doit échouer À CAUSE des nouveaux tests), en green il est standard (suite verte).
    if nature == RED_NATURE:
        test_rules = ("Tes tests doivent être EXÉCUTABLES et RAPIDES : INTERDICTION de "
                      "Testcontainers, de Docker et de tout I/O réseau ou base de données.\n"
                      "Avant d'écrire tes tests, LIS les critères d'acceptation de la tranche de "
                      "besoin ci-dessous et, si tu raccordes de l'existant, les signatures réelles "
                      "des fichiers listés : chaque test exprime un COMPORTEMENT attendu précis "
                      "(jamais une assertion toujours vraie, jamais une assertion toujours fausse).")
        verdict_block = (f"L'orchestrateur lance automatiquement la commande de vérification "
                         f"« {verify_cmd} » (verdict universel : compilation + suite complète) : "
                         f"elle DOIT ÉCHOUER (code de sortie ≠ 0) À CAUSE de tes nouveaux tests — "
                         f"c'est la preuve mécanique qu'ils sont falsifiables. Si la suite reste "
                         f"verte, la phase est REJETÉE (tes tests passent déjà ou ne sont pas "
                         f"découverts par le runner). Les tests préexistants doivent, eux, "
                         f"CONTINUER de passer. C'est ton UNIQUE critère de réussite.")
    else:
        test_rules = ("Tu ne SUPPRIMES ni n'AFFAIBLIS JAMAIS un test pour faire passer la "
                      "vérification : si un test est rouge, c'est le code de production qu'il "
                      "faut écrire ou corriger.")
        verdict_block = (f"L'orchestrateur lance automatiquement la commande de vérification "
                         f"« {verify_cmd} » (verdict universel : compilation + suite complète) : "
                         f"elle DOIT réussir (code de sortie 0), sinon la phase est rejetée. "
                         f"C'est ton UNIQUE critère de réussite.")

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
Tu es un Agent Codeur ultra-spécialisé pour la Phase {phase['id']} UNIQUEMENT (cycle TDD {cycle}).
Tu réalises QUE les tâches de CETTE phase et tu t'arrêtes dès qu'elles sont faites.
Ne fais PAS le travail prévu pour d'autres phases : l'implémentation appartient à la phase
green du cycle, les autres comportements aux cycles suivants. Principe YAGNI : rien qui ne
soit pas explicitement demandé par la checklist de cette phase.

--- VÉRIFICATION AUTOMATIQUE DE CETTE PHASE ---
{nature_line}
{prod_edit_policy}
{test_rules}
{verdict_block}

{context_block}{files_block}--- BESOIN (extrait de la spec couvert par cette phase) ---
{user_need}

--- OBJECTIF PHASE {phase['id']} ({'RED' if nature == RED_NATURE else 'GREEN'}, cycle {cycle}) : {phase['name']} ---
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


# ─── MESSAGE D'ÉCHEC ──────────────────────────────────────────────────────────


def print_failure_message(phase: dict, blackboard: dict, critic_feedback: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    nature = str(phase.get("nature") or "").strip().lower()
    # Pistes de diagnostic PROPRES au TDD : un red qui n'échoue jamais et un green qui ne
    # converge jamais n'ont pas les mêmes causes probables — ni le même remède humain (les
    # agents n'ayant pas le droit de toucher aux tests, un test red fautif se corrige à la main).
    if nature == RED_NATURE:
        tdd_hint = ("   Phase RED : si la suite reste verte, le modèle n'écrit pas de tests qui\n"
                    "   expriment le comportement MANQUANT (ou les place hors conventions du runner).\n")
    elif nature == GREEN_NATURE:
        tdd_hint = (f"   Phase GREEN : si le blocage vient d'un test du cycle {phase.get('cycle', '?')} "
                    f"lui-même fautif\n   (assertion erronée, échec fabriqué en red), corrige ce test "
                    f"TOI-MÊME — les agents\n   n'y ont pas le droit (tests gelés) — puis relance.\n")
    else:
        tdd_hint = ""
    print(f"""
{'='*60}
❌ La phase {phase['id']} « {phase['name']} » (cycle {phase.get('cycle', '?')}) n'a pas convergé après {MAX_ATTEMPTS} tentatives.

   Dernier point bloquant relevé par la vérification :
   « {critic_feedback} »

{tdd_hint}💡 Le modèle actuel ({model}) cale sur cette étape précise.
   Le plus efficace : relance après avoir amené un modèle un cran au-dessus,
   soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : les {done_count} phase(s) déjà validée(s) seront reprises
   automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
{'='*60}
""")


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
        lines = ["# Rapport d'échec — Advanced-TDD", "", f"## {title}", "", "### Cause", reason.strip(), ""]
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

def ensure_executable_scaffold(blackboard: dict, user_need: str):
    """Garantit un projet exécutable ET une suite VERTE avant le premier cycle.

    Prérequis dur de la brique A, et DOUBLEMENT du verdict inversé : la suite doit être
    verte (même à un seul test santé) avant chaque phase red pour qu'un échec après elle
    soit attribuable aux nouveaux tests. Si la commande de vérification globale ne passe
    pas (toolchain/scaffold absents), un agent dédié crée le squelette minimal (build file
    + arborescence + un test santé trivial), puis on re-teste. Échec précoce et lisible
    plutôt que N phases rouges sans rapport avec leur logique.

    Idempotent : si la vérification passe déjà (reprise après crash, ou projet pré-amorcé),
    l'étape est sautée sans solliciter d'agent.
    """
    verify_cmd = (blackboard.get("verify_cmd") or "").strip()
    if not verify_cmd:
        print("⚠️  Aucune commande de vérification globale : étape de scaffold sautée "
              "(la vérification par exécution sera inopérante).")
        return

    # REPRISE : dès qu'une phase est validée, le scaffold appartient au passé. Spécificité
    # TDD qui rend ce court-circuit OBLIGATOIRE (et pas seulement une économie) : si le run
    # a été interrompu après un red validé, la suite est ROUGE PAR CONSTRUCTION jusqu'au
    # green du cycle — le contrôle « la commande passe-t-elle ? » ci-dessous conclurait à
    # tort à une chaîne cassée, lancerait un agent scaffold sur un projet déjà avancé, puis
    # avorterait le run sur un état parfaitement nominal.
    if any(isinstance(p, dict) and p.get("status") == "DONE" and p.get("verdict") == "OK"
           for p in blackboard.get("phases", []) or []):
        print("↩️  Reprise en cours de production : étape de scaffold sautée (une phase est "
              "déjà validée ; après un red, la suite est rouge par construction jusqu'à son "
              "green).")
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


# ─── YOLO · CHEMIN VERT : VÉRIFICATEUR LLM DE PHASE ───────────────────────────

# ─── YOLO · CHEMIN ROUGE : TRIAGE IMPACT, RÉPARATION, ARBITRAGE ───────────────

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


def attempt_impact_resolution(phase: dict, blackboard: dict, phase_need: str,
                              failure_output: str, attempt: int, phase_cmd: str,
                              phase_start_sha: str) -> tuple:
    """Chemin rouge Yolo (fig. 4 du schéma validé) : triage contre impact.md, suppression
    mécanique des cassures entérinées, réparation des effets de bord, arbitrage humain des
    impacts imprévus. Retourne (is_ok, output) : is_ok=True si la suite est revenue au VERT
    (l'appelant enchaîne sur le chemin vert, vérificateur LLM compris) ; sinon la sortie
    rouge la plus fraîche redevient le feedback du codeur (la tentative est consommée).
    Tout timeout d'agent ou de vérification dégrade vers (False, ...) : jamais de blocage,
    le filet REJECTED après MAX_ATTEMPTS reste le même qu'en base.
    """
    phase_id = phase["id"]

    # 1. TRIAGE : quels tests cassent, et la revue d'impact validée les couvre-t-elle ?
    print(f"🔎 [TRIAGE IMPACT] Des tests échouent : confrontation à '{IMPACT_FILE}'...")
    RUNNER.new_context()
    RUNNER.send_task(build_triage_prompt(phase, failure_output, attempt))
    if not wait_for_file_creation(triage_sentinel(phase_id, attempt)):
        print("⏱️  Le triage n'a rendu aucun verdict : retour au flux normal (feedback codeur).")
        return False, failure_output
    prevu, imprevu = read_triage(phase_id, attempt)
    print(f"   → Triage : {len(prevu)} cassure(s) PRÉVUE(s), {len(imprevu)} IMPRÉVUE(s).")

    # 2. Cassures PRÉVUES : suppression mécanique par l'orchestrateur + re-vérification.
    if prevu:
        deleted = delete_planned_tests(prevu, blackboard, phase,
                                       "cassure prévue par la revue d'impact validée")
        if deleted:
            is_ok, output, timed_out = run_verify_resilient(phase_cmd)
            if timed_out:
                return False, failure_output
            if is_ok:
                print("✅ [TRIAGE IMPACT] Suite verte après suppression des tests entérinés : "
                      "le flux continue.")
                return True, output
            failure_output = output  # il reste du rouge : cap sur la réparation

    # 3. RÉPARATEUR : effet de bord imprévu — tests en échec GELÉS, comportement de phase exigé.
    print(f"🔧 [RÉPARATEUR] Correction de l'effet de bord imprévu (tests en échec GELÉS)...")
    pre_repair = files_changed_since_phase_start(phase_start_sha)
    RUNNER.new_context()
    RUNNER.send_task(build_repair_prompt(phase, blackboard, failure_output, phase_cmd, attempt))
    if not wait_for_file_creation(repair_sentinel(phase_id, attempt)):
        print("⏱️  Le réparateur n'a pas signalé la fin : retour au flux normal (feedback codeur).")
        return False, failure_output

    touched = repair_touched_tests(blackboard, phase_start_sha, pre_repair)
    if touched:
        restore_test_files(touched)
        print(f"🛡️  [REJET] Le réparateur a modifié des tests gelés ({', '.join(touched)}) — restaurés.")
        return False, (failure_output + "\n\n[Orchestrateur] La passe de réparation a été "
                       "annulée : les fichiers de test sont gelés, corrige le code de production.")

    is_conflict, conflict_tests = read_repair_outcome(phase_id, attempt)

    if not is_conflict:
        is_ok, output, timed_out = run_verify_resilient(phase_cmd)
        if timed_out:
            return False, failure_output
        if is_ok:
            print("✅ [RÉPARATEUR] Effet de bord résorbé : suite verte, le flux continue.")
            return True, output
        print("⚠️  [RÉPARATEUR] La suite reste rouge après réparation.")
        return False, output

    # 4. CONFLIT RÉEL déclaré : arbitrage humain (porte mid-run impact-phase-<id>).
    if not os.path.exists(impact_phase_file(phase_id)):
        print("⚠️  Le réparateur déclare un conflit mais n'a pas écrit le rapport d'impact : "
              "retour au flux normal (feedback codeur).")
        return False, failure_output
    print(f"\n{'='*50}")
    print(f"⚖️  IMPACT IMPRÉVU DÉTECTÉ (phase {phase_id}) : relis '{impact_phase_file(phase_id)}'.")
    print(f"   y → l'impact est entériné : les tests concernés seront supprimés par l'orchestrateur.")
    print(f"   n → l'ancien comportement fait foi : un agent corrige la phase en le préservant.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Accepter ce nouvel impact et continuer ? (y/n) : ")
    mm_audit.event("gate", id="impact-phase", gate_kind="yn", answer=confirm.strip().lower())

    if confirm.strip().lower() == 'y':
        append_arbitration(phase_id, accepted=True)
        deleted = delete_planned_tests(
            conflict_tests, blackboard, phase,
            f"impact imprévu entériné par l'humain (cf. {impact_phase_file(phase_id)})")
        if not deleted:
            print("⚠️  Aucun test supprimable dans la déclaration de conflit : retour au flux "
                  "normal (feedback codeur).")
            return False, failure_output
        is_ok, output, timed_out = run_verify_resilient(phase_cmd)
        if timed_out:
            return False, failure_output
        if is_ok:
            print("✅ [ARBITRAGE] Impact entériné, tests supprimés : le flux continue.")
            return True, output
        return False, output

    # Refusé : l'ancien comportement fait foi — correction en le préservant (décision 5 :
    # une seule passe ; rouge persistant = conflit de spec, la tentative échoue).
    append_arbitration(phase_id, accepted=False)
    print(f"↩️  [ARBITRAGE] Impact refusé : correction en préservant le comportement historique...")
    pre_fix = files_changed_since_phase_start(phase_start_sha)
    RUNNER.new_context()
    RUNNER.send_task(build_correction_prompt(phase, blackboard, failure_output, phase_cmd, attempt))
    if not wait_for_file_creation(correction_sentinel(phase_id, attempt)):
        print("⏱️  Le correcteur n'a pas signalé la fin : retour au flux normal (feedback codeur).")
        return False, failure_output
    touched = repair_touched_tests(blackboard, phase_start_sha, pre_fix)
    if touched:
        restore_test_files(touched)
        print(f"🛡️  [REJET] Le correcteur a modifié des tests gelés ({', '.join(touched)}) — restaurés.")
        return False, failure_output
    is_ok, output, timed_out = run_verify_resilient(phase_cmd)
    if timed_out:
        return False, failure_output
    if is_ok:
        print(f"✅ [ARBITRAGE] Comportement historique préservé, suite verte — arbitrage "
              f"consigné dans '{impact_phase_file(phase_id)}'.")
        return True, output
    print("⚠️  [ARBITRAGE] La suite reste rouge après correction : conflit de spec probable "
          "(pas un bug de code). La tentative échoue — cf. le rapport d'arbitrage.")
    return False, output


# ─── REFACTOR PAR CYCLE (3e TEMPS DE BECK) ────────────────────────────────────

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


# ─── BOUCLE DE PRODUCTION PRINCIPALE ──────────────────────────────────────────

def run_production_phases(blackboard: dict, user_need: str, need_is_spec: bool = False):
    total = len(blackboard["phases"])

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} déjà validée : {phase['name']}")
            continue

        # Décisions de l'Architecte TDD (recopiées du plan, validées avant production) :
        # la nature pilote les gardes git ET la sémantique du verdict ; le cycle relie la
        # phase red à sa phase green.
        nature = str(phase.get("nature") or "").strip().lower()
        cycle = phase.get("cycle", "?")
        icon = "🔴 red" if nature == RED_NATURE else "🟢 green"
        print(f"\n{'='*50}\n🛠️  PHASE {phase['id']}/{total} [cycle {cycle} — {icon}] : {phase['name']}\n{'='*50}")

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

        attempts = 0
        verify_timeouts = 0
        success  = False
        critic_feedback = "Premier jet — aucune critique précédente."
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

            # ── PROTECTION DES TESTS DES CYCLES PRÉCÉDENTS (garde mécanique, best-effort) ── :
            # pendant une phase red, les tests VALIDÉS des cycles antérieurs sont hors
            # limites — le rouge doit venir des tests AJOUTÉS par CE cycle, sinon
            # l'attribution mécanique de l'échec s'effondre (et un red pourrait « préparer »
            # un green facile en affaiblissant l'existant). L'interdiction par prompt seul
            # est invérifiable ; ce diff ne l'est pas. Faux positif connu (un helper de test
            # légitimement partagé à étendre) : le feedback nomme les fichiers, l'humain
            # arbitre (cf. protected_test_files dans le blackboard).
            if nature == RED_NATURE and _GIT["enabled"]:
                protected = set(blackboard.get("protected_test_files") or [])
                if protected:
                    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                    touched_protected = sorted(set(diff_out.splitlines()) & protected) if ok_diff else []
                    if touched_protected:
                        run_git(["checkout", "--"] + touched_protected)
                        critic_feedback = (
                            f"Tu as modifié des tests PROTÉGÉS de cycles précédents pendant ta phase "
                            f"red : {', '.join(touched_protected)}. Ils ont été restaurés. Une phase "
                            f"red AJOUTE les tests de SON cycle ; les tests déjà verts sont "
                            f"intouchables."
                        )
                        phase["critic_feedback"] = critic_feedback
                        save_blackboard(blackboard)
                        print(f"🛡️  [REJET] Tentative {attempts} : tests protégés modifiés "
                              f"({', '.join(touched_protected)}) — restaurés.")
                        RUNNER.new_context()
                        continue

            # ── GEL DE LA PRODUCTION EN RED (garde mécanique, best-effort) ── : une phase
            # red ne modifie QUE des fichiers de test ; le code de production est GELÉ. Tout
            # fichier de prod touché est restauré (git checkout) et la tentative rejetée.
            # Placée AVANT la vérification : on attrape la triche quelle que soit la couleur
            # de la suite, et on évite un verify gaspillé sur un état qu'on va rejeter. C'est
            # CE gel qui rend le verdict inversé fiable : la suite était verte avant la phase
            # et la prod n'a pas bougé, donc un échec ne peut venir que des nouveaux tests.
            # Caveat tranché : un red ne « répare » jamais la prod en douce, même pour rendre
            # son test écrivable — l'implémentation appartient au green.
            if nature == RED_NATURE and _GIT["enabled"]:
                ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                # Exclut les fichiers de l'orchestrateur lui-même (prompts, blackboard,
                # sentinelles, .pyc, son propre script…), qu'il réécrit à chaque phase : les
                # compter comme « code de prod modifié » rejetterait TOUTE tentative red et,
                # pire, leur restauration (git checkout ci-dessous) saboterait l'état — voire
                # le script — de l'orchestrateur. Cf. is_orchestration_file.
                touched_prod = sorted(f for f in diff_out.splitlines()
                                      if f.strip() and not is_test_file(f.strip())
                                      and not is_orchestration_file(f.strip())) if ok_diff else []
                if touched_prod:
                    run_git(["checkout", "--"] + touched_prod)
                    critic_feedback = (
                        f"En phase red, tu ne touches QU'AUX fichiers de test. Tu as modifié du "
                        f"code de production : {', '.join(touched_prod)}. Ces fichiers ont été "
                        f"restaurés. Écris uniquement les tests du cycle : l'implémentation "
                        f"appartient à la phase green qui suit."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJET] Tentative {attempts} : code de production modifié en phase "
                          f"red ({', '.join(touched_prod)}) — restauré.")
                    RUNNER.new_context()
                    continue

            # ── GEL DES TESTS EN GREEN (miroir du gel de la prod, best-effort) ── : en green,
            # AUCUN fichier de test n'est créé ni modifié — même un test NOUVEAU (donc non
            # protégé) est rejeté : écrire des tests est le rôle exclusif des phases red, et
            # un green qui « complète » ou adapte la suite brouille tout (anti-triche : le
            # green ne peut pas faire passer la suite en retouchant les tests du cycle).
            # Fichiers suivis → restaurés ; fichiers nouveaux → supprimés (l'équivalent de la
            # restauration pour un fichier qui n'existait pas). Le feedback nomme tout,
            # l'humain arbitre les helpers limites.
            if nature == GREEN_NATURE and _GIT["enabled"]:
                # Yolo : les tests supprimés par l'ORCHESTRATEUR (cassure entérinée) diffèrent
                # du début de phase mais ne sont l'œuvre d'aucun agent — les restaurer ici
                # annulerait un arbitrage humain.
                yolo_deleted = set(blackboard.get("_yolo_deleted_tests") or [])
                touched_tests = sorted(
                    f for f in files_changed_since_phase_start(phase_start_sha)
                    if f.strip() and is_test_file(f.strip()) and not is_orchestration_file(f.strip())
                    and f.strip() not in yolo_deleted)
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
                    critic_feedback = (
                        f"En phase green, tu n'écris ni ne modifies AUCUN test ; tu as touché : "
                        f"{', '.join(touched_tests)}. Tout a été restauré ou supprimé — écrire les "
                        f"tests est le rôle exclusif des phases red. Implémente le code de "
                        f"PRODUCTION minimal qui fait passer la suite telle qu'elle est."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJET] Tentative {attempts} : fichiers de test créés/modifiés en "
                          f"phase green ({', '.join(touched_tests)}) — restaurés/supprimés.")
                    RUNNER.new_context()
                    continue

            print(f"  → Codeur terminé ({len(touched_files)} fichier(s) déclaré(s)). Vérification par EXÉCUTION...")

            # ── BRIQUE A : le verdict EST le code de sortie. ──
            # Python exécute lui-même la commande ; aucun LLM ne juge la complétude
            # fonctionnelle. Signal objectif que ni le codeur ni un vérificateur ne peuvent
            # halluciner. La SÉMANTIQUE du code de sortie dépend de la nature : échec attendu
            # en red, réussite exigée en green. Un TIMEOUT n'est PAS un verdict (ni rouge ni
            # vert) : branche dédiée ci-dessous.
            is_ok, output, verify_timed_out = run_verify_resilient(verify_cmd)

            if verify_timed_out:
                # Timeout d'INFRA, pas un verdict : on NE consomme PAS la tentative (sinon
                # quelques lenteurs machine épuiseraient les MAX_ATTEMPTS du codeur). On
                # rejoue la même tentative après reset, sous garde-fou anti-boucle si l'infra
                # est durablement cassée. Vaut pour les DEUX natures : un timeout ne prouve
                # pas plus un rouge légitime (red) qu'une suite verte (green).
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
                attempts -= 1  # tentative non décomptée : ce n'était pas un verdict du code
                print(f"⏱️  [TIMEOUT INFRA] Vérification non concluante (délai dépassé). Tentative NON "
                      f"décomptée ({verify_timeouts}/{MAX_PHASE_VERIFY_TIMEOUTS}) — relance après reset.")
                RUNNER.new_context()
                continue

            if nature == RED_NATURE:
                # ── VERDICT INVERSÉ DU RED ── : la phase est validée quand la suite ÉCHOUE.
                # Le code de production est gelé (garde ci-dessus), les tests des cycles
                # précédents protégés, et la suite était verte à la phase précédente : un
                # échec est donc mécaniquement attribuable aux NOUVEAUX tests — preuve de
                # falsifiabilité, cœur du TDD. Une suite VERTE signifie que les tests
                # n'expriment rien de nouveau (ou ne sont pas découverts par le runner) : rejet.
                if is_ok:
                    critic_feedback = (
                        "La suite de vérification PASSE alors que ta phase red doit la faire "
                        "ÉCHOUER. Causes probables : tes tests passent déjà (ils ne testent pas le "
                        "comportement NOUVEAU demandé par la checklist), ils ne sont pas découverts "
                        "par le runner (nom ou emplacement hors conventions), ou leurs assertions "
                        "sont creuses. Écris des tests qui expriment le comportement ATTENDU des "
                        "critères d'acceptation et qui échouent contre le code ACTUEL."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🟢 [REJET] Tentative {attempts} : la suite reste VERTE — les nouveaux "
                          f"tests n'échouent pas (red non atteint).")
                    RUNNER.new_context()
                    continue
                damage = red_suite_damage(output, blackboard)
                if damage:
                    critic_feedback = damage
                    phase["critic_feedback"] = damage
                    save_blackboard(blackboard)
                    print(f"🛡️  [REJET] Tentative {attempts} : suite rouge mais des tests EXISTANTS "
                          f"ont été cassés (le rouge doit venir des seuls nouveaux tests).")
                    RUNNER.new_context()
                    continue
                # Red atteint. On n'enregistre PAS de compte de tests (suite rouge : le
                # dernier état VERT reste la référence des gardes de non-décroissance).
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"🔴 [SUCCÈS] Phase {phase['id']} : la suite échoue comme attendu — les tests "
                      f"du cycle {cycle} sont falsifiables (red atteint).")
                # Le commit d'une phase red capture volontairement un état suite-rouge : c'est
                # le journal du cycle TDD, et le jalon HEAD dont le green mesurera son diff.
                commit_phase(f"cycle {cycle} red: {phase['name']}")
                # Les tests écrits en red deviennent immédiatement PROTÉGÉS : ni le green de
                # ce cycle ni aucune phase ultérieure ne doit les adapter au code. Best-effort
                # assumé : si le commit ci-dessus a échoué, le diff est vide et la protection
                # est simplement manquée (même compromis que la variante mère).
                if _GIT["enabled"] and phase_start_sha:
                    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"])
                    if ok_diff:
                        protected = set(blackboard.get("protected_test_files") or [])
                        # Filtres : ni les artefacts d'orchestration committés pendant la phase
                        # (blackboard, prompts… — protégés, ils feraient caler toutes les phases
                        # suivantes), ni les non-tests (le gel de la prod ne voit que les fichiers
                        # SUIVIS : un stub de prod créé puis committé en red ne doit pas entrer
                        # dans protected_test_files, dont la sémantique est « tests validés »).
                        protected.update(line.strip() for line in diff_out.splitlines()
                                         if line.strip() and is_test_file(line.strip())
                                         and not is_orchestration_file(line.strip()))
                        blackboard["protected_test_files"] = sorted(protected)
                        save_blackboard(blackboard)
                continue  # phase suivante : le green du cycle

            # ── VERDICT STANDARD DU GREEN ── : code de sortie 0 = suite complète verte.
            # ── YOLO · CHEMIN ROUGE (green uniquement) ── : avant de renvoyer un rouge au
            # codeur, le triage impact tente de le résoudre — cassure entérinée par l'humain
            # → suppression mécanique du test et poursuite ; effet de bord imprévu →
            # réparateur (tests gelés) ; vrai conflit → arbitrage humain
            # (impact-phase-<id>.md). S'il rend la suite verte, le green enchaîne ci-dessous
            # (vérificateur LLM compris) ; sinon la sortie rouge la plus fraîche redevient le
            # feedback du codeur. Les phases red (verdict inversé) ne passent JAMAIS ici.
            if not is_ok:
                is_ok, output = attempt_impact_resolution(
                    phase, blackboard, phase_need, output, attempts, verify_cmd, phase_start_sha)
            if is_ok:
                # ── COMPTE DE TESTS NON DÉCROISSANT (garde mécanique, best-effort) ── :
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

                # ── YOLO · VÉRIFICATEUR LLM DE PHASE (chemin vert, green uniquement) ── :
                # la suite verte prouve « rien n'est cassé », pas « la phase a tout livré ».
                # Un agent indépendant au contexte neuf confronte le code produit à la
                # checklist de la phase du blackboard. Il ne tamponne jamais DONE : un rejet
                # (ou l'absence de verdict) consomme la tentative et renvoie au codeur —
                # placé AVANT la brique B pour ne pas payer une mutation sur une tentative
                # qui sera rejetée.
                print("🧐 Suite verte : routage vers le Vérificateur LLM de phase (agent indépendant)...")
                RUNNER.new_context()
                RUNNER.send_task(build_phase_verifier_prompt(phase, blackboard, phase_need,
                                                             touched_files, attempts))
                if not wait_for_file_creation(verdict_sentinel(phase["id"], attempts)):
                    critic_feedback = ("Le vérificateur LLM n'a rendu aucun verdict (timeout) : "
                                       "la conformité de la phase à sa checklist n'a pas pu être "
                                       "confirmée. Vérifie que CHAQUE tâche de la checklist est "
                                       "réellement livrée, puis recrée la sentinelle de fin.")
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"⏱️  [REJET] Tentative {attempts} : aucun verdict du vérificateur LLM.")
                    RUNNER.new_context()
                    continue
                verdict_ok, verdict_feedback = read_verdict(phase["id"], attempts)
                if not verdict_ok:
                    critic_feedback = verdict_feedback
                    phase["critic_feedback"] = verdict_feedback
                    save_blackboard(blackboard)
                    print(f"🧐 [REJET] Tentative {attempts} : suite verte mais le vérificateur LLM "
                          f"constate des écarts avec la checklist du blackboard :\n{verdict_feedback}")
                    RUNNER.new_context()
                    continue
                print("🧐 Vérificateur LLM : la phase a livré toute sa checklist (conforme).")

                # ── BRIQUE B : la suite MORD-elle l'implémentation FINALE ? (signal) ── :
                # le red a déjà prouvé que les tests échouent SANS l'implémentation ; la
                # mutation vérifie qu'ils rougissent encore quand l'implémentation LIVRÉE est
                # altérée. WARN-ONLY en mode TDD, jamais un verdict ni un retry : le seul
                # agent relançable ici est le green, qui n'a précisément PAS le droit de
                # durcir les tests (gelés) — lui renvoyer les mutants survivants le mènerait
                # droit sur la garde de gel. Les mutants survivants sont un signal qualité
                # pour l'HUMAIN. Dégradation gracieuse partout (outil absent / timeout → warn).
                mcmd = resolve_mutation_cmd(phase, blackboard)
                targets = build_mutation_targets(phase, phase_start_sha)
                if not mcmd:
                    print("ℹ️  Brique B inactive (pas de 'mutation_cmd' déclarée).")
                elif "{targets}" in mcmd and not targets:
                    print("⚠️  Brique B : aucune cible mutable (aucun fichier de production visible "
                          "pour cette phase) — sautée.")
                elif not mutation_tool_available(mcmd):
                    print("⚠️  Brique B : outil de mutation introuvable — sautée (dégradation gracieuse).")
                else:
                    run_cmd = mcmd.replace("{targets}", " ".join(shlex.quote(t) for t in targets)) if "{targets}" in mcmd else mcmd
                    print("🧬 Brique B : la suite passe — on vérifie qu'elle MORD l'implémentation "
                          "finale (mutation ciblée, signal warn-only)...")
                    mut_started = time.time()
                    ok_mut, mout, mut_timed_out = run_mutation(run_cmd)
                    print(f"   ⏱️  Brique B : mutation terminée en {time.time() - mut_started:.0f}s.")
                    if mut_timed_out:
                        print(f"⏱️  Brique B : mutation expirée ({MUTATION_TIMEOUT}s) — ignorée, "
                              f"phase validée sur le verdict universel (dégradation gracieuse, "
                              f"run jamais rallongé sans borne).")
                    elif not ok_mut:
                        print("⚠️  Brique B : des mutants SURVIVENT aux tests du cycle — signal "
                              "qualité (durcis les tests TOI-MÊME : les agents n'ont pas le droit "
                              "d'y toucher). La phase reste validée sur le verdict universel :\n"
                              + truncate_output(mout, 1200))
                    else:
                        print("🧬 Brique B : la suite MORD (mutants tués). Green réellement validé.")

                record_test_count(output, blackboard, expect_growth=True)
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCÈS] Phase {phase['id']} : la suite complète passe — cycle {cycle} "
                      f"refermé (red → green).")
                green_committed = commit_phase(f"cycle {cycle} green: {phase['name']}")
                # ── REFACTOR PAR CYCLE (3e temps de Beck) ── : joué seulement si le
                # commit green a réussi — sans ce jalon, aucun rollback fiable n'existe,
                # et un refactor non annulable casserait l'attribution du rouge du
                # cycle suivant (le red suppose une suite verte au départ).
                if green_committed:
                    execute_cycle_refactoring(blackboard, phase, verify_cmd, cycle,
                                              phase_start_sha)
                else:
                    print(f"ℹ️  Refactor du cycle {cycle} sauté : le commit green a échoué, "
                          f"aucun point de rollback fiable.")
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
            # Piste TDD spécifique au green : les agents n'ont pas le droit de toucher aux
            # tests — si le test red du cycle est LUI-MÊME fautif (assertion erronée, échec
            # fabriqué), aucun green ne convergera jamais. Seul l'humain peut le corriger.
            tdd_hint = ""
            if nature == GREEN_NATURE:
                tdd_hint = (f"\nPiste TDD : un test écrit par la phase red du cycle {cycle} peut être "
                            f"lui-même fautif (assertion erronée, échec fabriqué). Les agents n'ont "
                            f"pas le droit de le corriger (tests gelés) : inspecte les tests du "
                            f"cycle, corrige-les TOI-MÊME si besoin, puis relance.")
            write_fail_report(
                f"Phase {phase['id']} « {phase['name']} » (cycle {cycle}) non convergée après "
                f"{MAX_ATTEMPTS} tentatives",
                f"Dernier point bloquant relevé par la vérification :\n{critic_feedback}{tdd_hint}",
                blackboard, details=critic_feedback)
            RUNNER.kill()
            sys.exit(1)

        RUNNER.new_context()


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


def execute_final_refactoring(blackboard: dict, user_need: str):
    print(f"\n{'='*50}\n🛡️  ETAPE 5 : REFACTOR GLOBAL DE FIN DE RUN (COMPLÉMENT INTER-CYCLES, RE-VÉRIFIÉ)\n{'='*50}")
    print("   ℹ️  Chaque cycle a déjà eu son refactor (3e temps de Beck) : cette passe ne vise")
    print("      que ce qu'aucun cycle ne pouvait voir seul — la duplication INTER-cycles.")

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
   ce projet est produit en TDD, les tests sont sa spécification exécutable et font foi —
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
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "yolo-tdd", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # Une sentinelle d'approbation orpheline (spec.md supprimée depuis) ne doit jamais
    # valider une spec FUTURE : on la purge avant toute chose. Même contrat pour la revue
    # d'impact (Yolo).
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)
    if os.path.exists(IMPACT_APPROVED_SENTINEL) and not os.path.exists(IMPACT_FILE):
        os.remove(IMPACT_APPROVED_SENTINEL)

    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du run
    # courant (volet D, §6.8) : on le purge au démarrage, comme le refactoring_report résiduel.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # 🚀 ÉTAPE ZÉRO : Boot immédiat du harness Data Center dans Tmux
    RUNNER.start()

    # Étape 1 : Affinage PO via le TUI (need.md → spec.md), validé par l'HUMAIN.
    # La spec validée devient la source de vérité de tout l'aval (plan, production).
    # Trois états de reprise : pas de spec → génération + confirmation ; spec SANS la
    # sentinelle d'approbation (run interrompu : timeout, Ctrl-C pendant le y/n) → on
    # redemande à l'humain au lieu de croire un fichier peut-être jamais validé ;
    # spec + sentinelle → étape passée.
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
    if not os.path.exists(PLAN_FILE):
        generate_plan_from_need_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 '{PLAN_FILE}' existant trouvé. Étape passée.")

    # Étape 2bis (Yolo) : revue d'impact du plan sur le code EXISTANT, validée par l'HUMAIN.
    # Les cassures entérinées ICI seront traitées sans nouvel arrêt en production (le test
    # rouge couvert est supprimé mécaniquement par l'orchestrateur). Mêmes états de reprise
    # que la spec : pas de revue → génération + confirmation ; revue jamais approuvée (run
    # interrompu pendant le y/n) → on redemande ; revue approuvée → étape passée.
    if not os.path.exists(IMPACT_FILE):
        generate_impact_review_tui()
        confirm_impact_with_human()
        RUNNER.new_context()
    elif not os.path.exists(IMPACT_APPROVED_SENTINEL):
        print(f"🔄 '{IMPACT_FILE}' existante trouvée mais JAMAIS approuvée (run interrompu ?).")
        confirm_impact_with_human()
    else:
        print(f"🔄 '{IMPACT_FILE}' existante trouvée (approuvée par l'humain). Étape passée.")

    # Étape 3 : Configuration du Blackboard via le TUI
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
        print(f"   Phases : {len(blackboard['phases'])} "
              f"({len(blackboard['phases']) // 2} cycle(s) TDD red → green)")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers', []))
            own_cmd = (p.get('verify_cmd') or '').strip()
            extra = f" — vérif spécifique: {own_cmd}" if own_cmd else ""
            icon = "🔴 red " if str(p.get('nature') or '').strip().lower() == RED_NATURE else "🟢 green"
            print(f"   Phase {p['id']} [cycle {p.get('cycle', '?')} {icon}]: {p['name']} [{skills}] "
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
    ensure_executable_scaffold(blackboard, user_need)

    print(f"\n🚀 Démarrage de la production TDD (cycles red → green) : {blackboard.get('project', '')}")

    # Étape 4 : Boucle de production (cycles TDD : red = la suite doit échouer,
    # green = la suite doit passer, refactor du cycle re-vérifié avec rollback)
    run_production_phases(blackboard, user_need, need_is_spec)

    # Étape 5 : Refactor global complémentaire re-vérifié (duplication inter-cycles ; chaque cycle a déjà joué son 3e temps)
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
    # Yolo : même contrat pour l'approbation de la revue d'impact ; 'impact.md' et les
    # éventuels 'impact-phase-<id>.md' restent, eux (piste d'audit des arbitrages, committée).
    if os.path.exists(IMPACT_APPROVED_SENTINEL):
        os.remove(IMPACT_APPROVED_SENTINEL)
    print("\n🏁 [CONGRATULATIONS] L'usine Advanced-TDD a refermé tous ses cycles (red → green → refactor) en un seul run !")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    GITIGNORE_BODY=GITIGNORE_BODY,
    IMPACT_DONE_SENTINEL=IMPACT_DONE_SENTINEL,
    IMPACT_FILE=IMPACT_FILE,
    IMPACT_PHASE_PREFIX=IMPACT_PHASE_PREFIX,
    MAX_VERIFY_RETRIES_ON_TIMEOUT=MAX_VERIFY_RETRIES_ON_TIMEOUT,
    PIPELINE_SKILLS=PIPELINE_SKILLS,
    PLAN_FILE=PLAN_FILE,
    POLL_INTERVAL=POLL_INTERVAL,
    REFACTO_FIX_PHASE_ID=REFACTO_FIX_PHASE_ID,
    REQUIRED_GLOBAL_RULES=REQUIRED_GLOBAL_RULES,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    SPEC_FILE=SPEC_FILE,
    TMP_CODER_FILE=TMP_CODER_FILE,
    TMP_IMPACT_FILE=TMP_IMPACT_FILE,
    TMP_REPAIR_FILE=TMP_REPAIR_FILE,
    TMP_TRIAGE_FILE=TMP_TRIAGE_FILE,
    TMP_VERIFIER_FILE=TMP_VERIFIER_FILE,
    US_HEADING_RE=US_HEADING_RE,
    _GIT=_GIT,
    _PHASE_STATUS_SEEN=_PHASE_STATUS_SEEN,
    _TEST_COUNT=_TEST_COUNT,
    cleanup_pipeline_sentinel=cleanup_pipeline_sentinel,
    parse_skill_frontmatter=parse_skill_frontmatter,
    run_git=run_git,
    wait_for_pipeline_file=wait_for_pipeline_file,
    write_fail_report=write_fail_report,
)


if __name__ == "__main__":
    main()
