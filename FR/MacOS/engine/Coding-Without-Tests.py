#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à code avec un harness d'agent + tmux (Version Full TUI Data Center)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « CODE ONLY » : plan sans tests (skill plan-no-test), vérification de chaque
phase par un Agent Vérificateur LLM (pas d'exécution de tests).

Pipeline PO → Architecte :
  - Étape 1 : un Agent PO affine 'need.md' en spécification métier 'spec.md' (user stories,
    critères d'acceptation, hors-périmètre, hypothèses), VALIDÉE par l'humain.
  - Étape 2 : un Agent Architecte (mode code-only) convertit 'spec.md' en plan d'implémentation.
  - Étape 3 : la conversion en blackboard est une RECOPIE mécanique des décisions du plan.

Stratégie Data Center & TUI :
  - La session tmux est initialisée DIRECTEMENT au démarrage.
  - On lance directement le TUI du harness choisi (Modèle Cloud / Data Center).
  - Les étapes 1 (Spec PO), 2 (Plan) et 3 (Blackboard) sont exécutées directement dans le TUI.
  - Production : chaque phase passe par un Agent Codeur puis un Agent Vérificateur
    indépendant qui RELIT le code réellement produit. Les agents communiquent par
    fichiers sentinelles ('.phase_<id>.done' / '.phase_<id>.verdict') ; le seul
    maître du blackboard est l'orchestrateur Python (aucune écriture concurrente).
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
    build_skills_dictionary, collect_spec_us_ids, done_sentinel, git_head_sha,
    inject_skills_dictionary, is_orchestration_file, load_blackboard, load_skills,
    signal_handler, validate_all_skills, wait_for_file_creation,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="factory", messages={
    "follow":   "   👀 Suis en direct dans un autre terminal : tmux attach -t {session}",
    "new_warn": "   ⚠️  Le TUI n'a peut-être pas été réinitialisé (littéral '/new' encore "
                "à l'écran) : si le run dérive, vérifier avec tmux attach.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-no-test/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Skills système du pipeline : jamais routés vers les phases de production.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-to-blackboard", "refacto"}

# Fichiers temporaires de routage de contexte
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_VERIFIER_FILE     = RUNNER.tmp_file("verifier")
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
# rien (un timeout peut laisser derrière lui une spec jamais validée, voir fail_pipeline).
# Volontairement hors du motif '.pipeline_*.done' purgé par cleanup_all_sentinels :
# l'approbation doit survivre à une reprise.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Nom de la session tmux, suffixé d'une empreinte du répertoire projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session (les prompts du
# projet B atterriraient dans l'agent du projet A). Reprendre le MÊME projet
# réutilise sa session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par phase (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable pipeline accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes).
                                       # 30 s : un modèle local lent qui marque une pause entre deux écritures
                                       # ne doit pas voir accepté son livrable à moitié écrit (voir aussi structural_check)


def fail_pipeline(message: str):
    """Point de sortie unique des échecs d'étape du pipeline (étapes 1 à 3).

    Tue toujours la session tmux AVANT de sortir : une sortie qui laisse l'agent vivant
    le laisse finir d'écrire son livrable APRÈS l'abandon de l'orchestrateur — à la
    relance, ce fichier à moitié validé serait pris pour un état de reprise valide
    (c'est ainsi qu'une spec jamais approuvée devenait la source de vérité). RUNNER.kill()
    est sans effet quand aucune session n'existe : ce helper est donc sûr partout.
    """
    mm_audit.end("failed")
    print(message)
    RUNNER.kill()
    sys.exit(1)


# ─── SENTINELLES DE PHASE (CANAL CODEUR / VÉRIFICATEUR → ORCHESTRATEUR) ────────

def verdict_sentinel(phase_id: int, attempt: int) -> str:
    """Fichier écrit par le Vérificateur (signal 'OK' ou 'REJECTED' + motifs)."""
    return f".phase_{phase_id}.attempt{attempt}.verdict"


def cleanup_sentinels(phase_id: int):
    """Supprime toutes les sentinelles (toutes tentatives) d'une phase."""
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
                or (name.startswith(".pipeline_") and name.endswith(".done")):
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
    """Lit la liste des fichiers déclarés par le Codeur dans son sentinelle .done.

    Les petits modèles formatent souvent la liste en puces ('- src/foo.ts', '* a.py',
    '1. b.go') : les marqueurs de liste en tête sont retirés pour que le vérificateur
    reçoive de vrais chemins plutôt que des lignes décorées.
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


def read_verdict(phase_id: int, attempt: int) -> tuple:
    """Lit le verdict du Vérificateur. Retourne (is_ok: bool, feedback: str).

    Parsing tolérant : on ignore les lignes vides et les barrières markdown en
    tête, puis on lit le premier mot de la première ligne utile. 'OK', 'OK.',
    'OK, conforme'... valident ; tout le reste (dont 'REJECTED') rejette.
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


# ─── JALONS GIT (BEST-EFFORT) ─────────────────────────────────────────────────
# BEST-EFFORT : sans git (binaire absent, échec d'init), l'usine tourne à l'identique
# mais sans jalons — dégradation gracieuse, ne jamais bloquer le run pour de l'outillage.
# Dans cette variante code seul (vérificateur LLM, pas de déclencheur de rollback), git
# apporte une piste d'audit : baseline au démarrage de la production, un commit par
# phase validée, un après la refacto — un point de rollback manuel par étape pour l'humain.

_GIT = {"enabled": False}

# Identité passée à chaque commande : l'usine ne doit pas dépendre de la config git de la machine.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

GITIGNORE_BODY = f"""# Artefacts d'orchestration MAIster-Mind (éphémères)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.spec_approved
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

    --allow-empty : une phase validée qui n'a rien changé reçoit quand même son
    commit jalon, pour que les shas par phase restent fiables pour les diffs et
    rollbacks manuels.
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

    Aucun commit intermédiaire n'est posé pendant une phase : le travail vit dans l'arbre de
    travail. On compare donc l'arbre au sha de référence ('git diff <sha>', fichiers suivis)
    et on ajoute les fichiers non suivis ('ls-files --others').
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


# Artefacts de l'orchestrateur (jamais du code produit) : exclus du périmètre de refacto.
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_VERIFIER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


def ensure_phase_repo():
    """Jalons git par phase, mis en place avant la production (best-effort).

    Si le projet est déjà un dépôt git (géré par l'humain), il est réutilisé TEL QUEL.
    Sinon 'git init' + un .gitignore minimal (fichiers d'orchestration éphémères
    uniquement) + un commit de baseline. Sans git : avertit une fois et tourne sans jalons.
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
    commit_phase("baseline: factory start")


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def spec_structural_check(path: str) -> bool:
    """Plancher structurel minimal pour une spec acceptée SANS sentinelle : sa section
    obligatoire « Hors périmètre » doit être présente (une spec à moitié écrite s'arrête avant)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "hors périmètre" in f.read().lower()
    except OSError:
        return False


def plan_structural_check(path: str) -> bool:
    """Plancher structurel minimal pour un plan accepté SANS sentinelle : le bloc d'en-tête
    obligatoire « Stack & Vérification » doit être présent."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "stack & vérification" in f.read().lower()
    except OSError:
        return False


def blackboard_structural_check(path: str) -> bool:
    """Plancher structurel minimal pour un blackboard accepté SANS sentinelle : le YAML
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
    jamais bloquer 10 minutes pour un simple oubli de signal). Le paramètre optionnel
    'structural_check' ne durcit QUE ce filet : un livrable stable mais structurellement
    incomplet continue d'attendre (l'agent peut marquer une pause plus longue que la
    fenêtre de stabilité) jusqu'au timeout global.
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
    with open(BLACKBOARD_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
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


def validate_phase_ids(blackboard: dict) -> tuple:
    """Garde-fous d'unicité/séquence sur phases[].id (produits par un petit LLM faillible).
    Retourne (fatal, soft).

    Un id dupliqué fait PARTAGER à deux phases leurs sentinelles '.phase_N.attemptM.done' /
    '.verdict' (faux signaux de fin) : fatal. Une séquence non contiguë est
    simplement signalée.
    """
    fatal, soft = [], []
    phases = blackboard.get("phases") if isinstance(blackboard, dict) else None
    if not isinstance(phases, list) or not phases:
        return ["Bloc 'phases' manquant ou vide : rien à produire."], []
    ids = [str(phase.get("id")) for phase in phases if isinstance(phase, dict) and "id" in phase]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        fatal.append(
            f"phases[].id dupliqués ({', '.join(duplicated)}) : les sentinelles '.phase_N.attemptM.*' "
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


# ─── DÉCOUPE DE LA SPEC PAR PHASE (FENÊTRE DE CONTEXTE) ───────────────────────

# En-tête d'une user story dans la spec PO (ex. « ### US-1 : Calcul du solde »).
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Tranche de la spec limitée aux US couvertes par la phase (+ tout le hors-US).

    Les prompts codeur et vérificateur embarquaient la spec ENTIÈRE à chaque phase : sur
    une grosse spec, chaque phase payait tout le contexte. On ne garde ici que les sections
    '### US-n' listées dans 'covers' (champ recopié du plan par le compilateur blackboard),
    plus tout ce qui n'est pas une section d'US (objectif métier, contraintes,
    hors-périmètre, hypothèses). Prudence de petit modèle : si 'covers' est vide, si la
    spec ne suit pas le format à US, ou si aucune US couverte n'y est trouvée, on renvoie
    la spec ENTIÈRE (dégradation gracieuse — ne jamais priver l'agent de contexte).
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
    probable du compilateur blackboard), et US de la spec couverte par aucune phase
    (exigence potentiellement OUBLIÉE par l'Architecte). Warn-only : 'covers' est
    optionnel ; c'est l'œil humain au y/n qui tranche.
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
    # une spec sans cette sentinelle repasse par le y/n au lieu d'être crue sur parole.
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
    # elle est recopiée mécaniquement en aval par le compilateur blackboard.
    plan_spec = inject_skills_dictionary(plan_spec)
    with open(TMP_PLAN_FILE, "w", encoding="utf-8") as f:
        f.write(plan_spec)

    print("   📚 Skills détectés et proposés à l'architecte :")
    for line in (build_skills_dictionary().splitlines() or ["(aucun skill de phase détecté)"]):
        print(f"      {line}")

    planning_prompt = f"""Lis le fichier '{SPEC_FILE}' à la racine de notre projet (spécification métier validée), ainsi que les consignes d'architecture du fichier '{TMP_PLAN_FILE}'.
Tu es un Architecte Logiciel senior. En appliquant SCRUPULEUSEMENT les consignes de '{TMP_PLAN_FILE}', génère un plan d'implémentation séquentiel au format Markdown et sauvegarde-le DIRECTEMENT dans un nouveau fichier nommé '{PLAN_FILE}' à la racine du projet.

Directives pour le fichier '{PLAN_FILE}' :
- Le plan DOIT commencer par le bloc « Stack & Vérification » (avec la commande de COMPILATION, verdict de toutes les phases en mode code-only) et CHAQUE phase DOIT déclarer sa Nature et son champ « Couvre » (US-x) : les étapes suivantes du pipeline recopient ces décisions sans les déduire.
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

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    # Contexte de l'architecte et liste de lecture, portés depuis le plan : un GUIDAGE qui
    # épargne au codeur une ré-exploration libre du projet. Rien ne sandboxe ses lectures,
    # le gain de fenêtre de contexte est donc probabiliste, pas garanti.
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
Architecture: {blackboard['global_rules']['target']}
Design & CSS: {blackboard['global_rules']['styling']}
Interdictions: {blackboard['global_rules']['constraints']}
Accessibilité: {blackboard['global_rules']['accessibility']}

{skills_context}
--- CONTRAT COMPORTEMENTAL ---
Tu es un Agent Codeur ultra-spécialisé pour la Phase {phase['id']} UNIQUEMENT.
Tu n'implémentes QUE les tâches de cette phase. Arrête-toi dès que c'est fait.
Principe YAGNI : tu ne réalises rien qui ne soit pas explicitement demandé.

--- RÈGLE ABSOLUE SUR LES TESTS ---
INTERDICTION formelle de lire, modifier, corriger ou ajouter des fichiers de test.
Les tests existants sont hors limite. Ignore-les complètement.
Concentre-toi uniquement sur le code source de production.

{context_block}{files_block}--- BESOIN INITIAL ---
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


def build_verifier_prompt(phase: dict, blackboard: dict, user_need: str,
                          touched_files: list, attempt: int) -> str:
    files_block = "\n".join(f"- {p}" for p in touched_files) if touched_files \
        else "(aucun fichier déclaré — explore le projet avec tes outils pour retrouver le travail du codeur)"

    full_context = f"""Tu es un Agent Vérificateur QA Senior, strict et indépendant.
Ta mission : vérifier que la Phase '{phase['name']}' a été RÉELLEMENT implémentée dans le code,
conformément à la checklist ET aux règles globales du projet.

--- RÈGLES GLOBALES À FAIRE RESPECTER ---
Architecture: {blackboard['global_rules']['target']}
Design & CSS: {blackboard['global_rules']['styling']}
Interdictions: {blackboard['global_rules']['constraints']}
Accessibilité: {blackboard['global_rules']['accessibility']}

--- BESOIN INITIAL ---
{user_need}

--- CHECKLIST DE LA PHASE À VÉRIFIER ---
{chr(10).join([f'- {t}' for t in phase['tasks']])}

--- FICHIERS MODIFIÉS PAR LE CODEUR ---
{files_block}

--- MÉTHODE DE VÉRIFICATION OBLIGATOIRE ---
1. Ouvre et LIS réellement le contenu de chaque fichier ci-dessus avec tes outils de lecture. Ne te fie à aucun résumé.
2. Confronte le code réel à CHAQUE tâche de la checklist ET à CHAQUE règle globale.
3. Ne valide que ce que tu as effectivement constaté dans le code.

--- VERDICT ---
Écris ta conclusion dans le fichier sentinelle '{verdict_sentinel(phase['id'], attempt)}' à la racine du projet :
  - Si tout est implémenté sans défaut et conforme aux règles : la PREMIÈRE ligne contient EXACTEMENT le mot "OK" (rien d'autre).
  - Sinon : la PREMIÈRE ligne contient EXACTEMENT le mot "REJECTED", puis les lignes suivantes listent
    précisément les manques, erreurs ou violations à corriger.
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE} : l'orchestrateur le met à jour à partir de ton verdict.
"""
    with open(TMP_VERIFIER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Lis le fichier d'audit '{TMP_VERIFIER_FILE}' à la racine du projet. Suis ses instructions pour vérifier la Phase {phase['id']}."


# ─── MESSAGE D'ÉCHEC ──────────────────────────────────────────────────────────


def print_failure_message(phase: dict, blackboard: dict, critic_feedback: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    print(f"""
{'='*60}
❌ La phase {phase['id']} « {phase['name']} » n'a pas convergé après {MAX_ATTEMPTS} tentatives.

   Dernier point bloquant relevé par le vérificateur :
   « {critic_feedback} »

💡 Le modèle actuel ({model}) cale sur cette étape précise.
   Le plus efficace : relance après avoir amené un modèle un cran au-dessus,
   soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : les {done_count} phase(s) déjà validée(s) seront reprises
   automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
{'='*60}
""")


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

        # Fenêtre de contexte : codeur ET vérificateur ne reçoivent que la tranche de spec
        # couverte par SA phase (champ 'covers'), jamais la spec entière — sauf dégradation
        # gracieuse (covers absent ou spec sans format à US).
        phase_need = extract_spec_slice(user_need, phase.get("covers")) if need_is_spec else user_need
        if need_is_spec and len(phase_need) < len(user_need):
            print(f"   ✂️  Spec tranchée pour la phase : {len(phase_need)}/{len(user_need)} caractères "
                  f"(couvre {', '.join(phase.get('covers') or [])}).")

        attempts = 0
        success  = False
        critic_feedback = "Premier jet — aucune critique précédente."

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
            print(f"  → Codeur terminé ({len(touched_files)} fichier(s) déclaré(s)). Routage vers le Vérificateur QA indépendant...")

            RUNNER.new_context()
            verifier_prompt = build_verifier_prompt(phase, blackboard, phase_need, touched_files, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(verifier_prompt))
            RUNNER.send_task(verifier_prompt)

            if not wait_for_file_creation(verdict_sentinel(phase["id"], attempts)):
                print("⏱️  Le vérificateur n'a rendu aucun verdict. Nouvelle tentative.")
                RUNNER.new_context()
                continue

            is_ok, feedback = read_verdict(phase["id"], attempts)

            if is_ok:
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCÈS] Phase {phase['id']} validée par le Vérificateur QA.")
                commit_phase(f"phase {phase['id']}: {phase['name']}")
            else:
                critic_feedback = feedback
                phase["critic_feedback"] = feedback
                save_blackboard(blackboard)
                print(f"⚠️  [REJET] Tentative {attempts}. Motif retransmis au codeur :\n{feedback}")
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


def execute_final_refactoring(blackboard: dict, user_need: str):
    print(f"\n{'='*50}\n🛡️  ETAPE 5 : AGENT RÉFACTORISATION & POLISH FINAL\n{'='*50}")

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
4. Tu ne supprimes NI n'affaiblis JAMAIS un test existant pour faire passer quoi que ce
   soit : si un test passe au rouge, c'est le code de production qu'il faut corriger.
5. Rédige un rapport technique récapitulant les optimisations appliquées dans {REFACTO_REPORT_FILE}.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{REFACTO_REPORT_FILE}', crée le fichier sentinelle
'{REFACTO_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur.
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

    # Même contrat de sentinelle que les étapes 1 à 3 (avec le filet de stabilité hérité de
    # wait_for_pipeline_file) : la simple EXISTENCE du rapport n'est pas un signal de fin —
    # l'agent peut le créer puis continuer à modifier du code.
    if wait_for_pipeline_file(REFACTO_REPORT_FILE, REFACTO_DONE_SENTINEL):
        print(f"✅ Rapport de refactoring généré dans '{REFACTO_REPORT_FILE}'.")
    else:
        print(f"⚠️  Timeout : '{REFACTO_REPORT_FILE}' non généré (la refacto a peut-être quand même modifié du code).")

    # Nettoie les fichiers temporaires, quelle que soit l'issue de la refacto.
    for tmp_f in [TMP_CODER_FILE, TMP_VERIFIER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE,
                  TMP_PO_FILE, TMP_PLAN_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_sentinels()
    # Commit jalon : cette variante n'a pas de verdict mécanique de refacto (vérificateur
    # LLM uniquement), la paire avant/après refacto du log git est donc la poignée de
    # rollback manuel de l'humain.
    commit_phase("refacto: final polish (no mechanical verdict in this variant)")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)

def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "code", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # Une sentinelle d'approbation orpheline (spec.md supprimée depuis) ne doit jamais
    # valider une spec FUTURE : on la purge avant toute chose.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # 🚀 ÉTAPE ZÉRO : Boot immédiat du harness Data Center dans Tmux
    RUNNER.start()

    # Étape 1 : Affinage PO via le TUI (need.md → spec.md), validé par l'HUMAIN.
    # La spec validée devient la source de vérité de tout l'aval (plan, production).
    # Trois états de reprise : pas de spec → génération + confirmation ; spec SANS la
    # sentinelle d'approbation (run interrompu : timeout, Ctrl-C pendant le y/n) →
    # redemander à l'humain plutôt que de croire un fichier peut-être jamais validé ;
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

    # Étape 3 : Configuration du Blackboard via le TUI
    if not os.path.exists(BLACKBOARD_FILE):
        blackboard = transform_plan_to_blackboard_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 '{BLACKBOARD_FILE}' existant trouvé. Chargement...")
        blackboard = load_blackboard()

    # Le contexte « besoin » injecté aux agents de production est la SPEC affinée et validée
    # (critères d'acceptation testables) ; need.md ne sert que de secours (anciens runs).
    # need_is_spec conditionne la découpe par US (extract_spec_slice) en production.
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Garde-fou sur phases[].id (blackboard produit par un petit LLM faillible) : un id
    # dupliqué corrompt silencieusement le canal des sentinelles, on s'arrête AVANT de
    # payer un run.
    fatal_ids, soft_ids = validate_phase_ids(blackboard)
    for warning in soft_ids:
        print(f"⚠️  {warning}")
    if fatal_ids:
        for problem in fatal_ids:
            print(f"❌ {problem}")
        fail_pipeline(f"   → Corrige '{BLACKBOARD_FILE}' puis relance.")

    # La séquence récap → y/n BOUCLE : l'humain peut éditer le blackboard dans un autre terminal
    # pendant que le prompt attend, or la production tourne sur ce dict en mémoire et
    # save_blackboard() réécrit le fichier depuis celui-ci — une édition non rechargée avant le
    # 'y' serait ignorée puis écrasée en silence. Tout changement du fichier pendant le prompt
    # déclenche donc un rechargement et une nouvelle confirmation sur le récap réaffiché.
    while True:
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
        print(f"   Projet : {blackboard['project']}")
        print(f"   Stack (global_rules.target) : {(blackboard.get('global_rules') or {}).get('target') or '(non précisée)'}")
        print(f"   Phases : {len(blackboard['phases'])}")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers') or [])
            print(f"   Phase {p['id']}: {p['name']} [{skills}] "
                  f"({len(p.get('tasks', []))} tâche(s) ; couvre: {covers or '?'})")
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
            RUNNER.kill()
            sys.exit(1)

    validate_all_skills(blackboard)

    # Jalons git (best-effort) : baseline maintenant, puis un commit par phase validée.
    ensure_phase_repo()

    # Référentiel du run : tout ce qui diffère de ce sha est l'œuvre de l'usine, jamais le
    # legacy préexistant. Persisté car une REPRISE recapturerait un HEAD déjà avancé, et le
    # refacto raterait alors les fichiers des phases antérieures.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    print(f"\n🚀 Démarrage de la production active : {blackboard['project']}")

    # Étape 4 : Boucle de production
    run_production_phases(blackboard, user_need, need_is_spec)

    # Étape 5 : Polish final
    execute_final_refactoring(blackboard, user_need)

    # Fermeture propre
    RUNNER.kill()
    # Run réussi : plus rien à reprendre, on purge le marqueur d'approbation de la spec. Gardé
    # hors de cleanup_all_sentinels (qui tourne aussi en cours de route) car il doit survivre à
    # une INTERRUPTION ; ici on est sur le chemin succès, donc sa suppression est sûre.
    if os.path.exists(SPEC_APPROVED_SENTINEL):
        os.remove(SPEC_APPROVED_SENTINEL)
    print("\n🏁 [CONGRATULATIONS] L'usine de code Data Center a tout validé en un seul run !")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    PIPELINE_SKILLS=PIPELINE_SKILLS,
    POLL_INTERVAL=POLL_INTERVAL,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    US_HEADING_RE=US_HEADING_RE,
    _ORCH_BASENAMES=_ORCH_BASENAMES,
    parse_skill_frontmatter=parse_skill_frontmatter,
    run_git=run_git,
)


if __name__ == "__main__":
    main()
