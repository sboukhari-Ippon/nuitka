#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Pipeline PARTIEL « challenge du besoin » (harness d'agent + tmux)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « CHALLENGE-NEED » (AIDD-7) : confronte 'need.md' à ses propres ambiguïtés
AVANT de payer une spec. Un agent au contexte neuf produit 'need_review.md' —
ambiguïtés, contradictions, zones d'ombre, présupposés, questions à trancher — que
l'humain entérine (y/n) puis exploite : il met à jour 'need.md' LUI-MÊME et relance
le pipeline de son choix.

Pourquoi un point d'entrée dédié :
  - La porte la plus en AMONT est la moins chère de toutes : un besoin flou coûte
    une spec, un plan et des phases ; une question tranchée ici ne coûte rien.
  - AUCUN couplage aval (v1) : aucun orchestrateur ne lit 'need_review.md' ni
    n'exige '.need_reviewed'. Ce script est opt-in, les 15 pipelines existants ne
    changent pas d'une ligne.

Contrats repris tels quels de l'existant (gabarit : Spec.py) :
  - reprise par fichiers : 'need_review.md' présent SANS '.need_reviewed' → la porte
    est re-présentée sans re-payer l'agent ; les deux présents → déjà fait, sortie 0 ;
    supprimer 'need_review.md' force la régénération ;
  - verdict : les contrôles Python peuvent rejeter (3 tentatives max, feedback = liste
    exacte des manquements), le LLM ne valide jamais — et toute CITATION du besoin
    doit exister dans 'need.md' (même garde anti-source-inventée que Documentation.py) ;
  - journal de run (mm_audit) posé dès l'écriture.
