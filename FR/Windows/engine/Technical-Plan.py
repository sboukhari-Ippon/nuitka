#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Pipeline PARTIEL « du besoin au blackboard » (harness d'agent + tmux)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « PLAN TECHNIQUE SEUL » : exécute les étapes 1 à 3 du pipeline MAIsterMind —
Agent PO ('need.md' → 'spec.md' validée par l'humain), Agent Architecte ('spec.md' →
'plan.md'), Compilateur Blackboard ('plan.md' → 'blackboard.yaml') — valide la structure
du blackboard produit, affiche le récapitulatif, puis s'arrête proprement AVANT toute
production. Aucun scaffold, aucune phase de code, aucun refactoring.

Pourquoi un point d'entrée dédié :
  - Les étapes 1 à 3 sont les one-shots à FORT LEVIER du pipeline : c'est là qu'un gros
    modèle rapporte le plus, et là que l'humain arbitre (spec, plan, blackboard). Ce
    script permet de ne payer QUE cette partie « pensée » : préparer le plan technique
    aujourd'hui, lancer la production plus tard (autre moment, autre machine, autre
    modèle — typiquement un petit modèle économique).
  - Mêmes CONTRATS DE FICHIERS que la variante complète : 'spec.md' + '.spec_approved',
    'plan.md', 'blackboard.yaml'. Relancer ensuite Safe-Coding.py reprend ces livrables
    TELS QUELS (étapes 1 à 3 sautées) et démarre directement au y/n de production.
    Ce script vise la variante « verdict universel » (Safe-Coding.py) : mêmes skills de
    pipeline ('plan', 'plan-to-blackboard'), donc mêmes champs (nature, covers,
    verify_cmd…) dans le blackboard produit.

Le découpage de la fenêtre de contexte par étape reste le principe directeur : chaque
agent (PO, Architecte, Compilateur) tourne dans une session neuve (/new) et ne reçoit
QUE ses consignes et le livrable amont — jamais l'historique des étapes précédentes.
"""

import os
import re
import sys
import time
import signal
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
    collect_spec_us_ids, load_blackboard, signal_handler,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="techplan", messages={
    "follow": "   👀 Suis le run en direct dans un autre terminal : tmux attach -t {session}",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
FAIL_REPORT_FILE      = "failReport.md"   # rapport d'arrêt persistant (même contrat que l'usine)
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"

# Config du harness actif, telle que les messages de CE script l'ont toujours citée :
# sans le './' de tête ('.opencode/opencode.json', '.codex/config.toml'). Le préfixe est
# retiré ici et pas dans le runner : les autres orchestrateurs, eux, citent la forme
# './…' — la migration ne réécrit aucun message existant.
AGENT_CONFIG_FILE     = RUNNER.config_file.removeprefix("./")
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")

# Skills système du pipeline : jamais routés vers les phases de production.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-to-blackboard", "refacto"}

# Fichiers temporaires de routage de contexte
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : c'est le
# seul choix valable sur les 3 OS (Windows n'a pas de /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Sentinelles de fin des livrables du pipeline (étapes 1 à 3) : même contrat que la
# production (l'agent crée le .done APRÈS avoir sauvegardé le livrable).
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"
PLAN_DONE_SENTINEL       = ".pipeline_plan.done"
BLACKBOARD_DONE_SENTINEL = ".pipeline_blackboard.done"

# Approbation HUMAINE de la spec, matérialisée : la simple EXISTENCE de spec.md ne prouve
# rien. Cette sentinelle SURVIT à la fin de ce run : c'est elle que Safe-Coding.py lira
# pour sauter son étape 1 lors du lancement de la production.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Nom de la session tmux, suffixé d'une empreinte du répertoire du projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session. Préfixe DISTINCT
# des variantes complètes : ce script ne peut pas injecter de prompt dans un run de
# production qui tournerait sur le même projet.
TMUX_SESSION          = RUNNER.session

POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par étape (filet de sécurité)
VERIFY_FEEDBACK_LIMIT = 4000           # taille max des extraits repris dans le rapport d'échec
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable pipeline accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes)


def fail_pipeline(message: str):
    """Point de sortie unique des échecs d'étape du pipeline (étapes 1 à 3).

    Tue toujours la session tmux AVANT de quitter : un exit qui laisse l'agent vivant
    le laisse finir d'écrire son livrable APRÈS que l'orchestrateur a abandonné — au
    relancement, ce fichier à moitié validé serait pris pour un état de reprise valide.
    """
    print(message)
    write_fail_report("Échec d'une étape du pipeline", message)
    RUNNER.kill()
    sys.exit(1)


# ─── RAPPORT D'ÉCHEC ──────────────────────────────────────────────────────────

def truncate_output(text: str, limit: int = VERIFY_FEEDBACK_LIMIT) -> str:
    """Tronque un texte long en conservant le DÉBUT ET la FIN (la cause racine d'une
    erreur apparaît généralement au début, le résumé à la fin)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return (text[:head]
            + f"\n[... sortie tronquée ({len(text)} caractères au total) ...]\n"
            + text[-tail:])


