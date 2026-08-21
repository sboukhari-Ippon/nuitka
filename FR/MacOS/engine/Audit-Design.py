#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à AUDIT DESIGN avec un harness d'agent + tmux (grille Nielsen)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « AUDIT » : elle n'écrit AUCUN code — elle évalue une interface web EXISTANTE
(prototype issu de Design-Prototype.py, front-end produit par Safe-Coding.py, ou tout
autre projet web) contre les 10 heuristiques d'utilisabilité de Nielsen, et livre un
rapport consolidé 'design_audit_report.md' (sévérités 0 à 4, localisations,
recommandations actionnables).

C'est l'application directe de la logique MAIsterMind — trancher la fenêtre de contexte
par phase pour rendre les modèles petits ou moyens fiables sur la durée — à un travail
d'AUDIT : demander « les 10 heuristiques d'un coup » sature le contexte et produit des
constats superficiels ; ici CHAQUE heuristique est une phase dédiée, exécutée dans une
session neuve (/new), qui ne reçoit QUE sa tranche de grille (tronc commun + SA section)
et n'écrit QUE son fichier de constats. Une phase de synthèse finale consolide les dix
fichiers en un rapport unique trié par sévérité.

Pipeline :
  - Étape 0 : découverte du périmètre (fichiers UI du projet) par PYTHON — déterministe,
    zéro LLM — puis confirmation humaine (y/n) AVANT de payer 11 tours d'agent.
  - Étape 1 : 10 phases d'audit, une par heuristique. Chaque auditeur écrit ses constats
    dans 'audit_nielsen/Hxx_<slug>.md' puis signale sa fin par sentinelle. Pas de verdict
    exécutable (un audit n'a ni build ni test) : filet de vivacité (3 tentatives) + plancher
    STRUCTUREL sur le fichier de constats (sections obligatoires), comme le proto.
  - Étape 2 : synthèse. Un agent consolide les 10 fichiers de constats en
    'design_audit_report.md' — il ne réaudite rien, il recopie et ordonne (même famille
    de contrat que le compilateur blackboard : zéro inférence demandée au petit modèle).

Reprise par fichiers, comme les autres variantes : un fichier de constats présent et
structurellement valide saute sa phase ; la synthèse est TOUJOURS rejouée en fin de run
(elle doit refléter les constats à jour). Pour refaire un audit complet : supprimer
'audit_nielsen/' et relancer.

Garde READ-ONLY (best-effort, si le projet est déjà un dépôt git) : un audit ne modifie
pas le projet audité. Tout fichier suivi modifié par un auditeur est restauré
(git checkout) et signalé ; tout fichier créé hors des livrables d'audit est signalé
(jamais supprimé : décision laissée à l'humain). Sans git, l'interdiction reste portée
par les prompts (dégradation gracieuse, comme partout ailleurs dans l'usine).
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil

from mm_runner import resolve_runner, resolve_timeout

# Journal de run (boîte noire .mm-runs/, plan-big-last Lot 2) : purement additif,
# no-op intégral si MM_AUDIT=0, ne fait JAMAIS échouer un run.
import mm_audit

# Fonctions partagées extraites au Lot 4a (plan-big-last) : voir mm_core.py.
# La configuration (constantes/objets de CE module) est injectée en fin de
# fichier via mm_core.configure(...) — tous les noms y sont alors définis.
import mm_core
from mm_core import (
    is_ui_file, signal_handler,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="audit", messages={
    "follow": "   👀 Suis l'audit en direct dans un autre terminal : tmux attach -t {session}",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
AUDIT_DIR             = "audit_nielsen"          # constats intermédiaires (un fichier par heuristique)
AUDIT_REPORT_FILE     = "design_audit_report.md" # livrable final consolidé
FAIL_REPORT_FILE      = "failReport.md"          # rapport d'arrêt persistant (même contrat que l'usine)
AUDIT_SKILL_FILE      = "./.agents/pipeline/audit-nielsen/SKILL.md"
AGENT_CONFIG_FILE     = RUNNER.config_file

# Fichier temporaire de routage de contexte (prompt déporté, nommé par le harness)
TMP_AUDIT_FILE        = RUNNER.tmp_file("audit")

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : c'est le
# seul choix valable sur les 3 OS (Windows n'a pas de /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Nom de la session tmux, suffixé d'une empreinte du répertoire du projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session. Préfixe DISTINCT
# des autres variantes (rôles 'factory' / 'proto') : un audit peut coexister sur la machine
# avec une production sur un AUTRE projet sans risque de collision de session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3              # Tentatives par passe (filet de vivacité + plancher structurel)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par passe d'audit (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes)

# Au-delà de cette taille, la liste des fichiers du périmètre est tronquée dans le prompt
# (fenêtre de contexte de l'auditeur) : les fichiers restants sont comptés, pas listés.
MAX_SCOPE_FILES_IN_PROMPT = 150

# ─── LES 10 HEURISTIQUES DE NIELSEN (id, slug de fichier, intitulé) ───────────
# Liste FIXE et déterministe : l'audit n'a besoin ni de PO, ni d'Architecte, ni de
# blackboard — le découpage en phases est connu d'avance, Python le pilote seul.
# Les intitulés doivent correspondre aux sections '### H<n>' de la grille
# (AUDIT_SKILL_FILE) : c'est elle qui porte le contenu, ici on ne porte que le plan.
NIELSEN_HEURISTICS = [
    (1,  "visibilite-etat-systeme",   "Visibilité de l'état du système"),
    (2,  "correspondance-monde-reel", "Correspondance entre le système et le monde réel"),
    (3,  "controle-liberte",          "Contrôle et liberté de l'utilisateur"),
    (4,  "coherence-standards",       "Cohérence et standards"),
    (5,  "prevention-erreurs",        "Prévention des erreurs"),
    (6,  "reconnaissance-rappel",     "Reconnaissance plutôt que rappel"),
    (7,  "flexibilite-efficacite",    "Flexibilité et efficacité d'utilisation"),
    (8,  "esthetique-minimalisme",    "Esthétique et design minimaliste"),
    (9,  "recuperation-erreurs",      "Aide à la reconnaissance, au diagnostic et à la récupération des erreurs"),
    (10, "aide-documentation",        "Aide et documentation"),
]


# ─── SENTINELLES (CANAL AUDITEUR → ORCHESTRATEUR) ─────────────────────────────
# Préfixe '.audit_' DISTINCT des '.phase_' / '.pipeline_' des autres variantes : un
# résidu d'un ancien run de production ne peut pas être pris pour un signal d'audit,
# et réciproquement.

def audit_sentinel(slot: str, attempt: int) -> str:
    """Fichier écrit par l'auditeur en toute fin de passe (signal 'j'ai terminé').

    'slot' identifie la passe ('h1'…'h10', 'synthese'). Le numéro de tentative est inclus
    dans le nom : une sentinelle écrite tardivement par l'agent d'une tentative précédente
    ne peut pas être prise pour le signal de la tentative courante.
    """
    return f".audit_{slot}.attempt{attempt}.done"


def cleanup_slot_sentinels(slot: str):
    """Supprime toutes les sentinelles (toutes tentatives) d'une passe."""
    prefix = f".audit_{slot}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_audit_sentinels():
    """Nettoyage final de toutes les sentinelles d'audit résiduelles."""
    for name in os.listdir("."):
        if name.startswith(".audit_") and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def wait_for_deliverable(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> bool:
    """Attend un livrable d'audit signalé par SENTINELLE (même contrat que le pipeline
    des autres variantes : l'agent crée le .done APRÈS avoir sauvegardé le livrable).

    FILET pour un agent qui oublie la sentinelle : si le livrable existe, est non vide et
    n'a plus bougé depuis STABLE_POLLS_FALLBACK contrôles consécutifs, on l'accepte avec
    avertissement (dégradation gracieuse). Le 'structural_check' optionnel ne durcit QUE
    ce filet : un livrable stable mais structurellement incomplet continue d'attendre
    (l'agent écrit peut-être encore) jusqu'au timeout global.
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
                        print(f"   ⏳ '{filepath}' est stable mais structurellement incomplet : "
                              f"on continue d'attendre (l'agent écrit peut-être encore).")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinelle '{sentinel}' absente mais '{filepath}' est stable depuis "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s : livrable accepté (filet de secours).")
                return True
    return False


def findings_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'un fichier de constats : ses sections obligatoires
    '## Constats' et '## Bilan' doivent être présentes (un fichier à moitié écrit — ou
    du bavardage hors format — s'arrête avant)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return "## constats" in content and "## bilan" in content
    except OSError:
        return False


def report_structural_check(path: str) -> bool:
    """Plancher structurel minimal du rapport final : sa section obligatoire
    « Synthèse exécutive » doit être présente."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "synthèse exécutive" in f.read().lower()
    except OSError:
        return False


def findings_path(h_id: int, slug: str) -> str:
    """Chemin du fichier de constats d'une heuristique (zéro-paddé pour le tri à l'œil)."""
    return f"{AUDIT_DIR}/H{h_id:02d}_{slug}.md"


def findings_ok(path: str) -> bool:
    """Un fichier de constats est-il exploitable (présent, non vide, structurellement valide) ?
    Sert à la reprise (phase sautée), à l'affichage d'avancement et au rapport d'échec."""
    return os.path.exists(path) and os.path.getsize(path) > 0 and findings_structural_check(path)


# ─── GRILLE D'AUDIT : CHARGEMENT ET DÉCOUPE PAR HEURISTIQUE ───────────────────
# Cœur de la logique MAIsterMind appliquée à l'audit : chaque passe ne reçoit que le
# TRONC COMMUN de la grille (rôle, règles de fer, échelle de sévérité, format de sortie)
# plus SA section '### H<n>' — jamais les 9 autres. Même famille qu'extract_spec_slice
# dans les variantes de production.

# En-tête d'une section d'heuristique dans la grille (ex. « ### H4 : Cohérence et standards »).
H_HEADING_RE = re.compile(r"^###\s+H(\d+)\b")


def load_audit_grid() -> str:
    """Charge la grille d'audit (SKILL.md). Son absence est un échec IMMÉDIAT : sans
    grille, les auditeurs improviseraient — exactement ce que l'usine interdit."""
    if not os.path.exists(AUDIT_SKILL_FILE):
        return ""
    with open(AUDIT_SKILL_FILE, "r", encoding="utf-8") as f:
        return f.read()


def collect_grid_h_ids(grid_text: str) -> set:
    """Identifiants (int) des sections '### H<n>' présentes dans la grille."""
    ids = set()
    for line in grid_text.splitlines():
        match = H_HEADING_RE.match(line.strip())
        if match:
            ids.add(int(match.group(1)))
    return ids


def extract_heuristic_slice(grid_text: str, h_id: int) -> str:
    """Tranche de la grille limitée au tronc commun + l'heuristique de LA passe.

    Prudence de petit modèle : si la grille ne suit pas le format à sections H, ou si
    l'heuristique demandée n'y figure pas (grille éditée à la main), on renvoie la grille
    ENTIÈRE (dégradation gracieuse — ne jamais priver l'auditeur de sa définition par
    excès de zèle du découpage).
    """
    grid_ids = collect_grid_h_ids(grid_text)
    if not grid_ids or h_id not in grid_ids:
        return grid_text
    kept = []
    current_h = None  # id de la section H en cours, None = tronc commun
    for line in grid_text.splitlines():
        match = H_HEADING_RE.match(line.strip())
        if match:
            current_h = int(match.group(1))
        elif current_h is not None and line.startswith("## "):
            current_h = None  # fin de la zone des heuristiques : retour au tronc commun
        if current_h is None or current_h == h_id:
            kept.append(line)
    return "\n".join(kept)


# ─── DÉCOUVERTE DU PÉRIMÈTRE (PYTHON, DÉTERMINISTE, ZÉRO LLM) ─────────────────
# Le périmètre est établi par l'orchestrateur, jamais par un agent : liste stable,
# reproductible, affichée à l'humain AVANT de payer le moindre tour de LLM.

UI_EXTENSIONS = {".html", ".htm", ".css", ".scss", ".sass", ".less",
                 ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                 ".vue", ".svelte", ".astro", ".ejs", ".hbs", ".njk", ".twig"}

# Répertoires exclus par NOM ; tout répertoire caché ('.git', '.agents', '.opencode'/'.codex',
# '.venv', '.next'…) est exclu d'office par le filtre startswith('.') du walk.
EXCLUDED_DIR_NAMES = {"node_modules", "dist", "build", "out", "coverage", "target",
                      "vendor", "__pycache__", AUDIT_DIR}


def is_test_file(path: str) -> bool:
    """Heuristique de nommage best-effort : 'path' ressemble-t-il à un fichier de test ?

    Les tests ne sont pas une interface exposée à l'utilisateur : ils sortent du périmètre
    d'audit (bruit). Mêmes conventions multi-langages que les variantes de production.
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


def discover_ui_scope() -> list:
    """Liste triée (chemins relatifs, séparateur '/') des fichiers UI à auditer."""
    scope = []
    for root, dirs, files in os.walk(".", topdown=True):
        dirs[:] = sorted(d for d in dirs
                         if d not in EXCLUDED_DIR_NAMES and not d.startswith("."))
        for name in files:
            if not is_ui_file(name):
                continue
            rel = os.path.normpath(os.path.join(root, name)).replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            if is_test_file(rel):
                continue
            scope.append(rel)
    return sorted(scope)


def business_context_file() -> str:
    """Fichier de contexte métier disponible ('spec.md' prioritaire, sinon 'need.md'),
    ou chaîne vide. L'audit n'en a PAS besoin pour tourner : c'est un plus optionnel."""
    for candidate in (SPEC_FILE, NEED_FILE):
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


def business_context_hint() -> str:
    """Pointeur OPTIONNEL vers le contexte métier : on n'inline jamais la spec dans le
    prompt d'audit (fenêtre de contexte), on indique seulement où la trouver."""
    context = business_context_file()
    if context:
        return (f"Le fichier '{context}' (contexte métier) existe à la racine : consulte-le "
                f"UNIQUEMENT si un parcours t'est incompréhensible sans lui (économise ton contexte).")
    return "(aucun fichier de contexte métier détecté : audite l'interface telle qu'elle se présente)"


# ─── GARDE READ-ONLY (GIT, BEST-EFFORT) ───────────────────────────────────────
# « Python vérifie ce qui est vérifiable » : l'interdiction de modifier le projet audité
# est portée par les prompts (invérifiable seule) ET par ce diff mécanique quand un dépôt
# git préexiste. Contrairement aux variantes de production, on ne fait JAMAIS de
# 'git init' ni de commit : un audit ne doit laisser AUCUNE trace dans le projet audité
# en dehors de ses livrables ('audit_nielsen/', rapport).

_GIT = {"enabled": False, "baseline_untracked": set(), "baseline_dirty": set()}

# Identité passée à chaque commande : l'usine ne doit pas dépendre de la config git de la machine.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]


def run_git(args: list, timeout: int = 60) -> tuple:
    """Exécute une commande git. Renvoie (ok, stdout strippé). Ne lève jamais."""
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


# Livrables et artefacts de l'AUDIT lui-même : les seuls fichiers que l'auditeur a le
# droit de produire — jamais restaurés ni signalés par la garde read-only. Contrairement
# à la production, PAS de '.gitignore' ici : l'audit n'écrit jamais ce fichier, un
# auditeur qui y toucherait doit être restauré comme pour tout fichier du projet.
_AUDIT_BASENAMES = {AUDIT_REPORT_FILE, FAIL_REPORT_FILE, TMP_AUDIT_FILE,
                    TMP_PROMPT_BUFFER, os.path.basename(__file__)}


def is_audit_artifact(path: str) -> bool:
    """'path' est-il un livrable/artefact de l'audit (et non un fichier du projet audité) ?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _AUDIT_BASENAMES:
        return True
    if segments[0] == AUDIT_DIR:
        return True
    # Sentinelles et tampons éphémères, où qu'ils se trouvent dans l'arbre.
    if base.startswith(".audit_") and base.endswith(".done"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Caches Python, environnement virtuel et répertoires d'outillage : hors projet audité.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def init_readonly_guard():
    """Active la garde read-only si (et seulement si) le projet est DÉJÀ un dépôt git.

    DEUX baselines sont capturées maintenant, AVANT le premier agent :
      - les fichiers non suivis préexistants : sans cette baseline, les fichiers non
        suivis de l'utilisateur seraient signalés à chaque passe comme « créés par
        l'auditeur » (faux positif permanent) ;
      - les fichiers suivis DÉJÀ MODIFIÉS (worktree sale) : sans cette baseline, la
        restauration 'git checkout' DÉTRUIRAIT du travail humain non commité antérieur
        à l'audit — inacceptable. Ces fichiers sortent de la garde pour tout le run
        (compromis assumé : une retouche d'auditeur sur un fichier déjà sale n'est pas
        restaurée ; ne jamais détruire du travail humain prime sur la garde).
    """
    if shutil.which("git") is None or not os.path.isdir(".git"):
        print("ℹ️  Pas de dépôt git préexistant : la garde read-only mécanique est inactive "
              "(l'interdiction de modifier le projet reste portée par les prompts).")
        return
    _GIT["enabled"] = True
    ok, out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok:
        _GIT["baseline_untracked"] = {line.strip() for line in out.splitlines() if line.strip()}
    ok_dirty, dirty_out = run_git(["diff", "--name-only", "HEAD"])
    if ok_dirty:
        _GIT["baseline_dirty"] = {line.strip() for line in dirty_out.splitlines() if line.strip()}
    print("✓ Dépôt git détecté : garde read-only active (tout fichier suivi modifié par "
          "un auditeur sera restauré).")
    dirty_project = sorted(f for f in _GIT["baseline_dirty"] if not is_audit_artifact(f))
    if dirty_project:
        print(f"   ⚠️  {len(dirty_project)} fichier(s) déjà modifié(s) AVANT l'audit (travail en "
              f"cours ?) : ils sont exclus de la garde (jamais restaurés d'office) — "
              f"{', '.join(dirty_project[:10])}{'…' if len(dirty_project) > 10 else ''}")


def enforce_readonly(label: str):
    """Restaure les fichiers SUIVIS modifiés pendant une passe et signale les fichiers créés
    hors livrables d'audit (best-effort, après CHAQUE passe).

    Restauration d'office pour les modifications (un audit ne corrige pas) ; simple
    SIGNALEMENT pour les créations (on ne supprime jamais un fichier qu'on n'a pas créé :
    décision laissée à l'humain, comme pour protected_test_files dans l'usine).
    """
    if not _GIT["enabled"]:
        return
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    # 'baseline_dirty' exclu de la restauration : un fichier déjà modifié AVANT l'audit
    # porte du travail humain non commité — le restaurer le DÉTRUIRAIT (cf. init).
    touched = sorted(f for f in diff_out.splitlines()
                     if f.strip() and not is_audit_artifact(f.strip())
                     and f.strip() not in _GIT["baseline_dirty"]) if ok_diff else []
    if touched:
        run_git(["checkout", "--"] + touched)
        print(f"🛡️  [{label}] AUDIT = LECTURE SEULE : {len(touched)} fichier(s) du projet "
              f"modifié(s) par l'auditeur — restauré(s) : {', '.join(touched)}")
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        strays = sorted(
            f for f in ({line.strip() for line in others_out.splitlines() if line.strip()}
                        - _GIT["baseline_untracked"])
            if not is_audit_artifact(f))
        if strays:
            print(f"⚠️  [{label}] Fichier(s) créé(s) hors livrables d'audit (non supprimés, "
                  f"à inspecter) : {', '.join(strays)}")


# ─── RAPPORT D'ÉCHEC & MESSAGE D'ÉCHEC ────────────────────────────────────────


def audited_count() -> int:
    """Nombre d'heuristiques dont le fichier de constats est déjà exploitable."""
    return sum(1 for (h_id, slug, _t) in NIELSEN_HEURISTICS if findings_ok(findings_path(h_id, slug)))


def write_fail_report(title: str, reason: str, details: str = ""):
    """Écrit un rapport d'arrêt persistant à la racine (même contrat que l'usine :
    tout arrêt NON nominal en produit un). Best-effort : ne lève JAMAIS."""
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind (audit design)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "", "### Avancement",
                 f"- Heuristiques auditées : {audited_count()}/{len(NIELSEN_HEURISTICS)}"]
        for h_id, slug, h_title in NIELSEN_HEURISTICS:
            mark = "✅" if findings_ok(findings_path(h_id, slug)) else "⏳"
            lines.append(f"  - {mark} H{h_id} : {h_title}")
        lines.append("")
        if details.strip():
            lines.append("### Détails")
            lines.append(details.strip()[:4000])
            lines.append("")
        lines.append("### Action recommandée")
        lines.append("Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model ou "
                     f"'{AGENT_CONFIG_FILE}'), puis relance : les heuristiques déjà auditées "
                     "seront reprises automatiquement.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Rapport d'échec écrit dans '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


def fail_audit(message: str, details: str = ""):
    """Point de sortie unique des échecs. Tue toujours la session tmux AVANT de quitter :
    un exit qui laisse l'agent vivant le laisse finir d'écrire son livrable APRÈS
    l'abandon de l'orchestrateur (état de reprise trompeur au relancement)."""
    print(message)
    write_fail_report("Échec d'une passe de l'audit", message, details)
    RUNNER.kill()
    sys.exit(1)


def print_pass_failure(label: str, reason: str):
    model = RUNNER.configured_model()
    print(f"""
{'='*60}
❌ La passe « {label} » n'a pas abouti après {MAX_ATTEMPTS} tentatives.

   Cause : {reason}

💡 Le modèle actuel ({model}) cale sur cette passe (souvent un problème d'appels
   d'outils : le fichier de constats ou la sentinelle ne sont jamais créés, ou le
   format demandé n'est pas respecté).
   Le plus efficace : relance après avoir amené un modèle un cran au-dessus,
   soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : les {audited_count()} heuristique(s) déjà auditée(s) seront reprises
   automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
{'='*60}
""")


# ─── PROMPTS DÉPORTÉS PAR FICHIER ─────────────────────────────────────────────

def build_scope_block(scope_files: list) -> str:
    """Bloc « périmètre » des prompts : liste bornée (fenêtre de contexte de l'auditeur)."""
    listed = scope_files[:MAX_SCOPE_FILES_IN_PROMPT]
    block = "\n".join(f"- {f}" for f in listed)
    overflow = len(scope_files) - len(listed)
    if overflow > 0:
        block += (f"\n(+ {overflow} autre(s) fichier(s) non listé(s) : concentre-toi sur les "
                  f"écrans et parcours principaux ci-dessus.)")
    return block


def build_auditor_prompt(h_id: int, title: str, skill_slice: str, scope_files: list,
                         findings_file: str, feedback: str, attempt: int) -> str:
    sentinel = audit_sentinel(f"h{h_id}", attempt)
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Agent Auditeur UX ultra-spécialisé, affecté à UNE SEULE heuristique de Nielsen :
H{h_id} « {title} ». C'est la passe {h_id}/10 d'une évaluation heuristique découpée.
AUDIT = LECTURE SEULE : tu ne modifies, ne corriges, ne crées AUCUN fichier du projet.
Tu n'écris QUE deux fichiers : ton fichier de constats, puis ta sentinelle de fin.
Ignore tout problème relevant d'une AUTRE heuristique que la tienne : une passe dédiée
s'en charge (le signaler ici créerait des doublons dans le rapport).

--- GRILLE D'AUDIT (tronc commun + TON heuristique) ---
{skill_slice}

--- PÉRIMÈTRE À AUDITER ({len(scope_files)} fichier(s) UI, découverts par l'orchestrateur) ---
{build_scope_block(scope_files)}
Procède écran par écran (les .html d'abord, puis les styles et scripts qu'ils référencent) ;
ne charge pas tout le périmètre d'un coup.

--- CONTEXTE MÉTIER (optionnel) ---
{business_context_hint()}

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- LIVRABLE OBLIGATOIRE ---
Écris tes constats dans '{findings_file}' (crée le dossier '{AUDIT_DIR}/' au besoin) en
respectant STRICTEMENT le format de la grille ci-dessus : sections '## Constats' et
'## Bilan' obligatoires ; « Aucun constat. » explicite si l'heuristique est respectée.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{findings_file}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur. Ne le crée que lorsque le fichier de constats est VRAIMENT terminé.
"""
    with open(TMP_AUDIT_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_AUDIT_FILE}' à la racine du projet et réalise "
            f"la passe d'audit H{h_id}.")


def build_synthesis_prompt(attempt: int) -> str:
    sentinel = audit_sentinel("synthese", attempt)
    findings_list = "\n".join(f"- {findings_path(h_id, slug)} (H{h_id} : {title})"
                              for h_id, slug, title in NIELSEN_HEURISTICS)
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Lead Product Designer chargé de CONSOLIDER une évaluation heuristique de Nielsen
réalisée en dix passes indépendantes. Tu ne réaudites RIEN et tu ne relis PAS le code du
projet : tu synthétises les constats existants, c'est tout. ZÉRO invention : le rapport ne
contient QUE des constats présents dans les fichiers listés ci-dessous — tu peux reformuler
pour la lisibilité, jamais ajouter, retirer ni requalifier une sévérité.
Tu ne modifies aucun fichier du projet ; tu n'écris QUE le rapport final, puis ta sentinelle.

--- FICHIERS DE CONSTATS À CONSOLIDER (un par heuristique, lis-les TOUS) ---
{findings_list}

--- RAPPORT À PRODUIRE : '{AUDIT_REPORT_FILE}' ---
Structure OBLIGATOIRE :

# Audit design — Grille de Nielsen

## 1. Synthèse exécutive
[3 à 6 phrases : état général de l'interface, décompte total des constats par sévérité
(appuie-toi sur les lignes '## Bilan' des fichiers de constats), les 2 ou 3 chantiers
prioritaires.]

## 2. Vue par heuristique
[Tableau Markdown : Heuristique | Intitulé | Nb constats | Sévérité max.]

## 3. Problèmes majeurs et bloquants
[Tous les constats de sévérité 4, puis 3 — chacun avec titre, heuristique d'origine,
localisation, impact utilisateur et recommandation, repris des fichiers de constats.]

## 4. Quick wins
[Les constats de sévérité 1 ou 2 dont la recommandation est peu coûteuse à appliquer.]

## 5. Détail par heuristique
[Pour chaque heuristique H1 → H10 : ses constats repris tels quels, ou « Aucun constat. ».]

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{AUDIT_REPORT_FILE}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur.
"""
    with open(TMP_AUDIT_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_AUDIT_FILE}' à la racine du projet et consolide "
            f"l'audit en un rapport final.")


# ─── BOUCLE D'AUDIT (UNE PASSE PAR HEURISTIQUE) ───────────────────────────────

def run_audit_passes(grid_text: str, scope_files: list):
    total = len(NIELSEN_HEURISTICS)

    for h_id, slug, title in NIELSEN_HEURISTICS:
        findings_file = findings_path(h_id, slug)

        # Reprise par fichiers : un fichier de constats exploitable saute sa passe.
        if findings_ok(findings_file):
            print(f"⏭️  Passe H{h_id}/{total} déjà auditée ('{findings_file}') : sautée.")
            continue
        if os.path.exists(findings_file):
            # Résidu à moitié écrit d'un run interrompu : on repart proprement.
            try:
                os.remove(findings_file)
                print(f"🧹 '{findings_file}' résiduel (incomplet) supprimé : la passe est rejouée.")
            except OSError:
                pass

        print(f"\n{'='*50}\n🔎 PASSE H{h_id}/{total} : {title}\n{'='*50}")

        # Fenêtre de contexte : l'auditeur ne reçoit que le tronc commun de la grille
        # plus SA section — jamais les 9 autres heuristiques.
        skill_slice = extract_heuristic_slice(grid_text, h_id)
        if len(skill_slice) < len(grid_text):
            print(f"   ✂️  Grille tranchée pour la passe : {len(skill_slice)}/{len(grid_text)} caractères.")

        attempts = 0
        success = False
        feedback = "Premier passage — aucun retour précédent."

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1

            # Rattrapage d'un livrable TARDIF : l'agent de la tentative précédente a pu
            # finir d'écrire APRÈS le timeout de l'orchestrateur. Si son fichier est
            # devenu exploitable entre-temps, on le prend tel quel plutôt que de payer
            # un tour d'agent pour tout refaire.
            if attempts > 1 and findings_ok(findings_file):
                print(f"   ♻️  '{findings_file}' est finalement arrivé (livrable tardif) : accepté.")
                success = True
                break

            cleanup_slot_sentinels(f"h{h_id}")
            print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Passe H{h_id} — lancement de l'Auditeur UX...")

            prompt = build_auditor_prompt(h_id, title, skill_slice, scope_files,
                                          findings_file, feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(prompt))
            RUNNER.send_task(prompt)

            got_deliverable = wait_for_deliverable(findings_file,
                                                   audit_sentinel(f"h{h_id}", attempts),
                                                   structural_check=findings_structural_check)
            # Garde read-only après CHAQUE tentative (aboutie ou non) : un auditeur qui a
            # « corrigé » du code en cours de route est restauré immédiatement.
            enforce_readonly(f"H{h_id}")

            if not got_deliverable:
                feedback = ("Au passage précédent, aucun livrable n'a été reçu (fichier de "
                            "constats absent, vide ou jamais signalé). Écris d'abord le fichier "
                            "de constats complet, PUIS la sentinelle, dans cet ordre.")
                print(f"⏱️  L'auditeur n'a pas signalé la fin de la passe H{h_id}. Nouvelle tentative.")
                RUNNER.new_context()
                continue

            # Plancher structurel APRÈS coup, même quand la sentinelle est arrivée : le
            # chemin sentinelle de wait_for_deliverable ne vérifie pas la structure, et un
            # fichier hors format serait inconsolidable par la synthèse.
            if not findings_structural_check(findings_file):
                feedback = (f"Ton fichier '{findings_file}' ne respecte pas le format demandé : "
                            f"les sections '## Constats' (avec des constats au format de la "
                            f"grille, ou la seule ligne « Aucun constat. ») et '## Bilan' sont "
                            f"OBLIGATOIRES. Réécris-le entièrement au bon format.")
                try:
                    os.remove(findings_file)
                except OSError:
                    pass
                print(f"⚠️  [REJET] Tentative {attempts} : fichier de constats hors format "
                      f"(sections obligatoires absentes).")
                RUNNER.new_context()
                continue

            success = True

        if not success:
            reason = feedback
            cleanup_all_audit_sentinels()
            print_pass_failure(f"H{h_id} : {title}", reason)
            fail_audit(f"❌ Passe H{h_id} non aboutie après {MAX_ATTEMPTS} tentatives.", details=reason)

        print(f"✅ Passe H{h_id} terminée : constats dans '{findings_file}'.")
        cleanup_slot_sentinels(f"h{h_id}")
        RUNNER.new_context()


# ─── SYNTHÈSE FINALE ──────────────────────────────────────────────────────────

def run_synthesis():
    print(f"\n{'='*50}\n🧾 SYNTHÈSE : CONSOLIDATION DES 10 PASSES EN UN RAPPORT\n{'='*50}")

    # La synthèse est TOUJOURS rejouée (elle doit refléter les constats à jour) : un
    # rapport résiduel — de ce run comme d'un précédent — est purgé pour que l'attente
    # ci-dessous n'observe que le rapport de CETTE passe.
    if os.path.exists(AUDIT_REPORT_FILE):
        try:
            os.remove(AUDIT_REPORT_FILE)
            print(f"   🧹 '{AUDIT_REPORT_FILE}' résiduel supprimé (la synthèse est régénérée).")
        except OSError:
            pass

    attempts = 0
    success = False
    while not success and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Rattrapage d'un livrable TARDIF (même logique que les passes d'audit) : un
        # rapport devenu valide après le timeout de la tentative précédente est accepté.
        if attempts > 1 and os.path.exists(AUDIT_REPORT_FILE) \
                and os.path.getsize(AUDIT_REPORT_FILE) > 0 \
                and report_structural_check(AUDIT_REPORT_FILE):
            print(f"   ♻️  '{AUDIT_REPORT_FILE}' est finalement arrivé (livrable tardif) : accepté.")
            success = True
            break

        cleanup_slot_sentinels("synthese")
        print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Lancement de l'agent de synthèse...")

        prompt = build_synthesis_prompt(attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable = wait_for_deliverable(AUDIT_REPORT_FILE,
                                               audit_sentinel("synthese", attempts),
                                               structural_check=report_structural_check)
        enforce_readonly("Synthèse")

        if not got_deliverable or not report_structural_check(AUDIT_REPORT_FILE):
            if os.path.exists(AUDIT_REPORT_FILE) and not report_structural_check(AUDIT_REPORT_FILE):
                try:
                    os.remove(AUDIT_REPORT_FILE)
                except OSError:
                    pass
            print("⏱️  Synthèse absente ou hors format. Nouvelle tentative.")
            RUNNER.new_context()
            continue
        success = True

    if not success:
        # Échec de la seule CONSOLIDATION : les constats bruts restent exploitables tels
        # quels — on le dit explicitement pour que le run ne paraisse pas perdu.
        cleanup_all_audit_sentinels()
        print_pass_failure("Synthèse", "le rapport consolidé n'a jamais été produit au bon format.")
        fail_audit(f"❌ Synthèse non aboutie après {MAX_ATTEMPTS} tentatives. Les constats bruts "
                   f"restent exploitables dans '{AUDIT_DIR}/'.")

    print(f"✅ Rapport d'audit consolidé : '{AUDIT_REPORT_FILE}'.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "audit-design", RUNNER.name,
                   model=RUNNER.configured_model())
    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du
    # run courant : on le purge au démarrage (même contrat que l'usine).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # La grille est le référentiel de TOUT l'audit : son absence est un échec immédiat
    # (sans elle, les auditeurs improviseraient — exactement ce que l'usine interdit).
    grid_text = load_audit_grid()
    if not grid_text.strip():
        print(f"❌ Grille d'audit manquante ou vide : '{AUDIT_SKILL_FILE}'.")
        write_fail_report("Grille d'audit manquante",
                          f"'{AUDIT_SKILL_FILE}' est introuvable ou vide : impossible d'auditer sans référentiel.")
        sys.exit(1)
    grid_ids = collect_grid_h_ids(grid_text)
    missing = [str(h_id) for h_id, _s, _t in NIELSEN_HEURISTICS if h_id not in grid_ids]
    if missing:
        # Warn-only : la découpe retombe sur la grille entière pour ces passes
        # (dégradation gracieuse d'extract_heuristic_slice).
        print(f"⚠️  Sections manquantes dans la grille : H{', H'.join(missing)} — ces passes "
              f"recevront la grille ENTIÈRE au lieu de leur tranche.")

    # Étape 0 : périmètre découvert par PYTHON (déterministe), montré à l'humain AVANT
    # de payer le moindre tour d'agent.
    scope_files = discover_ui_scope()
    if not scope_files:
        print("❌ Aucun fichier d'interface trouvé dans ce répertoire (extensions cherchées : "
              + ", ".join(sorted(UI_EXTENSIONS)) + ").")
        print("   → Lance l'audit depuis la racine du projet qui contient l'interface à évaluer.")
        write_fail_report("Périmètre d'audit vide",
                          "Aucun fichier d'interface détecté dans le répertoire courant.")
        sys.exit(1)

    already = audited_count()
    preview = scope_files[:20]

    print(f"\n{'='*50}")
    print(f"🔎 AUDIT NIELSEN — Périmètre découvert :")
    print(f"   Répertoire : {os.getcwd()}")
    print(f"   {len(scope_files)} fichier(s) UI à auditer. Aperçu :")
    for f in preview:
        print(f"      - {f}")
    if len(scope_files) > len(preview):
        print(f"      … et {len(scope_files) - len(preview)} autre(s).")
    context = business_context_file()
    if context:
        print(f"   Contexte métier : '{context}' détecté (pointé aux auditeurs en lecture optionnelle).")
    else:
        print(f"   Contexte métier : aucun ('{SPEC_FILE}'/'{NEED_FILE}' absents) — l'interface est "
              f"auditée telle qu'elle se présente.")
    if already:
        print(f"   Reprise : {already}/{len(NIELSEN_HEURISTICS)} heuristique(s) déjà auditée(s) "
              f"(constats présents dans '{AUDIT_DIR}/').")
    print(f"   Déroulé : {len(NIELSEN_HEURISTICS)} passes d'audit (une par heuristique, contexte "
          f"réinitialisé entre chaque) + 1 synthèse → '{AUDIT_REPORT_FILE}'.")
    print(f"{'='*50}")

    confirm = input("\n▶️  Lancer l'audit sur ce périmètre ? (y/n) : ")
    mm_audit.event("gate", id="scope", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print("⏹️  Annulé par l'utilisateur.")
        sys.exit(0)

    # Garde read-only : baseline capturée AVANT le premier agent.
    init_readonly_guard()

    # 🚀 Boot du harness Data Center dans tmux
    RUNNER.start()

    # Étape 1 : les 10 passes d'audit (une session neuve par heuristique).
    run_audit_passes(grid_text, scope_files)

    # Étape 2 : consolidation en rapport final.
    run_synthesis()

    # Dernier passage de la garde read-only : couvre la fenêtre entre le dernier enforce
    # d'une passe et la fin du run (notamment le chemin « rapport tardif accepté » de la
    # synthèse, qui sort sans enforce).
    enforce_readonly("final")

    # Nettoyage des fichiers temporaires et sentinelles, puis fermeture propre.
    for tmp_f in [TMP_AUDIT_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_audit_sentinels()
    RUNNER.kill()
    # Run réussi : aucun rapport d'échec ne doit subsister.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    print(f"""
🏁 [CONGRATULATIONS] Audit Nielsen terminé !
   📄 Rapport consolidé : '{AUDIT_REPORT_FILE}'
   🗂️  Constats détaillés par heuristique : '{AUDIT_DIR}/'
   ♻️  Pour refaire un audit COMPLET (après corrections, p. ex.) : supprime '{AUDIT_DIR}/'
      puis relance — un fichier de constats conservé fait sauter sa passe (reprise par fichiers).""")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    RUNNER=RUNNER,
    UI_EXTENSIONS=UI_EXTENSIONS,
)


if __name__ == "__main__":
    main()