"""

import os
import re
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
# Toute la couche tmux vit dans 'mm_runner.py'. Préfixe de session DISTINCT (rôle
# 'challenge') : ce script ne peut pas injecter de prompt dans un autre run du projet.
RUNNER = resolve_runner(os.getcwd(), role="challenge")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
REVIEW_FILE           = "need_review.md"
FAIL_REPORT_FILE      = "failReport.md"   # rapport d'arrêt persistant (même contrat que l'usine)
CHALLENGE_SKILL_FILE  = "./.agents/pipeline/challenge-need/SKILL.md"

AGENT_CONFIG_FILE     = RUNNER.config_file.removeprefix("./")

# Fichier temporaire de routage de contexte (prompt déporté)
TMP_CHALLENGE_FILE    = RUNNER.tmp_file("challenge")

# Fichier tampon du prompt envoyé au TUI via tmux.
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Sentinelle de fin du livrable (l'agent crée le .done APRÈS avoir sauvegardé la revue).
REVIEW_DONE_SENTINEL     = ".pipeline_challenge.done"

# Approbation HUMAINE de la revue, matérialisée : l'EXISTENCE de need_review.md ne
# prouve rien (un timeout peut laisser une revue jamais entérinée).
REVIEW_APPROVED_SENTINEL = ".need_reviewed"

TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max pour l'étape (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes)

# Sections OBLIGATOIRES de la revue (le format verrouillé de la grille) : chacune doit
# être présente ET non vide (« Aucune. » est un contenu valide — l'absence de problème
# est un résultat, l'absence de section est un livrable à moitié écrit).
REVIEW_SECTIONS = ["## Ambiguïtés", "## Contradictions", "## Zones d'ombre",
                   "## Présupposés", "## Questions à trancher avant la spec"]

# Une citation du besoin est un passage entre guillemets doubles ; en dessous de ce
# seuil, on ne vérifie pas (mots isolés, faux positifs). Garde anti-source-inventée :
# chaque citation assez longue DOIT exister dans need.md (espaces normalisés).
MIN_QUOTE_CHARS = 12
QUOTE_RE = re.compile(r'"([^"\n]{%d,})"' % MIN_QUOTE_CHARS)


def fail_pipeline(message: str):
    """Point de sortie unique des échecs d'étape.

    Tue toujours la session tmux AVANT de quitter : un exit qui laisse l'agent vivant
    le laisse finir d'écrire son livrable APRÈS que l'orchestrateur a abandonné — au
    relancement, ce fichier à moitié validé serait pris pour un état de reprise valide.
    """
    print(message)
    write_fail_report("Échec de l'étape de challenge du besoin", message)
    RUNNER.kill()
    sys.exit(1)


def write_fail_report(title: str, reason: str):
    """Écrit un rapport d'arrêt persistant à la racine (même contrat que l'usine :
    tout arrêt NON nominal en produit un). Best-effort : ne lève JAMAIS."""
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind (challenge du besoin)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "",
                 "### Action recommandée",
                 "Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model dans le "
                 f"TUI ou '{AGENT_CONFIG_FILE}'), puis relance."]
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


def review_structural_check(path: str) -> bool:
    """Plancher structurel LÉGER d'une revue acceptée sans sentinelle : ses sections
    obligatoires doivent être présentes (une revue à moitié écrite s'arrête avant).
    Le contrôle FORT (sections non vides, citations réelles) est validate_review."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return all(section.lower() in content for section in REVIEW_SECTIONS)
    except OSError:
        return False


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Attend un livrable du pipeline signalé par SENTINELLE.

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


# ─── CONTRÔLES STRUCTURELS FORTS (PYTHON : LE LLM NE VALIDE JAMAIS) ───────────

def normalize_ws(text: str) -> str:
    """Espaces normalisés (la comparaison de citations tolère les retours à la ligne)."""
    return " ".join(str(text).split()).lower()


def validate_review(path: str, need_text: str) -> list:
    """Contrôle FORT de la revue : sections obligatoires présentes et NON VIDES, et
    chaque citation du besoin (passage entre guillemets doubles assez long) doit
    EXISTER dans need.md — une citation inventée est un rejet avec l'écart exact,
    formulé pour être renvoyé TEL QUEL à l'agent (même philosophie que la garde
    anti-source-inventée de Documentation.py). Renvoie la liste des manquements."""
    issues = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return [f"'{path}' est illisible."]

    # 1. Sections obligatoires, dans l'ordre du format verrouillé, non vides.
    positions = []
    low = content.lower()
    for section in REVIEW_SECTIONS:
        idx = low.find(section.lower())
        if idx < 0:
            issues.append(f"Section obligatoire manquante : '{section}'.")
        positions.append(idx)
    if not issues:
        bounds = positions + [len(content)]
        for i, section in enumerate(REVIEW_SECTIONS):
            body = content[bounds[i] + len(section):bounds[i + 1]].strip()
            if not body:
                issues.append(f"Section '{section}' vide : écris son contenu, ou la seule "
                              f"ligne « Aucune. » si tu n'as rien relevé.")

    # 2. Garde anti-citation-inventée : tout passage cité doit exister dans le besoin.
    need_norm = normalize_ws(need_text)
    for quote in QUOTE_RE.findall(content):
        if normalize_ws(quote) not in need_norm:
            issues.append(f"La citation \"{quote[:80]}\" n'existe pas dans '{NEED_FILE}' : "
                          f"cite le besoin MOT POUR MOT (copie exacte), ou reformule sans "
                          f"guillemets si c'est ton interprétation.")
    return issues


# ─── ÉTAPE UNIQUE : AGENT CHALLENGEUR DANS LE TUI ─────────────────────────────

def build_challenge_prompt(feedback: str) -> str:
    with open(CHALLENGE_SKILL_FILE, "r", encoding="utf-8") as f:
        grid = f.read()
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Challenger de besoin : tu confrontes un besoin brut à ses ambiguïtés AVANT
qu'une spécification soit payée. Tu ne modifies AUCUN fichier du projet : tu n'écris
QUE ta revue '{REVIEW_FILE}', puis ta sentinelle de fin.

--- GRILLE (consignes et format de sortie VERROUILLÉ) ---
{grid}

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- LIVRABLE OBLIGATOIRE ---
Lis '{NEED_FILE}' à la racine, puis écris ta revue dans '{REVIEW_FILE}' (racine du
projet) en respectant STRICTEMENT le format de la grille.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{REVIEW_FILE}', crée le fichier
sentinelle '{REVIEW_DONE_SENTINEL}' à la racine (contenu : le seul mot done) : c'est le
signal de fin pour l'orchestrateur.
"""
    with open(TMP_CHALLENGE_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)
    return (f"Lis le fichier de consignes '{TMP_CHALLENGE_FILE}' à la racine du projet et "
            f"réalise la revue de besoin demandée.")


def generate_review_tui(need_text: str):
    """Boucle de production de la revue : 3 tentatives max, les manquements EXACTS des
    contrôles Python deviennent le feedback de la tentative suivante."""
    print("\n🔍 [ÉTAPE UNIQUE : CHALLENGER] Revue critique du besoin dans le TUI Cloud...")

    if not os.path.exists(CHALLENGE_SKILL_FILE):
        fail_pipeline(f"❌ Grille du challenge manquante : '{CHALLENGE_SKILL_FILE}'")

    feedback = "Premier passage — aucun retour précédent."
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cleanup_pipeline_sentinel(REVIEW_DONE_SENTINEL)
        print(f"\n🚀 [TENTATIVE {attempt}/{MAX_ATTEMPTS}] Lancement du Challenger de besoin...")
        prompt = build_challenge_prompt(feedback)
        mm_audit.event("agent_task", prompt_bytes=len(prompt), attempt=attempt)
        RUNNER.send_task(prompt)

        if not wait_for_pipeline_file(REVIEW_FILE, REVIEW_DONE_SENTINEL,
                                      structural_check=review_structural_check):
            feedback = (f"Au passage précédent, aucun livrable n'a été reçu ('{REVIEW_FILE}' "
                        f"absent, vide ou jamais signalé). Écris d'abord la revue complète, "
                        f"PUIS la sentinelle, dans cet ordre.")
            print(f"⏱️  Le challenger n'a pas signalé la fin de sa passe. Nouvelle tentative.")
            RUNNER.new_context()
            continue

        issues = validate_review(REVIEW_FILE, need_text)
        if not issues:
            print(f"✅ Revue '{REVIEW_FILE}' produite et conforme aux contrôles mécaniques.")
            return
        feedback = ("Ta revue ne passe pas les contrôles mécaniques :\n"
                    + "\n".join(f"- {issue}" for issue in issues)
                    + "\nRéécris le fichier entièrement au format de la grille.")
        try:
            os.remove(REVIEW_FILE)
        except OSError:
            pass
        print(f"⚠️  [REJET] Tentative {attempt} : revue hors contrat "
              f"({len(issues)} manquement(s) : {' ; '.join(issues[:2])}…).")
        RUNNER.new_context()

    fail_pipeline(f"❌ Revue du besoin non aboutie après {MAX_ATTEMPTS} tentatives.")


def confirm_review_with_human():
    """Validation humaine de la revue : l'entériner ne modifie RIEN au projet — c'est
    un intrant humain. L'utilisateur lit, met à jour 'need.md' lui-même, et relance
    le pipeline de son choix."""
    print(f"\n{'='*50}")
    print(f"🔍 REVUE DU BESOIN PRÊTE : relis '{REVIEW_FILE}' (questions [BLOQUANT] en priorité).")
    print(f"   Elle ne modifie rien : mets à jour '{NEED_FILE}' TOI-MÊME, puis relance le "
          f"pipeline de ton choix.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Entériner cette revue du besoin ? (y/n) : ")
    mm_audit.event("gate", id="review", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Arrêt propre. '{REVIEW_FILE}' est conservé : supprime-le pour rejouer la "
              f"revue, ou relance pour re-présenter cette porte.")
        RUNNER.kill()
        sys.exit(0)
    # L'approbation est MATÉRIALISÉE : à la reprise, une revue sans cette sentinelle
    # repasse par le y/n au lieu d'être tenue pour entérinée.
    with open(REVIEW_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("reviewed\n")
    mm_audit.snapshot(REVIEW_FILE)   # copie figée de la revue TELLE QU'ENTÉRINÉE


def print_handover():
    """Rappelle comment enchaîner : la revue est un intrant humain, pas un état."""
    print(f"""
{'─'*50}
➡️  Exploite la revue : tranche les questions [BLOQUANT], mets à jour '{NEED_FILE}',
   puis relance le pipeline de ton choix (Spec.py, Technical-Plan.py, Safe-Coding.py…).
   AUCUN pipeline ne lit '{REVIEW_FILE}' ni n'exige '.need_reviewed' : zéro couplage,
   zéro ralentissement — cette revue ne vaut que par ce que TU en retires.
{'─'*50}""")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "challenge-need", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # Une sentinelle d'approbation orpheline (revue supprimée depuis) ne doit jamais
    # entériner une revue FUTURE : on la purge avant toute chose.
    if os.path.exists(REVIEW_APPROVED_SENTINEL) and not os.path.exists(REVIEW_FILE):
        os.remove(REVIEW_APPROVED_SENTINEL)

    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du
    # run courant : on le purge au démarrage.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # Étape déjà TERMINÉE (revue présente ET entérinée) : rien à faire, pas même un boot
    # de TUI — ce script est idempotent, comme les autres pipelines partiels.
    if os.path.exists(REVIEW_FILE) and os.path.exists(REVIEW_APPROVED_SENTINEL):
        print(f"✓ '{REVIEW_FILE}' existe déjà et a été entérinée : rien à faire.")
        print(f"   → Pour rejouer la revue depuis '{NEED_FILE}' : supprime '{REVIEW_FILE}' puis relance.")
        print_handover()
        return

    # Reprise par fichiers : une revue jamais entérinée (run interrompu pendant le y/n)
    # re-présente la porte SANS re-payer l'agent.
    if not os.path.exists(REVIEW_FILE):
        with open(NEED_FILE, "r", encoding="utf-8") as f:
            need_text = f.read()
        RUNNER.start()
        generate_review_tui(need_text)
        confirm_review_with_human()
    else:
        print(f"🔄 '{REVIEW_FILE}' existante trouvée mais JAMAIS entérinée (run interrompu ?).")
        confirm_review_with_human()

    # Fermeture propre : '.need_reviewed' SURVIT volontairement (trace de l'arbitrage) ;
    # les fichiers temporaires et une éventuelle sentinelle .done tardive sont purgés.
    for tmp_f in [TMP_CHALLENGE_FILE, TMP_PROMPT_BUFFER, REVIEW_DONE_SENTINEL]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    RUNNER.kill()

    print(f"\n🏁 Revue du besoin '{REVIEW_FILE}' entérinée.")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")
    print_handover()


mm_core.configure(
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
