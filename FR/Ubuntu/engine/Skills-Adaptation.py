#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Adaptateur de skills « à ta stack » (harness d'agent + tmux)
─────────────────────────────────────────────────────────────────────────────
Les skills de codage livrés (backend-coding : Java/Spring Boot, frontend-coding :
React/TypeScript, et leurs pendants testing) sont des GABARITS. Cet orchestrateur
les réécrit pour TA stack, via un questionnaire court, puis écrase les originaux
du projet — chacun après TA validation, ancien contenu sauvegardé en .bak.

Chaîne de qualité, dans l'esprit de l'usine (l'IA propose, Python vérifie,
l'humain tranche) :
  1. QUESTIONNAIRE (Python, gratuit) → profil d'adaptation persisté
     ('skill_adapt_profile.yaml') : périmètre, stacks cibles, conventions,
     limite de lignes (200 par défaut, 250 ou 300 au choix), gabarit du modèle
     qui CONSOMMERA les skills (standard ≥ 100B, ou compact ~27B type Qwen3 27B —
     consignes plus courtes et mécaniques).
  2. GÉNÉRATION (agent, contexte neuf par skill) : réécriture guidée par la grille
     '.agents/pipeline/skill-adapt/SKILL.md' — des ORDRES, jamais des descriptions ;
     tableau patterns/anti-patterns obligatoire ; checklist finale vérifiable.
  3. GARDE-FOUS PYTHON (déterministes) : limite de lignes, frontmatter contractuel
     (name inchangé — c'est la clé de routage du blackboard), tableau ❌/✅ et
     checklist présents. Échec → consigne de réparation, au plus MAX_REPAIRS fois.
  4. REVUE QUALITÉ (agent indépendant, contexte neuf) : audit contre la grille
     '.agents/pipeline/skill-adapt-review/SKILL.md', verdict première ligne
     ('VERDICT : CONFORME' / 'VERDICT : NON CONFORME') parsé par Python — un
     verdict NON CONFORME déclenche UNE réparation puis une re-revue ; s'il
     persiste, l'humain arbitre en connaissance de cause.
  5. PORTE HUMAINE par skill : aperçu de la proposition ('skill_adapt-<name>.md'
     à la racine, éditable avant validation), y → sauvegarde .bak + écrasement,
     n → proposition conservée pour inspection, jamais appliquée.

Reprise par fichiers, comme partout : profil existant → proposé tel quel ;
proposition déjà générée → re-présentée à la porte humaine sans re-payer
génération ni revue. Rien à configurer.
"""

import os
import sys
import time
import signal

from mm_runner import resolve_runner, resolve_timeout

# Journal de run (boîte noire .mm-runs/, plan-big-last Lot 2) : purement additif,
# no-op intégral si MM_AUDIT=0, ne fait JAMAIS échouer un run.
import mm_audit

# Fonctions partagées extraites au Lot 4a (plan-big-last) : voir mm_core.py.
# La configuration (constantes/objets de CE module) est injectée en fin de
# fichier via mm_core.configure(...) — tous les noms y sont alors définis.
import mm_core
from mm_core import (
    signal_handler,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux vit dans 'mm_runner.py'. Rôle de session DISTINCT
# ('skilladapt') : cet orchestrateur ne peut pas injecter de prompt dans un run
# de production qui tournerait sur le même projet.
RUNNER = resolve_runner(os.getcwd(), role="skilladapt")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
SKILLS_DIR            = "./.agents/skills"
ADAPT_SKILL_FILE      = "./.agents/pipeline/skill-adapt/SKILL.md"
REVIEW_SKILL_FILE     = "./.agents/pipeline/skill-adapt-review/SKILL.md"
PROFILE_FILE          = "skill_adapt_profile.yaml"
REPORT_FILE           = "skill_adapt_report.md"
REVIEW_FILE           = "skill_review.md"
FAIL_REPORT_FILE      = "failReport.md"
PROPOSAL_PREFIX       = "skill_adapt-"

AGENT_CONFIG_FILE     = RUNNER.config_file

# Fichiers temporaires de routage de contexte (prompt déporté)
TMP_ADAPT_FILE        = RUNNER.tmp_file("skilladapt")
TMP_REVIEW_FILE       = RUNNER.tmp_file("skillreview")
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Sentinelles de fin de passe (même contrat que la production : l'agent crée le
# .done APRÈS avoir sauvegardé le livrable — signal sans ambiguïté).
ADAPT_DONE_SENTINEL   = ".pipeline_skill_adapt.done"
REVIEW_DONE_SENTINEL  = ".pipeline_skill_review.done"

TMUX_SESSION          = RUNNER.session

POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)
STABLE_POLLS_FALLBACK = 15
MAX_REPAIRS           = 2

# Verdicts du contrôleur qualité, parsés sur la PREMIÈRE ligne du rapport.
VERDICT_LINE_PREFIX   = "VERDICT :"
VERDICT_OK            = "CONFORME"
VERDICT_KO            = "NON CONFORME"

# Limites de lignes proposées au questionnaire (choix 1 = défaut).
LINE_CAPS             = {"1": 200, "2": 250, "3": 300}
MODEL_TARGETS         = {"1": "standard", "2": "compact"}

# Directives de calibrage injectées dans le prompt de génération, par gabarit.
MODEL_DIRECTIVES = {
    "standard": "modèles ≥ 100B : concision experte permise, vocabulaire technique standard sans le définir.",
    "compact":  "petit modèle local (~27B, ex. Qwen3 27B) : phrases de 20 mots max, zéro sous-entendu, "
                "chaque règle mécaniquement applicable, définis tout sigle, un seul template minimal par couche.",
}

# Domaine affiché par skill (contexte donné au générateur et au contrôleur).
SKILL_DOMAINS = {
    "backend-coding":   "code de production backend",
    "backend-testing":  "tests backend",
    "frontend-coding":  "code de production frontend",
    "frontend-testing": "tests frontend",
}


def fail_pipeline(message: str):
    """Point de sortie unique des échecs. Tue toujours la session tmux AVANT de
    quitter : un agent laissé vivant finirait d'écrire sa proposition APRÈS
    l'abandon, et la reprise par fichiers prendrait ce fichier à moitié écrit
    pour une proposition valide."""
    print(message)
    write_fail_report("Échec de l'adaptation de skills", message)
    RUNNER.kill()
    sys.exit(1)


def write_fail_report(title: str, reason: str):
    """Rapport d'arrêt persistant à la racine (même contrat que l'usine).
    Best-effort : ne lève JAMAIS."""
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind (adaptation de skills)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "",
                 "### Action recommandée",
                 "Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model dans le "
                 f"TUI ou '{AGENT_CONFIG_FILE}'), puis relance : les propositions déjà "
                 "acceptées sont reprises telles quelles."]
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Rapport d'échec écrit dans '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def cleanup_pipeline_sentinel(sentinel: str):
    try:
        os.remove(sentinel)
    except OSError:
        pass


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT) -> bool:
    """Attend un livrable signalé par SENTINELLE, avec le filet de stabilité
    standard de l'usine (livrable non vide, immobile pendant N contrôles)."""
    start = time.time()
    print(f"   ⏳ En attente de '{filepath}' (signal de fin : '{sentinel}')...")
    stable_streak = 0
    last_size = -1
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
                print(f"   ⚠️  Sentinelle '{sentinel}' absente mais '{filepath}' est stable depuis "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s : livrable accepté (filet de secours).")
                return True
    return False


# ─── PROFIL D'ADAPTATION (QUESTIONNAIRE PYTHON, GRATUIT) ──────────────────────

def ask_scope() -> str:
    """Question de périmètre. Le prompt est LITTÉRAL au point d'appel input() :
    c'est le contrat de check_gate_labels.py (lecture AST) et du manifeste."""
    print("\n   [1] Backend + Frontend")
    print("   [2] Backend seul")
    print("   [3] Frontend seul")
    while True:
        answer = input("   → Périmètre à adapter (1/2/3) : ").strip()
        mm_audit.event("gate", id="scope", gate_kind="choice", answer=answer)
        if answer in ("1", "2", "3"):
            return answer
        print("   ↳ Réponds par 1, 2 ou 3.")


def ask_backend_stack() -> str:
    while True:
        answer = input("   → Stack backend cible (ex. Kotlin + Spring Boot) : ").strip()
        mm_audit.event("gate", id="stack-back", gate_kind="text", answer=answer)
        if answer:
            return answer
        print("   ↳ Réponse obligatoire : nomme le langage et le framework.")


def ask_frontend_stack() -> str:
    while True:
        answer = input("   → Stack frontend cible (ex. Vue 3 + Vite) : ").strip()
        mm_audit.event("gate", id="stack-front", gate_kind="text", answer=answer)
        if answer:
            return answer
        print("   ↳ Réponse obligatoire : nomme le framework et l'outillage.")


def ask_conventions() -> str:
    answer = input("   → Conventions particulières à imposer (- si aucune) : ").strip()
    mm_audit.event("gate", id="conventions", gate_kind="text", answer=answer)
    return answer if answer else "-"


def ask_line_cap() -> str:
    print("\n   [1] 200 lignes par skill (défaut recommandé)")
    print("   [2] 250 lignes par skill")
    print("   [3] 300 lignes par skill")
    while True:
        answer = input("   → Limite de lignes par skill (1/2/3) : ").strip()
        mm_audit.event("gate", id="line-cap", gate_kind="choice", answer=answer)
        if answer in LINE_CAPS:
            return answer
        print("   ↳ Réponds par 1, 2 ou 3.")


def ask_model_target() -> str:
    print("\n   [1] Standard : modèles ≥ 100B (cloud ou gros local)")
    print("   [2] Compact : petit modèle local ~27B (ex. Qwen3 27B) — consignes plus courtes et mécaniques")
    while True:
        answer = input("   → Gabarit du modèle cible (1/2) : ").strip()
        mm_audit.event("gate", id="model-target", gate_kind="choice", answer=answer)
        if answer in MODEL_TARGETS:
            return answer
        print("   ↳ Réponds par 1 ou 2.")


def run_questionnaire() -> dict:
    """La suite de questions qui fabrique le profil. Chaque prompt est un libellé
    EXACT du manifeste orchestrators.json : l'app les détecte comme des portes
    (choix numérotés, texte libre, y/n) — ne les reformule jamais sans lui."""
    print(f"\n{'=' * 62}")
    print("🧬 PROFIL D'ADAPTATION — quelques questions, zéro agent payé.")
    print("   Les skills livrés sont des gabarits (Java/Spring, React/TS) :")
    print("   décris ta stack, l'usine les réécrit pour elle.")
    print(f"{'=' * 62}")

    scope = ask_scope()

    testing = input("\n▶️  Adapter aussi les skills de testing associés ? (y/n) : ").strip().lower()
    mm_audit.event("gate", id="testing", gate_kind="yn", answer=testing)

    backend_stack = "-"
    frontend_stack = "-"
    if scope in ("1", "2"):
        backend_stack = ask_backend_stack()
    if scope in ("1", "3"):
        frontend_stack = ask_frontend_stack()

    conventions = ask_conventions()

    cap = ask_line_cap()

    model = ask_model_target()

    return {
        "scope": scope,
        "include_testing": "y" if testing == "y" else "n",
        "backend_stack": backend_stack,
        "frontend_stack": frontend_stack,
        "conventions": conventions,
        "line_cap": str(LINE_CAPS[cap]),
        "model_target": MODEL_TARGETS[model],
    }


PROFILE_KEYS = ["scope", "include_testing", "backend_stack", "frontend_stack",
                "conventions", "line_cap", "model_target"]


def write_profile(profile: dict):
    """Profil persisté en YAML plat (clé: valeur) : lisible, éditable à la main,
    parsé ici sans dépendance."""
    lines = ["# Profil d'adaptation des skills — généré par Skills-Adaptation.py",
             "# Éditable à la main : relance l'orchestrateur et réponds y pour le réutiliser."]
    for key in PROFILE_KEYS:
        lines.append(f"{key}: {profile[key]}")
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_profile() -> dict:
    """Relit le profil persisté. Renvoie {} si absent ou incomplet (le
    questionnaire sera rejoué : jamais de profil deviné)."""
    if not os.path.exists(PROFILE_FILE):
        return {}
    profile = {}
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                profile[key.strip()] = value.strip()
    except OSError:
        return {}
    if any(key not in profile or profile[key] == "" for key in PROFILE_KEYS):
        return {}
    if profile["scope"] not in ("1", "2", "3") or profile["model_target"] not in MODEL_TARGETS.values():
        return {}
    if profile["line_cap"] not in {str(cap) for cap in LINE_CAPS.values()}:
        return {}
    return profile


def profile_summary(profile: dict) -> str:
    """Bloc PROFIL injecté tel quel dans les prompts de génération et de revue."""
    scope_labels = {"1": "backend + frontend", "2": "backend seul", "3": "frontend seul"}
    return (f"- Périmètre : {scope_labels[profile['scope']]}"
            f" (skills de testing inclus : {profile['include_testing']})\n"
            f"- Stack backend cible : {profile['backend_stack']}\n"
            f"- Stack frontend cible : {profile['frontend_stack']}\n"
            f"- Conventions imposées : {profile['conventions']}\n"
            f"- Limite STRICTE : {profile['line_cap']} lignes au total (frontmatter compris)\n"
            f"- Modèle cible : {MODEL_DIRECTIVES[profile['model_target']]}")


def target_skills(profile: dict) -> list:
    """Skills à adapter, dans l'ordre (code d'abord, tests ensuite)."""
    targets = []
    if profile["scope"] in ("1", "2"):
        targets.append("backend-coding")
    if profile["scope"] in ("1", "3"):
        targets.append("frontend-coding")
    if profile["include_testing"] == "y":
        if profile["scope"] in ("1", "2"):
            targets.append("backend-testing")
        if profile["scope"] in ("1", "3"):
            targets.append("frontend-testing")
    return targets


def stack_for(name: str, profile: dict) -> str:
    return profile["backend_stack"] if name.startswith("backend") else profile["frontend_stack"]


# ─── GARDE-FOUS PYTHON (DÉTERMINISTES, GRATUITS) ──────────────────────────────

def parse_frontmatter(content: str) -> dict:
    """name + description du frontmatter YAML d'un SKILL.md, sans dépendance."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() in ("name", "description"):
            meta[key.strip()] = value.strip()
    return meta


def check_proposal(path: str, expected_name: str, line_cap: int) -> list:
    """Contrôles structurels d'une proposition. Chaque échec devient une consigne
    de réparation envoyée à l'agent — jamais un avis, toujours un constat mesuré."""
    failures = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return [f"le fichier '{path}' est illisible ou absent"]
    total_lines = len(content.splitlines())
    if total_lines > line_cap:
        failures.append(f"le fichier fait {total_lines} lignes, la limite STRICTE est "
                        f"{line_cap} (coupe dans les templates, jamais dans les règles)")
    meta = parse_frontmatter(content)
    if meta.get("name") != expected_name:
        failures.append(f"le frontmatter doit conserver exactement 'name: {expected_name}' "
                        f"(clé de routage des phases)")
    if not meta.get("description"):
        failures.append("le frontmatter doit porter une 'description:' d'une ligne qui "
                        "nomme la stack cible")
    if "| ❌" not in content or "| ✅" not in content:
        failures.append("le tableau patterns/anti-patterns (colonnes '❌ INTERDIT' / "
                        "'✅ CORRECT') est obligatoire")
    if "- [ ]" not in content:
        failures.append("la checklist finale (cases '- [ ]') est obligatoire")
    return failures


def parse_verdict(path: str) -> tuple:
    """Verdict (première ligne) + constats du rapport du contrôleur qualité.
    Renvoie (verdict|None, constats) — None si le format n'est pas respecté."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return None, ""
    lines = content.splitlines()
    if not lines:
        return None, ""
    first = lines[0].strip()
    issues = "\n".join(lines[1:]).strip()
    if first == f"{VERDICT_LINE_PREFIX} {VERDICT_KO}":
        return VERDICT_KO, issues
    if first == f"{VERDICT_LINE_PREFIX} {VERDICT_OK}":
        return VERDICT_OK, issues
    return None, issues


# ─── PASSES D'AGENT (GÉNÉRATION, RÉPARATION, REVUE) ───────────────────────────

_RUNNER_STARTED = {"done": False}


def ensure_runner_started():
    """Boot paresseux du TUI : une reprise où toutes les propositions existent
    déjà ne paie pas même un démarrage d'agent."""
    if not _RUNNER_STARTED["done"]:
        RUNNER.start()
        _RUNNER_STARTED["done"] = True


def route_grid(grid_path: str, tmp_path: str):
    """Copie une grille pipeline vers son fichier de routage (prompt déporté)."""
    if not os.path.exists(grid_path):
        fail_pipeline(f"❌ Grille pipeline manquante : '{grid_path}' (projet à rééquiper depuis l'app).")
    with open(grid_path, "r", encoding="utf-8") as f:
        grid = f.read()
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(grid)


def generate_proposal(name: str, profile: dict, proposal: str):
    """Passe de GÉNÉRATION : réécriture du skill pour la stack cible, dans un
    contexte neuf, guidée par la grille skill-adapt."""
    print(f"\n🧬 [GÉNÉRATION] Adaptation du skill '{name}' pour : {stack_for(name, profile)}")
    ensure_runner_started()
    RUNNER.new_context()
    route_grid(ADAPT_SKILL_FILE, TMP_ADAPT_FILE)
    skill_path = f"{SKILLS_DIR}/{name}/SKILL.md"

    prompt = f"""Lis les consignes d'adaptation du fichier '{TMP_ADAPT_FILE}', puis le skill actuel '{skill_path}'.
Tu es un Adaptateur de Skills. En appliquant SCRUPULEUSEMENT les consignes de '{TMP_ADAPT_FILE}', réécris ce skill ({SKILL_DOMAINS[name]}) pour la stack cible du PROFIL ci-dessous, et sauvegarde le résultat DIRECTEMENT dans un nouveau fichier '{proposal}' à la racine du projet. Le skill d'origine '{skill_path}' reste INTACT.

PROFIL D'ADAPTATION :
{profile_summary(profile)}

Rappels NON NÉGOCIABLES :
- Conserve 'name: {name}' tel quel dans le frontmatter ; réécris 'description' en nommant la stack cible.
- Des ORDRES à l'impératif, jamais des descriptions.
- Tableau ❌/✅ (anti-patterns → patterns) d'au moins 6 lignes, spécifique à la stack cible.
- Checklist finale cochable, limite de {profile['line_cap']} lignes au total.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.
En toute DERNIÈRE action, après avoir sauvegardé '{proposal}', crée le fichier sentinelle '{ADAPT_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le signal de fin pour l'orchestrateur.
"""
    cleanup_pipeline_sentinel(ADAPT_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)
    if not wait_for_pipeline_file(proposal, ADAPT_DONE_SENTINEL):
        fail_pipeline(f"❌ [GÉNÉRATION] Timeout ou échec de création de '{proposal}'.")


def repair_proposal(name: str, profile: dict, proposal: str, failures: str):
    """Passe de RÉPARATION : constats (garde-fous Python ou revue qualité)
    renvoyés à l'agent, dans le MÊME contexte (le fichier est la vérité)."""
    print(f"   🔧 [RÉPARATION] Constats renvoyés à l'agent sur '{proposal}'.")
    prompt = f"""Ta proposition '{proposal}' est REJETÉE en l'état. Constats à corriger, un par un :
{failures}

Corrige DIRECTEMENT '{proposal}' via tes outils d'édition (le skill d'origine reste intact), en respectant toujours les consignes de '{TMP_ADAPT_FILE}' et la limite de {profile['line_cap']} lignes au total.
En toute DERNIÈRE action, recrée le fichier sentinelle '{ADAPT_DONE_SENTINEL}' à la racine (contenu : le seul mot done).
"""
    cleanup_pipeline_sentinel(ADAPT_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)
    if not wait_for_pipeline_file(proposal, ADAPT_DONE_SENTINEL):
        fail_pipeline(f"❌ [RÉPARATION] Timeout ou échec de mise à jour de '{proposal}'.")


def review_proposal(name: str, profile: dict, proposal: str) -> tuple:
    """Passe de REVUE : contrôleur qualité indépendant, contexte neuf, verdict
    parsé par Python. Renvoie (verdict|None, constats)."""
    print(f"   🔎 [REVUE QUALITÉ] Audit indépendant de '{proposal}'...")
    ensure_runner_started()
    RUNNER.new_context()
    route_grid(REVIEW_SKILL_FILE, TMP_REVIEW_FILE)

    prompt = f"""Lis la grille de contrôle qualité du fichier '{TMP_REVIEW_FILE}', puis le skill proposé '{proposal}' et le skill d'origine '{SKILLS_DIR}/{name}/SKILL.md'.
Tu es un Contrôleur Qualité de skills, indépendant de l'auteur. En appliquant la grille de '{TMP_REVIEW_FILE}', audite la proposition contre le PROFIL ATTENDU ci-dessous et écris ton rapport DIRECTEMENT dans '{REVIEW_FILE}' à la racine du projet. Lecture seule sur tout le reste.

PROFIL ATTENDU :
{profile_summary(profile)}

Format STRICT du rapport '{REVIEW_FILE}' :
- Première ligne, EXACTEMENT : 'VERDICT : CONFORME' ou 'VERDICT : NON CONFORME'.
- Puis un constat par ligne : '- [BLOQUANT] …' ou '- [MINEUR] …' (sans constat : '- [MINEUR] RAS').
En toute DERNIÈRE action, après avoir sauvegardé '{REVIEW_FILE}', crée le fichier sentinelle '{REVIEW_DONE_SENTINEL}' à la racine (contenu : le seul mot done).
"""
    try:
        os.remove(REVIEW_FILE)
    except OSError:
        pass
    cleanup_pipeline_sentinel(REVIEW_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)
    if not wait_for_pipeline_file(REVIEW_FILE, REVIEW_DONE_SENTINEL):
        fail_pipeline(f"❌ [REVUE QUALITÉ] Timeout ou échec de production de '{REVIEW_FILE}'.")
    return parse_verdict(REVIEW_FILE)


# ─── CHAÎNE QUALITÉ PAR SKILL ─────────────────────────────────────────────────

def build_proposal(name: str, profile: dict, proposal: str):
    """Génération + garde-fous Python + revue qualité, avec réparations bornées.
    À la sortie, la proposition est structurellement valide ; un verdict qualité
    encore NON CONFORME est AFFICHÉ, jamais caché : l'humain tranche à la porte."""
    generate_proposal(name, profile, proposal)

    line_cap = int(profile["line_cap"])
    repairs = 0
    failures = check_proposal(proposal, name, line_cap)
    while failures and repairs < MAX_REPAIRS:
        repairs += 1
        print(f"   ⚠️  Garde-fous Python : {len(failures)} constat(s) (réparation {repairs}/{MAX_REPAIRS}).")
        repair_proposal(name, profile, proposal, "\n".join(f"- {failure}" for failure in failures))
        failures = check_proposal(proposal, name, line_cap)
    if failures:
        details = "\n".join(f"   - {failure}" for failure in failures)
        fail_pipeline(f"❌ '{proposal}' reste invalide après {MAX_REPAIRS} réparation(s) :\n{details}")
    print("   ✓ Garde-fous Python : structure, frontmatter et limite de lignes respectés.")

    verdict, issues = review_proposal(name, profile, proposal)
    if verdict != VERDICT_OK:
        shown = issues if issues else "(rapport sans constats exploitables)"
        print(f"   ⚠️  Revue qualité : {VERDICT_KO if verdict == VERDICT_KO else 'verdict illisible'} — une réparation puis re-revue.\n{shown}")
        repair_proposal(name, profile, proposal,
                        issues if issues else "- le rapport de revue est vide : reprends chaque règle de la grille d'adaptation une à une")
        failures = check_proposal(proposal, name, line_cap)
        if failures:
            details = "\n".join(f"   - {failure}" for failure in failures)
            fail_pipeline(f"❌ La réparation post-revue a cassé la structure de '{proposal}' :\n{details}")
        verdict, issues = review_proposal(name, profile, proposal)
    if verdict == VERDICT_OK:
        print("   ✅ Revue qualité : CONFORME.")
    else:
        print(f"   ⚠️  Revue qualité toujours réservée après réparation — constats affichés, à toi de trancher :\n{issues}")


def confirm_overwrite(name: str, proposal: str) -> bool:
    """Porte humaine par skill : l'aperçu est la proposition à la racine,
    éditable dans l'app ou un autre terminal avant validation."""
    print(f"\n{'=' * 50}")
    print(f"📋 PROPOSITION PRÊTE : relis '{proposal}' (elle remplacera "
          f"'{SKILLS_DIR}/{name}/SKILL.md', ancien contenu sauvegardé en .bak).")
    print(f"   Tu peux l'éditer directement avant de valider : le fichier fait foi.")
    print(f"{'=' * 50}")
    answer = input(f"\n▶️  Écraser le skill '{name}' avec la version adaptée ? (y/n) : ").strip().lower()
    mm_audit.event("gate", id="overwrite", gate_kind="yn", answer=answer)
    return answer == "y"


def apply_proposal(name: str, proposal: str) -> str:
    """Sauvegarde .bak puis écrasement. Le .bak est la piste de retour arrière
    (en plus de git quand le projet en a un)."""
    skill_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    backup_path = skill_path + ".bak"
    with open(skill_path, "r", encoding="utf-8") as f:
        original = f.read()
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(original)
    with open(proposal, "r", encoding="utf-8") as f:
        adapted = f.read()
    with open(skill_path, "w", encoding="utf-8") as f:
        f.write(adapted)
    os.remove(proposal)
    print(f"   ✅ '{skill_path}' écrasé (ancien contenu : '{backup_path}').")
    return backup_path


def write_report(profile: dict, rows: list):
    """Livrable final : ce qui a été adapté, refusé, et où sont les retours arrière."""
    scope_labels = {"1": "backend + frontend", "2": "backend seul", "3": "frontend seul"}
    lines = ["# Rapport d'adaptation des skills — MAIsterMind", "",
             "## Profil appliqué",
             f"- Périmètre : {scope_labels[profile['scope']]} "
             f"(testing inclus : {profile['include_testing']})",
             f"- Stack backend : {profile['backend_stack']} · Stack frontend : {profile['frontend_stack']}",
             f"- Conventions : {profile['conventions']}",
             f"- Limite : {profile['line_cap']} lignes · Modèle cible : {profile['model_target']}",
             "", "## Skills traités", ""]
    lines.extend(rows)
    lines.extend(["", "Retour arrière : restaure le '.bak' correspondant (ou 'git checkout' du skill).",
                  f"Profil réutilisable : '{PROFILE_FILE}' (relance l'orchestrateur et réponds y)."])
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n🧾 Rapport écrit dans '{REPORT_FILE}'.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "skill-adapt", RUNNER.name,
                   model=RUNNER.configured_model())
    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour
    # celui du run courant : purge au démarrage.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # ── Profil : réutilisé sur accord explicite, sinon questionnaire. ──
    profile = read_profile()
    if profile:
        print(f"🔄 Profil d'adaptation existant trouvé ('{PROFILE_FILE}') :")
        for line in profile_summary(profile).splitlines():
            print(f"   {line}")
        answer = input("\n▶️  Réutiliser le profil d'adaptation existant ? (y/n) : ").strip().lower()
        mm_audit.event("gate", id="reuse-profile", gate_kind="yn", answer=answer)
        if answer != "y":
            profile = {}
    if not profile:
        profile = run_questionnaire()
        write_profile(profile)
        print(f"   ✓ Profil sauvegardé dans '{PROFILE_FILE}' (éditable à la main).")

    # ── Périmètre : chaque skill visé doit exister (projet équipé). ──
    targets = target_skills(profile)
    missing = [name for name in targets
               if not os.path.exists(os.path.join(SKILLS_DIR, name, "SKILL.md"))]
    if missing:
        print(f"❌ Skill(s) absent(s) du projet : {', '.join(missing)}.")
        write_fail_report("Projet non équipé pour ce périmètre",
                          f"Skills manquants sous '{SKILLS_DIR}' : {', '.join(missing)}. "
                          "Équipe le projet depuis l'app (ou réduis le périmètre), puis relance.")
        sys.exit(1)
    print(f"\n🎯 Périmètre : {len(targets)} skill(s) → {', '.join(targets)}")

    # ── Chaîne qualité + porte humaine, skill par skill. ──
    rows = []
    for name in targets:
        proposal = f"{PROPOSAL_PREFIX}{name}.md"
        if os.path.exists(proposal) and os.path.getsize(proposal) > 0:
            # Reprise par fichiers : la proposition est l'état ; on re-arbitre
            # sans re-payer génération ni revue (édite-la si besoin, elle fait foi).
            print(f"\n🔄 Proposition existante trouvée pour '{name}' ('{proposal}') : reprise sans re-génération.")
        else:
            build_proposal(name, profile, proposal)
        if confirm_overwrite(name, proposal):
            backup_path = apply_proposal(name, proposal)
            rows.append(f"- **{name}** : ÉCRASÉ (stack : {stack_for(name, profile)} ; retour arrière : '{backup_path}').")
        else:
            rows.append(f"- **{name}** : REFUSÉ — proposition conservée dans '{proposal}', l'original est intact.")
            print(f"   ⏭️  '{name}' laissé intact ; '{proposal}' conservé pour inspection.")

    write_report(profile, rows)

    # Fermeture propre : fichiers de routage, tampon tmux, sentinelles tardives
    # et rapport de revue transitoire sont purgés ; profil et .bak SURVIVENT.
    for tmp_f in [TMP_ADAPT_FILE, TMP_REVIEW_FILE, TMP_PROMPT_BUFFER,
                  ADAPT_DONE_SENTINEL, REVIEW_DONE_SENTINEL, REVIEW_FILE]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    RUNNER.kill()

    applied = sum(1 for row in rows if "ÉCRASÉ" in row)
    print(f"\n🏁 Adaptation terminée : {applied}/{len(targets)} skill(s) écrasé(s). "
          f"Les prochains runs de production utiliseront ces skills adaptés tels quels.")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
