#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Pipeline PARTIEL « du besoin à la spec » (harness d'agent + tmux)
─────────────────────────────────────────────────────────────────────────────
VARIANTE « SPEC SEULE » : exécute UNIQUEMENT l'étape 1 du pipeline MAIsterMind —
l'Agent PO affine 'need.md' en spécification métier 'spec.md' (user stories, critères
d'acceptation testables, hors-périmètre, hypothèses), VALIDÉE par l'humain — puis
s'arrête proprement.

Pourquoi un point d'entrée dédié :
  - C'est la porte humaine LA MOINS CHÈRE de tout le pipeline : corriger une exigence
    mal comprise ici évite de payer (et de refaire) un plan, un blackboard et des phases
    de production. Ce script permet de ne payer QUE cette étape (relecture asynchrone,
    atelier avec le métier, gros modèle réservé à l'affinage…).
  - Mêmes CONTRATS DE FICHIERS que les variantes complètes ('spec.md' + sentinelle
    d'approbation '.spec_approved') : n'importe quel orchestrateur relancé ensuite —
    Technical-Plan.py (jusqu'au blackboard), Safe-Coding.py, Coding-Without-Tests.py
    ou Design-Prototype.py — trouve la spec approuvée et saute l'étape 1 (reprise par
    fichiers, aucune configuration).

Le découpage de la fenêtre de contexte par étape reste le principe directeur : l'Agent PO
tourne dans une session neuve, ne reçoit QUE le besoin et ses consignes, et le run
s'arrête avant d'accumuler quoi que ce soit d'autre.
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
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture,
# kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex), choisie
# ici au démarrage. Le reste du script n'en sait rien — sentinelles, portes, verdicts
# et prompts sont agnostiques. Préfixe de session DISTINCT des variantes complètes
# (rôle 'spec') : ce script ne peut pas injecter de prompt dans un run de production
# qui tournerait sur le même projet.
RUNNER = resolve_runner(os.getcwd(), role="spec")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
FAIL_REPORT_FILE      = "failReport.md"   # rapport d'arrêt persistant (même contrat que l'usine)
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"

# Config du harness actif, telle que les messages de CE script l'ont toujours citée :
# sans le './' de tête ('.opencode/opencode.json', '.codex/config.toml'). Le préfixe est
# retiré ici et pas dans le runner : les autres orchestrateurs, eux, citent la forme
# './…' — la migration ne réécrit aucun message existant.
AGENT_CONFIG_FILE     = RUNNER.config_file.removeprefix("./")

# Fichier temporaire de routage de contexte (prompt déporté)
TMP_PO_FILE           = RUNNER.tmp_file("po")

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : c'est le
# seul choix valable sur les 3 OS (Windows n'a pas de /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Sentinelle de fin du livrable (même contrat que la production : l'agent crée le .done
# APRÈS avoir sauvegardé le livrable — signal sans ambiguïté, robuste aux pauses d'écriture).
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"

# Approbation HUMAINE de la spec, matérialisée : la simple EXISTENCE de spec.md ne prouve
# rien (un timeout peut laisser derrière lui une spec jamais validée). C'est CETTE
# sentinelle que les orchestrateurs complets liront pour sauter leur étape 1.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Nom de la session tmux du harness, suffixé d'une empreinte du répertoire du projet :
# deux usines tournant sur la même machine ne doivent JAMAIS partager une session.
TMUX_SESSION          = RUNNER.session

POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max pour l'étape (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes)


def fail_pipeline(message: str):
    """Point de sortie unique des échecs d'étape.

    Tue toujours la session tmux AVANT de quitter : un exit qui laisse l'agent vivant
    le laisse finir d'écrire son livrable APRÈS que l'orchestrateur a abandonné — au
    relancement, ce fichier à moitié validé serait pris pour un état de reprise valide
    (c'est ainsi qu'une spec jamais approuvée deviendrait la source de vérité).
    """
    print(message)
    write_fail_report("Échec de l'étape de spécification", message)
    RUNNER.kill()
    sys.exit(1)


def write_fail_report(title: str, reason: str):
    """Écrit un rapport d'arrêt persistant à la racine (même contrat que l'usine :
    tout arrêt NON nominal en produit un). Best-effort : ne lève JAMAIS."""
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind (spec seule)", "",
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


def spec_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'une spec acceptée SANS sentinelle : sa section
    obligatoire « Hors périmètre » doit être présente (une spec à moitié écrite s'arrête avant)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "hors périmètre" in f.read().lower()
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


# ─── ETAPE 1 : AGENT PO DANS LE TUI (CLOUD) ───────────────────────────────────

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
    confirm = input("\n▶️  Valider la spécification ? (y/n) : ")
    mm_audit.event("gate", id="spec", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Annulé par l'utilisateur. Précise '{NEED_FILE}', supprime '{SPEC_FILE}', puis relance.")
        RUNNER.kill()
        sys.exit(0)
    # L'approbation est MATÉRIALISÉE (pas déduite de l'existence du fichier) : c'est cette
    # sentinelle que les orchestrateurs complets liront pour sauter leur propre étape 1.
    # Elle doit donc SURVIVRE à la fin de ce run (jamais purgée ici).
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # copie figée de la spec TELLE QU'APPROUVÉE


def print_handover():
    """Rappelle comment enchaîner : la reprise par fichiers fait tout le travail."""
    print(f"""
{'─'*50}
➡️  Étapes suivantes possibles (reprise par fichiers : la spec approuvée est reprise
   telle quelle, l'étape PO ne sera PAS rejouée) :
   - python3 Technical-Plan.py   → s'arrêter au blackboard (plan technique seul)
   - python3 Safe-Coding.py             → dérouler tout le pipeline jusqu'au code
   Astuce : c'est le bon moment pour changer de modèle (/model dans le TUI ou
   '{AGENT_CONFIG_FILE}') — gros modèle pour penser, petit modèle pour produire.
{'─'*50}""")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "spec", RUNNER.name,
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

    # Étape déjà TERMINÉE (spec présente ET approuvée) : rien à faire, pas même un boot
    # de TUI — ce script est idempotent, comme la reprise des variantes complètes.
    if os.path.exists(SPEC_FILE) and os.path.exists(SPEC_APPROVED_SENTINEL):
        print(f"✓ '{SPEC_FILE}' existe déjà et a été approuvée par l'humain : rien à faire.")
        print(f"   → Pour regénérer une spec depuis '{NEED_FILE}' : supprime '{SPEC_FILE}' puis relance.")
        print_handover()
        return

    # Trois états de reprise, comme dans les variantes complètes : pas de spec →
    # génération + confirmation ; spec SANS la sentinelle d'approbation (run interrompu :
    # timeout, Ctrl-C pendant le y/n) → on redemande à l'humain au lieu de croire un
    # fichier peut-être jamais validé.
    if not os.path.exists(SPEC_FILE):
        RUNNER.start()
        generate_spec_from_need_tui()
        confirm_spec_with_human()
    else:
        print(f"🔄 '{SPEC_FILE}' existante trouvée mais JAMAIS approuvée (run interrompu ?).")
        confirm_spec_with_human()

    # Fermeture propre : la sentinelle '.spec_approved' SURVIT volontairement (c'est le
    # signal de reprise des orchestrateurs aval) ; les fichiers temporaires et une
    # éventuelle sentinelle .done écrite tardivement (livrable accepté par le filet de
    # stabilité) sont purgés.
    for tmp_f in [TMP_PO_FILE, TMP_PROMPT_BUFFER, SPEC_DONE_SENTINEL]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    RUNNER.kill()

    print(f"\n🏁 Spécification '{SPEC_FILE}' validée et approuvée.")
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