def write_fail_report(title: str, reason: str, details: str = ""):
    """Écrit un rapport d'arrêt persistant à la racine (même contrat que l'usine :
    tout arrêt NON nominal en produit un). Best-effort : ne lève JAMAIS."""
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind (plan technique)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "", "### Avancement"]
        for label, path in ((f"Spécification ('{SPEC_FILE}')", SPEC_FILE),
                            (f"Plan ('{PLAN_FILE}')", PLAN_FILE),
                            (f"Blackboard ('{BLACKBOARD_FILE}')", BLACKBOARD_FILE)):
            mark = "✅" if os.path.exists(path) and os.path.getsize(path) > 0 else "⏳"
            lines.append(f"  - {mark} {label}")
        lines.append("")
        if details.strip():
            lines.append("### Détails")
            lines.append(truncate_output(details))
            lines.append("")
        lines.append("### Action recommandée")
        lines.append("Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model dans le "
                     f"TUI ou '{AGENT_CONFIG_FILE}'), puis relance : les livrables déjà produits "
                     "seront repris automatiquement.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Rapport d'échec écrit dans '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def cleanup_pipeline_sentinel(sentinel: str):
    """Supprime une sentinelle de pipeline résiduelle (run précédent interrompu)."""
    try:
        os.remove(sentinel)
    except OSError:
        pass


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
    le livrable. FILET pour un agent qui oublie la sentinelle : si le livrable existe, est
    non vide et n'a plus bougé depuis STABLE_POLLS_FALLBACK contrôles consécutifs, on
    l'accepte avec avertissement (dégradation gracieuse). Le 'structural_check' optionnel
    ne durcit QUE ce filet : un livrable stable mais structurellement incomplet continue
    d'attendre (l'agent peut marquer une pause plus longue que la fenêtre de stabilité)
    jusqu'au timeout global.
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


# ─── LECTURE BLACKBOARD & FICHIER DE BESOINS ──────────────────────────────────

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


# ─── VALIDATION DU SCHÉMA DU BLACKBOARD (PRODUIT PAR UN PETIT LLM FAILLIBLE) ───
# Copie conforme de la validation de Safe-Coding.py : le blackboard produit ici est
# DESTINÉ à sa production — le valider maintenant, c'est éviter de découvrir au
# lancement de la production (plus tard, ailleurs) qu'il est inutilisable.

REQUIRED_GLOBAL_RULES = ["target", "styling", "constraints", "accessibility"]


