#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à PROTOTYPES avec un harness d'agent + tmux (Version Designer / UX)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « PROTOTYPE » destinée aux designers : génère des prototypes cliquables en
HTML/CSS/JavaScript VANILLA (aucun framework, aucun build, aucun test). La qualité est
contrôlée à DEUX niveaux : à CHAQUE phase (gardes mécaniques + Agent Vérificateur
indépendant qui relit les fichiers produits contre la checklist et le design system),
puis UNE fois à la fin par un Reviewer global qui vérifie trois choses : (A) le respect
de la grille UX (skill 'ux'), (B) la conformité au blackboard (chaque phase réalisée,
chaque user story couverte) et (C) l'application du design system, de bout en bout.

DESIGN SYSTEM (garde anti-hallucination) :
  - Le design system est déclaré par l'HUMAIN dans 'need.md' (section « ## Design
    system » : nom + comment le trouver — serveur MCP, librairie/CDN, dossier local,
    URL de doc). S'il n'y est pas, une porte y/n demande de confirmer les tokens par
    défaut du prototype (accord matérialisé dans '.design_system_ack', qui survit à
    une reprise) — un design system n'est JAMAIS inventé par un agent.
  - Le PO le transcrit dans 'spec.md' (section « Design system », relue à la porte 1),
    l'Architecte le transporte dans le plan (« Stack & Livrables → Design system »),
    le compilateur l'émet dans 'global_rules.design_system', et chaque prompt de
    production, de vérification et de review le rappelle.
  - Deux gardes MÉCANIQUES par phase (Python, zéro LLM) : anti « designer fantôme »
    (les fichiers déclarés ont réellement changé) et anti « tokens hallucinés » (tout
    var(--x) consommé par les fichiers produits est défini dans un CSS du projet — un
    design system inventé se trahit d'abord par ses tokens).

Pipeline (portes humaines : design system → spec → blackboard) :
  - Étape 1 : un Agent PO/UX affine 'need.md' en spécification 'spec.md' orientée écrans,
    parcours et critères UX observables, VALIDÉE par l'humain.
  - Étape 2 : un Agent Architecte (mode prototype) convertit 'spec.md' en plan
    d'implémentation par micro-phases bornées (livrables = fichiers .html/.css/.js) :
    fondations (tokens du design system) → composants mutualisés (groupés par famille)
    → écrans qui ASSEMBLENT sans créer de nouveau composant.
  - Étape 3 : la conversion en blackboard est une RECOPIE mécanique des décisions du plan
    (aucune commande de vérification : un prototype n'a ni build ni test).
  - Étape 4 : PRODUCTION par phase en instances découpées (contexte tranché, /new entre
    phases). Chaque phase passe les gardes mécaniques PUIS le verdict d'un Agent
    Vérificateur au contexte neuf (OK / REJECTED + écarts, retransmis au designer-dev,
    boucle bornée à MAX_ATTEMPTS). Un vérificateur muet ne bloque pas le run : une
    relance, puis acceptation avec avertissement (les gardes mécaniques ont déjà
    tourné, la review finale reste). Le verdict d'un LLM reste une opinion : les
    gardes mécaniques passent d'abord, la review finale re-contrôle tout.
  - Étape 5 : REVIEW globale unique. Le Reviewer rend un verdict (OK / REJECTED + écarts)
    et écrit 'review_report.md'. En cas d'écarts, une boucle de correction bornée
    (MAX_REVIEW_ATTEMPTS passes) corrige puis re-vérifie.

Les agents communiquent par fichiers sentinelles ; le seul maître du blackboard est
l'orchestrateur Python (aucune écriture concurrente).
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil
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
    collect_spec_us_ids, git_head_sha, load_blackboard, load_skills,
    signal_handler, wait_for_file_creation,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="proto", messages={
    "follow":   "   👀 Suis en direct dans un autre terminal : tmux attach -t {session}",
    "new_warn": "   ⚠️  Le TUI n'a peut-être pas été réinitialisé (littéral '/new' encore "
                "à l'écran) : si le run dérive, vérifier avec tmux attach.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REVIEW_REPORT_FILE    = "review_report.md"
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard-proto/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-proto/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Compétences système du prototype : appliquées AUTOMATIQUEMENT à chaque phase de
# production ET utilisées comme grille par le reviewer final. 'ux' = qualité d'expérience,
# 'proto-coding' = conventions de code HTML/CSS/JS vanilla.
UX_SKILL              = "ux"
PROTO_CODING_SKILL    = "proto-coding"
PROTO_SYSTEM_SKILLS   = [UX_SKILL, PROTO_CODING_SKILL]

# Skills système du pipeline : jamais traités comme du code produit.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-proto",
                         "plan-to-blackboard", "plan-to-blackboard-proto", "refacto"}

# Fichiers temporaires de routage de contexte
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_REVIEW_FILE       = RUNNER.tmp_file("review")
TMP_VERIF_FILE        = RUNNER.tmp_file("verif")
TMP_FIX_FILE          = RUNNER.tmp_file("fix")
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")

# ─── DESIGN SYSTEM (DÉCLARÉ PAR L'HUMAIN, JAMAIS INVENTÉ PAR UN AGENT) ─────────
# La source de vérité est 'need.md' (section « ## Design system » : nom + comment le
# trouver — serveur MCP, librairie/CDN, dossier local, URL de doc). Sans déclaration, une
# porte y/n fait CONFIRMER les tokens par défaut : l'accord est MATÉRIALISÉ (il survit à
# une reprise) et volontairement hors du motif '.pipeline_*' purgé par
# cleanup_all_sentinels. Le pipeline TRANSPORTE ensuite la déclaration (spec → plan →
# global_rules.design_system) sans jamais la compléter.
DS_ACK_SENTINEL       = ".design_system_ack"
DS_DEFAULT            = "(aucun — tokens par défaut du prototype)"
# Titre de section (## Design system / ### Design-système…) et mention libre : la
# détection est volontairement LARGE — un faux positif coûte une transcription fidèle par
# le PO, un faux négatif coûte une simple porte y/n où l'humain tranche.
DS_HEADING_RE         = re.compile(r"^#{1,4}\s*design[ -]?syst[eè]me?s?\b\s*:?\s*(.*)$", re.IGNORECASE)
DS_KEYWORD_RE         = re.compile(r"design[ -]?system|syst[eè]me\s+de\s+design|design\s+syst[eè]me", re.IGNORECASE)

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : seul choix
# valable sur les 3 OS (Windows n'a pas de /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Sentinelles de fin des livrables du pipeline (étapes 1 à 3) : l'agent crée le .done APRÈS
# avoir sauvegardé le livrable, signal sans ambiguïté robuste aux pauses d'écriture.
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"
PLAN_DONE_SENTINEL       = ".pipeline_plan.done"
BLACKBOARD_DONE_SENTINEL = ".pipeline_blackboard.done"

# Approbation HUMAINE de la spec, matérialisée : la simple EXISTENCE de spec.md ne prouve
# rien (un timeout peut laisser derrière lui une spec jamais validée, voir fail_pipeline).
# Volontairement hors du motif '.pipeline_*' purgé par cleanup_all_sentinels :
# l'approbation doit survivre à une reprise.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Nom de la session tmux, suffixé d'une empreinte du répertoire projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3              # Tentatives d'une phase (production + gardes + verdict du vérificateur)
MAX_REVIEW_ATTEMPTS   = 3             # Passes de la boucle review -> correction -> re-review
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par phase / par review (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable pipeline accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes).


def fail_pipeline(message: str):
    """Point de sortie unique des échecs d'étape du pipeline (étapes 1 à 3).

    Tue toujours la session tmux AVANT de sortir : une sortie qui laisse l'agent vivant
    le laisse finir d'écrire son livrable APRÈS l'abandon de l'orchestrateur — à la
    relance, ce fichier à moitié validé serait pris pour un état de reprise valide.
    """
    mm_audit.end("failed")
    print(message)
    RUNNER.kill()
    sys.exit(1)


# ─── SENTINELLES DE PHASE (CANAL DESIGNER-DEV → ORCHESTRATEUR) ─────────────────

def done_sentinel(phase_id: int, attempt: int) -> str:
    """Fichier écrit par le designer-dev en toute fin de phase (signal 'j'ai terminé').

    Le numéro de tentative est inclus dans le nom : une sentinelle écrite tardivement par
    l'agent d'une tentative précédente ne peut pas être prise pour le signal de la tentative
    courante (pas de faux positif de fin de phase).
    """
    return f".phase_{phase_id}.attempt{attempt}.done"


def verdict_sentinel(phase_id: int, attempt: int) -> str:
    """Fichier écrit par l'Agent Vérificateur de phase (verdict OK/REJECTED + écarts).

    Même principe que la sentinelle .done : le numéro de tentative dans le nom évite
    qu'un verdict tardif d'une tentative précédente soit pris pour le verdict courant.
    """
    return f".phase_{phase_id}.attempt{attempt}.verdict"


def cleanup_sentinels(phase_id: int):
    """Supprime toutes les sentinelles (toutes tentatives) d'une phase (.done ET .verdict)."""
    prefix = f".phase_{phase_id}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and (name.endswith(".done") or name.endswith(".verdict")):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_sentinels():
    """Nettoyage final de toutes les sentinelles résiduelles (phases ET pipeline)."""
    for name in os.listdir("."):
        if (name.startswith(".phase_") and (name.endswith(".done") or name.endswith(".verdict"))) \
                or (name.startswith(".pipeline_") and (name.endswith(".done") or name.endswith(".verdict"))):
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


def read_touched_files(phase_id: int, attempt: int) -> list:
    """Lit la liste des fichiers déclarés par le designer-dev dans sa sentinelle .done.

    Les petits modèles formatent souvent la liste en puces ('- a.html', '* b.css',
    '1. c.js') : les marqueurs de liste en tête sont retirés pour ne garder que des chemins.
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


def no_declared_file_touched(files: list, since_ts: float, changed_since_phase: set = None) -> bool:
    """True si AUCUN fichier déclaré n'a réellement changé DEPUIS LE DÉBUT DE LA PHASE.

    Signature du « designer fantôme » : sentinelle écrite sans travail réel. Aucun verdict
    LLM ne peut attraper ce cas de façon fiable : ce contrôle bon marché et mécanique s'en
    charge AVANT de payer un vérificateur. Référentiel = la PHASE, pas la tentative : un
    fichier produit à une tentative et re-déclaré inchangé à la suivante reste reconnu
    comme du travail réel (LENIENT volontairement — il suffit d'UN fichier réellement
    touché DANS LA PHASE pour passer). Deux signaux : 'changed_since_phase' (diff git
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


# ─── GARDE MÉCANIQUE « TOKENS HALLUCINÉS » (LE DESIGN SYSTEM SE TRAHIT PAR SES TOKENS) ──
# Un design system inventé (ou mal appliqué) se manifeste d'abord par des tokens qui
# n'existent nulle part : var(--color-brand-500) consommé alors qu'aucun CSS du projet ne
# le définit. Ce contrôle est PUREMENT mécanique (regex, zéro LLM) : il ne juge pas le
# design, il prouve qu'un identifiant consommé a une définition. Faux négatif assumé :
# des tokens inventés MAIS définis localement par le même agent passent la garde — c'est
# le rôle de l'Agent Vérificateur (et du volet C de la review) de comparer au design
# system déclaré.

CSS_VAR_USE_RE = re.compile(r"var\(\s*--([A-Za-z0-9_-]+)")
CSS_VAR_DEF_RE = re.compile(r"--([A-Za-z0-9_-]+)\s*:")

_TOKEN_SCAN_SKIP_DIRS = {".git", ".agents", ".venv", "node_modules", "__pycache__",
                         RUNNER.equip_dir}


def collect_defined_css_tokens() -> set:
    """Ensemble des custom properties CSS définies dans le projet (fichiers .css et .html,
    hors artefacts d'orchestration et outillage). Best-effort : illisible = ignoré."""
    defined = set()
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in _TOKEN_SCAN_SKIP_DIRS]
        for name in files:
            if not name.lower().endswith((".css", ".html", ".htm")):
                continue
            path = os.path.relpath(os.path.join(root, name)).replace("\\", "/")
            if is_orchestration_file(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    defined.update(CSS_VAR_DEF_RE.findall(f.read()))
            except OSError:
                continue
    return defined


def undefined_css_tokens(touched_files: list) -> list:
    """Tokens var(--x) CONSOMMÉS par les fichiers déclarés de la phase mais définis dans
    AUCUN CSS/HTML du projet. Retourne des paires (token, fichier) triées, vides si tout
    est défini. Ne regarde que les fichiers déclarés existants (.css/.html/.js)."""
    defined = None  # calculé paresseusement : la plupart des phases n'utilisent aucun var()
    missing = []
    for raw in touched_files:
        clean = raw.strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean or not clean.lower().endswith((".css", ".html", ".htm", ".js")):
            continue
        if not os.path.exists(clean) or is_orchestration_file(clean):
            continue
        try:
            with open(clean, "r", encoding="utf-8") as f:
                used = set(CSS_VAR_USE_RE.findall(f.read()))
        except OSError:
            continue
        if not used:
            continue
        if defined is None:
            defined = collect_defined_css_tokens()
        for token in sorted(used - defined):
            missing.append((token, clean))
    return sorted(missing)


def read_review_verdict(path: str) -> tuple:
    """Lit le verdict du Reviewer final. Retourne (is_ok: bool, gaps: str).

    Parsing tolérant : on ignore les lignes vides et les barrières markdown en tête, puis on
    lit le premier mot de la première ligne utile. 'OK', 'OK.', 'OK, conforme'... valident ;
    tout le reste (dont 'REJECTED') rejette, le corps devenant la liste des écarts.
    """
    if not os.path.exists(path):
        return False, "Le reviewer n'a produit aucun verdict."
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return False, "Verdict vide produit par le reviewer."

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
    return False, body or "Le reviewer a rejeté le prototype sans en préciser les écarts."


# ─── JALONS GIT (BEST-EFFORT) ─────────────────────────────────────────────────
# BEST-EFFORT : sans git (binaire absent, échec d'init), l'usine tourne à l'identique mais
# sans jalons — dégradation gracieuse. Cette variante prototype n'a pas de verdict mécanique
# de phase : git fournit une piste d'audit (baseline, un commit par phase signalée, un après
# la review) — un point de rollback manuel par étape pour l'humain.

_GIT = {"enabled": False}

# Identité passée à chaque commande : l'usine ne doit pas dépendre de la config git locale.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

GITIGNORE_BODY = f"""# Artefacts d'orchestration MAIster-Mind (éphémères)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.spec_approved
.design_system_ack
.mm-runs/
__pycache__/
"""


def run_git(args: list, timeout: int = 60) -> tuple:
    """Exécute une commande git. Retourne (ok, stdout strippé). Ne lève jamais."""
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


def commit_phase(label: str) -> bool:
    """Committe tout l'arbre de travail (best-effort ; échec → avertit et continue).

    --allow-empty : une phase signalée qui n'a rien changé reçoit quand même son commit
    jalon, pour que les shas par phase restent fiables pour les diffs et rollbacks manuels.
    """
    if not _GIT["enabled"]:
        return False
    ok_add, _ = run_git(["add", "-A"])
    ok_commit = False
    if ok_add:
        ok_commit, _ = run_git(["commit", "-q", "--allow-empty", "-m", label])
    if not ok_commit:
        print(f"⚠️  Échec du commit git pour '{label}' (poursuite sans ce jalon).")
    return ok_commit


def files_changed_since_phase_start(start_sha: str) -> set:
    """Ensemble des fichiers modifiés/créés depuis un sha de référence (le périmètre de
    l'usine, échelle RUN). Vide sans git ou sans sha → l'appelant retombe sur le fallback.
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


# Artefacts de l'orchestrateur (jamais du code produit) : exclus du périmètre de review.
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REVIEW_REPORT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_REVIEW_FILE, TMP_VERIF_FILE, TMP_FIX_FILE,
    TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, DS_ACK_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


def is_orchestration_file(path: str) -> bool:
    """'path' est-il un artefact de l'orchestrateur (et non du prototype produit) ?"""
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
    # Caches Python, environnement virtuel et répertoires d'outillage : jamais du proto produit.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def ensure_phase_repo():
    """Jalons git par phase, mis en place avant la production (best-effort).

    Si le projet est déjà un dépôt git (géré par l'humain), il est réutilisé TEL QUEL.
    Sinon 'git init' + un .gitignore minimal + un commit de baseline.
    """
    if shutil.which("git") is None:
        print("⚠️  git introuvable : les commits par phase sont désactivés pour ce run.")
        return
    if os.path.isdir(".git"):
        _GIT["enabled"] = True
        print("✓ Dépôt git existant réutilisé (commits par phase activés).")
        return
    ok, _ = run_git(["init", "-q"])
    if not ok:
        print("⚠️  Échec de 'git init' : les commits par phase sont désactivés pour ce run.")
        return
    if not os.path.exists(".gitignore"):
        with open(".gitignore", "w", encoding="utf-8") as f:
            f.write(GITIGNORE_BODY)
    _GIT["enabled"] = True
    commit_phase("baseline: proto factory start")


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def spec_structural_check(path: str) -> bool:
    """Plancher structurel minimal pour une spec acceptée SANS sentinelle : sa section
    obligatoire « Hors périmètre » doit être présente."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "hors périmètre" in f.read().lower()
    except OSError:
        return False


def plan_structural_check(path: str) -> bool:
    """Plancher structurel minimal pour un plan accepté SANS sentinelle : le bloc d'en-tête
    obligatoire « Stack & Livrables » doit être présent."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "stack & livrables" in f.read().lower()
    except OSError:
        return False


def blackboard_structural_check(path: str) -> bool:
    """Plancher structurel minimal pour un blackboard accepté SANS sentinelle : le YAML
    doit au moins se parser."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) is not None
    except (OSError, yaml.YAMLError):
        return False


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Attend un livrable du pipeline (spec/plan/blackboard/rapport) signalé par SENTINELLE.

    L'agent crée un fichier .done APRÈS avoir sauvegardé le livrable. FILET pour un agent qui
    oublie la sentinelle : si le livrable existe, est non vide et n'a plus bougé depuis
    STABLE_POLLS_FALLBACK contrôles consécutifs, on l'accepte avec avertissement. Le paramètre
    optionnel 'structural_check' ne durcit QUE ce filet.
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
                              f"attente maintenue (l'agent écrit peut-être encore).")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinelle '{sentinel}' absente mais '{filepath}' est stable depuis "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s : livrable accepté (filet de secours).")
                return True
    return False


# ─── LECTURE / ÉCRITURE BLACKBOARD ────────────────────────────────────────────

# Derniers statuts de phase journalisés (détection des TRANSITIONS par save_blackboard).
_PHASE_STATUS_SEEN = {}


def save_blackboard(data: dict):
    """Écrit le blackboard de façon ATOMIQUE (fichier temporaire + os.replace).

    Le blackboard est l'UNIQUE état de reprise (quelles phases sont DONE/OK). Un kill pile
    pendant un dump en mode 'w' classique (qui tronque puis réécrit en place) laisserait un
    YAML à moitié écrit → reprise impossible, tout le run perdu. On écrit donc dans un
    fichier temporaire, on force le flush sur disque, puis on renomme atomiquement.
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


def global_rule(blackboard: dict, key: str) -> str:
    """Lit une règle globale avec repli honnête '(non spécifié)' (blackboard produit par un
    petit LLM faillible : un champ peut manquer)."""
    value = (blackboard.get("global_rules") or {}).get(key)
    return value if value else "(non spécifié)"


def design_system_rule(blackboard: dict) -> str:
    """Le design system transporté jusqu'au blackboard, avec repli honnête sur les tokens
    par défaut (un champ absent ne doit jamais laisser un agent en inventer un)."""
    value = (blackboard.get("global_rules") or {}).get("design_system")
    return value if value else DS_DEFAULT


def ensure_design_system(blackboard: dict, declared: str):
    """Garde MÉCANIQUE de transport : un design system déclaré par l'HUMAIN (need.md,
    transcrit dans la spec qu'il a validée) ne doit JAMAIS se perdre en route.

    Si le compilateur a omis global_rules.design_system, design_system_rule retomberait
    en SILENCE sur les tokens par défaut alors qu'un design system existe : on RECOPIE
    donc la déclaration (texte humain, jamais une invention) et on sauvegarde — la porte
    humaine relit ensuite la valeur réparée dans le récap et dans blackboard.yaml.
    Le placeholder « (mentionné dans need.md …) » du repli par mot-clé n'est pas une
    description : il ne répare rien (le champ écrit par le compilateur fait foi).
    """
    if not declared or declared.startswith("(mentionné"):
        return
    rules = blackboard.get("global_rules")
    if not isinstance(rules, dict):
        rules = {}
        blackboard["global_rules"] = rules
    if rules.get("design_system"):
        return
    rules["design_system"] = declared
    save_blackboard(blackboard)
    print(f"🎨 Transport réparé : 'global_rules.design_system' manquait dans "
          f"'{BLACKBOARD_FILE}' — déclaration recopiée telle quelle depuis la spec validée.")


def validate_phase_ids(blackboard: dict) -> tuple:
    """Garde-fous d'unicité/séquence sur phases[].id. Retourne (fatal, soft).

    Un id dupliqué fait PARTAGER à deux phases leurs sentinelles '.phase_N.attemptM.done'
    (faux signaux de fin) : fatal. Une séquence non contiguë est simplement signalée.
    """
    fatal, soft = [], []
    phases = blackboard.get("phases") if isinstance(blackboard, dict) else None
    if not isinstance(phases, list) or not phases:
        return ["Bloc 'phases' manquant ou vide : rien à produire."], []
    ids = [str(phase.get("id")) for phase in phases if isinstance(phase, dict) and "id" in phase]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        fatal.append(
            f"phases[].id dupliqués ({', '.join(duplicated)}) : les sentinelles '.phase_N.attemptM.done' "
            f"seraient PARTAGÉES entre deux phases (faux signaux de fin)."
        )
    elif ids and ids != [str(i) for i in range(1, len(ids) + 1)]:
        soft.append(
            f"phases[].id n'est pas une séquence contiguë 1..N ({', '.join(ids)}) : toléré, "
            f"mais vérifie que le compilateur n'a pas sauté ou renuméroté une phase."
        )
    return fatal, soft


def check_need_file():
    if not os.path.exists(NEED_FILE):
        print(f"❌ Erreur critique : '{NEED_FILE}' est manquant.")
        sys.exit(1)
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print(f"❌ Erreur critique : '{NEED_FILE}' est vide.")
        sys.exit(1)
    print("✓ Validation du fichier de besoins (need.md) : OK")


# ─── PORTE DESIGN SYSTEM (HUMAN-IN-THE-LOOP, AVANT TOUT AGENT) ─────────────────

def read_design_system_from_need(need_text: str) -> str:
    """Description du design system déclarée dans need.md, sinon chaîne vide.

    Deux formes reconnues : une section titrée (« ## Design system » — son corps est la
    description, transcrite ensuite par le PO) ou une simple mention en texte libre (on
    signale alors au PO de transcrire depuis need.md). AUCUNE inférence au-delà : si rien
    n'est trouvé, c'est la porte y/n qui tranche — jamais un agent.
    """
    lines = need_text.splitlines()
    for i, line in enumerate(lines):
        match = DS_HEADING_RE.match(line.strip())
        if match:
            level = len(line.strip()) - len(line.strip().lstrip("#"))
            body = []
            for follower in lines[i + 1:]:
                stripped = follower.strip()
                if stripped.startswith("#") and (len(stripped) - len(stripped.lstrip("#"))) <= level:
                    break
                body.append(follower)
            text = (match.group(1).strip() + "\n" + "\n".join(body)).strip()
            if text:
                return text
    if DS_KEYWORD_RE.search(need_text):
        return "(mentionné dans need.md — à transcrire fidèlement depuis need.md)"
    return ""


def confirm_default_design_system():
    """Porte y/n AVANT tout agent : sans design system déclaré dans need.md, l'humain
    confirme les tokens par défaut ou s'arrête pour déclarer le sien.

    C'est la réponse mécanique au risque « design system halluciné » : la déclaration ne
    peut venir QUE de l'humain (need.md), jamais d'un agent. L'accord est MATÉRIALISÉ
    (comme l'approbation de spec) : une reprise ne redemande pas. Aucun agent ni session
    tmux n'existe encore à ce stade : le refus est gratuit.
    """
    print(f"\n{'='*50}")
    print(f"🎨 DESIGN SYSTEM — aucune mention détectée dans '{NEED_FILE}'.")
    print(f"   Si tu utilises un design system : réponds n, puis décris-le dans '{NEED_FILE}'")
    print(f"   (section « ## Design system » : son nom, et comment le trouver — serveur MCP")
    print(f"   à mentionner aux agents, librairie/CDN, dossier local, URL de doc) et relance.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Continuer avec les tokens par défaut du prototype ? (y/n) : ")
    mm_audit.event("gate", id="design-system", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Annulé par l'utilisateur. Déclare ton design system dans '{NEED_FILE}' "
              f"(section « ## Design system »), puis relance.")
        RUNNER.kill()
        sys.exit(0)
    with open(DS_ACK_SENTINEL, "w", encoding="utf-8") as f:
        f.write("default tokens acknowledged\n")


# ─── DÉCOUPE DE LA SPEC PAR PHASE (FENÊTRE DE CONTEXTE) ───────────────────────

# En-tête d'une user story dans la spec PO (ex. « ### US-1 : Écran d'accueil »).
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Tranche de la spec limitée aux US couvertes par la phase (+ tout le hors-US).

    On ne garde que les sections '### US-n' listées dans 'covers', plus tout ce qui n'est pas
    une section d'US (objectif, contraintes, hors-périmètre, hypothèses). Prudence de petit
    modèle : si 'covers' est vide, si la spec ne suit pas le format à US, ou si aucune US
    couverte n'y est trouvée, on renvoie la spec ENTIÈRE (dégradation gracieuse).
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

    Deux directions : US référencée par une phase mais absente de la spec (hallucination
    probable du compilateur), et US de la spec couverte par aucune phase (écran potentiellement
    OUBLIÉ par l'Architecte). Warn-only : c'est l'œil humain au y/n qui tranche.
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
                        f"(écran oublié par l'Architecte ? Vérifie le plan).")
    return warnings


# ─── CHARGEMENT DES SKILLS ────────────────────────────────────────────────────

def present_system_skills() -> list:
    """Compétences système RÉELLEMENT présentes dans le projet (scan disque, jamais une
    déclaration d'agent) : c'est la seule source de vérité de ce qui sera appliqué."""
    return [s for s in PROTO_SYSTEM_SKILLS
            if os.path.exists(os.path.join(SKILLS_DIR, s, "SKILL.md"))]


def inject_system_skills(blackboard: dict) -> list:
    """Matérialise MÉCANIQUEMENT 'skills_required' sur chaque phase du blackboard.

    Le compilateur LLM n'émet JAMAIS ce champ en mode prototype (consigne explicite) :
    c'est l'orchestrateur qui l'écrit, depuis le scan disque des compétences système
    (ux, proto-coding). Le blackboard devient ainsi la trace VISIBLE, dès la porte
    humaine, de ce qui sera appliqué à chaque phase — et le skill 'ux', s'il existe,
    est TOUJOURS de la partie : réinjecté même après une édition manuelle qui l'aurait
    retiré. Les compétences additionnelles ajoutées à la main sont conservées.
    Renvoie la liste des compétences système présentes.
    """
    system = present_system_skills()
    changed = False
    for phase in blackboard.get("phases", []) or []:
        if not isinstance(phase, dict):
            continue
        declared = phase.get("skills_required") or []
        merged = system + [s for s in declared if s not in system]
        if declared != merged:
            phase["skills_required"] = merged
            changed = True
    if changed:
        save_blackboard(blackboard)
    return system


def phase_skills(phase: dict) -> list:
    """Compétences à charger pour CETTE phase : les compétences système présentes
    (garantie d'orchestrateur, quoi que dise le blackboard), puis les additionnelles
    que le blackboard déclare — si elles existent sur disque et ne sont pas des skills
    du pipeline (jamais routés vers la production)."""
    extras = [s for s in (phase.get("skills_required") or [])
              if s not in PROTO_SYSTEM_SKILLS and s not in PIPELINE_SKILLS
              and os.path.exists(os.path.join(SKILLS_DIR, s, "SKILL.md"))]
    return present_system_skills() + extras


def check_proto_skills():
    """Vérifie la présence des compétences système du prototype (ux, proto-coding).

    Elles sont appliquées automatiquement à chaque phase et servent de grille au reviewer :
    leur absence dégrade fortement la qualité, on prévient l'humain sans bloquer.
    """
    present = present_system_skills()
    missing = [s for s in PROTO_SYSTEM_SKILLS if s not in present]
    if missing:
        print(f"\n⚠️  Compétence(s) système introuvable(s) : {', '.join(missing)}")
        print(f"   Chemin attendu : {SKILLS_DIR}/<skill>/SKILL.md")
        print("   → Les phases et la review s'exécuteront sans elles (qualité dégradée).\n")
    else:
        print(f"✅ Compétences système présentes : {', '.join(PROTO_SYSTEM_SKILLS)}.\n")


# ─── ETAPES INTERACTIVES 1 À 3 DANS LE TUI (CLOUD) ────────────────────────────

def generate_spec_from_need_tui(ds_declared: str):
    print("\n📖 [ETAPE 1 : AGENT PO/UX] Affinage du besoin en spécification (écrans & parcours)...")

    if not os.path.exists(PO_SKILL_FILE):
        fail_pipeline(f"❌ Skill PO manquant : '{PO_SKILL_FILE}'")
    with open(PO_SKILL_FILE, "r", encoding="utf-8") as f:
        po_spec = f.read()
    with open(TMP_PO_FILE, "w", encoding="utf-8") as f:
        f.write(po_spec)

    # Le design system est une DÉCLARATION HUMAINE (need.md ou porte y/n) que le PO
    # TRANSCRIT — jamais une décision d'agent. La consigne dépend donc de ce que l'humain
    # a déclaré : transcription fidèle, ou section « (aucun) » explicite.
    if ds_declared:
        ds_directive = (f"- DESIGN SYSTEM : le besoin en déclare un. Transcris-le FIDÈLEMENT dans une "
                        f"section « ## Design system » de '{SPEC_FILE}' : son nom et comment y accéder "
                        f"(serveur MCP, librairie/CDN, dossier local, URL de doc), tels que '{NEED_FILE}' "
                        f"les donne. Recopie, zéro invention, zéro complément.")
    else:
        ds_directive = (f"- DESIGN SYSTEM : aucun design system n'est déclaré (choix confirmé par "
                        f"l'humain). Ajoute dans '{SPEC_FILE}' une section « ## Design system » "
                        f"contenant exactement : « {DS_DEFAULT} ». N'en invente JAMAIS un.")

    po_prompt = f"""Lis le fichier '{NEED_FILE}' à la racine de notre projet, ainsi que les consignes de Product Owner du fichier '{TMP_PO_FILE}'.
Tu es un Product Owner orienté design/UX. En appliquant SCRUPULEUSEMENT les consignes de '{TMP_PO_FILE}', affine le besoin brut en une spécification de PROTOTYPE et sauvegarde-la DIRECTEMENT dans un nouveau fichier nommé '{SPEC_FILE}' à la racine du projet.

Directives spécifiques à ce prototype :
- Zéro invention : chaque exigence doit découler du besoin exprimé dans '{NEED_FILE}'.
- Pense en ÉCRANS et en PARCOURS : chaque user story décrit un écran ou une étape du parcours utilisateur.
- Les critères d'acceptation sont des comportements OBSERVABLES À L'ÉCRAN (Étant donné / Quand / Alors) : ce qui s'affiche, les états (vide, erreur, chargement), les actions possibles.
{ds_directive}
- Toute ambiguïté du besoin devient une hypothèse explicite dans « Hypothèses & Questions ».
- La section « Hors périmètre » est obligatoire (verrou anti sur-ingénierie : un prototype n'implémente pas tout).
- Ne fais AUCUN choix technique : c'est un prototype HTML/CSS/JS, l'architecte tranchera le découpage.
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

    C'est ici que corriger coûte le moins cher : une exigence mal comprise rejetée à ce stade
    évite de payer (et de refaire) un plan, un blackboard et des phases de production.
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
    # une spec sans cette sentinelle repasse par le y/n au lieu d'être crue sur parole.
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # copie figée de la spec TELLE QU'APPROUVÉE


def generate_plan_from_need_tui():
    print("\n📖 [ETAPE 2 : AGENT ARCHITECTE PROTO] Génération du plan d'implémentation...")

    if not os.path.exists(PLAN_SKILL_FILE):
        fail_pipeline(f"❌ Skill de planification manquant : '{PLAN_SKILL_FILE}'")
    with open(PLAN_SKILL_FILE, "r", encoding="utf-8") as f:
        plan_spec = f.read()
    with open(TMP_PLAN_FILE, "w", encoding="utf-8") as f:
        f.write(plan_spec)

    planning_prompt = f"""Lis le fichier '{SPEC_FILE}' à la racine de notre projet (spécification validée), ainsi que les consignes d'architecture du fichier '{TMP_PLAN_FILE}'.
Tu es un Architecte de prototype. En appliquant SCRUPULEUSEMENT les consignes de '{TMP_PLAN_FILE}', génère un plan d'implémentation séquentiel au format Markdown et sauvegarde-le DIRECTEMENT dans un nouveau fichier nommé '{PLAN_FILE}' à la racine du projet.

Directives pour le fichier '{PLAN_FILE}' :
- Stack imposée : HTML5 + CSS + JavaScript VANILLA. AUCUN framework, AUCUN build, AUCUN test, AUCUNE commande de vérification.
- Le plan DOIT commencer par le bloc « Stack & Livrables » et CHAQUE phase DOIT déclarer son champ « Couvre » (US-x) : les étapes suivantes du pipeline recopient ces décisions sans les déduire.
- DESIGN SYSTEM : le bloc « Stack & Livrables » DOIT porter la ligne « **Design system :** … » RECOPIÉE de la section « Design system » de '{SPEC_FILE}' (nom + source d'accès — serveur MCP, librairie/CDN, dossier local, URL), ou « {DS_DEFAULT} » si la spec le dit. Tu ne complètes ni n'inventes JAMAIS un design system.
- Pose les fondations en première phase (tokens du design system matérialisés dans assets/css/tokens.css — la source UNIQUE des tokens —, base, index.html), puis les COMPOSANTS mutualisés en une ou plusieurs phases bornées (groupés par famille : formulaires, navigation, affichage de données — seulement ceux que les écrans de la spec exigent), puis un écran (ou groupe cohérent d'écrans) par phase, qui ASSEMBLE les composants existants sans en créer de nouveaux.
- Découpe en micro-phases BORNÉES (1 à 5 tâches, au plus 5 fichiers créés/modifiés, au plus 3 fichiers à lire par phase) ; la fourchette indicative de 3 à 10 phases cède toujours devant ces bornes de taille.
- Principe YAGNI : ne planifie QUE ce que la spécification demande ; sa section « Hors périmètre » est une interdiction.
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

    print("\n📖 [ETAPE 3 : COMPILATEUR BLACKBOARD PROTO] Génération du Blackboard YAML...")

    with open(BLACKBOARD_SKILL_FILE, "r", encoding="utf-8") as f:
        compiler_spec = f.read()
    with open(TMP_ARCHITECT_FILE, "w", encoding="utf-8") as f:
        f.write(compiler_spec)

    prompt = f"""Tu es un Compilateur Blackboard : tu RECOPIES les décisions du plan, tu n'en prends aucune. Lis le plan qui vient d'être généré dans '{PLAN_FILE}' ainsi que les consignes de structure du fichier '{TMP_ARCHITECT_FILE}'.
Génère le fichier '{BLACKBOARD_FILE}' à la racine de notre projet en respectant scrupuleusement le format YAML demandé.

RAPPEL MODE PROTOTYPE : n'émets AUCUN champ verify_cmd, build_cmd, mutation_cmd, skills_required ou nature (un prototype HTML/JS n'a ni build ni test ; les compétences système — ux, proto-coding — sont matérialisées dans skills_required par l'ORCHESTRATEUR lui-même après ta compilation, depuis un scan disque : toi, tu n'émets JAMAIS ce champ). Émets en revanche global_rules.design_system : la RECOPIE de la ligne « Stack & Livrables → Design system » du plan (« {DS_DEFAULT} » si le plan le déclare ainsi — jamais inventé).

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

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    # Contexte de l'architecte et liste de lecture, portés depuis le plan : un GUIDAGE qui
    # épargne au designer-dev une ré-exploration libre du projet.
    context_block = ""
    if str(phase.get("context") or "").strip():
        context_block = f"""--- TA PLACE DANS LE PLAN (contexte de l'architecte) ---
{str(phase.get("context")).strip()}

"""
    files_to_read = [str(p).strip() for p in (phase.get("files_to_read") or []) if str(p).strip()]
    files_block = ""
    if files_to_read:
        files_block = ("--- FICHIERS À LIRE EN PREMIER ---\n"
                       "Lis ces fichiers AVANT de coder (l'Architecte les a sélectionnés pour cette "
                       "phase) ; n'explore pas le reste du projet sauf nécessité stricte :\n"
                       + "\n".join(files_to_read) + "\n\n")

    full_context = f"""--- SYSTEM RULES ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Direction visuelle: {global_rule(blackboard, 'styling')}
Contraintes: {global_rule(blackboard, 'constraints')}
Accessibilité: {global_rule(blackboard, 'accessibility')}

{skills_context}--- CONTRAT COMPORTEMENTAL ---
Tu es un Agent Designer-Dev ultra-spécialisé pour la Phase {phase['id']} UNIQUEMENT.
Tu n'implémentes QUE les tâches de cette phase. Arrête-toi dès que c'est fait.
Principe YAGNI : tu ne réalises rien qui ne soit pas explicitement demandé.

--- NATURE DU LIVRABLE : PROTOTYPE ---
Tu produis un prototype cliquable en HTML/CSS/JavaScript VANILLA : aucun framework, aucun
bundler, aucun `npm install`, aucune étape de build. Chaque écran doit s'ouvrir directement
dans un navigateur (double-clic sur le `.html`). Les données sont mockées en dur (objets JS) ;
aucun backend ni appel réseau réel. Applique scrupuleusement les compétences ci-dessus
(`ux` et `proto-coding`) : états d'interface, accessibilité, BEM, tokens CSS.

--- DESIGN SYSTEM (RÈGLE ANTI-HALLUCINATION) ---
Le design system de ce prototype est : {design_system_rule(blackboard)}.
Si un design system est déclaré ci-dessus : tes tokens, composants et classes en proviennent
EXCLUSIVEMENT — les tokens vivent dans assets/css/tokens.css (source unique, matérialisée par
la phase de fondations depuis la source déclarée : serveur MCP, librairie, dossier, doc), les
composants dans les CSS partagés. Tu n'INVENTES jamais un token (var(--…)) ni un composant qui
n'existe pas dans ce design system ; l'orchestrateur vérifie mécaniquement que tout var(--…)
consommé est défini. Dans une phase d'ÉCRAN, tu ASSEMBLES les composants existants sans en
créer de nouveaux. Sans design system déclaré, tu utilises les tokens par défaut du prototype
(déjà dans tokens.css) — sans jamais prétendre suivre un design system nommé.

{context_block}{files_block}--- BESOIN / SPÉCIFICATION ---
{user_need}

--- OBJECTIF PHASE {phase['id']} : {phase['name']} ---
Checklist :
{chr(10).join([f'- [ ] {t}' for t in phase.get('tasks', [])])}

--- RETOUR DU VÉRIFICATEUR À CORRIGER (le cas échéant) ---
{critic_feedback}

--- INSTRUCTION DE FIN DE PHASE OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE} : c'est l'orchestrateur qui le gère.
Quand toutes les tâches de la phase sont RÉELLEMENT réalisées dans les fichiers, et en toute
dernière action, crée le fichier sentinelle '{done_sentinel(phase['id'], attempt)}' à la racine du projet.
Il doit contenir la liste des fichiers que tu as créés ou modifiés (un chemin par ligne), et rien d'autre.
Ce fichier est le signal de fin de phase : ne le crée que lorsque tu as VRAIMENT terminé.
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier de consignes '{TMP_CODER_FILE}' à la racine du projet. Suis scrupuleusement ses instructions pour réaliser la Phase {phase['id']}."


def build_phase_verifier_prompt(phase: dict, blackboard: dict, phase_need: str,
                                touched_files: list, attempt: int) -> str:
    """Consignes de l'Agent Vérificateur DE PHASE (contexte neuf, indépendant du designer-dev).

    Périmètre volontairement ÉTROIT (fenêtre de contexte) : les fichiers déclarés par la
    phase + la tranche de spec couverte + le design system — jamais tout le prototype
    (c'est le rôle de la review finale). Son verdict est une OPINION de LLM : il passe
    APRÈS les gardes mécaniques (anti-fantôme, tokens hallucinés) et AVANT la review
    finale, qui re-contrôle tout.
    """
    files_block = "\n".join(f"   - {f}" for f in touched_files) or "   (aucun fichier déclaré)"
    tasks_block = "\n".join(f"   - {t}" for t in (phase.get("tasks") or [])) or "   (aucune tâche listée)"
    full_context = f"""Tu es un Vérificateur de phase strict et indépendant (Lead Designer-Dev QA). Tu n'as PAS produit ce code : tu le juges. Tu vérifies UNIQUEMENT la Phase {phase['id']} « {phase['name']} », rien d'autre.

--- RÈGLES GLOBALES DU PROJET ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Direction visuelle: {global_rule(blackboard, 'styling')}
Contraintes: {global_rule(blackboard, 'constraints')}
Accessibilité: {global_rule(blackboard, 'accessibility')}

--- BESOIN COUVERT PAR CETTE PHASE (extrait de la spec) ---
{phase_need}

--- CHECKLIST DE LA PHASE (à confronter au code réel) ---
{tasks_block}

--- FICHIERS DÉCLARÉS PAR LE DESIGNER-DEV (ton périmètre de lecture) ---
{files_block}

--- MÉTHODE OBLIGATOIRE ---
1. Ouvre et LIS réellement chaque fichier déclaré avec tes outils. Ne te fie à aucun résumé.
2. CHECKLIST : chaque tâche ci-dessus est-elle CONCRÈTEMENT réalisée dans ces fichiers ?
3. DESIGN SYSTEM : si un design system est déclaré dans les règles globales, vérifie qu'il est RÉELLEMENT appliqué et jamais halluciné : les tokens consommés (var(--…)) et les composants/classes utilisés proviennent du design system déclaré (matérialisé dans assets/css/tokens.css et les CSS partagés) — aucun token ni composant inventé, aucune classe « à la manière de ». Si cette phase est une phase d'ÉCRAN, elle ASSEMBLE les composants existants : signale tout composant nouveau créé dans une phase d'écran.
4. LIVRABLE : le ou les .html livrés s'ouvrent de façon autonome (chemins relatifs cohérents, aucun framework, aucune dépendance réseau non déclarée, données mockées).
5. Ne signale que des écarts que tu as RÉELLEMENT constatés dans les fichiers ; cite le fichier fautif. N'exige RIEN qui dépasse la checklist de CETTE phase (les autres écrans, la review UX globale et le reste du plan ne sont pas ton périmètre).

--- VERDICT OBLIGATOIRE ---
En toute DERNIÈRE action, écris ton verdict dans le fichier sentinelle '{verdict_sentinel(phase['id'], attempt)}' à la racine :
- Si la checklist est honorée ET le design system correctement appliqué : la PREMIÈRE ligne contient EXACTEMENT le mot "OK" (rien d'autre).
- Sinon : la PREMIÈRE ligne contient EXACTEMENT le mot "REJECTED", puis les lignes suivantes listent précisément et brièvement les écarts à corriger (fichier + problème + correction attendue) : ils seront transmis tels quels au designer-dev.
Tu ne modifies AUCUN fichier du prototype et tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}.
"""
    with open(TMP_VERIF_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_VERIF_FILE}' à la racine du projet et vérifie "
            f"la Phase {phase['id']} comme il l'exige.")


def serialize_phases_for_review(blackboard: dict) -> str:
    """Liste compacte des phases (id, nom, US couvertes, tâches) pour le volet conformité du
    reviewer : il vérifie que CHAQUE tâche est concrètement réalisée et chaque US couverte."""
    blocks = []
    for phase in blackboard.get("phases", []) or []:
        covers = ", ".join(phase.get("covers") or []) or "(non précisé)"
        tasks = "\n".join(f"      - {t}" for t in (phase.get("tasks") or [])) or "      (aucune tâche listée)"
        blocks.append(f"   Phase {phase.get('id')} — {phase.get('name')} (couvre : {covers})\n{tasks}")
    return "\n".join(blocks)


def build_review_scope_block(blackboard: dict) -> tuple:
    """Construit (scope_block, scope_files) : le périmètre des fichiers du prototype à relire,
    limité au diff du run (jamais le legacy). Sans git, on demande de relire tout le proto."""
    baseline_sha = blackboard.get("_run_baseline_sha", "")
    scope_files = sorted(
        f for f in files_changed_since_phase_start(baseline_sha)
        if not is_orchestration_file(f) and os.path.exists(f)
    )
    if scope_files:
        scope_block = (
            "Relis UNIQUEMENT les fichiers ci-dessous, produits par l'usine (tout le reste — "
            "legacy, dépendances — est HORS PÉRIMÈTRE) :\n"
            + "\n".join(f"   - {f}" for f in scope_files)
            + "\n   Ouvre chaque écran et confronte-le à la grille et au blackboard."
        )
    else:
        scope_block = ("Relis l'ensemble des fichiers du prototype produits (index.html, écrans, "
                       "CSS, JS). Ouvre chaque écran et confronte-le à la grille et au blackboard.")
    return scope_block, scope_files


def build_review_prompt(blackboard: dict, user_need: str, grille: str,
                        scope_block: str, verdict_path: str) -> str:
    phases_block = serialize_phases_for_review(blackboard)
    full_context = f"""Tu es un Lead Product Designer + QA, strict et indépendant. Tu réalises la revue GLOBALE de qualité de ce prototype, en toute fin de fabrication. Ta mission a TROIS volets, également importants :
  (A) QUALITÉ UX : le prototype respecte-t-il la grille UX ci-dessous (états d'interface, accessibilité, hiérarchie visuelle, responsive, feedback) ?
  (B) CONFORMITÉ AU BLACKBOARD : chaque phase a-t-elle été réellement réalisée, et chaque user story de la spécification est-elle couverte par un écran/parcours du prototype produit ?
  (C) DESIGN SYSTEM : le design system déclaré dans les règles globales est-il RÉELLEMENT appliqué de bout en bout (tokens et composants issus du design system, assets/css/tokens.css comme source unique, aucun token ni composant inventé, cohérence d'un écran à l'autre) ? S'il vaut « {DS_DEFAULT} », vérifie qu'aucun écran ne prétend suivre un design system nommé.

--- GRILLE DE RÉFÉRENCE (UX + conventions de code) ---
{grille}--- RÈGLES GLOBALES DU PROJET ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Direction visuelle: {global_rule(blackboard, 'styling')}
Contraintes: {global_rule(blackboard, 'constraints')}
Accessibilité: {global_rule(blackboard, 'accessibility')}

--- SPÉCIFICATION (source de vérité du besoin) ---
{user_need}

--- BLACKBOARD À HONORER (phase par phase, à confronter au code réel) ---
{phases_block}

--- PÉRIMÈTRE À RELIRE ---
{scope_block}

--- MÉTHODE OBLIGATOIRE ---
1. Ouvre et LIS réellement chaque fichier du périmètre avec tes outils. Ne te fie à aucun résumé.
2. Volet B : pour CHAQUE phase ci-dessus, vérifie que ses tâches sont concrètement réalisées dans les fichiers ; pour CHAQUE user story de la spec, vérifie qu'un écran/parcours la couvre.
3. Volet A : confronte le prototype à la checklist UX (états manquants, focus clavier, contrastes, sémantique, responsive, feedback...).
4. Volet C : confronte les tokens et composants réellement utilisés au design system déclaré (tokens.css = source unique ; aucun token ni composant inventé ; cohérence entre écrans).
5. Ne signale que des écarts que tu as RÉELLEMENT constatés dans le code.

--- DEUX LIVRABLES OBLIGATOIRES ---
1. Rédige un rapport lisible dans '{REVIEW_REPORT_FILE}' à la racine, structuré ainsi :
   - Synthèse (appréciation d'ensemble + verdict)
   - Conformité au blackboard (phase par phase, US par US : conforme ou écart précis)
   - Qualité UX (points conformes / écarts, écran par écran)
   - Application du design system (conforme ou écarts : tokens/composants hors design system)
   - Écarts à corriger en priorité (liste actionnable)
2. En toute DERNIÈRE action, écris ton verdict machine dans le fichier sentinelle '{verdict_path}' à la racine :
   - Si le prototype honore le blackboard ET respecte la grille UX ET applique le design system sans écart bloquant : la PREMIÈRE ligne contient EXACTEMENT le mot "OK" (rien d'autre).
   - Sinon : la PREMIÈRE ligne contient EXACTEMENT le mot "REJECTED", puis les lignes suivantes listent précisément et brièvement les écarts à corriger (ce sont eux qui seront transmis pour correction).
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}.
"""
    with open(TMP_REVIEW_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier d'audit '{TMP_REVIEW_FILE}' à la racine du projet et réalise la revue finale complète du prototype."


def build_fix_prompt(blackboard: dict, grille: str, scope_block: str,
                     gaps: str, fix_done: str) -> str:
    full_context = f"""Tu es un Agent Designer-Dev. Le reviewer de qualité a relevé des écarts entre le prototype et (A) la grille UX, (B) le blackboard, (C) le design system déclaré. Corrige UNIQUEMENT ces écarts.

--- GRILLE DE RÉFÉRENCE (UX + conventions de code) ---
{grille}--- RÈGLES GLOBALES ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Direction visuelle: {global_rule(blackboard, 'styling')}
Contraintes: {global_rule(blackboard, 'constraints')}
Accessibilité: {global_rule(blackboard, 'accessibility')}

--- PÉRIMÈTRE (fichiers du prototype) ---
{scope_block}

--- ÉCARTS À CORRIGER (relevés par le reviewer) ---
{gaps}

--- RÈGLES DE CORRECTION ---
- Corrige les écarts listés, et rien d'autre (pas de sur-ingénierie, pas de refonte gratuite).
- Reste en HTML/CSS/JS vanilla, sans framework ni build ; chaque écran reste ouvrable directement.
- Reste dans le design system déclaré ci-dessus : aucun token (var(--…)) ni composant inventé, tokens.css reste la source unique.
- Ne crée AUCUNE régression sur les écrans déjà conformes.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}.
Quand les écarts sont corrigés, et en toute dernière action, crée le fichier sentinelle '{fix_done}' à la racine du projet (contenu : la liste des fichiers modifiés, un chemin par ligne).
"""
    with open(TMP_FIX_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier de consignes '{TMP_FIX_FILE}' à la racine du projet et corrige les écarts relevés par le reviewer."


# ─── MESSAGE D'ÉCHEC ──────────────────────────────────────────────────────────


def print_failure_message(phase: dict, blackboard: dict, reason: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    print(f"""
{'='*60}
❌ La phase {phase['id']} « {phase['name']} » n'a pas convergé après {MAX_ATTEMPTS} tentatives.

   Dernier point bloquant (garde mécanique ou vérificateur) :
   « {reason} »

💡 Le modèle actuel ({model}) cale sur cette étape précise (sentinelle jamais créée =
   souvent un problème d'appel d'outil ; écarts répétés = design system ou checklist
   pas honorés). Le plus efficace : relance après avoir amené un modèle un cran
   au-dessus, soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : les {done_count} phase(s) déjà produite(s) seront reprises
   automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
{'='*60}
""")


# ─── BOUCLE DE PRODUCTION PRINCIPALE (GARDES MÉCANIQUES + VÉRIFICATEUR PAR PHASE) ──

def run_production_phases(blackboard: dict, user_need: str, need_is_spec: bool = False):
    total = len(blackboard["phases"])

    # Les compétences viennent du BLACKBOARD (skills_required, matérialisé par
    # inject_system_skills avant la porte humaine), avec une garantie d'orchestrateur :
    # les compétences système présentes (ux, proto-coding) sont TOUJOURS appliquées —
    # s'il existe un skill 'ux', chaque phase l'utilise, quoi que dise le fichier.
    system_skills = inject_system_skills(blackboard)
    if system_skills:
        print(f"   📦 Compétences système injectées à chaque phase : {', '.join(system_skills)}")
    else:
        print(f"   ⚠️  Aucune compétence système trouvée dans {SKILLS_DIR} : "
              f"les phases et la review tourneront sans grille (qualité dégradée).")

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} déjà produite : {phase['name']}")
            continue

        print(f"\n{'='*50}\n🎨 PHASE {phase['id']}/{total} : {phase['name']}\n{'='*50}")

        # Fenêtre de contexte : le designer-dev ne reçoit que la tranche de spec couverte par
        # SA phase (champ 'covers'), jamais la spec entière — sauf dégradation gracieuse.
        phase_need = extract_spec_slice(user_need, phase.get("covers")) if need_is_spec else user_need
        if need_is_spec and len(phase_need) < len(user_need):
            print(f"   ✂️  Spec tranchée pour la phase : {len(phase_need)}/{len(user_need)} caractères "
                  f"(couvre {', '.join(phase.get('covers') or [])}).")

        # Compétences de CETTE phase : le skills_required du blackboard, avec les
        # compétences système garanties en tête (phase_skills filtre les skills du
        # pipeline et ceux absents du disque — un skill halluciné ne charge rien).
        skills_for_phase = phase_skills(phase)
        extras = [s for s in skills_for_phase if s not in system_skills]
        if extras:
            print(f"   📦 Compétences additionnelles du blackboard : {', '.join(extras)}")
        skills_context = load_skills(skills_for_phase)

        attempts = 0
        success = False
        critic_feedback = "Premier jet — aucune critique précédente."
        # Jalon du diff par phase (garde anti-fantôme) : vide sans git → fallback mtime.
        phase_start_sha = git_head_sha()
        # Référentiel temporel de la PHASE, capturé UNE seule fois (un référentiel
        # par-tentative reclasserait à tort en « fantôme » un fichier écrit à une
        # tentative précédente et re-déclaré inchangé ensuite).
        phase_started = time.time()

        phase["status"]  = "IN_PROGRESS"
        phase["verdict"] = "PENDING"
        save_blackboard(blackboard)
        cleanup_sentinels(phase["id"])

        # Boucle qualité par phase (« loop engineering ») : designer-dev → gardes
        # MÉCANIQUES (anti-fantôme, tokens hallucinés — gratuites, avant tout LLM) →
        # Agent Vérificateur indépendant (verdict OK/REJECTED + écarts, retransmis au
        # designer-dev à la tentative suivante). Le contrôle global (grille UX complète,
        # conformité de bout en bout) reste à la review finale.
        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1
            cleanup_sentinels(phase["id"])
            print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Phase {phase['id']} — lancement du Designer-Dev...")

            coder_prompt = build_coder_prompt(phase, blackboard, phase_need, skills_context,
                                              critic_feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(coder_prompt))
            RUNNER.send_task(coder_prompt)

            if not wait_for_file_creation(done_sentinel(phase["id"], attempts)):
                print(f"⏱️  Le designer-dev n'a pas signalé la fin (sentinelle '{done_sentinel(phase['id'], attempts)}' absente). Nouvelle tentative.")
                RUNNER.new_context()
                continue

            touched_files = read_touched_files(phase["id"], attempts)

            # ── GARDE ANTI « DESIGNER FANTÔME » (mécanique, gratuite) ── : sentinelle
            # écrite sans travail réel. On rejette AVANT de payer un vérificateur.
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
                      f"déclaré n'a été touché (designer fantôme).")
                RUNNER.new_context()
                continue

            # ── GARDE « TOKENS HALLUCINÉS » (mécanique, gratuite) ── : tout var(--x)
            # consommé par les fichiers produits doit être défini dans un CSS du projet.
            # Un design system inventé se trahit d'abord ici — feedback exact, zéro LLM.
            missing_tokens = undefined_css_tokens(touched_files)
            if missing_tokens:
                details = "\n".join(f"- var(--{token}) consommé dans {path} mais défini nulle part"
                                    for token, path in missing_tokens)
                critic_feedback = (
                    f"Des tokens CSS consommés par tes fichiers ne sont DÉFINIS nulle part dans "
                    f"le projet :\n{details}\nUtilise UNIQUEMENT des tokens existants "
                    f"(assets/css/tokens.css est la source unique — design system : "
                    f"{design_system_rule(blackboard)}) ; si un token manque vraiment à la "
                    f"checklist de CETTE phase, définis-le dans tokens.css depuis la source "
                    f"déclarée, jamais en l'inventant."
                )
                phase["critic_feedback"] = critic_feedback
                save_blackboard(blackboard)
                print(f"🎨 [REJET] Tentative {attempts} : {len(missing_tokens)} token(s) CSS "
                      f"consommé(s) mais défini(s) nulle part (design system halluciné ?).")
                RUNNER.new_context()
                continue

            # ── AGENT VÉRIFICATEUR DE PHASE (contexte neuf, indépendant) ── : relit les
            # fichiers déclarés contre la checklist et le design system. Un vérificateur
            # MUET ne bloque pas le run : une relance (le travail du designer n'a pas
            # changé), puis acceptation avec avertissement — les gardes mécaniques ont
            # déjà tourné et la review finale re-contrôle tout.
            print(f"  → Designer-dev terminé ({len(touched_files)} fichier(s) déclaré(s)). "
                  f"Vérification par un agent indépendant...")
            verdict_path = verdict_sentinel(phase["id"], attempts)
            cleanup_pipeline_sentinel(verdict_path)
            verifier_prompt = build_phase_verifier_prompt(phase, blackboard, phase_need,
                                                          touched_files, attempts)
            RUNNER.new_context()
            mm_audit.event("agent_task", prompt_bytes=len(verifier_prompt))
            RUNNER.send_task(verifier_prompt)
            got_verdict = wait_for_file_creation(verdict_path)
            if not got_verdict:
                print("⏱️  Le vérificateur n'a rendu aucun verdict dans le temps imparti : une relance...")
                RUNNER.new_context()
                mm_audit.event("agent_task", prompt_bytes=len(verifier_prompt))
                RUNNER.send_task(verifier_prompt)
                got_verdict = wait_for_file_creation(verdict_path)

            if not got_verdict:
                print("⚠️  Vérificateur muet après relance : phase acceptée sur les seules gardes "
                      "mécaniques (la review finale re-contrôlera l'ensemble).")
                is_ok, gaps = True, ""
            else:
                is_ok, gaps = read_review_verdict(verdict_path)
                cleanup_pipeline_sentinel(verdict_path)

            if is_ok:
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ Phase {phase['id']} produite et VÉRIFIÉE "
                      f"({len(touched_files)} fichier(s) déclaré(s)).")
                commit_phase(f"phase {phase['id']}: {phase['name']}")
            else:
                critic_feedback = gaps
                phase["critic_feedback"] = gaps
                save_blackboard(blackboard)
                print(f"⚠️  [REJET] Tentative {attempts} : le vérificateur a relevé des écarts. "
                      f"Retransmis au designer-dev :\n{gaps}")
                RUNNER.new_context()

        if not success:
            phase["status"]  = "TODO"
            phase["verdict"] = "REJECTED"
            phase["critic_feedback"] = critic_feedback
            save_blackboard(blackboard)
            cleanup_all_sentinels()
            print_failure_message(phase, blackboard, critic_feedback)
            RUNNER.kill()
            sys.exit(1)

        RUNNER.new_context()


# ─── ÉTAPE 5 : REVIEW GLOBALE UNIQUE (UX + CONFORMITÉ BLACKBOARD) ──────────────

def review_verdict_sentinel(attempt: int) -> str:
    return f".pipeline_review.attempt{attempt}.verdict"


def fix_done_sentinel(attempt: int) -> str:
    return f".pipeline_fix.attempt{attempt}.done"


def execute_final_review(blackboard: dict, user_need: str) -> bool:
    """Reviewer global unique : vérifie (A) la grille UX et (B) la conformité au blackboard.

    Rend un verdict actionnable ; en cas d'écarts, boucle de correction bornée
    (MAX_REVIEW_ATTEMPTS passes review -> correction -> re-review). Retourne True si conforme.
    """
    print(f"\n{'='*50}\n🔎 ETAPE 5 : REVIEW GLOBALE (UX + CONFORMITÉ AU BLACKBOARD)\n{'='*50}")

    grille = load_skills(PROTO_SYSTEM_SKILLS)
    scope_block, scope_files = build_review_scope_block(blackboard)

    success = False
    last_gaps = ""
    for attempt in range(1, MAX_REVIEW_ATTEMPTS + 1):
        # Purge le rapport résiduel d'une passe précédente : l'attente ci-dessous ne doit
        # observer que le rapport de CETTE passe.
        if os.path.exists(REVIEW_REPORT_FILE):
            os.remove(REVIEW_REPORT_FILE)
        verdict_path = review_verdict_sentinel(attempt)
        cleanup_pipeline_sentinel(verdict_path)

        print(f"\n🧪 [PASSE {attempt}/{MAX_REVIEW_ATTEMPTS}] Lancement du Reviewer global...")
        RUNNER.new_context()
        review_prompt = build_review_prompt(blackboard, user_need, grille, scope_block, verdict_path)
        mm_audit.event("agent_task", prompt_bytes=len(review_prompt))
        RUNNER.send_task(review_prompt)

        # Le VERDICT (sentinelle) est le signal de fin qui pilote la boucle ; le rapport est
        # l'artefact humain, écrit juste avant. On attend donc le verdict (et non le rapport,
        # dont le nom pourrait être halluciné) ; le rapport est best-effort.
        if not wait_for_file_creation(verdict_path):
            print("⏱️  Le reviewer n'a rendu aucun verdict dans le temps imparti.")
            last_gaps = "Le reviewer n'a pas rendu de verdict (timeout)."
            break

        is_ok, gaps = read_review_verdict(verdict_path)
        cleanup_pipeline_sentinel(verdict_path)
        if is_ok:
            success = True
            print("✅ [REVIEW] Prototype CONFORME : UX respectée et blackboard honoré.")
            break

        last_gaps = gaps
        print(f"⚠️  [REVIEW] Écarts relevés (passe {attempt}) :\n{gaps}")

        if attempt == MAX_REVIEW_ATTEMPTS:
            # Dernière passe : on ne relance pas de correction qui ne serait pas re-vérifiée.
            break

        print("🛠️  Lancement d'une passe de correction ciblée sur les écarts...")
        fix_done = fix_done_sentinel(attempt)
        cleanup_pipeline_sentinel(fix_done)
        RUNNER.new_context()
        fix_prompt = build_fix_prompt(blackboard, grille, scope_block, gaps, fix_done)
        mm_audit.event("agent_task", prompt_bytes=len(fix_prompt))
        RUNNER.send_task(fix_prompt)
        if not wait_for_file_creation(fix_done):
            print("⏱️  La passe de correction n'a pas signalé sa fin : nouvelle revue quand même.")
        cleanup_pipeline_sentinel(fix_done)
        # Le périmètre peut s'être étendu (nouveaux fichiers créés par la correction).
        scope_block, scope_files = build_review_scope_block(blackboard)

    # Nettoyage des fichiers temporaires, quelle que soit l'issue.
    for tmp_f in [TMP_CODER_FILE, TMP_REVIEW_FILE, TMP_FIX_FILE, TMP_ARCHITECT_FILE,
                  TMP_PO_FILE, TMP_PLAN_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_sentinels()

    if success:
        commit_phase("review: prototype conforme (UX + blackboard)")
    else:
        commit_phase("review: ecarts persistants apres correction")
        print(f"\n⚠️  Des écarts subsistent après {MAX_REVIEW_ATTEMPTS} passe(s). "
              f"Le prototype reste livré et exploitable.")
        print(f"   Détail dans '{REVIEW_REPORT_FILE}'. Derniers écarts relevés :\n{last_gaps}")

    print_open_hint(scope_files)
    return success


def print_open_hint(scope_files: list):
    """Indique au designer comment ouvrir le prototype (point d'entrée HTML)."""
    htmls = [f for f in scope_files if f.lower().endswith(".html")]
    entry = next((f for f in htmls if os.path.basename(f).lower() == "index.html"), None)
    if entry is None and htmls:
        entry = sorted(htmls)[0]
    if entry is None and os.path.exists("index.html"):
        entry = "index.html"
    print(f"\n{'─'*50}")
    if entry:
        print(f"👀 Ouvre le prototype : double-clique sur '{entry}' (ou glisse-le dans ton navigateur).")
        print(f"   Au besoin, sers-le en local : python3 -m http.server 8000  puis http://localhost:8000")
    else:
        print("👀 Ouvre le fichier HTML d'entrée du prototype dans ton navigateur.")
    print(f"{'─'*50}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)

def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "proto", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # Une sentinelle d'approbation orpheline (spec.md supprimée depuis) ne doit jamais
    # valider une spec FUTURE : on la purge avant toute chose.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # 🎨 PORTE DESIGN SYSTEM (avant tout agent, avant même le boot du harness) : la
    # déclaration ne peut venir QUE de l'humain. Trois états : déclaré dans need.md
    # (un accord antérieur devenu caduc est purgé) ; déjà confirmé « sans design
    # system » lors d'un run précédent (reprise silencieuse) ; sinon porte y/n.
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        ds_declared = read_design_system_from_need(f.read())
    if ds_declared:
        if os.path.exists(DS_ACK_SENTINEL):
            os.remove(DS_ACK_SENTINEL)
        print(f"🎨 Design system déclaré dans '{NEED_FILE}' : il sera transcrit dans la spec "
              f"puis transporté jusqu'à chaque agent.")
    elif os.path.exists(DS_ACK_SENTINEL):
        print(f"🔄 Tokens par défaut déjà confirmés lors d'un run précédent "
              f"('{DS_ACK_SENTINEL}'). Porte design system passée.")
    else:
        confirm_default_design_system()

    # 🚀 ÉTAPE ZÉRO : Boot immédiat du harness Data Center dans Tmux
    RUNNER.start()

    # Étape 1 : Affinage PO/UX via le TUI (need.md → spec.md), validé par l'HUMAIN.
    if not os.path.exists(SPEC_FILE):
        generate_spec_from_need_tui(ds_declared)
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

    # Étape 3 : Configuration du Blackboard via le TUI
    if not os.path.exists(BLACKBOARD_FILE):
        blackboard = transform_plan_to_blackboard_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 '{BLACKBOARD_FILE}' existant trouvé. Chargement...")
        blackboard = load_blackboard()

    # Le contexte « besoin » injecté aux agents est la SPEC affinée et validée (critères
    # observables) ; need.md ne sert que de secours (anciens runs). need_is_spec conditionne
    # la découpe par US (extract_spec_slice) en production.
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Design system tel que le pipeline le transporte : relu dans la SPEC validée par
    # l'humain (ou need.md en secours), pour la garde de transport du blackboard.
    ds_transported = read_design_system_from_need(user_need)

    # Garde-fou sur phases[].id : un id dupliqué corrompt silencieusement le canal des
    # sentinelles, on s'arrête AVANT de payer un run.
    fatal_ids, soft_ids = validate_phase_ids(blackboard)
    for warning in soft_ids:
        print(f"⚠️  {warning}")
    if fatal_ids:
        for problem in fatal_ids:
            print(f"❌ {problem}")
        fail_pipeline(f"   → Corrige '{BLACKBOARD_FILE}' puis relance.")

    # La séquence récap → y/n BOUCLE : l'humain peut éditer le blackboard dans un autre
    # terminal pendant que le prompt attend ; tout changement déclenche un rechargement et une
    # nouvelle confirmation sur le récap réaffiché.
    while True:
        # Compétences : matérialisées PAR L'ORCHESTRATEUR dans blackboard.yaml (champ
        # skills_required de chaque phase) AVANT la porte humaine — l'humain lit dans le
        # fichier ce qui sera réellement appliqué, et une édition qui retirerait une
        # compétence système est réinjectée au rechargement (le skill 'ux', s'il existe,
        # est TOUJOURS appliqué).
        system_skills = inject_system_skills(blackboard)
        # Même garantie pour le design system : réparé mécaniquement s'il s'est perdu
        # entre le plan et le blackboard (recopie de la déclaration humaine, jamais plus).
        ensure_design_system(blackboard, ds_transported)
        if need_is_spec:
            coverage_warnings = check_spec_coverage(blackboard, user_need)
            if coverage_warnings:
                print("\n⚠️  Traçabilité spec → phases :")
                for warning in coverage_warnings:
                    print(f"   - {warning}")

        print(f"\n{'='*50}")
        print(f"📋 BLACKBOARD PRÊT — Récapitulatif :")
        print(f"   Projet : {blackboard['project']}")
        print(f"   Stack (global_rules.target) : {(blackboard.get('global_rules') or {}).get('target') or '(non précisée)'}")
        print(f"   Design system : {design_system_rule(blackboard)}")
        print(f"   Compétences appliquées à chaque phase (skills_required) : "
              f"{', '.join(system_skills) or '(aucune trouvée)'}")
        if UX_SKILL not in system_skills:
            print(f"   ⚠️  Skill '{UX_SKILL}' introuvable ({SKILLS_DIR}/{UX_SKILL}/SKILL.md) : "
                  f"la grille UX ne sera PAS appliquée — qualité dégradée.")
        print(f"   Phases : {len(blackboard['phases'])}")
        for p in blackboard['phases']:
            covers = ', '.join(p.get('covers') or [])
            print(f"   Phase {p['id']}: {p['name']} "
                  f"({len(p.get('tasks', []))} tâche(s) ; couvre: {covers or '?'})")
        print(f"{'='*50}")
        print(f"   Contrôle qualité : gardes mécaniques (fantôme, tokens) + vérificateur par phase, puis review UX + conformité + design system à la fin.")
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
            RUNNER.kill()
            sys.exit(1)

    check_proto_skills()

    # Jalons git (best-effort) : baseline maintenant, puis un commit par phase signalée.
    ensure_phase_repo()

    # Référentiel du run : tout ce qui diffère de ce sha est l'œuvre de l'usine, jamais le
    # legacy. Persisté car une REPRISE recapturerait un HEAD déjà avancé, et la review raterait
    # alors les fichiers des phases antérieures.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    print(f"\n🚀 Démarrage de la production du prototype : {blackboard['project']}")

    # Étape 4 : Boucle de production (sans vérificateur par phase)
    run_production_phases(blackboard, user_need, need_is_spec)

    # Étape 5 : Review globale unique (UX + conformité au blackboard)
    review_ok = execute_final_review(blackboard, user_need)

    # Fermeture propre
    RUNNER.kill()
    # Run terminé : on purge les marqueurs d'approbation (spec, design system), gardés hors
    # de cleanup_all_sentinels car ils doivent survivre à une INTERRUPTION.
    if os.path.exists(SPEC_APPROVED_SENTINEL):
        os.remove(SPEC_APPROVED_SENTINEL)
    if os.path.exists(DS_ACK_SENTINEL):
        os.remove(DS_ACK_SENTINEL)

    if review_ok:
        print("\n🏁 [CONGRATULATIONS] Prototype produit ET validé (UX + blackboard) en un seul run !")
    else:
        print(f"\n🏁 Prototype produit. Review finale : écarts subsistants, voir '{REVIEW_REPORT_FILE}'.")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    POLL_INTERVAL=POLL_INTERVAL,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    US_HEADING_RE=US_HEADING_RE,
    run_git=run_git,
)


if __name__ == "__main__":
    main()
