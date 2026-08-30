#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Réparateur arbitré des arrêts sur suite rouge — compagnon de Coding.py.
─────────────────────────────────────────────────────────────────────────────
Quand un run MAIsterMind s'arrête (phase REJECTED après MAX_ATTEMPTS, run tué en pleine
phase, régression post-refacto non résorbée), l'humain n'avait que deux options : la
chirurgie manuelle (UC4) ou « monte le modèle d'un cran et relance ». Ce script outille
la troisième voie, fidèle à la philosophie de l'usine (l'HUMAIN arbitre, PYTHON vérifie,
le LLM exécute) :

  1. DIAGNOSTIC : un agent lit la sortie rouge, le diff de la phase fautive et la spec,
     puis écrit un rapport à nom UNIQUE 'fix_report-<uid>.md' qui regroupe les échecs par
     COMPORTEMENT métier cassé (pas par fichier). Unique car MAIsterMind purge son
     'failReport.md' au démarrage : le rapport de fix, lui, SURVIT aux relances et fait
     piste d'audit des arbitrages (committé, comme spec/plan/blackboard).
  2. TRIAGE HUMAIN : pour chaque comportement cassé, l'humain tranche dans la console —
     RÉGRESSION non souhaitée (les tests ont raison, le code sera corrigé) ou ÉVOLUTION
     souhaitée (le code a raison, spec PUIS tests seront alignés). Détail à la demande,
     récapitulatif du plan d'action, confirmation y/n avant de payer le moindre agent.
  3. RÉPARATION ENCADRÉE : les deux modes sont les MIROIRS des gardes de production —
     régression = prod modifiable / TOUS les fichiers de test gelés (git checkout
     d'office) ; évolution = tests modifiables / prod gelée. L'interdiction par prompt
     seul est invérifiable ; le diff, lui, ne l'est pas. Le verdict reste l'exécution
     de 'verify_cmd' par Python (code de sortie 0), jamais un avis de LLM.
  4. HANDSHAKE : au vert, la phase fautive est marquée status 'FIXED' (une RÉCLAMATION,
     pas un verdict) — fix.py ne tamponne JAMAIS DONE/OK. À la relance (MANUELLE, par
     toi), MAIsterMind revalide par exécution puis tamponne lui-même : il reste l'unique
     autorité du verdict, et on ne rejoue pas un codeur sur une phase déjà complète (sa
     garde anti-fantôme pousserait l'agent à des modifications gratuites).

Pourquoi l'ÉVOLUTION passe par la spec AVANT les tests : la spec alimente les phases
restantes (tranches par US) et le refacto final. Adapter les tests sans elle, c'est
laisser un codeur ultérieur — prompté sur l'ancienne spec avec l'interdiction d'affaiblir
les tests — réintroduire l'ancien comportement : l'usine se battrait contre elle-même.

Discipline git NON négociable : fix.py committe l'état à l'arrêt (wip) PUIS chaque passe
appliquée. Les gardes de MAIsterMind diffent contre HEAD : un fix non committé serait
pris pour le travail de la phase suivante — et RESTAURÉ (perdu en silence) par la garde
tests-only de la première phase 'tests' venue.

Espace de nommage dédié (aucun résidu d'un autre pipeline ne peut être pris pour un
signal de celui-ci) : session tmux '<harness>-fix-<hash>', sentinelles '.fix_<slot>.attempt<N>.done',
prompt déporté nommé par le harness.
"""

import os
import re
import sys
import time
import signal
import subprocess
import hashlib
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
    load_blackboard, signal_handler,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="fix", new_context_check=False, messages={
    "reuse":     "♻️  Session tmux '{session}' déjà active. Réutilisation.",
    "start":     "🖥️  Démarrage de la session tmux '{session}' (réparation)...",
    "boot":      "⏳ Attente du boot du TUI {tui} ({wait}s)...",
    "ready":     None,
    "follow":    "   👀 Suis la réparation en direct : tmux attach -t {session}",
    "new_reset": "🔄 Réinitialisation du contexte {tui} (/new)...",
    "kill":      "🛑 Session tmux '{session}' fermée.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
FAIL_REPORT_FILE      = "failReport.md"    # écrit par MAIsterMind ; LU ici, jamais purgé
FIX_REPORT_PREFIX     = "fix_report-"      # rapport à nom unique : la piste d'audit survit
SKILLS_DIR            = "./.agents/skills"
AGENT_CONFIG_FILE     = RUNNER.config_file
SPEC_APPROVED_SENTINEL = ".spec_approved"

# Fichier de consignes déporté et tampon de prompt tmux (chemins RELATIFS : seul choix
# valable sur les 3 OS, cf. Coding.py).
TMP_FIX_FILE          = RUNNER.tmp_file("fix")
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Session tmux DÉDIÉE, suffixée du projet : jamais partagée avec l'usine principale ni
# avec un autre projet (les prompts du projet B atterriraient dans l'agent du A).
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par passe d'agent (filet de sécurité)
VERIFY_TIMEOUT        = resolve_timeout("verify", 300)            # 5 min max pour la commande de vérification
MAX_VERIFY_RETRIES_ON_TIMEOUT = 2      # re-vérifications immédiates sur timeout d'infra
VERIFY_FEEDBACK_LIMIT = 4000           # taille max d'une sortie renvoyée à un agent
DIFF_PROMPT_LIMIT     = 6000           # taille max du diff coupable injecté aux prompts
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle du rapport de diagnostic

# États du handshake avec Coding.py : une RÉCLAMATION de réparation, pas un verdict.
# MAIsterMind revalide par exécution à la relance et tamponne DONE/OK lui-même.
FIXED_STATUS          = "FIXED"
FIXED_VERDICT         = "PENDING_RECHECK"


# ─── SENTINELLES DÉDIÉES (.fix_<slot>.attempt<N>.done) ────────────────────────

def fix_sentinel(slot: str, attempt: int) -> str:
    """Sentinelle par passe ET par tentative : une sentinelle tardive d'une tentative
    précédente ne peut pas être prise pour le signal de la tentative courante."""
    return f".fix_{slot}.attempt{attempt}.done"


def cleanup_fix_sentinels(slot: str = None):
    """Purge les sentinelles de fix (d'un slot, ou toutes si slot=None)."""
    prefix = f".fix_{slot}." if slot else ".fix_"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def read_declared_files(slot: str, attempt: int) -> list:
    """Liste des fichiers déclarés par l'agent dans sa sentinelle (marqueurs de liste
    retirés, cf. read_touched_files de Coding.py)."""
    path = fix_sentinel(slot, attempt)
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


def wait_for_file_creation(filepath: str, timeout: int = MAX_PHASE_TIMEOUT) -> bool:
    start = time.time()
    print(f"   ⏳ En attente de '{filepath}'...")
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_init = os.path.getsize(filepath)
            time.sleep(1.5)
            if os.path.getsize(filepath) == size_init:
                return True
    return False


def wait_for_report_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> bool:
    """Attente du rapport de diagnostic : sentinelle prioritaire, filet de stabilité en
    secours (même contrat que wait_for_pipeline_file de Coding.py)."""
    start = time.time()
    print(f"   ⏳ En attente de '{filepath}' (signal de fin : '{sentinel}')...")
    stable_streak = 0
    last_size = -1
    structural_warned = False
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        file_ready = os.path.exists(filepath) and os.path.getsize(filepath) > 0
        if file_ready and os.path.exists(sentinel):
            try:
                os.remove(sentinel)
            except OSError:
                pass
            return True
        if file_ready:
            size = os.path.getsize(filepath)
            stable_streak = stable_streak + 1 if size == last_size else 0
            last_size = size
            if stable_streak >= STABLE_POLLS_FALLBACK:
                if structural_check is not None and not structural_check(filepath):
                    if not structural_warned:
                        print(f"   ⏳ '{filepath}' est stable mais non structuré : on continue d'attendre.")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinelle '{sentinel}' absente mais '{filepath}' est stable : "
                      f"rapport accepté (filet de secours).")
                return True
    return False


# ─── BLACKBOARD (lecture / écriture ATOMIQUE, cf. Coding.py) ─────────────

# Derniers statuts de phase journalisés (détection des TRANSITIONS par save_blackboard).
_PHASE_STATUS_SEEN = {}


def save_blackboard(data: dict):
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


# ─── GARDE-FOUS GIT (BEST-EFFORT — sans git, gardes inactives, jamais bloquant) ──

_GIT = {"enabled": False}
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

# Même corps que Coding.py (avec '.fix_*') : un projet démarré AVANT cette évolution
# a un .gitignore sans ce motif — sans le filet append-only ci-dessous, commit_all (add -A)
# committerait les sentinelles de fix comme du bruit.
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


def ensure_orchestration_ignored():
    """Garantit les motifs d'orchestration dans un .gitignore existant (append-only,
    idempotent, best-effort — cf. Coding.py). Ne crée pas de .gitignore : c'est le
    rôle de l'usine principale."""
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


def run_git(args: list, timeout: int = 60) -> tuple:
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


def git_head_sha() -> str:
    ok, out = run_git(["rev-parse", "HEAD"])
    return out if ok else ""


def ensure_git_available():
    """fix.py N'initialise JAMAIS de dépôt (c'est le rôle de MAIsterMind) : il réutilise
    l'existant ou tourne en dégradé — les gardes de gel mécanique et le diff coupable
    exigent git, la réparation elle-même non."""
    if shutil.which("git") is None or not os.path.isdir(".git"):
        print("⚠️  git indisponible ou dépôt absent : gel mécanique des fichiers, diff "
              "coupable et commits de réparation DÉSACTIVÉS pour cette session (les "
              "interdictions ne seront portées que par les prompts).")
        return
    _GIT["enabled"] = True
    ensure_orchestration_ignored()


def commit_all(label: str) -> bool:
    """Committe tout l'arbre (--allow-empty : un jalon vaut d'exister même sans diff).

    OBLIGATOIRE avant de rendre la main : les gardes de MAIsterMind diffent contre HEAD —
    un fix non committé serait pris pour le travail de la phase suivante, et la garde
    tests-only d'une phase 'tests' RESTAURERAIT (perdrait) la correction en silence.
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


def files_changed_since(ref: str) -> set:
    """Fichiers suivis modifiés depuis 'ref' (arbre de travail inclus) + non suivis."""
    if not _GIT["enabled"] or not ref:
        return set()
    changed = set()
    ok_diff, diff_out = run_git(["diff", "--name-only", ref])
    if ok_diff:
        changed.update(line.strip() for line in diff_out.splitlines() if line.strip())
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        changed.update(line.strip() for line in others_out.splitlines() if line.strip())
    return changed


# ─── CLASSIFICATION DES FICHIERS (mêmes heuristiques que l'usine) ─────────────

def is_test_file(path: str) -> bool:
    """Heuristique multi-langages, volontairement LARGE côté test (cf. Coding.py)."""
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


_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE, FAIL_REPORT_FILE,
    TMP_FIX_FILE, TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


_ORCHESTRATOR_SCRIPTS = frozenset({
    "Coding.py", "Coding-Without-Tests.py", "Test-First.py", "Acceptance-First.py",
    "Design-Prototype.py",
    "Spec.py", "Audit-Design.py", "Pre-Audit-A11Y-RGAA.py",
    "Documentation.py", "Guided-Fix.py", "Skills-Adaptation.py",
    "MAIsterMind_App.py", "mm_runner.py",
})


def is_orchestration_file(path: str) -> bool:
    """Artefacts de l'usine (jamais du code produit). Inclut ici TOUTE la famille
    les scripts de l'usine : aucun agent de réparation n'a de raison légitime d'y toucher —
    classés orchestration, ils sortent du périmètre 'production autorisée' du correcteur
    et sont donc GELÉS (restaurés d'office) comme le reste."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _ORCH_BASENAMES:
        return True
    if base in _ORCHESTRATOR_SCRIPTS or (base.startswith("MAIsterMind") and base.endswith(".py")):
        return True
    if base.startswith(".phase_") or base.startswith(".pipeline_") or base.startswith(".fix_"):
        return True
    if base.startswith(FIX_REPORT_PREFIX) and base.endswith(".md"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


# ─── VÉRIFICATION PAR EXÉCUTION (le code de sortie EST le verdict) ────────────

def truncate_output(text: str, limit: int = VERIFY_FEEDBACK_LIMIT) -> str:
    """Tronque en gardant DÉBUT et FIN (la cause racine est souvent au début)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return (text[:head]
            + f"\n[... sortie tronquée ({len(text)} caractères au total) ...]\n"
            + text[-tail:])


def resolve_verify_cmd(phase: dict, blackboard: dict) -> str:
    return ((phase or {}).get("verify_cmd") or blackboard.get("verify_cmd") or "").strip()


def run_verify(cmd: str, timeout: int = VERIFY_TIMEOUT) -> tuple:
    """Exécute la vérification HORS tmux. (ok, output, timed_out) — cf. Coding.py."""
    print(f"   🧪 Vérification par exécution : {cmd}")
    env = os.environ.copy()
    local_bin = os.path.abspath(os.path.join("node_modules", ".bin"))
    if os.path.isdir(local_bin):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
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
    """Re-vérifie sur timeout d'INFRA (le code n'a pas changé) avant de conclure."""
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
            print(f"   ⏱️  Vérification expirée ({VERIFY_TIMEOUT}s) — incident d'infra probable. "
                  f"Re-vérification {i + 1}/{MAX_VERIFY_RETRIES_ON_TIMEOUT}...")
    return False, output, True


def parse_test_count(output: str):
    """Compte best-effort des tests passés (mêmes motifs que Coding.py)."""
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


def record_test_count(output: str, blackboard: dict):
    """Rafraîchit le plancher 'last_test_count' après le vert final : une évolution peut
    LÉGITIMEMENT réduire la suite (test devenu sans objet) — sans ce rafraîchissement, la
    garde de non-décroissance rejetterait à tort la phase suivante du run repris."""
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count != old_count:
        print(f"   ℹ️  Plancher de tests rafraîchi : {old_count} → {new_count} passants.")
    blackboard["last_test_count"] = new_count
    save_blackboard(blackboard)


# ─── DÉCOUPE DE LA SPEC PAR US (fenêtre de contexte du correcteur) ────────────

US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def collect_spec_us_ids(spec_text: str) -> set:
    ids = set()
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            ids.add(match.group(1).upper())
    return ids


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Tranche de la spec limitée aux US couvertes (+ tronc commun) — dégradation
    gracieuse : sans 'covers' exploitable, la spec ENTIÈRE (cf. Coding.py)."""
    wanted = {c.strip().upper() for c in (covers or []) if isinstance(c, str) and c.strip()}
    if not wanted:
        return spec_text
    spec_us_ids = collect_spec_us_ids(spec_text)
    if not spec_us_ids or not (wanted & spec_us_ids):
        return spec_text
    kept = []
    current_us = None
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            current_us = match.group(1).upper()
        elif current_us is not None and line.startswith("## "):
            current_us = None
        if current_us is None or current_us in wanted:
            kept.append(line)
    return "\n".join(kept)


def load_skills(skills_list: list) -> str:
    content = ""
    for skill in skills_list or []:
        skill_path = os.path.join(SKILLS_DIR, skill, "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                content += f"--- COMPÉTENCE : {skill.upper()} ---\n{f.read()}\n\n"
    return content


# ─── RAPPORT DE RÉPARATION (uid, format parsable, sections ajoutées) ──────────

def make_report_path() -> str:
    """Nom UNIQUE horodaté + hash court : triable à l'œil, jamais écrasé par une session
    suivante (l'historique des arbitrages est la valeur du fichier)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    salt = hashlib.sha1(f"{os.getcwd()}{time.time()}".encode("utf-8")).hexdigest()[:4]
    return f"{FIX_REPORT_PREFIX}{stamp}-{salt}.md"


# En-tête d'un groupe dans le rapport de diagnostic. Format STRICT imposé au prompt :
# c'est lui qui rend le triage console possible (un groupe = une décision humaine).
FIX_GROUP_RE = re.compile(r"^##\s*Comportement cassé\s+(\d+)\s*:\s*(.+?)\s*$",
                          re.MULTILINE | re.IGNORECASE)


def fix_report_structural_check(path: str) -> bool:
    """Plancher structurel du filet sans sentinelle : au moins un en-tête de groupe."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return bool(FIX_GROUP_RE.search(f.read()))
    except OSError:
        return False


def parse_fix_report(text: str) -> list:
    """Découpe le rapport en groupes [{num, title, body}]. Dégradation gracieuse : un
    rapport non structuré (agent qui a ignoré le format) devient UN groupe global —
    l'humain tranche alors d'un bloc au lieu de par comportement, mais tranche."""
    matches = list(FIX_GROUP_RE.finditer(text))
    if not matches:
        return [{"num": 1,
                 "title": "Ensemble des échecs (rapport non structuré par l'IA)",
                 "body": text.strip()}]
    groups = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        groups.append({"num": int(m.group(1)),
                       "title": m.group(2).strip(),
                       "body": text[m.end():end].strip()})
    return groups


def extract_ai_reading(body: str) -> str:
    """Ligne « Lecture IA » d'un groupe (affichée au triage), sinon chaîne vide."""
    match = re.search(r"\*\*Lecture IA\s*:?\*\*\s*:?\s*(.+)", body)
    return match.group(1).strip() if match else ""


def append_report(report_path: str, text: str):
    """Ajout en fin de rapport (arbitrage humain, résultat). Best-effort : ne lève jamais."""
    try:
        with open(report_path, "a", encoding="utf-8") as f:
            f.write("\n" + text.rstrip() + "\n")
    except OSError:
        pass


# ─── DÉTECTION DE LA PHASE FAUTIVE ────────────────────────────────────────────

def find_broken_phase(blackboard: dict):
    """Première phase laissée en échec par le run : REJECTED (arrêt nominal après
    MAX_ATTEMPTS), IN_PROGRESS (run tué en pleine phase) ou FIXED (réparation
    précédente à re-jouer). Les phases TODO/PENDING jamais entamées ne comptent pas."""
    for phase in blackboard.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        status = str(phase.get("status") or "").upper()
        verdict = str(phase.get("verdict") or "").upper()
        if status == "DONE" and verdict == "OK":
            continue
        if verdict == "REJECTED" or status in ("IN_PROGRESS", FIXED_STATUS):
            return phase
    return None


def mark_phase_fixed(phase: dict, blackboard: dict):
    """Pose la RÉCLAMATION de réparation (jamais DONE/OK : MAIsterMind revalide)."""
    phase["status"] = FIXED_STATUS
    phase["verdict"] = FIXED_VERDICT
    phase["critic_feedback"] = ""
    save_blackboard(blackboard)


# ─── GARDES MÉCANIQUES DES PASSES D'AGENT ─────────────────────────────────────

def enforce_allowed_files(allowed_pred) -> list:
    """Restaure (git checkout) tout fichier SUIVI modifié depuis HEAD hors périmètre
    autorisé de la passe. HEAD est fiable car fix.py committe l'état à l'arrêt PUIS
    chaque passe appliquée : le diff ne contient QUE le travail de la passe courante.
    Plus strict que les gardes de production (blackboard, spec, plan, scripts : tout ce
    qui n'est pas explicitement autorisé est gelé) : pendant une passe, fix.py ne
    réécrit RIEN lui-même, aucune exception n'est donc nécessaire. Limite héritée de la
    famille : les fichiers NON suivis créés par l'agent échappent au gel."""
    if not _GIT["enabled"]:
        return []
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    if not ok_diff:
        return []
    forbidden = sorted(f.strip() for f in diff_out.splitlines()
                       if f.strip() and not allowed_pred(f.strip()))
    if forbidden:
        run_git(["checkout", "--"] + forbidden)
    return forbidden


def nothing_declared_touched(declared: list, since_ts: float, allowed_pred) -> bool:
    """Garde anti « agent fantôme » d'une passe : True si AUCUN fichier déclaré n'a
    réellement changé. Signaux : diff git depuis HEAD filtré au périmètre autorisé de la
    passe (le travail committé des passes précédentes ne compte pas), puis mtime depuis
    le début de la passe (fallback sans git)."""
    changed_allowed = {f for f in files_changed_since("HEAD") if allowed_pred(f)}
    for path in declared:
        clean = path.strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue
        if clean in changed_allowed:
            return False
        try:
            if os.path.exists(clean) and os.path.getmtime(clean) >= since_ts:
                return False
        except OSError:
            continue
    return True


def run_agent_pass(slot: str, build_context, allowed_pred, forbidden_label: str,
                   allow_noop: bool = False) -> bool:
    """Une passe d'agent encadrée : prompt déporté → sentinelle → gel mécanique →
    anti-fantôme, avec MAX_ATTEMPTS tentatives. build_context(attempt, feedback) fournit
    les consignes complètes. allow_noop : l'agent peut légitimement conclure « rien à
    changer » (sentinelle contenant le seul mot NO_CHANGE) — réservé à la passe spec,
    où la spec peut DÉJÀ décrire le comportement entériné."""
    pass_started = time.time()
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cleanup_fix_sentinels(slot)
        print(f"\n🚀 [PASSE {slot.upper()} — tentative {attempt}/{MAX_ATTEMPTS}] Lancement de l'agent...")
        with open(TMP_FIX_FILE, "w", encoding="utf-8") as f:
            f.write(build_context(attempt, feedback))
        RUNNER.new_context()
        mm_audit.event("agent_task")
        RUNNER.send_task(f"Lis le fichier de consignes '{TMP_FIX_FILE}' à la racine du projet "
                         f"et suis scrupuleusement ses instructions.")
        if not wait_for_file_creation(fix_sentinel(slot, attempt)):
            print(f"⏱️  L'agent n'a pas signalé la fin (sentinelle absente). Nouvelle tentative.")
            feedback = ("Ta tentative précédente n'a produit aucun signal de fin : termine "
                        "IMPÉRATIVEMENT par la création de la sentinelle demandée.")
            continue
        declared = read_declared_files(slot, attempt)
        if allow_noop and [d.strip().upper() for d in declared] == ["NO_CHANGE"]:
            print(f"   ℹ️  Passe {slot} : l'agent déclare qu'aucun changement n'est nécessaire.")
            cleanup_fix_sentinels(slot)
            return True
        # Gel mécanique AVANT l'anti-fantôme : on restaure d'abord l'interdit, on juge
        # ensuite ce qui reste (une tentative 100 % hors périmètre devient un rejet de
        # gel, avec le feedback le plus utile).
        forbidden = enforce_allowed_files(allowed_pred)
        if forbidden:
            feedback = (f"Tu as modifié des fichiers INTERDITS pour cette étape : "
                        f"{', '.join(forbidden)}. Ils ont été restaurés d'office. "
                        f"{forbidden_label}")
            print(f"🔒 [REJET] Fichiers hors périmètre restaurés : {', '.join(forbidden)}.")
            continue
        if nothing_declared_touched(declared, pass_started, allowed_pred):
            feedback = (f"Ta sentinelle déclare {len(declared)} fichier(s), mais AUCUN n'a "
                        f"réellement été créé ou modifié pendant cette passe. Réalise "
                        f"CONCRÈTEMENT le travail demandé, puis seulement recrée la "
                        f"sentinelle avec la liste réelle des fichiers touchés.")
            print(f"👻 [REJET] Sentinelle écrite mais aucun fichier déclaré n'a été touché.")
            continue
        cleanup_fix_sentinels(slot)
        return True
    cleanup_fix_sentinels(slot)
    return False


# ─── PRÉDICATS DE PÉRIMÈTRE DES PASSES ────────────────────────────────────────
# Régression = miroir durci de la garde 'protected_test_files' (prod modifiable, TOUS les
# tests gelés). Évolution = miroir de la garde tests-only (tests modifiables, prod gelée).

def allowed_for_code_pass(path: str) -> bool:
    return not is_test_file(path) and not is_orchestration_file(path)


def allowed_for_tests_pass(path: str) -> bool:
    return is_test_file(path) and not is_orchestration_file(path)


def allowed_for_spec_pass(path: str) -> bool:
    clean = str(path).strip().replace("\\", "/")
    if clean.startswith("./"):
        clean = clean[2:]
    return clean == SPEC_FILE


def allowed_nothing(path: str) -> bool:
    return False


# ─── PROMPTS DES AGENTS ───────────────────────────────────────────────────────

def group_block(group: dict, limit: int = 2500) -> str:
    return truncate_output(f"[Comportement {group['num']}] {group['title']}\n{group['body']}", limit)


def build_diag_context(report_path: str, sentinel: str, phase: dict, verify_cmd: str,
                       verify_output: str, culprit_diff: str, fail_report_text: str) -> str:
    if phase:
        phase_block = (f"Phase arrêtée : Phase {phase.get('id', '?')} « {phase.get('name', '(sans nom)')} » "
                       f"(nature : {phase.get('nature') or 'non déclarée'})\n"
                       f"Checklist de la phase :\n"
                       + "\n".join(f"- {t}" for t in (phase.get("tasks") or []))
                       + f"\nDernier feedback enregistré :\n{truncate_output(str(phase.get('critic_feedback') or '(vide)'), 1500)}")
    else:
        phase_block = ("Aucune phase en échec dans le blackboard : l'arrêt vient probablement "
                       "du refactoring final ou d'une modification manuelle du code.")
    fail_block = f"\n--- RAPPORT D'ARRÊT DE L'USINE ({FAIL_REPORT_FILE}, tronqué) ---\n{truncate_output(fail_report_text, 1500)}\n" if fail_report_text else ""
    return f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Ingénieur Diagnostic senior. Le run de l'usine MAIsterMind s'est arrêté : la
suite de vérification est ROUGE. Tu ne CORRIGES rien et tu ne modifies AUCUN fichier du
projet : ta SEULE production est le rapport demandé (tout autre fichier modifié sera
restauré d'office).

--- CE QUI S'EST PASSÉ ---
{phase_block}
Commande de vérification (verdict universel) : « {verify_cmd} »
{fail_block}
--- SORTIE DE LA VÉRIFICATION QUI ÉCHOUE (tronquée) ---
{verify_output}

--- CHANGEMENTS INTRODUITS PAR LA PHASE ARRÊTÉE (diff, tronqué) ---
{culprit_diff or "(diff indisponible : git désactivé ou aucun changement committé)"}

--- TA MISSION ---
1. Lis '{SPEC_FILE}' (source de vérité du comportement ATTENDU) et, au besoin, les
   fichiers de test qui échouent pour comprendre ce qu'ils vérifient réellement.
2. Regroupe les échecs par COMPORTEMENT MÉTIER cassé (jamais par fichier) : un groupe =
   une décision que l'humain peut prendre d'un bloc. Vise PEU de groupes (1 à 5).
3. Rédige le rapport '{report_path}' au FORMAT STRICT ci-dessous. L'orchestrateur le
   PARSE (en-têtes exacts) et l'humain décidera, groupe par groupe : régression non
   souhaitée (le code sera corrigé) ou évolution souhaitée (spec et tests seront alignés).
   La vraie question posée à l'humain est : « le critère de la spec a-t-il encore
   raison ? » — rédige chaque groupe pour qu'il puisse y répondre.

--- FORMAT STRICT DU RAPPORT '{report_path}' ---
# Rapport de réparation — Guided-Fix
(2 à 4 lignes de contexte : phase arrêtée, commande de vérification, volume d'échecs.)

## Comportement cassé 1 : <titre court et parlant pour un humain métier>
- **Tests rouges :** <noms des tests et fichiers concernés>
- **Attendu (spec) :** <US et critère d'acceptation concernés, cités depuis {SPEC_FILE} ; écris « hors spec » si introuvable>
- **Observé :** <ce que fait le code actuellement, d'après la sortie du runner>
- **Changement suspect :** <fichier(s) et changement de la phase arrêtée qui semblent en cause>
- **Lecture IA :** <« régression probable » ou « évolution probable »> — <justification en 1 ou 2 phrases>

## Comportement cassé 2 : <...>
(autant de sections que de comportements distincts, numérotées 1, 2, 3, ...)

--- INSTRUCTION DE FIN OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}. En toute DERNIÈRE action, après avoir
sauvegardé '{report_path}', crée le fichier sentinelle '{sentinel}' à la racine
(contenu : le seul mot done) : c'est le signal de fin pour l'orchestrateur.
"""


def build_spec_context(sentinel: str, evolution_groups: list, feedback: str) -> str:
    groups_text = "\n\n".join(group_block(g) for g in evolution_groups)
    return f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Product Owner senior. L'humain vient d'ENTÉRINER une évolution de comportement :
le code actuel a raison, c'est la spécification qui est en retard sur lui.

--- ÉVOLUTIONS ENTÉRINÉES PAR L'HUMAIN ---
{groups_text}

--- TA MISSION ---
Mets à jour '{SPEC_FILE}' pour que les critères d'acceptation concernés décrivent le
comportement DÉSORMAIS souhaité (celui observé dans le code) :
- Modifie UNIQUEMENT les user stories et critères touchés par les évolutions ci-dessus.
- Ne réécris rien d'autre : zéro reformulation du reste, zéro nouvelle exigence (YAGNI).
- Tu ne modifies AUCUN autre fichier que '{SPEC_FILE}' (tout autre fichier modifié sera
  restauré d'office).
- Cas particulier : si '{SPEC_FILE}' décrit DÉJÀ correctement le comportement entériné
  (les tests seuls avaient dérivé), n'y touche pas et écris le seul mot NO_CHANGE dans
  la sentinelle de fin.

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback or "Première tentative — aucun retour précédent."}

--- INSTRUCTION DE FIN OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}. En toute DERNIÈRE action, crée le
fichier sentinelle '{sentinel}' à la racine, contenant la liste des fichiers modifiés
(un chemin par ligne — normalement la seule ligne {SPEC_FILE}, ou NO_CHANGE).
"""


def build_tests_context(sentinel: str, evolution_groups: list, verify_cmd: str, feedback: str) -> str:
    groups_text = "\n\n".join(group_block(g) for g in evolution_groups)
    return f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Ingénieur Test senior. L'humain a ENTÉRINÉ une évolution : le code actuel a
raison, et les tests listés ci-dessous vérifient encore l'ANCIEN comportement. La
spécification '{SPEC_FILE}' vient d'être alignée : c'est la nouvelle source de vérité.

--- ÉVOLUTIONS ENTÉRINÉES (tests à aligner) ---
{groups_text}

--- SORTIE ACTUELLE DE LA VÉRIFICATION (tronquée) ---
{feedback or "(voir les groupes ci-dessus)"}

--- TA MISSION ---
1. Lis '{SPEC_FILE}' (mise à jour) puis les fichiers de test concernés.
2. Adapte ces tests au comportement DÉSORMAIS souhaité, avec la même rigueur qu'avant :
   teste le COMPORTEMENT réel (jamais une assertion toujours vraie), couvre les bornes.
3. INTERDICTIONS : tu ne modifies QUE des fichiers de test (tout fichier de production
   modifié sera restauré d'office) ; tu n'affaiblis pas la suite (aucun test vidé,
   désactivé ou supprimé — SAUF test devenu strictement sans objet du fait de
   l'évolution) ; pas de Testcontainers, Docker, I/O réseau ou base de données.
4. Ton UNIQUE critère de réussite : la commande « {verify_cmd} » doit réussir
   (code de sortie 0). L'orchestrateur l'exécutera lui-même.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}. En toute DERNIÈRE action, crée le
fichier sentinelle '{sentinel}' à la racine, contenant la liste des fichiers modifiés
(un chemin par ligne).
"""


def build_code_context(sentinel: str, regression_groups: list, phase: dict, blackboard: dict,
                       spec_slice: str, culprit_diff: str, verify_cmd: str, feedback: str) -> str:
    groups_text = "\n\n".join(group_block(g) for g in regression_groups)
    rules = blackboard.get("global_rules") or {}
    skills_context = load_skills((phase or {}).get("skills_required"))
    if phase:
        phase_block = (f"Phase {phase.get('id', '?')} « {phase.get('name', '(sans nom)')} ». Sa checklist "
                       f"(réalise ce qui manque, MIEUX que la tentative arrêtée) :\n"
                       + "\n".join(f"- {t}" for t in (phase.get("tasks") or [])))
    else:
        phase_block = "(aucune phase identifiée : régression post-refacto ou modification manuelle)"
    return f"""--- RÈGLES SYSTÈME ---
Stack : {rules.get('target', '(non spécifié)')}
Interdictions : {rules.get('constraints', '(non spécifié)')}

{skills_context}--- CONTRAT COMPORTEMENTAL ---
Tu es un Ingénieur Correcteur senior. L'humain a CONFIRMÉ une régression NON souhaitée :
le comportement attendu par la spec est cassé, et ce sont les TESTS qui ont raison —
c'est donc le code de PRODUCTION qu'il faut corriger.

--- RÉGRESSIONS CONFIRMÉES PAR L'HUMAIN ---
{groups_text}

--- PHASE À L'ORIGINE DE L'ARRÊT ---
{phase_block}

--- EXTRAIT DE LA SPEC COUVERT PAR CETTE PHASE ---
{spec_slice}

--- CHANGEMENTS INTRODUITS PAR LA PHASE (diff, tronqué) ---
{culprit_diff or "(diff indisponible)"}

--- SORTIE ACTUELLE DE LA VÉRIFICATION (tronquée) ---
{feedback}

--- TA MISSION ---
Corrige le code de PRODUCTION pour que la commande « {verify_cmd} » réussisse (code de
sortie 0). C'est ton UNIQUE critère de réussite ; l'orchestrateur l'exécutera lui-même.
INTERDICTIONS : tu ne modifies AUCUN fichier de test (ils ont raison ; tout fichier de
test modifié sera restauré d'office) ; tu ne contournes JAMAIS un test — tu corriges le
comportement qu'il vérifie.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
Tu ne touches JAMAIS au fichier {BLACKBOARD_FILE}. En toute DERNIÈRE action, crée le
fichier sentinelle '{sentinel}' à la racine, contenant la liste des fichiers modifiés
(un chemin par ligne).
"""


# ─── TRIAGE HUMAIN (le cœur de l'UX : une décision par comportement) ──────────

def print_group_detail(group: dict):
    print(f"\n{'─' * 62}")
    print(f"## Comportement cassé {group['num']} : {group['title']}")
    print(group["body"])
    print(f"{'─' * 62}")


def triage_groups(groups: list, report_path: str):
    """Boucle de triage console. Renvoie {index_groupe: 'r'|'e'} ou None (abandon).

    Ergonomie voulue : tout se joue ICI (le détail s'affiche à la demande via 'o', pas
    besoin d'ouvrir le rapport ailleurs), la lecture IA est un AVIS affiché — jamais une
    décision pré-remplie (biais d'ancrage) —, et rien n'est lancé avant le récapitulatif
    confirmé ('n' refait le triage au lieu de tout abandonner).
    """
    total = len(groups)
    while True:
        decisions = {}
        print(f"\n{'=' * 62}")
        print(f"🔍 TRIAGE — {total} comportement(s) cassé(s) à arbitrer.")
        print(f"   Détail complet dans '{report_path}' (éditable dans un autre terminal) ;")
        print(f"   tu peux aussi l'afficher ici avec 'o'. La question, à chaque fois :")
        print(f"   « le critère de la spec a-t-il encore raison ? »")
        print(f"{'=' * 62}")
        for i, group in enumerate(groups, 1):
            print(f"\n[{i}/{total}] {group['title']}")
            reading = extract_ai_reading(group["body"])
            if reading:
                print(f"      🤖 Lecture IA : {reading}")
            print("      [r] régression NON souhaitée → le code sera corrigé (tests gelés)")
            print("      [e] évolution souhaitée      → spec puis tests alignés (production gelée)")
            print("      [o] afficher le détail complet de ce comportement")
            while True:
                answer = input("   → Ta décision (r/e/o) : ").strip().lower()
                mm_audit.event("gate", id="fix-triage", gate_kind="choice", answer=answer)
                if answer == "o":
                    print_group_detail(group)
                    continue
                if answer in ("r", "e"):
                    decisions[i] = answer
                    break
                print("   ↳ Réponds par r (régression), e (évolution) ou o (détail).")
        labels = {"r": "🔧 RÉGRESSION", "e": "📈 ÉVOLUTION "}
        print(f"\n{'=' * 62}")
        print("📋 ARBITRAGE À CONFIRMER")
        for i, group in enumerate(groups, 1):
            print(f"   {i}. {labels[decisions[i]]} — {group['title']}")
        print("Plan d'action :")
        step = 1
        if any(d == "e" for d in decisions.values()):
            print(f"   {step}) Mise à jour de '{SPEC_FILE}' (évolutions), validée par toi (y/n).")
            step += 1
            print(f"   {step}) Adaptation des tests des évolutions (code de production GELÉ).")
            step += 1
        if any(d == "r" for d in decisions.values()):
            print(f"   {step}) Correction du code des régressions (fichiers de test GELÉS).")
            step += 1
        print(f"   {step}) Vérification complète exécutée par Python ; au vert, marqueur FIXED")
        print(f"      posé — tu relanceras Coding.py toi-même pour la revalidation.")
        print(f"{'=' * 62}")
        answer = input("\n▶️  Confirmer cet arbitrage et lancer la réparation ? "
                       "(y = oui / n = refaire le triage / q = abandonner) : ").strip().lower()
        mm_audit.event("gate", id="fix-confirm", gate_kind="choice", answer=answer)
        if answer == "y":
            return decisions
        if answer == "q":
            return None
        print("\n🔁 On reprend le triage depuis le début.")


def write_arbitration_section(report_path: str, groups: list, decisions: dict):
    lines = ["## Arbitrage humain", f"_(horodaté {time.strftime('%Y-%m-%d %H:%M')})_", ""]
    for i, group in enumerate(groups, 1):
        if decisions[i] == "r":
            lines.append(f"- Comportement {i} « {group['title']} » : **RÉGRESSION non souhaitée** "
                         f"→ correction du code (tests gelés).")
        else:
            lines.append(f"- Comportement {i} « {group['title']} » : **ÉVOLUTION souhaitée** "
                         f"→ spec et tests alignés (production gelée).")
    append_report(report_path, "\n".join(lines))


# ─── COMPTABILITÉ DE FIN (handshake + gardes du run repris) ───────────────────

def update_protected_test_files(blackboard: dict, pre_wip_sha: str, phase: dict):
    """Si la phase réparée est une phase 'tests', ses livrables (travail de la phase +
    réparation, soit tout ce qui a changé depuis l'arrêt) rejoignent les fichiers
    protégés — comptabilité que MAIsterMind tient sur le chemin nominal et que la
    revalidation ne peut pas reconstituer (elle n'a pas le sha d'avant réparation)."""
    if not phase or str(phase.get("nature") or "").strip().lower() != "tests":
        return
    if not _GIT["enabled"] or not pre_wip_sha:
        return
    protected = set(blackboard.get("protected_test_files") or [])
    added = {f for f in files_changed_since(pre_wip_sha)
             if is_test_file(f) and not is_orchestration_file(f)}
    if added:
        protected.update(added)
        blackboard["protected_test_files"] = sorted(protected)
        save_blackboard(blackboard)
        print(f"   🛡️  {len(added)} fichier(s) de test ajoutés aux fichiers protégés.")


def print_failure_message(report_path: str, last_output: str):
    model = RUNNER.configured_model()
    print(f"""
{'=' * 62}
❌ La réparation n'a pas convergé après {MAX_ATTEMPTS} tentative(s) : la suite reste ROUGE.

   Dernière sortie (tronquée) :
   {truncate_output(last_output, 1200)}

💡 Le modèle actuel ({model}) cale sur cette réparation. Pistes :
   - Monte le modèle d'un cran (/model dans le TUI ou '{AGENT_CONFIG_FILE}')
     puis relance Guided-Fix.py : nouveau diagnostic, nouveau triage.
   - Ou corrige à la main, puis relance Guided-Fix.py : il constatera le
     vert et te proposera de poser le marqueur FIXED sans payer d'agent.
   Les arbitrages de cette session sont conservés dans '{report_path}'.
{'=' * 62}
""")


# ─── ORCHESTRATION PRINCIPALE ─────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "fix", RUNNER.name,
                   model=RUNNER.configured_model())
    print(f"{'=' * 62}\n🩺 Guided-Fix — réparation arbitrée d'un arrêt sur suite rouge\n{'=' * 62}")

    # ── Pré-requis : un run MAIsterMind a eu lieu (blackboard = état de reprise). ──
    if not os.path.exists(BLACKBOARD_FILE):
        print(f"❌ '{BLACKBOARD_FILE}' introuvable : rien à réparer ici. Lance d'abord "
              f"Coding.py (ce script soigne les arrêts de production, pas le pipeline).")
        sys.exit(1)
    try:
        blackboard = load_blackboard()
    except Exception as err:
        print(f"❌ '{BLACKBOARD_FILE}' illisible (YAML invalide ou corrompu) : {err}")
        print(f"   → Corrige ou supprime '{BLACKBOARD_FILE}' puis relance Coding.py.")
        sys.exit(1)
    if not isinstance(blackboard, dict) or not isinstance(blackboard.get("phases"), list):
        print(f"❌ '{BLACKBOARD_FILE}' sans bloc 'phases' exploitable : rien à réparer.")
        sys.exit(1)

    broken_phase = find_broken_phase(blackboard)
    verify_cmd = resolve_verify_cmd(broken_phase or {}, blackboard)
    if not verify_cmd:
        print(f"❌ Aucune commande de vérification ('verify_cmd' de phase ou globale) dans "
              f"'{BLACKBOARD_FILE}' : sans verdict exécutable, aucune réparation n'est prouvable.")
        sys.exit(1)

    ensure_git_available()
    cleanup_fix_sentinels()  # résidus d'une session de fix interrompue

    if broken_phase:
        print(f"🎯 Phase en échec détectée : Phase {broken_phase.get('id', '?')} "
              f"« {broken_phase.get('name', '(sans nom)')} » "
              f"[{broken_phase.get('status', '?')}/{broken_phase.get('verdict', '?')}]")
    else:
        print("ℹ️  Aucune phase en échec dans le blackboard : si la suite est rouge, l'arrêt "
              "vient du refactoring final ou d'une modification manuelle.")

    # ── Verdict d'entrée : Python exécute, personne ne suppose. ──
    ok, initial_output, timed_out = run_verify_resilient(verify_cmd)
    if timed_out:
        print(f"""
🛑 [TIMEOUT INFRA] La vérification « {verify_cmd} » expire de façon répétée : incident
   d'INFRASTRUCTURE (machine, réseau, process figé), pas un état du code. Il n'y a pas
   d'arbitrage régression/évolution à rendre : répare l'environnement, puis relance.
""")
        sys.exit(1)
    if ok:
        if broken_phase:
            print(f"\n✅ La suite passe DÉJÀ au vert : le code a probablement été réparé à la main "
                  f"(cf. UC4). La phase {broken_phase.get('id', '?')} peut être marquée '{FIXED_STATUS}' : "
                  f"MAIsterMind la revalidera par exécution à la relance, sans re-payer de codeur.")
            answer = input(f"\n▶️  Marquer la phase {broken_phase.get('id', '?')} '{FIXED_STATUS}' "
                           f"et te laisser relancer Coding.py ? (y/n) : ").strip().lower()
            mm_audit.event("gate", id="fix-mark-fixed", gate_kind="yn", answer=answer)
            if answer == "y":
                mark_phase_fixed(broken_phase, blackboard)
                record_test_count(initial_output, blackboard)
                update_protected_test_files(blackboard, git_head_sha(), broken_phase)
                commit_all(f"fix(phase {broken_phase.get('id', '?')}): état vert constaté (réparation manuelle)")
                print(f"\n🏁 Marqueur posé. Relance 'python3 Coding.py' pour revalider et poursuivre.")
            else:
                print("⏹️  Rien n'a été modifié.")
            sys.exit(0)
        print("\n✅ La suite de vérification passe : rien à réparer. Relance Coding.py "
              "si tu veux poursuivre ou rejouer le polish final.")
        sys.exit(0)

    # ── Suite rouge confirmée : on fige l'état AVANT toute intervention. ──
    # pre_wip_sha = dernier commit du run (les phases vertes sont committées) : le diff
    # pre_wip → wip est donc EXACTEMENT le travail de la phase fautive — cadeau pour le
    # diagnostic. Le commit wip rend ensuite HEAD fiable pour les gels mécaniques.
    pre_wip_sha = git_head_sha()
    commit_all("wip(fix): état du run à l'arrêt (avant réparation)")
    culprit_diff = ""
    if _GIT["enabled"] and pre_wip_sha:
        ok_diff, diff_out = run_git(["diff", pre_wip_sha, "HEAD"])
        if ok_diff:
            culprit_diff = truncate_output(diff_out, DIFF_PROMPT_LIMIT)

    fail_report_text = ""
    if os.path.exists(FAIL_REPORT_FILE):
        try:
            with open(FAIL_REPORT_FILE, "r", encoding="utf-8") as f:
                fail_report_text = f.read()
        except OSError:
            pass

    # ── Diagnostic par agent → rapport à nom unique. ──
    RUNNER.start()
    report_path = make_report_path()
    diag_sentinel = fix_sentinel("diag", 1)
    cleanup_fix_sentinels("diag")
    print(f"\n📖 [DIAGNOSTIC] Analyse des échecs par l'IA → '{report_path}'...")
    with open(TMP_FIX_FILE, "w", encoding="utf-8") as f:
        f.write(build_diag_context(report_path, diag_sentinel, broken_phase, verify_cmd,
                                   initial_output, culprit_diff, fail_report_text))
    mm_audit.event("agent_task")
    RUNNER.send_task(f"Lis le fichier de consignes '{TMP_FIX_FILE}' à la racine du projet "
                     f"et suis scrupuleusement ses instructions.")
    if not wait_for_report_file(report_path, diag_sentinel,
                                structural_check=fix_report_structural_check):
        print(f"❌ [DIAGNOSTIC] Timeout : '{report_path}' non produit. Suspecte le tool calling "
              f"du modèle (attache-toi : tmux attach -t {TMUX_SESSION}), puis relance.")
        RUNNER.kill()
        sys.exit(1)
    stray = enforce_allowed_files(allowed_nothing)
    if stray:
        print(f"   ⚠️  Le diagnostic avait modifié des fichiers ({', '.join(stray)}) : restaurés "
              f"(il est en lecture seule, seul son rapport compte).")
    with open(report_path, "r", encoding="utf-8") as f:
        groups = parse_fix_report(f.read())
    print(f"✅ [DIAGNOSTIC] {len(groups)} comportement(s) cassé(s) décrits dans '{report_path}'.")

    # ── Triage humain (l'arbitrage est LA valeur ajoutée de ce script). ──
    decisions = triage_groups(groups, report_path)
    if decisions is None:
        append_report(report_path, "## Arbitrage humain\n- Abandonné par l'utilisateur (aucune action).")
        print("⏹️  Abandonné : rien n'a été modifié (l'état à l'arrêt reste committé en wip).")
        RUNNER.kill()
        sys.exit(0)
    write_arbitration_section(report_path, groups, decisions)
    evolution_groups = [g for i, g in enumerate(groups, 1) if decisions[i] == "e"]
    regression_groups = [g for i, g in enumerate(groups, 1) if decisions[i] == "r"]

    # ── ÉVOLUTIONS D'ABORD : la spec redevient vraie, puis les tests la suivent. ──
    # Ordre imposé par l'architecture de l'usine : spec → tests → code. Corriger le code
    # contre des tests encore faux ferait osciller la réparation.
    if evolution_groups:
        print(f"\n📈 [ÉVOLUTION 1/2] Alignement de '{SPEC_FILE}' sur le comportement entériné...")
        if not run_agent_pass(
                "spec",
                lambda attempt, feedback: build_spec_context(fix_sentinel("spec", attempt),
                                                             evolution_groups, feedback),
                allowed_for_spec_pass,
                f"Cette étape ne modifie QUE '{SPEC_FILE}'.",
                allow_noop=True):
            print(f"❌ La mise à jour de la spec n'a pas abouti après {MAX_ATTEMPTS} tentatives.")
            commit_all(f"wip(fix): réparation inaboutie (voir {report_path})")
            RUNNER.kill()
            sys.exit(1)
        # Validation HUMAINE du diff de spec : même contrat que confirm_spec_with_human —
        # l'humain peut éditer spec.md dans un autre terminal AVANT de répondre.
        spec_changed = False
        if _GIT["enabled"]:
            ok_diff, spec_diff = run_git(["diff", "HEAD", "--", SPEC_FILE])
            spec_changed = bool(ok_diff and spec_diff.strip())
            if spec_changed:
                print(f"\n{'─' * 62}\n📋 DIFF DE LA SPEC PROPOSÉ :\n{truncate_output(spec_diff, 3000)}\n{'─' * 62}")
        if not spec_changed:
            print(f"\nℹ️  '{SPEC_FILE}' inchangée (ou diff indisponible sans git) : relis-la "
                  f"dans un autre terminal si tu veux vérifier.")
        print(f"   Tu peux modifier '{SPEC_FILE}' directement dans un autre terminal avant de valider.")
        answer = input("\n▶️  Valider cette spec mise à jour et adapter les tests ? (y/n) : ").strip().lower()
        mm_audit.event("gate", id="fix-spec-update", gate_kind="yn", answer=answer)
        if answer != "y":
            if _GIT["enabled"]:
                run_git(["checkout", "--", SPEC_FILE])
                print(f"↩️  '{SPEC_FILE}' restaurée.")
            print("⏹️  Annulé par l'utilisateur. Relance Guided-Fix.py pour refaire le triage.")
            RUNNER.kill()
            sys.exit(0)
        commit_all("fix: spec alignée sur l'évolution entérinée")

        print(f"\n📈 [ÉVOLUTION 2/2] Adaptation des tests au comportement entériné...")
        if not run_agent_pass(
                "tests",
                lambda attempt, feedback: build_tests_context(fix_sentinel("tests", attempt),
                                                              evolution_groups, verify_cmd,
                                                              feedback or initial_output),
                allowed_for_tests_pass,
                "Cette étape ne modifie QUE des fichiers de test : le code de production est GELÉ."):
            print(f"❌ L'adaptation des tests n'a pas abouti après {MAX_ATTEMPTS} tentatives.")
            commit_all(f"wip(fix): réparation inaboutie (voir {report_path})")
            RUNNER.kill()
            sys.exit(1)
        commit_all("fix: tests alignés sur l'évolution entérinée")

    # ── RÉGRESSIONS : le code se corrige, les tests font foi. ──
    spec_text = ""
    if os.path.exists(SPEC_FILE):
        with open(SPEC_FILE, "r", encoding="utf-8") as f:
            spec_text = f.read()
    spec_slice = extract_spec_slice(spec_text, (broken_phase or {}).get("covers")) if spec_text \
        else "(spec introuvable : appuie-toi sur les tests rouges)"

    def run_code_pass(feedback: str) -> bool:
        return run_agent_pass(
            "code",
            lambda attempt, fb: build_code_context(fix_sentinel("code", attempt),
                                                   regression_groups, broken_phase, blackboard,
                                                   spec_slice, culprit_diff, verify_cmd,
                                                   fb or feedback),
            allowed_for_code_pass,
            "Cette étape ne modifie QUE le code de production : les fichiers de test sont GELÉS.")

    def run_tests_retry_pass(feedback: str) -> bool:
        return run_agent_pass(
            "tests",
            lambda attempt, fb: build_tests_context(fix_sentinel("tests", attempt),
                                                    evolution_groups, verify_cmd, fb or feedback),
            allowed_for_tests_pass,
            "Cette étape ne modifie QUE des fichiers de test : le code de production est GELÉ.")

    if regression_groups:
        print(f"\n🔧 [RÉGRESSION] Correction du code de production...")
        if not run_code_pass(initial_output):
            print(f"❌ La correction du code n'a pas abouti après {MAX_ATTEMPTS} tentatives.")
            commit_all(f"wip(fix): réparation inaboutie (voir {report_path})")
            RUNNER.kill()
            sys.exit(1)
        commit_all("fix: régression corrigée dans le code de production")

    # ── BOUCLE DE VERDICT : Python exécute, l'agent adéquat retente sur rouge. ──
    # En cas mixte, les retries vont au CORRECTEUR (prod) : les tests, désormais
    # arbitrés, font foi. Limite assumée : si le rouge résiduel vient d'un test
    # d'évolution mal adapté, la boucle ne convergera pas — l'échec le documente.
    success = False
    last_output = initial_output
    for round_idx in range(1, MAX_ATTEMPTS + 1):
        ok, last_output, timed_out = run_verify_resilient(verify_cmd)
        if timed_out:
            print(f"🛑 [TIMEOUT INFRA] La vérification expire de façon répétée : incident "
                  f"d'infrastructure, pas un verdict sur la réparation. L'état courant est "
                  f"committé ; répare l'environnement puis relance Guided-Fix.py.")
            commit_all(f"wip(fix): réparation interrompue par timeout d'infra (voir {report_path})")
            RUNNER.kill()
            sys.exit(1)
        if ok:
            success = True
            break
        print(f"⚠️  [ROUGE {round_idx}/{MAX_ATTEMPTS}] La suite ne passe pas encore. "
              f"Sortie retransmise à l'agent.")
        if round_idx == MAX_ATTEMPTS:
            break
        retried = run_code_pass(last_output) if regression_groups else run_tests_retry_pass(last_output)
        if not retried:
            break
        commit_all(f"wip(fix): passe de correction {round_idx + 1}")

    if not success:
        append_report(report_path, f"## Résultat\n- **ÉCHEC** : la suite reste ROUGE après "
                                   f"{MAX_ATTEMPTS} tentative(s) de réparation.\n"
                                   f"- Dernière sortie (tronquée) :\n\n```\n"
                                   f"{truncate_output(last_output, 2000)}\n```\n"
                                   f"- Pistes : monter le modèle d'un cran puis relancer "
                                   f"Guided-Fix.py (nouveau triage), ou corriger à la main "
                                   f"puis relancer (le vert constaté posera le marqueur {FIXED_STATUS}).")
        commit_all(f"wip(fix): réparation inaboutie (voir {report_path})")
        print_failure_message(report_path, last_output)
        RUNNER.kill()
        sys.exit(1)

    # ── SUCCÈS : comptabilité, handshake, commit, handoff HUMAIN. ──
    record_test_count(last_output, blackboard)
    update_protected_test_files(blackboard, pre_wip_sha, broken_phase)
    result_lines = ["## Résultat", "- Suite complète **VERTE** après réparation."]
    if broken_phase:
        mark_phase_fixed(broken_phase, blackboard)
        result_lines.append(f"- Phase {broken_phase.get('id', '?')} "
                            f"« {broken_phase.get('name', '(sans nom)')} » marquée **{FIXED_STATUS}** : "
                            f"MAIsterMind la revalidera par exécution à la relance.")
    result_lines.append("- Prochaine étape : relancer `python3 Coding.py` (relance MANUELLE : "
                        "la reprise est de toute façon interactive).")
    append_report(report_path, "\n".join(result_lines))
    summary = []
    if regression_groups:
        summary.append(f"{len(regression_groups)} régression(s) corrigée(s)")
    if evolution_groups:
        summary.append(f"{len(evolution_groups)} évolution(s) entérinée(s)")
    commit_all(f"fix(phase {broken_phase.get('id', '?') if broken_phase else '-'}): "
               + ", ".join(summary))

    for tmp_f in [TMP_FIX_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_fix_sentinels()
    RUNNER.kill()
    print(f"""
{'=' * 62}
🏁 Réparation terminée : la suite complète est VERTE ({', '.join(summary)}).
   📄 Piste d'audit : '{report_path}' (committée avec la réparation).""")
    if broken_phase:
        print(f"""   🔁 Phase {broken_phase.get('id', '?')} « {broken_phase.get('name', '(sans nom)')} » marquée {FIXED_STATUS} :
      relance 'python3 Coding.py' — il revalide par exécution (sans re-payer
      de codeur) puis poursuit le run à la phase suivante.""")
    else:
        print("""   🔁 Aucune phase à marquer (arrêt hors production) : relance 'python3 Coding.py'
      si tu veux rejouer le polish final, ou livre tel quel.""")
    print(f"{'=' * 62}")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