def validate_blackboard_schema(blackboard: dict) -> tuple:
    """Contrôle la structure du blackboard. Renvoie (fatal, soft).

    fatal : manques STRUCTURANTS sur lesquels la production planterait ou tournerait à
    vide. soft : manques rattrapés par apply_blackboard_defaults ou purement cosmétiques.
    N'écrit rien et ne corrige rien : c'est l'humain qui décide.
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
                "prompt codeur utilisera la formulation neutre au lieu de celle pilotée par le plan."
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
        # → le contrôle que les tests MORDENT sera inactif en production. Toléré.
        has_test_phase = any(isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower() == "tests"
                             for phase in phases)
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if has_test_phase and not has_mutation_cmd:
            soft.append(
                "Aucune 'mutation_cmd' déclarée alors que des phases 'tests' existent : la brique B "
                "(contrôle que les tests MORDENT) sera inactive en production. Toléré ; déclare-la "
                "dans le plan pour des tests falsifiables."
            )
    if not (blackboard.get("verify_cmd") or "").strip():
        fatal.append(
            "Commande de vérification globale 'verify_cmd' manquante : c'est le fallback des "
            "phases sans 'verify_cmd' propre ET le verrou de l'étape de scaffold. Sans elle, la "
            "production ne pourra pas vérifier ses phases."
        )
    return fatal, soft


def apply_blackboard_defaults(blackboard: dict):
    """Comble EN MÉMOIRE les champs non critiques absents, pour l'affichage du récapitulatif
    uniquement : ce script n'écrit JAMAIS le blackboard (le fichier reste tel que produit
    par le compilateur ; la variante de production appliquera ses propres défauts)."""
    if not isinstance(blackboard, dict):
        return
    global_rules = blackboard.setdefault("global_rules", {})
    if isinstance(global_rules, dict):
        for key in REQUIRED_GLOBAL_RULES:
            global_rules.setdefault(key, "(non spécifié)")
    for phase in blackboard.get("phases", []) or []:
        if isinstance(phase, dict):
            phase.setdefault("skills_required", [])
            phase.setdefault("tasks", [])
            phase.setdefault("covers", [])


# ─── TRAÇABILITÉ SPEC → PHASES ('covers') ─────────────────────────────────────

# En-tête d'une user story dans la spec PO (ex. « ### US-1 : Calcul du solde »).
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def check_spec_coverage(blackboard: dict, spec_text: str) -> list:
    """AVERTISSEMENTS (non bloquants) de traçabilité spec → phases via 'covers'.

    Deux directions : une phase référence une US absente de la spec (hallucination
    probable du compilateur) ; une US de la spec n'est couverte par aucune phase
    (exigence potentiellement OUBLIÉE par l'Architecte — l'avertissement le plus précieux).
    Warn-only : c'est l'œil humain qui tranche.
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


def build_skills_dictionary() -> str:
    """Construit dynamiquement le catalogue des skills affectables aux phases.

    Scanne ./.agents/skills, lit le frontmatter (name + description) de chaque SKILL.md
    et exclut les skills système du pipeline. Le résultat est injecté dans les consignes
    de plan de l'ARCHITECTE (étape 2) : l'architecte déclare le Skill de chaque phase, et
    le compilateur blackboard ne fait ensuite que RECOPIER cette décision.
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


def inject_skills_dictionary(text: str) -> str:
    """Substitue le catalogue RÉEL des skills dans les consignes d'un skill du pipeline."""
    skills_dictionary = build_skills_dictionary()
    if "{{SKILLS_DICTIONARY}}" in text:
        return text.replace("{{SKILLS_DICTIONARY}}", skills_dictionary)
    return text + f"\n\nDICTIONNAIRE DES COMPÉTENCES AUTORISÉES :\n{skills_dictionary}\n"


def validate_all_skills(blackboard: dict):
    referenced = set()
    for phase in blackboard.get("phases", []) or []:
        if isinstance(phase, dict):
            for skill in phase.get("skills_required", []) or []:
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
        print(f"   → Corrige 'blackboard.yaml' avant de lancer la production.\n")
    else:
        print(f"✅ Tous les skills référencés existent ({len(referenced)} référencé(s)).\n")


# ─── ETAPES 1 À 3 DANS LE TUI (CLOUD) ─────────────────────────────────────────

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
    # L'approbation est MATÉRIALISÉE (pas déduite de l'existence du fichier) et SURVIT à
    # ce run : c'est elle que la variante de production lira pour sauter son étape 1.
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
    # dictionnaire des skills va à l'Architecte (étape 2), pas ici. Le filet Python
    # validate_all_skills attrape toujours les mots-clés hallucinés en aval.
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


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "tech-plan", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # Une sentinelle d'approbation orpheline (spec.md supprimée depuis) ne doit jamais
    # valider une spec FUTURE : on la purge avant toute chose.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du
    # run courant : on le purge au démarrage.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # 🚀 ÉTAPE ZÉRO : Boot immédiat du harness Data Center dans Tmux
    RUNNER.start()

    # Étape 1 : Affinage PO via le TUI (need.md → spec.md), validé par l'HUMAIN.
    # Trois états de reprise, comme dans la variante complète : pas de spec → génération
    # + confirmation ; spec SANS sentinelle d'approbation (run interrompu) → revalidation
    # humaine ; spec + sentinelle → étape passée.
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
    else:
        print(f"🔄 '{BLACKBOARD_FILE}' existant trouvé. Chargement...")
        try:
            blackboard = load_blackboard()
        except Exception as err:
            print(f"❌ '{BLACKBOARD_FILE}' présent mais illisible (YAML invalide ou corrompu) : {err}")
            print(f"   → Corrige ou supprime '{BLACKBOARD_FILE}', puis relance "
                  f"(il sera régénéré depuis '{PLAN_FILE}').")
            write_fail_report(
                "Blackboard illisible",
                f"'{BLACKBOARD_FILE}' est présent mais illisible (YAML invalide ou corrompu) : {err}. "
                f"Corrige ou supprime ce fichier puis relance.")
            RUNNER.kill()
            sys.exit(1)

    # La suite est de la PURE VALIDATION LOCALE (aucun agent) : la session peut être fermée
    # dès maintenant, l'humain lit le récapitulatif tranquillement.
    RUNNER.kill()

    # Contexte « besoin » pour la traçabilité : la spec validée (source de vérité).
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Garde-fou : le blackboard est produit par un petit LLM faillible. On le valide
    # MAINTENANT — découvrir au lancement de la production (plus tard, ailleurs) qu'il
    # est structurellement inutilisable ferait perdre tout l'intérêt de ce pipeline
    # partiel. Ce script ne CORRIGE rien et n'écrit jamais le blackboard : il signale.
    fatal, soft = validate_blackboard_schema(blackboard)
    if soft:
        print("\nℹ️  Champs non critiques absents (comblés automatiquement en production) :")
        for problem in soft:
            print(f"   - {problem}")
    if fatal:
        print("\n❌ Le blackboard présente des anomalies STRUCTURANTES :")
        for problem in fatal:
            print(f"   - {problem}")
        print(f"   → Corrige '{BLACKBOARD_FILE}' (ou édite '{PLAN_FILE}', supprime "
              f"'{BLACKBOARD_FILE}' et relance ce script) AVANT de lancer la production.")
        write_fail_report(
            "Blackboard structurellement invalide",
            "Le blackboard produit présente des anomalies STRUCTURANTES qui feraient échouer ou "
            "fausser la production.",
            details="\n".join(f"- {p}" for p in fatal))
        sys.exit(1)
    apply_blackboard_defaults(blackboard)

    # Avertissements NON bloquants de traçabilité spec → phases ('covers').
    if need_is_spec:
        coverage_warnings = check_spec_coverage(blackboard, user_need)
        if coverage_warnings:
            print("\n⚠️  Traçabilité spec → phases :")
            for warning in coverage_warnings:
                print(f"   - {warning}")

    validate_all_skills(blackboard)

    print(f"{'='*50}")
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

    # Fermeture propre : les LIVRABLES ('spec.md' + '.spec_approved', 'plan.md',
    # 'blackboard.yaml') survivent — c'est tout l'objet de ce script ; les fichiers
    # temporaires et les éventuelles sentinelles .done écrites tardivement (livrable
    # accepté par le filet de stabilité) sont purgés.
    for tmp_f in [TMP_PO_FILE, TMP_PLAN_FILE, TMP_ARCHITECT_FILE, TMP_PROMPT_BUFFER,
                  SPEC_DONE_SENTINEL, PLAN_DONE_SENTINEL, BLACKBOARD_DONE_SENTINEL]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)

    print(f"""
🏁 Pipeline technique terminé : du besoin au blackboard, sans production.
   📦 Livrables : '{SPEC_FILE}' (approuvée), '{PLAN_FILE}', '{BLACKBOARD_FILE}'
   ➡️  Pour lancer la production : python3 Safe-Coding.py
      Reprise par fichiers : spec, plan et blackboard sont repris TELS QUELS, la
      production démarre directement après ton y/n blackboard. C'est le bon moment pour
      basculer sur un modèle plus économique (/model ou '{AGENT_CONFIG_FILE}') :
      gros modèle pour penser (ce run), petit modèle pour produire (le suivant).
   ♻️  Pour retoucher avant production : petite retouche → édite '{BLACKBOARD_FILE}' ;
      refonte du découpage → édite '{PLAN_FILE}', supprime '{BLACKBOARD_FILE}', relance
      ce script (spec et plan existants seront repris tels quels).""")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    RUNNER=RUNNER,
    US_HEADING_RE=US_HEADING_RE,
)


if __name__ == "__main__":
    main()
