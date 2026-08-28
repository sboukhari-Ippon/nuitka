#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à PRÉ-AUDIT D'ACCESSIBILITÉ avec un harness d'agent + tmux (RGAA 4.1.2)
─────────────────────────────────────────────────────────────────────────────────
VARIANTE « PRÉ-AUDIT A11Y » : elle n'écrit AUCUN code — elle évalue une interface web
EXISTANTE contre les 106 critères du RGAA 4.1.2 (13 thématiques) et livre un rapport
consolidé 'accessibility_pre_audit_report.md' (statuts C/NC/NA/AVM par critère, taux
de conformité en fourchette, constats localisés) plus une synthèse courte des
résultats ('accessibility_pre_audit_summary.md'). C'est un PRÉ-audit statique : ni
audit de conformité, ni déclaration d'accessibilité.

C'est l'application de la logique MAIsterMind — trancher la fenêtre de contexte par
phase pour rendre les modèles petits ou moyens fiables sur la durée — à un audit
BIDIMENSIONNEL : le référentiel est trop gros pour une passe (106 critères) ET le code
aussi. Le découpage croise donc les deux axes :
  - axe RÉFÉRENTIEL : 13 « packs » thématiques (un fichier de grille par pack,
    './.agents/pipeline/audit-a11y/packs/'), routés par des déclencheurs regex
    DÉTERMINISTES déclarés dans 'packs.yaml' (pas de vidéo dans le code → le pack
    Multimédia n'est jamais payé : ses critères sont déclarés NA mécaniquement) ;
  - axe CODE : une cartographie d'interface ('a11y_map.yaml', validée par schéma
    Python PUIS par l'humain) répartit les fichiers UI en SOCLE (audité une fois),
    COMPOSANTS partagés (audités une fois, les écrans héritent) et ZONES d'écrans.
Chaque passe d'audit = UN pack × UN compartiment, dans une session neuve (/new), qui
ne reçoit QUE le tronc commun de la grille, SON pack et SES fichiers.

Pipeline :
  - Étape É0 : périmètre UI + routage des packs par PYTHON (déterministe, zéro LLM),
    mesures de contraste sur les paires CSS littérales, puis confirmation humaine (y/n)
    AVANT de payer le moindre tour d'agent.
  - Étape É1 : cartographie d'interface (1 passe LLM, sautée si 'a11y_map.yaml' valide),
    doublement validée : schéma Python (couverture totale garantie par une zone
    « Divers » mécanique) puis y/n humain — la carte affiche le décompte EXACT des
    passes avant de valider.
  - Étape É2 : N passes d'audit (une par pack × compartiment routé). Pas de verdict
    exécutable (un audit n'a ni build ni test) : filet de vivacité (3 tentatives) +
    PARSEUR de verdicts (set exact des critères du pack, statuts C/NC/NA/AVM, chaque
    NC constaté et localisé, Bilan cohérent) — plancher structurel FORT, les erreurs
    du parseur nourrissent le feedback de la tentative suivante.
  - Étape É3 : synthèse exécutive (1 passe LLM courte sur les chiffres agrégés,
    fallback MÉCANIQUE non bloquant — l'échec du chapeau n'invalide jamais N passes).
  - Étape É4 : agrégation et rapport 100 % Python (consolidation NC > AVM > C > NA,
    taux de conformité en fourchette — les AVM comptés NC pour le plancher, C pour le
    plafond —, annexes périmètre/routage/contrastes/limites, écriture atomique) +
    synthèse courte des résultats.

Reprise par fichiers, comme les autres variantes : une carte valide saute la
cartographie ; un fichier de verdicts qui PASSE LE PARSEUR saute sa passe ; la
synthèse, l'agrégation et le rapport sont TOUJOURS rejoués. Pour refaire un audit
complet : supprimer 'pre_audit_a11y/' (et 'a11y_map.yaml' pour rejouer la carte) et relancer.

Garde READ-ONLY (best-effort, si le projet est déjà un dépôt git) : un audit ne modifie
pas le projet audité. Tout fichier suivi modifié par un auditeur est restauré
(git checkout) et signalé ; tout fichier créé hors des livrables d'audit est signalé
(jamais supprimé : décision laissée à l'humain). Sans git, l'interdiction reste portée
par les prompts (dégradation gracieuse, comme partout ailleurs dans l'usine).

HONNÊTETÉ DU LIVRABLE : ceci est un PRÉ-AUDIT STATIQUE automatisé — il ne remplace pas
un audit de conformité RGAA opposable (tests au clavier, lecteurs d'écran, zoom 200 %,
rendu réel). Tout ce que le code seul ne démontre pas est explicitement marqué AVM
(« à vérifier manuellement ») et le taux de conformité est donné en fourchette.
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil
import unicodedata

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
    expand_dir_entry, residual_deliverable_warning, select_carto_sample,
    signal_handler, wait_should_continue,
)

# ─── HARNESS D'AGENT ──────────────────────────────────────────────────────────
# Toute la couche tmux (démarrage du TUI, collage de prompt, contexte neuf, capture
# d'écran, kill) vit dans 'mm_runner.py' : une classe par harness (OpenCode, Codex),
# choisie ici au démarrage selon l'équipement du projet ou MM_AGENT_HARNESS. Le reste
# du script n'en sait rien — sentinelles, portes, verdicts et prompts sont agnostiques.
RUNNER = resolve_runner(os.getcwd(), role="a11y", messages={
    "reuse":    None,
    "boot":     "⏳ Attente du boot du TUI {tui} cloud ({wait}s)...",
    "follow":   "   👀 Suis l'audit en direct dans un autre terminal : tmux attach -t {session}",
    "new_warn": "   ⚠️  La TUI n'a peut-être pas été réinitialisée ('/new' littéral encore "
                "à l'écran) : si le run dérive, vérifie avec tmux attach.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
A11Y_DIR              = "pre_audit_a11y"                       # verdicts intermédiaires (un fichier par passe)
A11Y_MAP_FILE         = "a11y_map.yaml"                    # carte d'interface (l'équivalent du blackboard)
A11Y_REPORT_FILE      = "accessibility_pre_audit_report.md"    # livrable final consolidé, à la RACINE
A11Y_SUMMARY_FILE = "accessibility_pre_audit_summary.md"  # synthèse courte des résultats, à la RACINE
SYNTHESIS_FILE        = f"{A11Y_DIR}/_synthese.md"         # chapeau rédigé (synthèse exécutive)
FAIL_REPORT_FILE      = "failReport.md"                    # rapport d'arrêt persistant (même contrat que l'usine)
A11Y_TRUNK_SKILL_FILE = "./.agents/pipeline/audit-a11y/SKILL.md"
A11Y_PACKS_FILE       = "./.agents/pipeline/audit-a11y/packs.yaml"
A11Y_PACKS_DIR        = "./.agents/pipeline/audit-a11y/packs"
A11Y_MAP_SKILL_FILE   = "./.agents/pipeline/a11y-map/SKILL.md"
DOC_MAP_FILE          = "doc_map.yaml"                     # carte du pipeline documentation : INDICE optionnel
AGENT_CONFIG_FILE     = RUNNER.config_file

# Fichier temporaire de routage de contexte (prompt déporté, nommé par le harness)
TMP_A11Y_FILE         = RUNNER.tmp_file("a11y")

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : c'est le
# seul choix valable sur les 3 OS (Windows n'a pas de /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Nom de la session tmux, suffixé d'une empreinte du répertoire du projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session. Préfixe DISTINCT
# des autres variantes (rôles 'factory' / 'proto' / 'audit' / 'doc') : un audit
# d'accessibilité peut coexister avec une production, une documentation ou un audit
# Nielsen sur un AUTRE projet sans risque de collision de session.
TMUX_SESSION          = RUNNER.session

# Marqueur HTML invisible des livrables générés : c'est lui qui distingue un rapport
# d'usine (écrasable) d'un document écrit à la main (annoncé avant le y/n, même contrat
# que le DOC_MARKER de la documentation).
A11Y_MARKER           = "<!-- généré par Pre-Audit-A11Y-RGAA -->"
A11Y_MARKER_LEGACY    = "<!-- généré par MAIsterMind_audit-a11y -->"

MAX_ATTEMPTS          = 3              # Tentatives par passe (filet de vivacité + parseur)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par passe (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes)

# Circuit breaker des passes d'audit. Les passes sont INDÉPENDANTES par construction
# (fichiers de verdicts et sentinelles distincts) : une passe non aboutie ne tue plus
# le run — ses critères sortent en AVM prudent et le rapport final est marqué PARTIEL.
# SAUF si le modèle cale systématiquement : au-delà de ces seuils, on s'arrête comme
# avant (inutile de brûler les tours restants). Un échec ISOLÉ ne déclenche jamais le
# breaker (le ratio ne s'applique qu'à partir de 2 échecs).
MAX_CONSECUTIVE_PASS_FAILURES = 2      # échecs de passes consécutifs → arrêt
MAX_PASS_FAILURE_RATIO        = 0.30   # part d'échecs parmi les passes traitées → arrêt

# Grâce accordée à une sentinelle SANS livrable (l'agent a pu créer les deux fichiers
# dans le désordre) : au-delà, la tentative échoue tout de suite — un agent qui a
# répondu vite mais mal ne coûte plus un timeout complet.
GRACE_POLLS_AFTER_SENTINEL = 3         # contrôles (× POLL_INTERVAL secondes)

# Bornes de fenêtre de contexte (mêmes familles que les autres variantes) :
MAX_SCOPE_FILES_IN_CARTO   = 400   # au-delà, le surplus du périmètre est résumé par répertoire
                                   # (assignable PAR RÉPERTOIRE : entrée de la carte terminée par '/')
DIVERS_RETRY_THRESHOLD     = 100   # au-delà de N fichiers en « Divers », la carte est REJOUÉE (tant
                                   # qu'il reste des tentatives) : 697 fichiers en résiduel = 28
                                   # tranches × 13 packs = 364 passes sur un « non classé »
MAX_BUCKET_FILES_IN_PROMPT = 150   # au-delà, la liste des fichiers d'une passe est tronquée dans le prompt
SOFT_MAX_FILES_PER_ZONE    = 25    # warn (non bloquant) au-delà — ALIGNÉ sur la borne de la
                                   # grille du cartographe (« 25 fichiers maximum par zone ») :
                                   # une seule vérité, la passe risque de saturer au-delà

# Bornes du scan déterministe (É0) :
MAX_TRIGGER_FILE_BYTES = 512 * 1024  # au-delà, seul le début du fichier est scanné (déclencheurs)
MAX_CONTRAST_PAIRS     = 40          # paires de contraste rapportées à la passe Couleurs (les pires d'abord)
MAX_TRIGGER_HITS_IN_PROMPT = 20      # hits de motifs annoncés à une passe (bloc MOTIFS, borné)
# L7 : un compartiment est SCINDÉ en tranches. Deux bornes, la première atteinte tranche :
# un BUDGET d'octets (ce qui sature réellement une fenêtre de contexte) et un plafond de
# fichiers (l'attention du modèle). 25 fichiers par tranche, quelle que soit leur taille,
# faisait 509 passes sur un monorepo dont la médiane des fichiers pèse 1,3 Ko.
MAX_FILES_PER_PASS = 40
MAX_PASS_BYTES     = 80 * 1024

STATUSES = ("C", "NC", "NA", "AVM")  # les quatre statuts de verdict (ordre d'affichage)


# ─── SENTINELLES (CANAL AUDITEUR → ORCHESTRATEUR) ─────────────────────────────
# Préfixe '.a11y_' DISTINCT des '.phase_' / '.pipeline_' / '.audit_' / '.doc_' des autres
# variantes : un résidu d'un ancien run d'un autre pipeline ne peut pas être pris pour
# un signal de celui-ci, et réciproquement.

def reset_agent_session():
    """Isolation GARANTIE entre passes (L5) : kill + start du harness — coût
    boot_wait (~6 s réels, ~4 min sur 40 passes), négligeable devant des passes de
    plusieurs minutes. Le /new warn-only était le maillon faible de la promesse
    « session neuve par passe » : un reset raté contamine silencieusement les
    verdicts suivants. Débrayable via MM_A11Y_HARD_RESET=0 (retour au /new)."""
    if os.environ.get("MM_A11Y_HARD_RESET", "").strip() == "0":
        RUNNER.new_context()
        return
    RUNNER.kill()
    RUNNER.start()


def a11y_sentinel(slot: str, attempt: int) -> str:
    """Fichier écrit par l'auditeur en toute fin de passe (signal 'j'ai terminé').

    'slot' identifie la passe ('map', 't11-z3', 't8-socle', 'synthese'…). Le numéro de
    tentative est inclus dans le nom : une sentinelle écrite tardivement par l'agent
    d'une tentative précédente ne peut pas être prise pour le signal de la tentative
    courante.
    """
    return f".a11y_{slot}.attempt{attempt}.done"


def cleanup_slot_sentinels(slot: str):
    """Supprime toutes les sentinelles (toutes tentatives) d'une passe."""
    prefix = f".a11y_{slot}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_a11y_sentinels():
    """Nettoyage final de toutes les sentinelles d'audit résiduelles."""
    for name in os.listdir("."):
        if name.startswith(".a11y_") and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def wait_for_deliverable(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> tuple:
    """Attend un livrable d'audit signalé par SENTINELLE (même contrat que le pipeline
    des autres variantes : l'agent crée le .done APRÈS avoir sauvegardé le livrable).

    Renvoie (ok, raison), raison ∈ {"ok", "timeout", "sentinelle_sans_livrable",
    "stable_hors_format"} : les appelants transforment la raison en feedback DÉDIÉ
    (un agent qui a répondu vite mais mal ne coûte plus un timeout complet).

    FILET pour un agent qui oublie la sentinelle : si le livrable existe, est non vide et
    n'a plus bougé depuis STABLE_POLLS_FALLBACK contrôles consécutifs, on l'accepte avec
    avertissement (dégradation gracieuse). Le 'structural_check' optionnel durcit ce
    filet : un livrable stable mais hors format bénéficie d'un SECOND palier de
    stabilité (l'agent écrit peut-être encore), puis la tentative échoue tout de suite.

    Une sentinelle présente SANS livrable est un signal de fin explicite : après une
    grâce de GRACE_POLLS_AFTER_SENTINEL contrôles (l'agent a pu créer les deux fichiers
    dans le désordre à une seconde d'écart), la tentative échoue immédiatement — la
    sentinelle fautive est consommée pour ne pas polluer la tentative suivante.
    """
    start = time.time()
    print(f"   ⏳ En attente de '{filepath}' (signal de fin : '{sentinel}')...")
    stable_streak = 0
    last_size = -1
    structural_warned = False
    sentinel_alone_streak = 0
    activity = {}   # état de wait_should_continue : prolongation si l'agent travaille encore,
                    # arrêt immédiat s'il est figé sur une demande de permission
    while wait_should_continue(start, timeout, activity):
        time.sleep(POLL_INTERVAL)
        file_ready = os.path.exists(filepath) and os.path.getsize(filepath) > 0
        if file_ready and os.path.exists(sentinel):
            try:
                os.remove(sentinel)
            except OSError:
                pass
            return True, "ok"
        if os.path.exists(sentinel) and not file_ready:
            sentinel_alone_streak += 1
            if sentinel_alone_streak >= GRACE_POLLS_AFTER_SENTINEL:
                try:
                    os.remove(sentinel)
                except OSError:
                    pass
                print(f"   ⛔ Sentinelle '{sentinel}' présente mais livrable absent ou vide : "
                      f"échec immédiat de la tentative (inutile d'attendre le timeout).")
                return False, "sentinelle_sans_livrable"
            continue
        sentinel_alone_streak = 0
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
                    if stable_streak >= 2 * STABLE_POLLS_FALLBACK:
                        print(f"   ⛔ '{filepath}' est resté stable ET hors format pendant "
                              f"{2 * STABLE_POLLS_FALLBACK * POLL_INTERVAL}s : échec immédiat "
                              f"de la tentative (l'agent a terminé hors format).")
                        return False, "stable_hors_format"
                    continue
                print(f"   ⚠️  Sentinelle '{sentinel}' absente mais '{filepath}' est stable depuis "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s : livrable accepté (filet de secours).")
                return True, "ok"
    return False, "timeout"


# ─── FEEDBACK DE RETRY CUMULATIF ──────────────────────────────────────────────
# Le feedback d'une tentative était écrasé à chaque tour : si la tentative 1 échoue
# sur « critère manquant » et la 2 sur « Bilan incohérent », le prompt de la 3 ne
# mentionnait plus le critère — le modèle pouvait réintroduire l'erreur corrigée.
# Chaque boucle accumule désormais ses échecs et le composeur borne le rappel.

MAX_PREVIOUS_ERRORS_IN_FEEDBACK = 4    # erreurs antérieures rappelées au maximum
MAX_PREVIOUS_ERROR_CHARS        = 200  # taille de chaque rappel (résumé, pas le détail)


def compose_retry_feedback(error_history: list) -> str:
    """Bloc de feedback d'une tentative : l'erreur de la DERNIÈRE tentative en détail,
    puis les erreurs DISTINCTES des tentatives antérieures en résumé borné (à ne pas
    réintroduire). Fonction PURE (testée unitairement)."""
    if not error_history:
        return "Premier passage — aucun retour précédent."
    feedback = error_history[-1]
    older = []
    for err in error_history[:-1]:
        short = " ".join(str(err).split())[:MAX_PREVIOUS_ERROR_CHARS]
        if short and short not in older:
            older.append(short)
    older = older[-MAX_PREVIOUS_ERRORS_IN_FEEDBACK:]
    if older:
        feedback += ("\nErreurs déjà rencontrées aux essais précédents, à NE PAS "
                     "réintroduire :\n" + "\n".join(f"- {o}" for o in older))
    return feedback


# ─── PLANCHERS STRUCTURELS LÉGERS & OUTILS DE NOMMAGE ─────────────────────────
# Le plancher LÉGER sert de structural_check à wait_for_deliverable (sections présentes) ;
# le contrôle FORT est le parseur de verdicts (plus bas), rejoué après chaque livraison.

def findings_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'un fichier de verdicts : ses sections obligatoires
    '## Verdicts' et '## Bilan' doivent être présentes (un fichier à moitié écrit — ou
    du bavardage hors format — s'arrête avant)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return "## verdicts" in content and "## bilan" in content
    except OSError:
        return False


def synthesis_structural_check(path: str) -> bool:
    """Plancher structurel minimal de la synthèse : elle commence par son titre."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip().lower().startswith("## synthèse exécutive")
    except OSError:
        return False


def map_structural_check(path: str) -> bool:
    """Plancher structurel minimal de la carte : YAML parsable ET zones non vide.
    Sert de structural_check à wait_for_deliverable pendant la cartographie."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return isinstance(data, dict) and isinstance(data.get("zones"), list) and bool(data["zones"])
    except (OSError, yaml.YAMLError):
        return False


def slugify(name: str) -> str:
    """Slug de fichier dérivé par PYTHON (jamais par le modèle — une source d'erreur de
    moins) : minuscules, accents translittérés, kebab-case."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "zone"


# ─── GRILLES : CHARGEMENT ─────────────────────────────────────────────────────
# Le tronc commun (SKILL.md) et la grille du cartographe sont envoyés ENTIERS ; la
# « tranche » de contexte d'une passe vient de son PACK (un fichier dédié par
# thématique) et de son COMPARTIMENT (les fichiers de la carte) — le découpage est
# porté par la structure de fichiers, pas par un tranchage de texte.

def load_grid(path: str) -> str:
    """Charge une grille (SKILL.md, pack). Son absence est un échec IMMÉDIAT : sans
    grille, les auditeurs improviseraient — exactement ce que l'usine interdit."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def pack_grid_path(pack: dict) -> str:
    """Chemin du fichier de grille d'un pack (calculé, jamais fourni par le manifeste)."""
    return f"{A11Y_PACKS_DIR}/T{pack['id']:02d}-{pack['slug']}.md"


def load_packs_manifest() -> tuple:
    """Charge et valide 'packs.yaml' (le manifeste de routage). Renvoie (packs, fatal).

    Le manifeste est une DONNÉE distribuée à côté du binaire (les scripts sont livrés
    compilés) : sa validation est donc aussi stricte que celle d'une carte produite par
    un LLM — un manifeste édité à la main ne doit jamais faire dérailler le run en
    silence. Fatal : structure invalide, id/slug/criteres incohérents, regex
    incompilable, fichier de pack absent.
    """
    fatal = []
    try:
        with open(A11Y_PACKS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return [], [f"'{A11Y_PACKS_FILE}' illisible ou YAML invalide : {str(e)[:300]}"]

    if not isinstance(data, dict) or not isinstance(data.get("packs"), list) or not data["packs"]:
        return [], [f"'{A11Y_PACKS_FILE}' : bloc 'packs' manquant ou vide."]

    packs, seen_ids, seen_slugs = [], set(), set()
    for idx, raw in enumerate(data["packs"]):
        if not isinstance(raw, dict):
            fatal.append(f"packs[{idx}] n'est pas un mapping.")
            continue
        try:
            pack_id = int(raw.get("id"))
        except (TypeError, ValueError):
            fatal.append(f"packs[{idx}].id manquant ou non entier.")
            continue
        slug = str(raw.get("slug") or "").strip()
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            fatal.append(f"pack T{pack_id} : slug manquant ou invalide (attendu : kebab-case).")
            continue
        if pack_id in seen_ids or slug in seen_slugs:
            fatal.append(f"pack T{pack_id} '{slug}' : id ou slug dupliqué.")
            continue
        seen_ids.add(pack_id)
        seen_slugs.add(slug)
        nom = str(raw.get("nom") or "").strip() or slug
        criteres = [str(c).strip() for c in (raw.get("criteres") or []) if str(c).strip()]
        if not criteres:
            fatal.append(f"pack T{pack_id} '{slug}' : liste 'criteres' vide.")
            continue
        bad = [c for c in criteres if not c.startswith(f"{pack_id}.")]
        if bad:
            fatal.append(f"pack T{pack_id} '{slug}' : critère(s) hors thématique : {', '.join(bad)}.")
            continue
        regexes = []
        for pattern in (raw.get("declencheurs") or []):
            try:
                regexes.append(re.compile(str(pattern), re.IGNORECASE))
            except re.error as e:
                fatal.append(f"pack T{pack_id} '{slug}' : déclencheur incompilable '{pattern}' ({e}).")
        # Sondes NC : indices quasi certains détectables par regex (bloc OPTIONNEL).
        # Compilées ici, validées comme le reste du manifeste ; jamais un verdict.
        sondes = []
        for raw_sonde in (raw.get("sondes") or []):
            motif = str((raw_sonde or {}).get("motif") or "")
            crit_s = str((raw_sonde or {}).get("critere") or "").strip()
            conf = str((raw_sonde or {}).get("confiance") or "").strip()
            try:
                compiled = re.compile(motif, re.IGNORECASE)
            except re.error as e:
                fatal.append(f"pack T{pack_id} '{slug}' : sonde incompilable '{motif}' ({e}).")
                continue
            if crit_s not in criteres:
                fatal.append(f"pack T{pack_id} '{slug}' : sonde sur critère hors pack ({crit_s}).")
                continue
            if conf not in ("quasi-certain", "probable", "candidat"):
                fatal.append(f"pack T{pack_id} '{slug}' : confiance de sonde hors enum ({conf}).")
                continue
            sondes.append({"regex": compiled, "motif": motif, "critere": crit_s,
                           "confiance": conf})
        # Testabilité par critère : DONNÉE obligatoire (recopiée depuis les suffixes des
        # grilles). C'est elle qui rend applicable la règle de fer « un C sur un critère
        # manuel n'est pas démontrable statiquement » (requalification à l'agrégation).
        testabilite = raw.get("testabilite")
        if not isinstance(testabilite, dict):
            fatal.append(f"pack T{pack_id} '{slug}' : bloc 'testabilite' manquant "
                         f"(un mapping critère → statique|partielle|manuelle).")
            testabilite = {}
        else:
            testabilite = {str(k).strip(): str(v).strip() for k, v in testabilite.items()}
            missing_t = [c for c in criteres if c not in testabilite]
            extra_t = [c for c in testabilite if c not in criteres]
            bad_values = sorted({v for v in testabilite.values()
                                 if v not in ("statique", "partielle", "manuelle")})
            if missing_t:
                fatal.append(f"pack T{pack_id} '{slug}' : testabilite manquante pour "
                             f"{', '.join(missing_t)}.")
            if extra_t:
                fatal.append(f"pack T{pack_id} '{slug}' : testabilite pour critère(s) "
                             f"hors pack : {', '.join(extra_t)}.")
            if bad_values:
                fatal.append(f"pack T{pack_id} '{slug}' : testabilite hors enum "
                             f"(attendu statique|partielle|manuelle) : {', '.join(bad_values)}.")
        pack = {
            "id": pack_id,
            "slug": slug,
            "nom": nom,
            "criteres": criteres,
            "toujours": bool(raw.get("toujours")),
            "testabilite": testabilite,
            "sondes": sondes,
            "regexes": regexes,
            "grid_path": "",
            "grid_text": "",
        }
        pack["grid_path"] = pack_grid_path(pack)
        pack["grid_text"] = load_grid(pack["grid_path"])
        if not pack["grid_text"].strip():
            fatal.append(f"pack T{pack_id} '{slug}' : grille manquante ou vide ('{pack['grid_path']}').")
        packs.append(pack)

    packs.sort(key=lambda p: p["id"])

    # Contrôles d'ENSEMBLE (chaque pack est déjà validé isolément) : un critère déclaré
    # dans DEUX packs serait audité et consolidé en double — fatal. Une union qui
    # n'atteint pas les 106 critères du RGAA 4.1.2 est possible (manifeste édité ou
    # amputé volontairement) mais jamais silencieuse : warning console, et l'annexe
    # « Méthode et limites » du rapport affiche le décompte réellement audité.
    owner_by_criterion = {}
    for pack in packs:
        for crit in pack["criteres"]:
            if crit in owner_by_criterion:
                fatal.append(f"critère {crit} déclaré deux fois "
                             f"(pack T{owner_by_criterion[crit]} puis pack T{pack['id']}) : "
                             f"il serait audité et consolidé en double.")
            else:
                owner_by_criterion[crit] = pack["id"]
    version = str(data.get("version") or "").strip()
    if not fatal and version == "rgaa-4.1.2" and len(owner_by_criterion) != 106:
        print(f"⚠️  Manifeste '{A11Y_PACKS_FILE}' : {len(owner_by_criterion)} critère(s) au "
              f"total au lieu des 106 du RGAA 4.1.2 (manifeste édité ?). Non bloquant, mais "
              f"l'audit ne portera QUE sur ces critères (mention en annexe du rapport).")
    return packs, fatal


# ─── DÉCOUVERTE DU PÉRIMÈTRE (PYTHON, DÉTERMINISTE, ZÉRO LLM) ─────────────────
# Le périmètre est établi par l'orchestrateur, jamais par un agent : liste stable,
# reproductible, affichée à l'humain AVANT de payer le moindre tour de LLM.
# Par rapport à l'audit Nielsen, la liste couvre AUSSI les templates serveur
# (.php, .erb, .jsp, .liquid…) : un site PHP/Rails a une interface à auditer.

UI_EXTENSIONS = {".html", ".htm", ".css", ".scss", ".sass", ".less",
                 ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                 ".vue", ".svelte", ".astro", ".ejs", ".hbs", ".njk", ".twig",
                 ".php", ".erb", ".jsp", ".liquid", ".mustache", ".pug",
                 ".haml", ".slim", ".cshtml", ".razor"}

# Répertoires exclus par NOM ; tout répertoire caché ('.git', '.agents', '.opencode'/'.codex',
# '.venv', '.next'…) est exclu d'office par le filtre startswith('.') du walk.
# Les répertoires de tests de bout en bout (e2e, cypress, playwright) contiennent des
# .ts/.js qui ne sont PAS l'interface : exclus.
EXCLUDED_DIR_NAMES = {"node_modules", "dist", "build", "out", "coverage", "target",
                      "vendor", "__pycache__", "e2e", "cypress", "playwright", A11Y_DIR}

# Hors périmètre MÉCANIQUE (tracé en annexe du rapport, jamais silencieux) :
# - assets tiers livrés avec le projet (design system DSFR distribué dans public/, bundles
#   legacy, feuilles d'icônes) : auditer la bibliothèque n'est pas auditer le projet — ses
#   surcharges, elles, vivent dans src/ et restent dans le périmètre ;
# - fichiers de LOGIQUE PURE (.ts/.js sans balisage, ni DOM, ni ARIA) : un service, une
#   route d'API, un utilitaire ne sont pas une interface. Les extensions porteuses de
#   balisage (.tsx, .jsx, .vue, .html…) restent TOUJOURS dans le périmètre, même sans
#   signal (une page qui ne fait que composer des composants en est une).
VENDOR_PATH_RE = re.compile(r"(^|/)(public|static|assets)/|/dsfr/|/vendors?/|\.legacy\.|\.bundle\.",
                            re.IGNORECASE)
LOGIC_EXTENSIONS = {".ts", ".js", ".mjs", ".cjs"}
UI_SIGNAL_RE = re.compile(
    r"<(div|span|a|p|img|svg|button|input|form|label|select|option|textarea|table|thead|tbody|"
    r"tr|td|th|ul|ol|li|nav|header|footer|main|section|article|aside|h[1-6]|iframe|video|audio|"
    r"dialog|details|summary|fieldset|legend|canvas|figure|picture|source|template)\b"
    r"|<[A-Z][A-Za-z0-9]*(\s[a-zA-Z]|\s*/>)"           # composant JSX avec attribut ou auto-fermant
    r"|\baria-[a-z]+|\brole\s*=|\bclassName\b|\bhtmlFor\b|\btabIndex\b|\btabindex\b"
    r"|\bdangerouslySetInnerHTML\b|\binnerHTML\b|\bdocument\.|\bcreateElement\b"
    r"|\bquerySelector|\bgetElementBy|\baddEventListener\b|\.classList\b|\.setAttribute\b"
    r"|\bonClick\b|\bonKeyDown\b|\.focus\(\)", re.IGNORECASE)
# Exclusions de la dernière découverte de périmètre (pour l'écran É0 et l'annexe).
SCOPE_EXCLUSIONS = {"vendor": [], "logic": []}


def is_vendor_asset(rel_path: str) -> bool:
    """Le chemin désigne-t-il un asset tiers livré (design system, bundle legacy…) ?"""
    return bool(VENDOR_PATH_RE.search(rel_path))


def is_logic_without_ui_signal(rel_path: str) -> bool:
    """Fichier d'extension de LOGIQUE (.ts/.js/.mjs/.cjs) sans aucun signal d'interface
    (balise, composant JSX, ARIA, DOM) dans son contenu. Une extension porteuse de
    balisage (.tsx, .vue, .html…) n'est jamais concernée."""
    if os.path.splitext(rel_path)[1].lower() not in LOGIC_EXTENSIONS:
        return False
    return not UI_SIGNAL_RE.search(read_file_prefix(rel_path))


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


def is_ui_file(name: str) -> bool:
    """'name' (nom de fichier nu) est-il une source d'interface à auditer ?

    Volontairement pragmatique : extensions UI connues, MOINS l'outillage qui partage ces
    extensions sans être de l'interface — bundles minifiés (illisibles, générés),
    déclarations TypeScript, fichiers de configuration (vite/webpack/tailwind…),
    stories Storybook (démo, pas produit), dotfiles.
    """
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext not in UI_EXTENSIONS:
        return False
    if low.startswith("."):
        return False
    if low.endswith(".d.ts") or ".min." in low or ".config." in low or ".stories." in low:
        return False
    return True


def discover_ui_scope() -> list:
    """Liste triée (chemins relatifs, séparateur '/') des fichiers UI à auditer.

    Les assets tiers et la logique pure sans signal d'interface sont écartés
    MÉCANIQUEMENT et consignés dans SCOPE_EXCLUSIONS (écran É0 + annexe du rapport) :
    une exclusion doit toujours pouvoir être relue et contestée."""
    scope = []
    SCOPE_EXCLUSIONS["vendor"] = []
    SCOPE_EXCLUSIONS["logic"] = []
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
            if is_vendor_asset(rel):
                SCOPE_EXCLUSIONS["vendor"].append(rel)
                continue
            if is_logic_without_ui_signal(rel):
                SCOPE_EXCLUSIONS["logic"].append(rel)
                continue
            scope.append(rel)
    SCOPE_EXCLUSIONS["vendor"].sort()
    SCOPE_EXCLUSIONS["logic"].sort()
    return sorted(scope)


def summarize_by_directory(files: list, max_lines: int = 60) -> str:
    """Résumé par répertoire (les plus peuplés d'abord, borné à `max_lines`)."""
    counts = {}
    for f in files:
        d = os.path.dirname(f) or "."
        counts[d] = counts.get(d, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"- {d}/ : {n} fichier(s)" for d, n in ordered[:max_lines]]
    if len(ordered) > max_lines:
        lines.append(f"- (+ {len(ordered) - max_lines} autre(s) répertoire(s))")
    return "\n".join(lines)


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


# ─── SCAN DES DÉCLENCHEURS (PYTHON, DÉTERMINISTE, ZÉRO LLM) ───────────────────
# Le routage des packs est calculé par le code, jamais par un agent : chaque fichier UI
# est lu UNE fois et confronté aux regex du manifeste. Résultat : fichier → packs
# déclenchés, base du décompte de passes affiché à l'humain AVANT de payer.

def read_file_prefix(path: str, limit: int = MAX_TRIGGER_FILE_BYTES) -> str:
    """Lit (au plus 'limit' octets de) 'path' en tolérant les encodages exotiques :
    un octet illisible ne doit jamais faire tomber le scan déterministe."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def scan_triggers(scope_files: list, packs: list) -> tuple:
    """Confronte chaque fichier du périmètre aux déclencheurs de chaque pack.

    Renvoie (triggers, hits) :
    - triggers : {chemin: set(id de packs déclenchés)} (fichiers sans aucun match
      inclus, avec un set vide : la couverture du périmètre reste visible) ;
    - hits : {(id de pack, chemin): (ligne, motif)} — le PREMIER match par
      (pack, fichier), conservé pour ancrer les passes (bloc MOTIFS DÉTECTÉS du
      prompt) et documenter l'annexe des passes suspectes. Coût nul : regex.search
      calculait déjà l'objet match, on le lit au lieu de le jeter.
    """
    triggers, hits = {}, {}
    for path in scope_files:
        content = read_file_prefix(path)
        found = set()
        if content:
            for pack in packs:
                for regex in pack["regexes"]:
                    match = regex.search(content)
                    if match:
                        found.add(pack["id"])
                        hits[(pack["id"], path)] = (
                            content.count("\n", 0, match.start()) + 1, regex.pattern)
                        break
        triggers[path] = found
    return triggers, hits


def scan_sondes(scope_files: list, packs: list) -> dict:
    """Confronte chaque fichier du périmètre aux SONDES NC des packs (H3).
    Renvoie {(id de pack, chemin): [(ligne, motif, critère, confiance), …]} — le
    PREMIER match de chaque sonde par fichier. Un INDICE, jamais un verdict."""
    hits = {}
    for path in scope_files:
        content = read_file_prefix(path)
        if not content:
            continue
        for pack in packs:
            for sonde in pack["sondes"]:
                match = sonde["regex"].search(content)
                if match:
                    line = content.count("\n", 0, match.start()) + 1
                    hits.setdefault((pack["id"], path), []).append(
                        (line, sonde["motif"], sonde["critere"], sonde["confiance"]))
    return hits


# ─── MESURES DE CONTRASTE (PYTHON PUR, INDICE POUR LA PASSE COULEURS) ─────────
# Sous-ensemble SÛR uniquement : les paires color / background(-color) littérales
# déclarées dans un MÊME bloc CSS. Tout le reste (variables, thèmes, héritage, images)
# relève de l'agent (et le plus souvent d'un statut AVM). Jamais un verdict automatique :
# un INDICE chiffré fourni à la passe Couleurs et repris en annexe du rapport.

CSS_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "orange": (255, 165, 0), "purple": (128, 0, 128), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "silver": (192, 192, 192), "maroon": (128, 0, 0),
    "navy": (0, 0, 128), "teal": (0, 128, 128), "olive": (128, 128, 0),
    "lime": (0, 255, 0), "aqua": (0, 255, 255), "cyan": (0, 255, 255),
    "fuchsia": (255, 0, 255), "magenta": (255, 0, 255),
}

CSS_DECL_RE = re.compile(r"(?:^|[;{\s])(color|background-color|background)\s*:\s*([^;}{]+)",
                         re.IGNORECASE)


def parse_css_color(token: str):
    """Convertit un littéral CSS en (r, g, b), ou None si non littéral / non opaque.
    Couvre : #rgb, #rrggbb, rgb(), rgba() à alpha 1, et les couleurs nommées de base.
    Les rgba() semi-transparents sont EXCLUS (la couleur effective dépend du fond)."""
    token = str(token).strip().strip(";").strip().lower()
    match = re.fullmatch(r"#([0-9a-f]{3})", token)
    if match:
        h = match.group(1)
        return tuple(int(c * 2, 16) for c in h)
    match = re.fullmatch(r"#([0-9a-f]{6})", token)
    if match:
        h = match.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    match = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([0-9.]+)\s*)?\)", token)
    if match:
        r, g, b = (min(int(match.group(i)), 255) for i in (1, 2, 3))
        alpha = match.group(4)
        if alpha is not None and float(alpha) < 1.0:
            return None
        return (r, g, b)
    return CSS_NAMED_COLORS.get(token)


def relative_luminance(rgb: tuple) -> float:
    """Luminance relative WCAG d'une couleur sRGB (formule officielle)."""
    def channel(value):
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb1: tuple, rgb2: tuple) -> float:
    """Rapport de contraste WCAG entre deux couleurs (1.0 à 21.0)."""
    l1, l2 = relative_luminance(rgb1), relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def measure_css_contrasts(scope_files: list) -> list:
    """Mesure les paires color/background LITTÉRALES d'un même bloc CSS des feuilles de
    style du périmètre. Renvoie une liste de dicts triée du pire au meilleur ratio,
    bornée à MAX_CONTRAST_PAIRS. Best-effort assumé : parser naïf par blocs '{...}',
    silencieux sur tout ce qu'il ne comprend pas (jamais bloquant)."""
    results = []
    css_files = [f for f in scope_files
                 if os.path.splitext(f)[1] in (".css", ".scss", ".sass", ".less")]
    for path in css_files:
        content = read_file_prefix(path)
        if not content:
            continue
        # Découpage naïf en blocs : 'sélecteur { déclarations }'. Les at-rules imbriquées
        # (@media) laissent des fragments sans déclarations : inoffensif.
        for block_match in re.finditer(r"([^{}]{1,400})\{([^{}]*)\}", content):
            selector = " ".join(block_match.group(1).split())[-120:]
            body = block_match.group(2)
            color, background = None, None
            for decl in CSS_DECL_RE.finditer(body):
                prop = decl.group(1).lower()
                value = decl.group(2)
                if prop == "color":
                    color = parse_css_color(value)
                else:
                    # 'background' raccourci : ne garder que si la valeur ENTIÈRE est une
                    # couleur littérale (un raccourci avec image/position est ignoré).
                    parsed = parse_css_color(value)
                    if parsed is not None:
                        background = parsed
            if color is not None and background is not None:
                line = content[:block_match.start()].count("\n") + 1
                results.append({
                    "file": path,
                    "line": line,
                    "selector": selector,
                    "ratio": round(contrast_ratio(color, background), 2),
                })
    results.sort(key=lambda r: r["ratio"])
    return results[:MAX_CONTRAST_PAIRS]


def build_contrast_block(contrasts: list) -> str:
    """Bloc « MESURES DE CONTRASTE » injecté dans le prompt de la passe Couleurs (et
    repris en annexe du rapport). Chaîne vide si aucune paire mesurée."""
    if not contrasts:
        return ""
    lines = ["Paires color/fond littérales mesurées mécaniquement (ratio WCAG ; seuils : "
             "4,5:1 texte courant, 3:1 grand texte et composants) — ces mesures font foi "
             "pour CES paires ; tout le reste relève de ton analyse :"]
    for c in contrasts:
        lines.append(f"- {c['ratio']}:1 — {c['file']}:{c['line']} ({c['selector']})")
    return "\n".join(lines)


# ─── GARDE READ-ONLY (GIT, BEST-EFFORT) ───────────────────────────────────────
# « Python vérifie ce qui est vérifiable » : l'interdiction de modifier le projet audité
# est portée par les prompts (invérifiable seule) ET par ce diff mécanique quand un dépôt
# git préexiste. Comme l'audit Nielsen : JAMAIS de 'git init' ni de commit — un audit ne
# doit laisser AUCUNE trace dans le projet audité en dehors de ses livrables.

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
# droit de produire — jamais restaurés ni signalés par la garde read-only.
_A11Y_BASENAMES = {A11Y_REPORT_FILE, A11Y_SUMMARY_FILE, A11Y_MAP_FILE,
                   FAIL_REPORT_FILE, TMP_A11Y_FILE, TMP_PROMPT_BUFFER,
                   os.path.basename(__file__)}


def is_a11y_artifact(path: str) -> bool:
    """'path' est-il un livrable/artefact de l'audit (et non un fichier du projet audité) ?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _A11Y_BASENAMES:
        return True
    if segments[0] == A11Y_DIR:
        return True
    # Sentinelles et tampons éphémères, où qu'ils se trouvent dans l'arbre.
    if base.startswith(".a11y_") and base.endswith(".done"):
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
        (compromis assumé : ne jamais détruire du travail humain prime sur la garde).
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
    dirty_project = sorted(f for f in _GIT["baseline_dirty"] if not is_a11y_artifact(f))
    if dirty_project:
        print(f"   ⚠️  {len(dirty_project)} fichier(s) déjà modifié(s) AVANT l'audit (travail en "
              f"cours ?) : ils sont exclus de la garde (jamais restaurés d'office) — "
              f"{', '.join(dirty_project[:10])}{'…' if len(dirty_project) > 10 else ''}")


def enforce_readonly(label: str):
    """Restaure les fichiers SUIVIS modifiés pendant une passe et signale les fichiers créés
    hors livrables d'audit (best-effort, après CHAQUE passe).

    Restauration d'office pour les modifications (un audit ne corrige pas) ; simple
    SIGNALEMENT pour les créations (on ne supprime jamais un fichier qu'on n'a pas créé :
    décision laissée à l'humain).
    """
    if not _GIT["enabled"]:
        return
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    # 'baseline_dirty' exclu de la restauration : un fichier déjà modifié AVANT l'audit
    # porte du travail humain non commité — le restaurer le DÉTRUIRAIT (cf. init).
    touched = sorted(f for f in diff_out.splitlines()
                     if f.strip() and not is_a11y_artifact(f.strip())
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
            if not is_a11y_artifact(f))
        if strays:
            print(f"⚠️  [{label}] Fichier(s) créé(s) hors livrables d'audit (non supprimés, "
                  f"à inspecter) : {', '.join(strays)}")


# ─── RAPPORT D'ÉCHEC & MESSAGE D'ÉCHEC ────────────────────────────────────────

# État partagé pour le rapport d'échec : la liste des passes construites (connue après
# la carte) — permet un failReport indexé sur l'avancement réel.
_RUN_STATE = {"passes": []}


def audited_count() -> int:
    """Nombre de passes dont le fichier de verdicts est déjà exploitable."""
    return sum(1 for p in _RUN_STATE["passes"] if findings_ok(p["findings_path"], p["pack"]))


def write_fail_report(title: str, reason: str, details: str = ""):
    """Écrit un rapport d'arrêt persistant à la racine (même contrat que l'usine :
    tout arrêt NON nominal en produit un). Best-effort : ne lève JAMAIS."""
    try:
        lines = ["# Rapport d'échec — MAIsterMind (audit accessibilité)", "",
                 f"## {title}", "", "### Cause", reason.strip(), ""]
        passes = _RUN_STATE["passes"]
        if passes:
            lines.append("### Avancement")
            lines.append(f"- Passes d'audit exploitables : {audited_count()}/{len(passes)}")
            for p in passes:
                mark = "✅" if findings_ok(p["findings_path"], p["pack"]) else "⏳"
                lines.append(f"  - {mark} {p['label']}")
            lines.append("")
        if details.strip():
            lines.append("### Détails")
            lines.append(details.strip()[:4000])
            lines.append("")
        lines.append("### Action recommandée")
        lines.append("Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model ou "
                     f"'{AGENT_CONFIG_FILE}'), puis relance : les passes déjà exploitables "
                     "seront reprises automatiquement.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Rapport d'échec écrit dans '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


def fail_a11y(message: str, details: str = "", title: str = "Échec d'une passe de l'audit"):
    """Point de sortie unique des échecs. Tue toujours la session tmux AVANT de quitter :
    un exit qui laisse l'agent vivant le laisse finir d'écrire son livrable APRÈS
    l'abandon de l'orchestrateur (état de reprise trompeur au relancement)."""
    # Clôture du journal de run côté échec. PAS dans write_fail_report ici : l'audit
    # l'appelle aussi pour le rapport PARTIEL, qui ne quitte pas le run.
    mm_audit.end("failed")
    print(message)
    write_fail_report(title, message, details)
    RUNNER.kill()
    sys.exit(1)


def print_pass_failure(label: str, reason: str):
    model = RUNNER.configured_model()
    done = audited_count()
    total = len(_RUN_STATE["passes"]) or "?"
    print(f"""
{'='*60}
❌ La passe « {label} » n'a pas abouti après {MAX_ATTEMPTS} tentatives.

   Cause : {reason}

💡 Le modèle actuel ({model}) cale sur cette passe (souvent un problème d'appels
   d'outils : le fichier de verdicts ou la sentinelle ne sont jamais créés, ou le
   format verrouillé n'est pas respecté).
   Le plus efficace : relance après avoir amené un modèle un cran au-dessus,
   soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : les {done}/{total} passe(s) déjà exploitables seront reprises
   automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
{'='*60}
""")


# ─── ÉTAPE É1 : CARTOGRAPHIE — VALIDATION DE SCHÉMA (PYTHON) ──────────────────

def norm_rel(path) -> str:
    """Normalise un chemin fourni par le modèle vers le format du périmètre
    (relatif, séparateur '/', sans './')."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _normalize_bucket_block(a11y_map: dict, key: str, label: str, soft: list) -> dict:
    """Normalise le bloc 'socle' ou 'composants' de la carte : mapping {intent, files}.
    Absent ou malformé → bloc vide (soft), jamais fatal : ces compartiments sont optionnels."""
    block = a11y_map.get(key)
    if not isinstance(block, dict):
        if block is not None:
            soft.append(f"Bloc '{key}' malformé : remplacé par un bloc vide.")
        block = {}
    files = block.get("files")
    block = {"intent": str(block.get("intent") or f"({label} non renseigné)").strip(),
             "files": files if isinstance(files, list) else []}
    a11y_map[key] = block
    return block


def validate_and_normalize_a11y_map(a11y_map, scope_files: list) -> tuple:
    """Contrôle et normalise la carte. Renvoie (fatal, soft) et MUTE a11y_map en place.

    La carte sort d'un petit LLM faillible ; deux classes de problèmes (même famille que
    validate_and_normalize_doc_map du pipeline documentation) :
      - fatal : manques STRUCTURANTS (pas un mapping, zones absentes, zone sans id/nom,
        ids dupliqués — sentinelles partagées —, zone dont AUCUN fichier listé n'existe).
      - soft : manques rattrapés MÉCANIQUEMENT ici (chemins inventés retirés, doublons
        d'assignation dédupliqués — premier compartiment gagne —, couverture complétée
        par une zone « Divers », intent/project comblés) : signalés, jamais bloquants.
    Le modèle propose, le code vérifie, l'humain tranche (au y/n qui suit).
    """
    fatal, soft = [], []
    if not isinstance(a11y_map, dict):
        return ["La carte n'est pas un mapping YAML valide."], []
    zones = a11y_map.get("zones")
    if not isinstance(zones, list) or not zones:
        return ["Bloc 'zones' manquant ou vide : rien à auditer par écran."], []

    if not str(a11y_map.get("project") or "").strip():
        a11y_map["project"] = os.path.basename(os.getcwd()) or "Projet"
        soft.append(f"Champ 'project' manquant : comblé avec « {a11y_map['project']} » (affichage seul).")

    socle = _normalize_bucket_block(a11y_map, "socle", "socle", soft)
    composants = _normalize_bucket_block(a11y_map, "composants", "composants", soft)

    scope = set(scope_files)
    seen_paths = {}   # chemin -> étiquette du premier compartiment qui l'assigne
    seen_ids = set()

    def absorb_files(entries, owner_label):
        """Filtre une liste de chemins : hors périmètre retirés, doublons dédupliqués.
        Une entrée RÉPERTOIRE (chemin terminé par '/') s'étend à tous les fichiers du
        périmètre qu'il contient, non encore assignés (cartographier un monorepo sans
        recopier des milliers de chemins — et sans que le surplus tombe en « Divers »)."""
        kept, removed = [], []
        for entry in entries or []:
            p = norm_rel(entry)
            expanded = expand_dir_entry(p, scope_files, seen_paths)
            if expanded:
                for f in expanded:
                    seen_paths[f] = owner_label
                    kept.append(f)
                continue
            if p not in scope:
                removed.append(p)
                continue
            if p in seen_paths:
                soft.append(f"'{p}' assigné à plusieurs compartiments : conservé dans "
                            f"{seen_paths[p]} (première assignation), retiré de {owner_label}.")
                continue
            seen_paths[p] = owner_label
            kept.append(p)
        return kept, removed

    for key, block, label in (("socle", socle, "le socle"), ("composants", composants, "les composants")):
        kept, removed = absorb_files(block["files"], label)
        block["files"] = kept
        if removed:
            shown = ", ".join(removed[:10]) + ("…" if len(removed) > 10 else "")
            soft.append(f"Compartiment '{key}' : {len(removed)} chemin(s) hors périmètre "
                        f"retiré(s) mécaniquement ({shown}).")

    for idx, zone in enumerate(zones):
        if not isinstance(zone, dict):
            fatal.append(f"zones[{idx}] n'est pas un mapping.")
            continue
        try:
            zone["id"] = int(zone.get("id"))
        except (TypeError, ValueError):
            fatal.append(f"zones[{idx}].id manquant ou non entier.")
            continue
        if zone["id"] in seen_ids:
            fatal.append(f"zones[].id dupliqué ({zone['id']}) : les sentinelles des passes "
                         f"de cette zone seraient PARTAGÉES entre deux zones.")
        seen_ids.add(zone["id"])
        if not str(zone.get("name") or "").strip():
            fatal.append(f"zones[{idx}].name manquant.")
            continue
        zone["name"] = str(zone["name"]).strip()
        if not str(zone.get("intent") or "").strip():
            zone["intent"] = "(non renseigné)"
            soft.append(f"Zone Z{zone['id']} « {zone['name']} » : 'intent' manquant (comblé).")

        declared = len(zone.get("files") or []) if isinstance(zone.get("files"), list) else 0
        kept, removed = absorb_files(zone.get("files") if isinstance(zone.get("files"), list) else [],
                                     f"Z{zone['id']}")
        zone["files"] = kept
        if removed:
            shown = ", ".join(removed[:10]) + ("…" if len(removed) > 10 else "")
            soft.append(f"Zone Z{zone['id']} « {zone['name']} » : {len(removed)} chemin(s) hors "
                        f"périmètre retiré(s) mécaniquement ({shown}).")
        if declared and not zone["files"]:
            fatal.append(f"Zone Z{zone['id']} « {zone['name']} » : AUCUN des fichiers listés "
                         f"n'existe dans le périmètre (chemins inventés ?).")
        elif not declared:
            # « Divers » vide n'est pas une faute : le prompt demande de NE PAS y recopier le
            # surplus (c'est la couverture qui le remplit) — la rejeter contredisait la consigne.
            if slugify(zone["name"]) == "divers":
                soft.append(f"Zone Z{zone['id']} « {zone['name']} » déclarée vide : complétée "
                            f"par le contrôle de couverture (ou retirée si rien ne reste).")
            else:
                fatal.append(f"Zone Z{zone['id']} « {zone['name']} » : aucun fichier assigné.")
        if len(zone["files"]) > SOFT_MAX_FILES_PER_ZONE:
            soft.append(f"Zone Z{zone['id']} « {zone['name']} » : {len(zone['files'])} fichiers "
                        f"(> {SOFT_MAX_FILES_PER_ZONE}) — les passes de cette zone risquent de "
                        f"saturer leur fenêtre ; redécoupe la carte avant de valider si possible.")

    if fatal:
        return fatal, soft

    ids = sorted(seen_ids)
    if ids != list(range(1, len(ids) + 1)):
        soft.append(f"zones[].id n'est pas une séquence contiguë 1..N "
                    f"({', '.join(str(i) for i in ids)}) : toléré, l'ordre du YAML fait foi.")

    # COUVERTURE TOTALE : tout fichier du périmètre absent de la carte est ajouté
    # MÉCANIQUEMENT à une zone « Divers » (créée au besoin) — l'audit ne laisse aucun
    # angle mort silencieux.
    missing = [f for f in scope_files if f not in seen_paths]
    if missing:
        divers = next((z for z in zones if isinstance(z, dict)
                       and slugify(str(z.get("name") or "")) == "divers"), None)
        if divers is None:
            divers = {"id": max(seen_ids) + 1, "name": "Divers",
                      "intent": "Résiduel d'interface sans écran identifié "
                                "(complété mécaniquement par le contrôle de couverture).",
                      "files": []}
            zones.append(divers)
        divers["files"] = list(divers.get("files") or []) + missing
        soft.append(f"Couverture : {len(missing)} fichier(s) du périmètre absent(s) de la "
                    f"carte — ajouté(s) mécaniquement à la zone « Divers » (Z{divers['id']}).")

    # Une « Divers » déclarée vide et restée vide après couverture n'a plus de raison d'être
    # (une passe d'audit sur zéro fichier n'aurait aucun sens).
    zones[:] = [z for z in zones
                if not (isinstance(z, dict) and slugify(str(z.get("name") or "")) == "divers"
                        and not z.get("files"))]

    return fatal, soft


def divers_files(a11y_map: dict) -> list:
    """Fichiers rangés en zone « Divers » — [] si absente."""
    for zone in a11y_map.get("zones") or []:
        if isinstance(zone, dict) and slugify(str(zone.get("name") or "")) == "divers":
            return list(zone.get("files") or [])
    return []


def save_a11y_map(a11y_map: dict):
    """Persiste la carte NORMALISÉE (écriture atomique) : ce que l'humain valide au y/n
    est exactement ce qui est sur disque — et donc ce qu'un run de reprise rechargera."""
    tmp = f"{A11Y_MAP_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(a11y_map, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp, A11Y_MAP_FILE)


def peek_a11y_map():
    """Chargement best-effort de la carte pour l'affichage É0 (jamais bloquant)."""
    try:
        with open(A11Y_MAP_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and isinstance(data.get("zones"), list) and data["zones"]:
            return data
    except Exception:
        pass
    return None


def load_and_validate_map_file(scope_files: list) -> tuple:
    """Charge + valide 'a11y_map.yaml'. Renvoie (a11y_map, fatal, soft, parse_error)."""
    try:
        with open(A11Y_MAP_FILE, "r", encoding="utf-8") as f:
            a11y_map = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, [], [], str(e)
    except OSError as e:
        return None, [], [], str(e)
    fatal, soft = validate_and_normalize_a11y_map(a11y_map, scope_files)
    return a11y_map, fatal, soft, ""


# ─── COMPARTIMENTS ET MATRICE DES PASSES (PYTHON, DÉTERMINISTE) ───────────────
# La carte validée devient une liste ordonnée de compartiments (socle, composants,
# zones), croisée avec les packs déclenchés : c'est la matrice des passes. Tout est
# calculé ici — slots de sentinelles, chemins de livrables, libellés — le modèle ne
# fournit JAMAIS un nom de fichier.

def build_buckets(a11y_map: dict) -> list:
    """Compartiments ordonnés de l'audit. Socle et composants sont omis s'ils sont
    vides (aucune passe à payer sur un compartiment sans fichier)."""
    buckets = []
    socle = a11y_map.get("socle") or {}
    if socle.get("files"):
        buckets.append({"kind": "socle", "slot": "socle", "label": "SOCLE",
                        "name": "Socle (layout, navigation globale, styles globaux)",
                        "intent": socle.get("intent", ""), "files": socle["files"]})
    composants = a11y_map.get("composants") or {}
    if composants.get("files"):
        buckets.append({"kind": "composants", "slot": "comp", "label": "COMPOSANTS",
                        "name": "Composants partagés (design system)",
                        "intent": composants.get("intent", ""), "files": composants["files"]})
    for zone in a11y_map["zones"]:
        buckets.append({"kind": "zone", "slot": f"z{zone['id']}",
                        "label": f"Z{zone['id']:02d}_{slugify(zone['name'])}",
                        "name": f"Z{zone['id']} : {zone['name']}",
                        "intent": zone.get("intent", ""), "files": zone["files"]})
    return buckets


def triggered_pack_ids_for_bucket(bucket: dict, triggers: dict) -> set:
    """Packs déclenchés par le CONTENU des fichiers du compartiment (regex du manifeste
    seules, SANS la clause 'toujours') : sert au routage et au warning anti
    rubber-stamping — quand un motif existe dans les fichiers, une passe 100 % NA est
    incohérente par construction."""
    hits = set()
    for path in bucket["files"]:
        hits |= triggers.get(path, set())
    return hits


def active_pack_ids_for_bucket(bucket: dict, packs: list, triggers: dict) -> set:
    """Packs actifs sur un compartiment : union des déclencheurs de ses fichiers, plus
    les packs 'toujours' sur le SOCLE (l'absence de leurs motifs y est elle-même un
    constat potentiel : structure absente, focus jamais stylé…)."""
    active = set(triggered_pack_ids_for_bucket(bucket, triggers))
    if bucket["kind"] == "socle":
        active |= {p["id"] for p in packs if p["toujours"]}
    return active


def invalidated_passes(passes: list, changed_files: list) -> list:
    """Reprise diff-aware (L8) : passes dont au moins un fichier du compartiment
    apparaît dans le diff git. Fonction PURE (testée). La variante mtime est
    ÉCARTÉE : un touch global ou un changement de branche sur-invaliderait tout."""
    changed = {norm_rel(f) for f in changed_files}
    return [p for p in passes if changed & set(p["bucket"]["files"])]


def slice_bucket_files(files: list) -> list:
    """Tranches d'un compartiment : on remplit jusqu'à MAX_PASS_BYTES d'octets OU
    MAX_FILES_PER_PASS fichiers, la première borne atteinte tranche ; un fichier plus gros
    que le budget occupe sa tranche seul. Ordre des fichiers conservé (déterministe)."""
    slices, current, size = [], [], 0
    for path in files:
        try:
            file_size = os.path.getsize(path)
        except OSError:
            file_size = 0
        if current and (len(current) >= MAX_FILES_PER_PASS or size + file_size > MAX_PASS_BYTES):
            slices.append(current)
            current, size = [], 0
        current.append(path)
        size += file_size
    if current:
        slices.append(current)
    return slices or [list(files)]


def pass_needs_agent(audit_pass: dict) -> bool:
    """Une passe est confiée à l'agent si sa tranche porte au moins un motif du pack —
    sinon ses critères sont NA par routage déterministe et on ne paie pas un tour de LLM
    pour le lui faire constater (c'est aussi là qu'il hallucinait des C). Exception : les
    packs « toujours » sur le SOCLE, où l'ABSENCE de motif est elle-même un constat
    potentiel (structure absente, focus jamais stylé…)."""
    if audit_pass.get("declenche"):
        return True
    return audit_pass["bucket"].get("kind") == "socle" and bool(audit_pass["pack"].get("toujours"))


def mechanical_na_passes(passes: list) -> list:
    """Passes déclarées NA mécaniquement (tranche sans motif, hors socle « toujours »)."""
    return [p for p in passes if not pass_needs_agent(p)]


def write_mechanical_na_findings(audit_pass: dict):
    """Écrit le fichier de verdicts d'une tranche NA mécanique, au FORMAT des passes
    d'agent : le parseur, la consolidation (NA cède devant tout autre verdict des autres
    tranches), la reprise et le rapport le traitent comme n'importe quelle passe."""
    pack = audit_pass["pack"]
    reason = (f"aucun motif du pack T{pack['id']:02d} dans cette tranche "
              f"(routage déterministe, sans passe d'agent)")
    lines = [f"# T{pack['id']} : {pack['nom']} — {audit_pass['bucket']['name']}"
             + (" — NA mécanique" if True else ""), "",
             "<!-- Tranche déclarée NA par le routage déterministe : aucun déclencheur du pack "
             "dans ses fichiers. Aucun agent sollicité. -->", "",
             "## Verdicts"]
    lines += [f"- {crit} : NA — {reason}" for crit in pack["criteres"]]
    lines += ["", "## Constats", "Aucun constat.", "", "## Bilan",
              f"- Verdicts : C : 0, NC : 0, NA : {len(pack['criteres'])}, AVM : 0", ""]
    os.makedirs(os.path.dirname(audit_pass["findings_path"]) or ".", exist_ok=True)
    tmp = audit_pass["findings_path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, audit_pass["findings_path"])


def build_pass_list(buckets: list, packs: list, triggers: dict) -> list:
    """La matrice des passes : pour chaque compartiment (ordre de la carte), chaque pack
    actif (ordre des ids). Chaque passe porte son slot de sentinelle et son livrable.

    L7 : un compartiment au-delà de MAX_FILES_PER_PASS est SCINDÉ en tranches (slots
    t10-z3a, t10-z3b, …) — la saturation silencieuse de fenêtre était le trou de
    couverture le plus réel du pipeline (au-delà de la troncature du prompt, les
    fichiers surnuméraires n'étaient jamais audités, sans trace). La consolidation
    supporte nativement plusieurs passes par pack (NC > AVM > C > NA, dédup des
    constats) ; le décompte affiché au y/n est calculé APRÈS découpage, donc exact.
    'declenche' est recalculé PAR TRANCHE (une tranche sans motif est légitime à
    100 % NA)."""
    passes = []
    packs_by_id = {p["id"]: p for p in packs}
    for bucket in buckets:
        slices = slice_bucket_files(bucket["files"])
        for pack_id in sorted(active_pack_ids_for_bucket(bucket, packs, triggers)):
            pack = packs_by_id[pack_id]
            for idx, slice_files in enumerate(slices):
                multi = len(slices) > 1
                suffix = chr(ord("a") + idx) if multi and idx < 26 else (f"x{idx}" if multi else "")
                slice_bucket = dict(bucket, files=slice_files) if multi else bucket
                slice_triggered = any(pack_id in triggers.get(f, set()) for f in slice_files)
                passes.append({
                    "pack": pack,
                    "bucket": slice_bucket,
                    "slot": f"t{pack['id']}-{bucket['slot']}{suffix}",
                    "findings_path": f"{A11Y_DIR}/T{pack['id']:02d}_{pack['slug']}__"
                                     f"{bucket['label']}{('-' + suffix) if suffix else ''}.md",
                    "label": f"T{pack['id']:02d} {pack['nom']} × {bucket['name']}"
                             + (f" — tranche {idx + 1}/{len(slices)}" if multi else ""),
                    "declenche": slice_triggered,
                })
    return passes


def skipped_packs(passes: list, packs: list) -> list:
    """Packs sans AUCUNE passe (aucun déclencheur nulle part) : leurs critères seront
    déclarés NA mécaniquement, avec la raison en annexe du rapport."""
    active_ids = {p["pack"]["id"] for p in passes}
    return [p for p in packs if p["id"] not in active_ids]


# ─── PARSEUR DE VERDICTS (LE PLANCHER FORT DE CE PIPELINE) ────────────────────
# Contrairement à l'audit Nielsen (constats libres), le RGAA a des identifiants de
# critères FERMÉS et des statuts ÉNUMÉRABLES : le contrôle structurel peut donc être un
# vrai parseur — set exact des critères du pack, statuts dans l'enum, chaque NC constaté
# et localisé, Bilan cohérent. Ses erreurs nourrissent le feedback de retry.

VERDICT_LINE_RE = re.compile(r"^\s*-\s*(\d{1,2}\.\d{1,2})\s*:\s*(C|NC|NA|AVM)\b\s*(?:[—–-]\s*(.*))?$")
CONSTAT_HEADING_RE = re.compile(r"^###\s+K(\d+)\s*[—–-]\s*(\d{1,2}\.\d{1,2})\s*[—–-]\s*(.+)$")
CONSTAT_FIELD_RE = re.compile(r"^\s*-\s*\*\*(Impact|Localisation|Extrait|Constat|Impact utilisateur|Correction)\s*:\*\*\s*(.*)$")
BILAN_LINE_RE = re.compile(
    r"^\s*-\s*Verdicts\s*:\s*C\s*:\s*(\d+)\s*[,;]\s*NC\s*:\s*(\d+)\s*[,;]\s*"
    r"NA\s*:\s*(\d+)\s*[,;]\s*AVM\s*:\s*(\d+)", re.IGNORECASE)

# Suffixe ':ligne' (ou ':début-fin') d'une localisation — retiré avant le contrôle
# d'existence du fichier sur le disque.
LOCATION_LINE_SUFFIX_RE = re.compile(r":\d+(?:[-–]\d+)?$")


def extract_location_paths(localisation: str) -> list:
    """Chemins de fichiers extraits d'une ligne de Localisation (best-effort) : segments
    entre backticks s'il y en a (le format du tronc commun), sinon découpage par
    virgules ; normalisation norm_rel puis suffixe ':ligne' retiré. Un fragment avec
    espaces (commentaire libre du type « écran Panier ») n'est pas un chemin : ignoré."""
    text = str(localisation or "")
    tokens = re.findall(r"`([^`]+)`", text) or text.split(",")
    paths = []
    for token in tokens:
        p = LOCATION_LINE_SUFFIX_RE.sub("", norm_rel(token)).strip()
        if p and " " not in p:
            paths.append(p)
    return paths


def iter_lines_with_fence_state(content: str):
    """Itère (ligne, in_fence) : les lignes à l'intérieur des blocs ``` / ~~~ sont
    marquées pour que le parseur ne prenne jamais un exemple cité pour du contenu."""
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            yield line, True
            continue
        yield line, in_fence


def normalize_extract(text: str) -> str:
    """Espaces normalisés pour la comparaison d'extraits : le modèle recopie une ligne
    qu'il a réellement lue — l'indentation et les espacements ne sont pas des
    hallucinations, le CONTENU si."""
    return " ".join(str(text).split())


def locate_extrait(extrait: str, paths: list) -> tuple:
    """Vérité MATÉRIELLE d'un constat (H1) : la copie exacte de la ligne incriminée
    (espaces normalisés) doit apparaître dans l'un des fichiers cités. Renvoie
    (trouvé, chemin, ligne) — (False, "", 0) sinon. Fonction PURE (testée)."""
    needle = normalize_extract(extrait)
    if not needle:
        return False, "", 0
    for candidate in paths:
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if needle in normalize_extract(line):
                        return True, candidate, lineno
        except OSError:
            continue
    return False, "", 0


def parse_findings_file(path: str, pack: dict) -> tuple:
    """Parse un fichier de verdicts. Renvoie (data, fatal, soft).

    data = {"verdicts": {critère: {"statut", "note"}}, "constats": [dicts], "bilan": dict}
    fatal : ce qui rend le fichier inexploitable par l'agrégation (verdicts incomplets,
    statuts hors enum, NC sans constat, Bilan incohérent) → la passe est rejouée.
    soft : imperfections tolérées (champ de constat manquant, localisation vide) →
    signalées, l'agrégation les affiche « ? ».
    """
    fatal, soft = [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return None, [f"fichier illisible ({e})"], []

    expected = set(pack["criteres"])
    verdicts, constats, bilan = {}, [], None
    section = None          # None | 'verdicts' | 'constats' | 'bilan'
    current = None          # constat en cours de collecte

    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        low = line.strip().lower()
        if low.startswith("## "):
            if low.startswith("## verdicts"):
                section = "verdicts"
            elif low.startswith("## constats"):
                section = "constats"
            elif low.startswith("## bilan"):
                section = "bilan"
            else:
                section = None
            current = None
            continue

        if section == "verdicts":
            match = VERDICT_LINE_RE.match(line)
            if match:
                crit, statut, note = match.group(1), match.group(2), (match.group(3) or "").strip()
                if crit in verdicts:
                    soft.append(f"verdict dupliqué pour {crit} (le premier est conservé)")
                    continue
                verdicts[crit] = {"statut": statut, "note": note}
            elif line.strip().startswith("-"):
                soft.append(f"ligne de verdict non reconnue : « {line.strip()[:80]} »")

        elif section == "constats":
            match = CONSTAT_HEADING_RE.match(line.strip())
            if match:
                current = {"k": int(match.group(1)), "critere": match.group(2),
                           "titre": match.group(3).strip(), "impact": None,
                           "localisation": "", "constat": "", "impact_utilisateur": "",
                           "correction": "", "localisation_verifiee": True,
                           "extrait": "", "extrait_verifie": False}
                constats.append(current)
                continue
            if current is not None:
                field = CONSTAT_FIELD_RE.match(line)
                if field:
                    key = {"Impact": "impact", "Localisation": "localisation",
                           "Extrait": "extrait",
                           "Constat": "constat", "Impact utilisateur": "impact_utilisateur",
                           "Correction": "correction"}[field.group(1)]
                    value = field.group(2).strip()
                    if key == "impact":
                        digit = re.match(r"^([1-4])\b", value)
                        current["impact"] = int(digit.group(1)) if digit else None
                        if digit is None:
                            soft.append(f"constat K{current['k']} : impact illisible (« {value[:40]} »)")
                    else:
                        current[key] = value

        elif section == "bilan" and bilan is None:
            match = BILAN_LINE_RE.match(line)
            if match:
                bilan = {"C": int(match.group(1)), "NC": int(match.group(2)),
                         "NA": int(match.group(3)), "AVM": int(match.group(4))}

    # ── Contrôles FATALS : le set exact des critères, l'enum, les NC constatés, le Bilan.
    got = set(verdicts)
    missing = sorted(expected - got, key=lambda c: [int(x) for x in c.split(".")])
    unknown = sorted(got - expected, key=lambda c: [int(x) for x in c.split(".")])
    if missing:
        fatal.append(f"verdict(s) MANQUANT(s) pour : {', '.join(missing)}")
    if unknown:
        fatal.append(f"critère(s) HORS PACK : {', '.join(unknown)}")

    nc_criteria = {c for c, v in verdicts.items() if v["statut"] == "NC"}
    constated = {c["critere"] for c in constats}
    unconstated = sorted(nc_criteria - constated, key=lambda c: [int(x) for x in c.split(".")])
    if unconstated:
        fatal.append(f"critère(s) NC sans constat associé : {', '.join(unconstated)}")
    for constat in constats:
        if constat["critere"] not in expected:
            fatal.append(f"constat K{constat['k']} : critère hors pack ({constat['critere']})")
        if not constat["localisation"]:
            soft.append(f"constat K{constat['k']} ({constat['critere']}) : localisation absente")
        else:
            # Anti-hallucination, en SOFT (on ne repaye pas une passe pour un chemin mal
            # formaté) : chaque fichier cité doit exister sur le disque. Un chemin
            # introuvable marque le constat — le rapport suffixe sa ligne Localisation.
            missing_paths = [p for p in extract_location_paths(constat["localisation"])
                             if not os.path.exists(p)]
            if missing_paths:
                constat["localisation_verifiee"] = False
                soft.append(f"constat K{constat['k']} ({constat['critere']}) : fichier(s) de "
                            f"localisation introuvable(s) sur le disque "
                            f"({', '.join(missing_paths[:3])})")
        # ── VÉRITÉ MATÉRIELLE (H1) ── : l'extrait est la preuve que le code incriminé
        # existe TEL QUE DÉCRIT. Retrouvé → constat « vérifié » (badge au rapport) ;
        # absent ou introuvable → soft (le rapport marque « à vérifier ») ; le fatal
        # de passe (aucun extrait retrouvé) est contrôlé après la boucle.
        if not constat["extrait"]:
            soft.append(f"constat K{constat['k']} ({constat['critere']}) : champ Extrait "
                        f"absent — la matérialité du constat n'est pas vérifiable")
        else:
            cited = extract_location_paths(constat["localisation"])
            found, seen_path, seen_line = locate_extrait(constat["extrait"], cited)
            constat["extrait_verifie"] = found
            if not found:
                soft.append(f"constat K{constat['k']} ({constat['critere']}) : extrait NON "
                            f"retrouvé dans les fichiers cités — constat à vérifier")
            else:
                announced = re.search(r":(\d+)", constat["localisation"] or "")
                if announced and abs(int(announced.group(1)) - seen_line) > 5:
                    soft.append(f"constat K{constat['k']} ({constat['critere']}) : ligne "
                                f"annoncée {announced.group(1)}, extrait vu ligne "
                                f"{seen_line} ({seen_path})")

    # Hallucination FRANCHE (H1) : une passe qui produit des constats dont AUCUN extrait
    # n'est matériellement retrouvé décrit du code qui n'existe pas tel quel — rejet,
    # feedback dédié (les autres contrôles laissent les cas partiels en soft).
    if constats and not any(c["extrait_verifie"] for c in constats):
        fatal.append("aucun Extrait de la passe n'est retrouvé dans les fichiers cités : "
                     "chaque constat DOIT recopier EXACTEMENT (à l'identique) une ligne "
                     "incriminée que tu as réellement lue dans le fichier cité")

    if bilan is None:
        fatal.append("ligne de Bilan absente ou hors format "
                     "(attendu : '- Verdicts : C : <a>, NC : <b>, NA : <c>, AVM : <d>')")
    else:
        counted = {s: sum(1 for v in verdicts.values() if v["statut"] == s) for s in STATUSES}
        if counted != bilan:
            fatal.append(f"Bilan incohérent : annoncé {bilan}, compté {counted}")

    data = {"verdicts": verdicts, "constats": constats, "bilan": bilan}
    return data, fatal, soft


def bilan_only_fatals(fatal: list) -> bool:
    """TOUTES les anomalies fatales concernent-elles la seule ligne de Bilan ?
    (Bilan absent ou incohérent : les verdicts, eux, ont passé tous les autres
    contrôles — le fichier est réparable mécaniquement, cf. repair_bilan_line.)"""
    return bool(fatal) and all(f.startswith("Bilan incohérent")
                               or f.startswith("ligne de Bilan") for f in fatal)


def repair_bilan_line(path: str, verdicts: dict) -> bool:
    """Réécrit mécaniquement la ligne de Bilan depuis les verdicts parsés.

    Compter est le point faible notoire des petits modèles, et cette ligne n'apporte
    AUCUNE information : l'agrégation recompte tout (c'est précisément ainsi que le
    parseur détecte l'incohérence). L'exigence reste dans le prompt (effet checksum :
    forcer le modèle à se relire), mais un Bilan faux ne coûte plus une passe entière.
    Remplace la première ligne de Bilan hors fence, ou ajoute la section si absente.
    Renvoie True si le fichier a été réécrit."""
    counted = {s: sum(1 for v in verdicts.values() if v["statut"] == s) for s in STATUSES}
    line = (f"- Verdicts : C : {counted['C']}, NC : {counted['NC']}, "
            f"NA : {counted['NA']}, AVM : {counted['AVM']}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    out, replaced = [], False
    for text_line, in_fence in iter_lines_with_fence_state(content):
        if not replaced and not in_fence and BILAN_LINE_RE.match(text_line):
            out.append(line)
            replaced = True
        else:
            out.append(text_line)
    if not replaced:
        if not any(l.strip().lower().startswith("## bilan") for l in out):
            out += ["", "## Bilan"]
        out.append(line)
    atomic_write(path, "\n".join(out) + "\n")
    return True


def findings_ok(path: str, pack: dict) -> bool:
    """Un fichier de verdicts est-il exploitable (présent, non vide, et il PASSE le
    parseur sans erreur fatale) ? Sert à la reprise (passe sautée), à l'affichage
    d'avancement et au rapport d'échec."""
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return False
    _data, fatal, _soft = parse_findings_file(path, pack)
    return not fatal


def findings_all_na(data) -> bool:
    """TOUS les verdicts (parsés) d'une passe sont NA."""
    return bool(data and data["verdicts"]) and \
        all(v["statut"] == "NA" for v in data["verdicts"].values())


def findings_all_c(data) -> bool:
    """Mode de remplissage DUAL du 100 % NA (H5) : verdicts massivement C (≥ 90 %),
    zéro constat, zéro AVM — le motif type de la fausse conformité fabriquée."""
    if not (data and data["verdicts"]):
        return False
    statuts = [v["statut"] for v in data["verdicts"].values()]
    return (statuts.count("C") / len(statuts) >= 0.9
            and not data["constats"] and "AVM" not in statuts)


def suspicious_all_c_passes(passes: list) -> list:
    """Passes SUSPECTES côté C (H5) : pack DÉCLENCHÉ mais verdicts massivement C sans
    le moindre constat ni AVM. Symétrique du 100 % NA : jamais de retry automatique,
    warning + annexe, l'arbitrage reste humain. Seuils à calibrer à l'usage."""
    suspicious = []
    for audit_pass in passes:
        if not audit_pass.get("declenche"):
            continue
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"],
                                                 audit_pass["pack"])
        if not fatal and findings_all_c(data):
            suspicious.append(audit_pass)
    return suspicious


def suspicious_c_verdicts(passes: list, sonde_hits: dict) -> list:
    """Confrontation AVAL des sondes (H3) : sonde positive sur un fichier du
    compartiment + verdict C sur le critère sondé = « verdict C suspect » —
    symétrique exact de l'anti rubber-stamping 100 % NA. Jamais de retry
    automatique : warning en annexe, l'arbitrage reste humain."""
    suspects = []
    for audit_pass in passes:
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"],
                                                 audit_pass["pack"])
        if fatal or data is None:
            continue
        pack_id = audit_pass["pack"]["id"]
        for path in audit_pass["bucket"]["files"]:
            for line, motif, crit, conf in (sonde_hits or {}).get((pack_id, path), []):
                verdict = data["verdicts"].get(crit)
                if verdict and verdict["statut"] == "C":
                    suspects.append({"pass": audit_pass, "critere": crit, "path": path,
                                     "line": line, "motif": motif, "confiance": conf})
    return suspects


def suspicious_all_na_passes(passes: list) -> list:
    """Passes SUSPECTES (anti rubber-stamping) : pack routé par DÉCLENCHEUR — ses motifs
    existent donc dans les fichiers du compartiment — mais 100 % des verdicts sont NA,
    incohérent par construction (un auditeur qui « remplit » pour finir). Les passes
    'toujours' venues sans déclencheur sont légitimes à 100 % NA (elles tournent pour
    constater l'absence) : hors du champ. Relit les fichiers de verdicts : couvre aussi
    les passes reprises d'un run précédent. Jamais de retry automatique : l'arbitrage
    reste humain (warning console + annexe du rapport)."""
    suspicious = []
    for audit_pass in passes:
        if not audit_pass.get("declenche"):
            continue
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"], audit_pass["pack"])
        if not fatal and findings_all_na(data):
            suspicious.append(audit_pass)
    return suspicious


# ─── PROMPTS DÉPORTÉS PAR FICHIER ─────────────────────────────────────────────

def build_carto_scope_block(scope_files: list) -> str:
    """Bloc « fichiers UI à assigner » du prompt cartographe, borné à
    MAX_SCOPE_FILES_IN_CARTO. Les fichiers listés sont un ÉCHANTILLON représentatif de
    tous les répertoires (code applicatif d'abord), pas les N premiers par ordre
    alphabétique — sur un monorepo, ces N premiers étaient 300 feuilles de style
    d'icônes et zéro fichier de src/. Le surplus est résumé par répertoire et assignable
    PAR RÉPERTOIRE."""
    listed = select_carto_sample(scope_files, MAX_SCOPE_FILES_IN_CARTO)
    block = "\n".join(f"- {f}" for f in listed) or "(aucun)"
    listed_set = set(listed)
    overflow = [f for f in scope_files if f not in listed_set]
    if overflow:
        block += (f"\n(⚠️ Périmètre de {len(scope_files)} fichiers : {len(listed)} listés ci-dessus "
                  f"(échantillon représentatif de tous les répertoires), {len(overflow)} non "
                  f"listé(s), résumés par répertoire ci-dessous. Assigne-les PAR RÉPERTOIRE : une "
                  f"entrée de files: dont le chemin se termine par '/' couvre tous les fichiers du "
                  f"périmètre qu'il contient (récursivement). Ce que tu n'assignes pas ira "
                  f"mécaniquement en zone « Divers », qui doit rester un résiduel — pas l'essentiel "
                  f"du projet.)\n"
                  + summarize_by_directory(overflow))
    return block


def doc_map_hint() -> str:
    """Indice OPTIONNEL tiré de la carte du pipeline documentation ('doc_map.yaml') :
    les noms de zones fonctionnelles déjà validés par un humain aident le cartographe
    à nommer les parcours — on ne transmet QUE les noms/intents, jamais les fichiers
    (le découpage a11y par écrans est un autre axe que le découpage fonctionnel)."""
    try:
        with open(DOC_MAP_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        zones = data.get("zones") if isinstance(data, dict) else None
        if not isinstance(zones, list) or not zones:
            return ""
        lines = [f"Une carte FONCTIONNELLE du projet existe ('{DOC_MAP_FILE}', autre pipeline). "
                 f"Ses zones peuvent t'inspirer les noms de parcours (SANS obligation : ton "
                 f"découpage par écrans prime) :"]
        for z in zones[:12]:
            if isinstance(z, dict) and z.get("name"):
                lines.append(f"- {z['name']} : {str(z.get('intent') or '')[:100]}")
        return "\n".join(lines)
    except Exception:
        return ""


def build_carto_prompt(grid_text: str, scope_files: list, feedback: str, attempt: int) -> str:
    sentinel = a11y_sentinel("map", attempt)
    hint = doc_map_hint()
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es le Cartographe d'interface d'un pipeline d'audit d'accessibilité découpé : tu
ASSIGNES chaque fichier UI fourni ci-dessous au SOCLE, aux COMPOSANTS partagés ou à une
ZONE d'écrans nommée. Tu n'audites RIEN (des passes dédiées s'en chargent ensuite) et tu
ne lis pas le projet en profondeur : survole les seuls fichiers dont le nom ne permet pas
de trancher.
AUDIT = LECTURE SEULE : tu ne modifies, ne corriges, ne crées AUCUN fichier du projet.
Tu n'écris QUE deux fichiers : '{A11Y_MAP_FILE}' à la racine, puis ta sentinelle de fin.

--- GRILLE DU CARTOGRAPHE ---
{grid_text}

--- FICHIERS UI À ASSIGNER ({len(scope_files)}, découverts par l'orchestrateur ; chemins à RECOPIER tels quels) ---
Une entrée de files: peut aussi être un RÉPERTOIRE (chemin terminé par '/', ex. "src/pages/") :
elle assigne au compartiment tous les fichiers du périmètre qu'il contient et qui ne sont pas
déjà assignés ailleurs. La zone « Divers » peut être omise ou déclarée vide : l'orchestrateur
y range mécaniquement ce que tu n'auras pas assigné.
{build_carto_scope_block(scope_files)}

--- INDICE (optionnel) ---
{hint or "(aucune carte fonctionnelle existante)"}

--- CONTEXTE MÉTIER (optionnel) ---
{business_context_hint()}

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- LIVRABLE OBLIGATOIRE ---
Écris la carte dans '{A11Y_MAP_FILE}' à la racine du projet : YAML PUR conforme à la grille
ci-dessus (AUCUNE balise ```, toutes les valeurs textuelles entre guillemets doubles,
chemins recopiés depuis la liste fournie). Fais-le directement via tes outils d'édition
de fichier, sans bavardage inutile dans la console.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{A11Y_MAP_FILE}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur. Ne le crée que lorsque la carte est VRAIMENT terminée.
"""
    with open(TMP_A11Y_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_A11Y_FILE}' à la racine du projet et réalise "
            f"la passe de cartographie d'interface.")


def build_bucket_files_block(bucket: dict) -> str:
    """Bloc « ton périmètre » du prompt d'audit : liste bornée (fenêtre de contexte)."""
    files = bucket["files"]
    listed = files[:MAX_BUCKET_FILES_IN_PROMPT]
    lines = [f"- {f}" for f in listed]
    overflow = len(files) - len(listed)
    if overflow > 0:
        lines.append(f"(+ {overflow} autre(s) fichier(s) non listé(s) : concentre-toi sur "
                     f"les fichiers ci-dessus.)")
    return "\n".join(lines)


def build_trigger_hits_block(audit_pass: dict, trigger_hits: dict) -> str:
    """Bloc « MOTIFS DÉTECTÉS » d'une passe DÉCLENCHÉE : le premier match du pack dans
    chaque fichier du compartiment (borné). Triple effet : ancrage (l'agent lit
    d'abord les bons fichiers), anti-NA-erroné (répondre 100 % NA alors que le prompt
    liste les motifs devient une contradiction visible), et l'arbitrage humain des
    passes suspectes a les hits sous les yeux. Un INDICE, jamais un verdict."""
    if not audit_pass.get("declenche") or not trigger_hits:
        return ""
    pack_id = audit_pass["pack"]["id"]
    lines = []
    for path in audit_pass["bucket"]["files"]:
        hit = trigger_hits.get((pack_id, path))
        if hit:
            lines.append(f"- {path}:{hit[0]} — motif « {hit[1]} »")
    if not lines:
        return ""
    shown = lines[:MAX_TRIGGER_HITS_IN_PROMPT]
    overflow = len(lines) - len(shown)
    if overflow > 0:
        shown.append(f"(+ {overflow} autre(s) fichier(s) déclencheur(s) non listé(s))")
    return ("\n--- MOTIFS DÉTECTÉS PAR L'ORCHESTRATEUR (scan mécanique : confirme ou infirme chacun) ---\n"
            "Ces fichiers de ton périmètre contiennent des motifs de TA thématique ; lis-les en priorité.\n"
            + "\n".join(shown) + "\n")


def build_sonde_hits_block(audit_pass: dict, sonde_hits: dict) -> str:
    """Bloc « SONDES NC » d'une passe (H3) : les indices quasi certains détectés dans
    les fichiers du compartiment pour CE pack. L'agent confirme ou infirme chacun —
    l'orchestrateur confrontera ses verdicts à ces indices (annexe C suspect)."""
    if not sonde_hits:
        return ""
    pack_id = audit_pass["pack"]["id"]
    lines = []
    for path in audit_pass["bucket"]["files"]:
        for line, motif, crit, conf in sonde_hits.get((pack_id, path), []):
            lines.append(f"- critère {crit} : {path}:{line} — motif « {motif} » (NC {conf})")
    if not lines:
        return ""
    shown = lines[:MAX_TRIGGER_HITS_IN_PROMPT]
    overflow = len(lines) - len(shown)
    if overflow > 0:
        shown.append(f"(+ {overflow} autre(s) indice(s) non listé(s))")
    return ("\n--- SONDES NC (indices mécaniques : confirme ou infirme CHACUN dans tes verdicts) ---\n"
            "Ces motifs quasi certains ont été détectés par l'orchestrateur ; un verdict C "
            "sur un critère sondé sans traiter l'indice sera marqué SUSPECT au rapport.\n"
            + "\n".join(shown) + "\n")


def build_auditor_prompt(audit_pass: dict, trunk_text: str, position: int, total: int,
                         contrast_block: str, feedback: str, attempt: int,
                         trigger_hits: dict = None, sonde_hits: dict = None) -> str:
    pack, bucket = audit_pass["pack"], audit_pass["bucket"]
    sentinel = a11y_sentinel(audit_pass["slot"], attempt)
    findings_file = audit_pass["findings_path"]
    criteria_line = ", ".join(pack["criteres"])
    hits_section = build_trigger_hits_block(audit_pass, trigger_hits or {})
    sonde_section = build_sonde_hits_block(audit_pass, sonde_hits or {})
    contrast_section = ""
    if pack["id"] == 3 and contrast_block:
        contrast_section = f"\n--- MESURES DE CONTRASTE (calculées mécaniquement par l'orchestrateur) ---\n{contrast_block}\n"
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Agent Auditeur accessibilité ultra-spécialisé, affecté à UNE SEULE thématique
RGAA : T{pack['id']} « {pack['nom']} », sur UN SEUL périmètre : {bucket['name']}.
C'est la passe {position}/{total} d'un audit d'accessibilité découpé.
AUDIT = LECTURE SEULE : tu ne modifies, ne corriges, ne crées AUCUN fichier du projet.
Tu n'écris QUE deux fichiers : ton fichier de verdicts, puis ta sentinelle de fin.
Ignore tout problème relevant d'une AUTRE thématique ou d'un AUTRE périmètre : une passe
dédiée s'en charge (le signaler ici créerait des doublons dans le rapport).

--- GRILLE D'AUDIT (tronc commun : statuts, règles de fer, format de sortie) ---
{trunk_text}

--- TON PACK THÉMATIQUE ---
{pack['grid_text']}

--- TON PÉRIMÈTRE : {bucket['name']} ({len(bucket['files'])} fichier(s), assignés par la cartographie validée par l'humain) ---
Rôle annoncé (intent) : {bucket['intent'] or '(non renseigné)'}
{build_bucket_files_block(bucket)}
{hits_section}{sonde_section}{contrast_section}
--- CONTEXTE MÉTIER (optionnel) ---
{business_context_hint()}

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- LIVRABLE OBLIGATOIRE ---
Écris tes verdicts dans '{findings_file}' (crée le dossier '{A11Y_DIR}/' au besoin) en
respectant STRICTEMENT le format du tronc commun :
- première ligne : '# T{pack['id']} : {pack['nom']} — {bucket['name']}' ;
- section '## Verdicts' : un verdict (C, NC, NA ou AVM) pour CHACUN de ces critères,
  dans cet ordre, aucun autre : {criteria_line} ;
- section '## Constats' : un bloc '### K<i> — <critère> — <titre>' par non-conformité
  (ou la seule ligne « Aucun constat. ») ;
- section '## Bilan' : la ligne verrouillée '- Verdicts : C : <a>, NC : <b>, NA : <c>, AVM : <d>'.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{findings_file}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur. Ne le crée que lorsque le fichier de verdicts est VRAIMENT terminé.
"""
    with open(TMP_A11Y_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_A11Y_FILE}' à la racine du projet et réalise "
            f"la passe d'audit T{pack['id']} × {bucket['label']}.")


def build_synthesis_prompt(stats: dict, top_ncs: list, feedback: str, attempt: int) -> str:
    sentinel = a11y_sentinel("synthese", attempt)
    topics_lines = "\n".join(
        f"- T{t['id']:02d} {t['nom']} : C {t['C']}, NC {t['NC']}, NA {t['NA']}, AVM {t['AVM']}"
        for t in stats["topics"])
    ncs_lines = "\n".join(
        f"- Critère {n['critere']} ({n['nom_pack']}), impact {n['impact'] if n['impact'] else '?'} : {n['titre']}"
        for n in top_ncs) or "(aucune non-conformité relevée)"
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Lead accessibilité chargé de RÉDIGER la synthèse exécutive d'un pré-audit RGAA
réalisé en passes indépendantes. Tu ne réaudites RIEN et tu ne relis PAS le code du
projet : tu rédiges 8 à 15 lignes lisibles par une direction, UNIQUEMENT à partir des
chiffres et constats ci-dessous. ZÉRO invention : aucun chiffre ni constat qui ne figure
pas ici. Tu ne modifies aucun fichier du projet ; tu n'écris QUE la synthèse, puis ta sentinelle.

--- CHIFFRES AGRÉGÉS (calculés mécaniquement, ils font foi) ---
Critères RGAA 4.1.2 : {stats['totals']['C']} conformes, {stats['totals']['NC']} non conformes,
{stats['totals']['NA']} non applicables, {stats['totals']['AVM']} à vérifier manuellement.
Conformité démontrable : {stats['rate_central']} % (fourchette {stats['rate_floor']} % à {stats['rate_ceiling']} %
selon l'issue des vérifications manuelles).

Par thématique :
{topics_lines}

--- PRINCIPALES NON-CONFORMITÉS (impact décroissant) ---
{ncs_lines}

--- LIVRABLE À PRODUIRE : '{SYNTHESIS_FILE}' ---
Structure OBLIGATOIRE : le fichier commence EXACTEMENT par la ligne '## Synthèse exécutive'
puis 8 à 15 lignes : état général, les 2 ou 3 chantiers prioritaires (appuie-toi sur les
non-conformités à plus fort impact), la part de vérifications manuelles restantes, et le
rappel en une phrase que ceci est un pré-audit statique (pas une attestation).

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{SYNTHESIS_FILE}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done).
"""
    with open(TMP_A11Y_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_A11Y_FILE}' à la racine du projet et rédige "
            f"la synthèse exécutive de l'audit.")


# ─── ÉTAPE É1 : CARTOGRAPHIE (1 PASSE LLM + DOUBLE VALIDATION) ────────────────

def print_a11y_map_recap(a11y_map: dict, soft: list, packs: list, triggers: dict):
    """Récapitulatif humain de la carte ET de la matrice des passes qu'elle implique :
    c'est le décompte EXACT de ce qui sera payé, affiché AVANT le y/n."""
    buckets = build_buckets(a11y_map)
    passes = build_pass_list(buckets, packs, triggers)
    mechanical = mechanical_na_passes(passes)
    paid = [p for p in passes if pass_needs_agent(p)]
    per_bucket = {}
    for p in paid:
        per_bucket.setdefault(p["bucket"]["label"], []).append(f"T{p['pack']['id']:02d}")
    print(f"\n{'='*60}")
    print(f"🗺️  CARTE D'INTERFACE — {a11y_map.get('project', '(sans nom)')} : "
          f"{len(buckets)} compartiment(s), {len(paid)} passe(s) d'audit"
          + (f" (+ {len(mechanical)} tranche(s) NA mécanique(s), sans agent)" if mechanical else ""))
    print(f"{'Compartiment':<34} | {'Fichiers':>8} | Packs audités")
    print(f"{'-'*34}-+-{'-'*8}-+-{'-'*30}")
    for bucket in buckets:
        packs_label = " ".join(per_bucket.get(bucket["label"], [])) or "(aucun)"
        print(f"{bucket['name'][:34]:<34} | {len(bucket['files']):>8} | {packs_label}")
    skipped = skipped_packs(passes, packs)
    if skipped:
        print(f"\n   ⏭️  Pack(s) jamais déclenché(s) (critères déclarés NA mécaniquement) : "
              + ", ".join(f"T{p['id']:02d} {p['nom']}" for p in skipped))
    if soft:
        print(f"\n⚠️  Points d'attention (non bloquants) :")
        for warning in soft:
            print(f"   - {warning}")
    print(f"\n   ✏️  La carte est ÉDITABLE : '{A11Y_MAP_FILE}' (l'ordre des zones = l'ordre "
          f"de lecture du rapport ; déplace un fichier de compartiment pour changer le routage).")
    print(f"{'='*60}")


def confirm_a11y_map(a11y_map: dict, soft: list, packs: list, triggers: dict):
    """Validation humaine de la carte (le y/n qui arbitre AVANT de payer N passes)."""
    print_a11y_map_recap(a11y_map, soft, packs, triggers)
    confirm = input("\n▶️  Valider cette carte et lancer le pré-audit d'accessibilité ? (y/n) : ")
    mm_audit.event("gate", id="map", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Arrêt. Édite '{A11Y_MAP_FILE}' puis relance (il sera repris tel quel), "
              f"ou supprime-le pour rejouer la cartographie.")
        RUNNER.kill()
        sys.exit(0)


def run_cartography(grid_text: str, scope_files: list, packs: list, triggers: dict) -> dict:
    """Étape É1 : produit (ou reprend) la carte d'interface, doublement validée.

    Reprise : un 'a11y_map.yaml' existant et valide saute la passe LLM (récapitulatif +
    y/n de nouveau affichés — c'est là que l'édition manuelle du YAML est prise en
    compte) ; un fichier existant mais structurellement invalide arrête le run avec
    consigne (corriger ou supprimer), même contrat que le blackboard.
    """
    if os.path.exists(A11Y_MAP_FILE):
        a11y_map, fatal, soft, parse_error = load_and_validate_map_file(scope_files)
        if parse_error:
            fail_a11y(f"❌ '{A11Y_MAP_FILE}' existant mais non parsable : corrige-le ou "
                      f"supprime-le (la cartographie sera rejouée), puis relance.",
                      details=parse_error[:1500], title="Carte existante invalide")
        if fatal:
            fail_a11y(f"❌ '{A11Y_MAP_FILE}' existant mais structurellement invalide :\n   - "
                      + "\n   - ".join(fatal)
                      + f"\n   → Corrige-le ou supprime-le (la cartographie sera rejouée), puis relance.",
                      details="\n".join(fatal), title="Carte existante invalide")
        save_a11y_map(a11y_map)
        print(f"♻️  '{A11Y_MAP_FILE}' existant et valide : cartographie sautée (reprise).")
        # Carte écrite APRÈS l'arrêt d'un run resté sans clôture : livrable d'un agent
        # orphelin, à relire avant de la reprendre comme valide.
        residual = residual_deliverable_warning(A11Y_MAP_FILE, "pre-audit-a11y")
        if residual:
            soft = list(soft) + [residual]
        confirm_a11y_map(a11y_map, soft, packs, triggers)
        return a11y_map

    print(f"\n{'='*50}\n🗺️  ÉTAPE É1 : CARTOGRAPHIE D'INTERFACE (1 passe LLM)\n{'='*50}")
    if len(scope_files) > MAX_SCOPE_FILES_IN_CARTO:
        print(f"   ⚠️  Périmètre de {len(scope_files)} fichiers > {MAX_SCOPE_FILES_IN_CARTO} : le "
              f"surplus sera résumé par répertoire dans le prompt et rangé en zone « Divers » "
              f"par le contrôle de couverture (dégradation assumée).")
    RUNNER.start()

    attempts = 0
    a11y_map, soft = None, []
    error_history = []   # échecs des tentatives précédentes (feedback cumulatif)

    while a11y_map is None and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Rattrapage d'un livrable TARDIF : l'agent de la tentative précédente a pu finir
        # d'écrire APRÈS le timeout de l'orchestrateur. Si sa carte est devenue valide
        # entre-temps, on la prend telle quelle plutôt que de payer un tour pour tout refaire.
        if attempts > 1 and os.path.exists(A11Y_MAP_FILE):
            late_map, late_fatal, late_soft, late_err = load_and_validate_map_file(scope_files)
            if not late_err and not late_fatal:
                print(f"   ♻️  '{A11Y_MAP_FILE}' est finalement arrivé (livrable tardif) : accepté.")
                a11y_map, soft = late_map, late_soft
                break

        cleanup_slot_sentinels("map")
        print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Lancement du Cartographe d'interface...")

        prompt = build_carto_prompt(grid_text, scope_files,
                                    compose_retry_feedback(error_history), attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable, wait_reason = wait_for_deliverable(
            A11Y_MAP_FILE, a11y_sentinel("map", attempts),
            structural_check=map_structural_check)
        # Garde read-only après CHAQUE tentative (aboutie ou non) : un cartographe qui a
        # « corrigé » du code en cours de route est restauré immédiatement.
        enforce_readonly("Carto")

        if not got_deliverable:
            if wait_reason == "sentinelle_sans_livrable":
                error_history.append(
                    f"Au passage précédent, tu as créé la sentinelle SANS le livrable : "
                    f"écris '{A11Y_MAP_FILE}' complet D'ABORD, la sentinelle en toute "
                    f"DERNIÈRE action.")
            elif wait_reason == "stable_hors_format":
                error_history.append(
                    f"Au passage précédent, '{A11Y_MAP_FILE}' est resté hors format : "
                    f"le YAML doit être parsable, avec une liste 'zones' non vide. "
                    f"Réécris le fichier entièrement, puis crée la sentinelle.")
            else:
                error_history.append(
                    f"Au passage précédent, aucun livrable n'a été reçu ('{A11Y_MAP_FILE}' "
                    f"absent, vide ou jamais signalé). Écris d'abord la carte YAML complète, "
                    f"PUIS la sentinelle, dans cet ordre.")
            print(f"⏱️  Le cartographe n'a pas signalé la fin de sa passe. Nouvelle tentative.")
            if os.path.exists(A11Y_MAP_FILE) and not map_structural_check(A11Y_MAP_FILE):
                try:
                    os.remove(A11Y_MAP_FILE)
                except OSError:
                    pass
            reset_agent_session()
            continue

        candidate, fatal, cand_soft, parse_error = load_and_validate_map_file(scope_files)
        if parse_error:
            error_history.append(
                f"Ton '{A11Y_MAP_FILE}' n'est pas du YAML parsable "
                f"(erreur : {parse_error[:400]}). Rappels : AUCUNE balise ```, toutes "
                f"les valeurs textuelles entre guillemets doubles, guillemets internes "
                f"échappés (\\\"). Réécris le fichier entièrement.")
            print(f"⚠️  [REJET] Tentative {attempts} : YAML non parsable.")
        elif fatal:
            error_history.append(
                "Ta carte ne respecte pas le schéma de la grille : "
                + " ; ".join(fatal)
                + " Rappels : chemins RECOPIÉS depuis la liste fournie (jamais "
                  "inventés), chaque zone avec un id entier unique, un name et au "
                  "moins un fichier existant. Réécris le fichier entièrement.")
            print(f"⚠️  [REJET] Tentative {attempts} : carte structurellement invalide "
                  f"({len(fatal)} anomalie(s)).")
        elif len(divers_files(candidate)) > DIVERS_RETRY_THRESHOLD and attempts < MAX_ATTEMPTS:
            # Une « Divers » qui contient l'essentiel du projet n'est pas une cartographie :
            # on rejoue tant qu'il reste des tentatives, en nommant les répertoires à assigner.
            overflow = len(divers_files(candidate))
            error_history.append(
                f"Ta carte laisse {overflow} fichiers en zone « Divers » (résiduel), soit "
                f"l'essentiel du projet : ce n'est pas un découpage par écrans. Assigne-les au "
                f"socle, aux composants ou à des zones d'écrans nommées, PAR RÉPERTOIRE (entrée "
                f"de files: terminée par '/'). Répertoires concernés :\n"
                + summarize_by_directory(divers_files(candidate)))
            print(f"⚠️  [REJET] Tentative {attempts} : {overflow} fichiers en « Divers » "
                  f"(> {DIVERS_RETRY_THRESHOLD}) — la carte ne découpe pas le projet.")
        else:
            a11y_map, soft = candidate, cand_soft
            break

        try:
            os.remove(A11Y_MAP_FILE)
        except OSError:
            pass
        reset_agent_session()

    if a11y_map is None:
        cleanup_all_a11y_sentinels()
        reason = compose_retry_feedback(error_history)
        print_pass_failure("Cartographie", reason)
        fail_a11y(f"❌ Cartographie non aboutie après {MAX_ATTEMPTS} tentatives.", details=reason)

    cleanup_slot_sentinels("map")
    save_a11y_map(a11y_map)
    # Contexte réinitialisé avant la première passe d'audit : la conversation du
    # cartographe ne doit pas fuiter dans les passes suivantes.
    reset_agent_session()
    confirm_a11y_map(a11y_map, soft, packs, triggers)
    return a11y_map


# ─── ÉTAPE É2 : LES PASSES D'AUDIT (UNE PAR PACK × COMPARTIMENT) ──────────────

def warn_orphan_findings(passes: list):
    """Fichiers de 'pre_audit_a11y/' ne correspondant à aucune passe de la matrice (carte
    rééditée à la main, p. ex.) : signalés en début d'étape, JAMAIS supprimés (décision
    humaine) ; ils ne seront PAS agrégés."""
    if not os.path.isdir(A11Y_DIR):
        return
    expected = {os.path.basename(p["findings_path"]) for p in passes}
    expected.add(os.path.basename(SYNTHESIS_FILE))
    orphans = sorted(name for name in os.listdir(A11Y_DIR)
                     if name.endswith(".md") and name not in expected)
    if orphans:
        print(f"⚠️  Fichier(s) orphelin(s) dans '{A11Y_DIR}/' (aucune passe de la matrice ne "
              f"les produit — carte rééditée ?) : {', '.join(orphans)}. Non supprimés ; ils ne "
              f"seront PAS agrégés.")


def pass_failure_breaker(consecutive: int, failed_count: int, treated: int) -> bool:
    """Le circuit breaker doit-il arrêter le run ? Fonction PURE (testée unitairement) :
    échecs consécutifs, ou ratio d'échecs parmi les passes traitées — mais jamais sur
    un échec isolé (le ratio n'est armé qu'à partir de 2 échecs)."""
    return (consecutive >= MAX_CONSECUTIVE_PASS_FAILURES
            or (failed_count >= 2 and failed_count > MAX_PASS_FAILURE_RATIO * treated))


def run_audit_passes(passes: list, trunk_text: str, contrast_block: str,
                     trigger_hits: dict = None, sonde_hits: dict = None) -> list:
    """Le cœur MAIsterMind : une session neuve par passe, une tranche de contexte par
    passe (tronc commun + UN pack + UN compartiment), un parseur en plancher fort.

    Renvoie la liste des passes NON ABOUTIES (vide au nominal) : les passes étant
    indépendantes, un échec ne tue plus le run — sauf circuit breaker (échecs
    consécutifs ou ratio d'échecs, cf. constantes)."""
    total = len(passes)
    warn_orphan_findings(passes)
    failed = []           # passes non abouties : [{"label", "findings_path", "reason"}]
    consecutive_failures = 0

    for position, audit_pass in enumerate(passes, start=1):
        findings_file = audit_pass["findings_path"]
        pack = audit_pass["pack"]
        slot = audit_pass["slot"]

        # Reprise par fichiers : un fichier de verdicts qui PASSE LE PARSEUR saute sa passe.
        if findings_ok(findings_file, pack):
            print(f"⏭️  Passe {position}/{total} ({audit_pass['label']}) déjà auditée "
                  f"('{findings_file}') : sautée.")
            continue
        if os.path.exists(findings_file):
            # Résidu à moitié écrit ou hors format d'un run interrompu : on repart proprement.
            try:
                os.remove(findings_file)
                print(f"🧹 '{findings_file}' résiduel (incomplet ou hors format) supprimé : "
                      f"la passe est rejouée.")
            except OSError:
                pass

        if not pass_needs_agent(audit_pass):
            # Tranche sans motif du pack : verdicts NA par routage déterministe, écrits au
            # format des passes d'agent — aucun tour de LLM payé, tracé en annexe.
            write_mechanical_na_findings(audit_pass)
            print(f"⏭️  Passe {position}/{total} ({audit_pass['label']}) : aucun motif du pack "
                  f"dans cette tranche → NA mécanique ('{findings_file}'), aucun agent sollicité.")
            continue

        print(f"\n{'='*50}\n🔎 PASSE {position}/{total} : {audit_pass['label']}\n{'='*50}")

        attempts = 0
        success = False
        error_history = []   # échecs des tentatives précédentes (feedback cumulatif)

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1

            # Rattrapage d'un livrable TARDIF : l'agent de la tentative précédente a pu
            # finir d'écrire APRÈS le timeout de l'orchestrateur.
            if attempts > 1 and findings_ok(findings_file, pack):
                print(f"   ♻️  '{findings_file}' est finalement arrivé (livrable tardif) : accepté.")
                success = True
                break

            cleanup_slot_sentinels(slot)
            print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] {audit_pass['label']} — "
                  f"lancement de l'Auditeur accessibilité...")

            prompt = build_auditor_prompt(audit_pass, trunk_text, position, total,
                                          contrast_block,
                                          compose_retry_feedback(error_history), attempts,
                                          trigger_hits, sonde_hits)
            mm_audit.event("agent_task", prompt_bytes=len(prompt))
            RUNNER.send_task(prompt)

            got_deliverable, wait_reason = wait_for_deliverable(
                findings_file, a11y_sentinel(slot, attempts),
                structural_check=findings_structural_check)
            # Garde read-only après CHAQUE tentative (aboutie ou non) : un auditeur qui a
            # « corrigé » du code en cours de route est restauré immédiatement.
            enforce_readonly(audit_pass["label"])

            if not got_deliverable:
                if wait_reason == "sentinelle_sans_livrable":
                    error_history.append(
                        f"Au passage précédent, tu as créé la sentinelle SANS le fichier "
                        f"de verdicts : écris '{findings_file}' complet D'ABORD, la "
                        f"sentinelle en toute DERNIÈRE action.")
                elif wait_reason == "stable_hors_format":
                    error_history.append(
                        f"Au passage précédent, '{findings_file}' est resté hors format : "
                        f"les sections '## Verdicts', '## Constats' et '## Bilan' sont "
                        f"OBLIGATOIRES. Réécris le fichier entièrement au format "
                        f"verrouillé du tronc commun, puis crée la sentinelle.")
                    # Résidu hors format : on repart proprement (même hygiène que le
                    # rejet du parseur).
                    try:
                        os.remove(findings_file)
                    except OSError:
                        pass
                else:
                    error_history.append(
                        "Au passage précédent, aucun livrable n'a été reçu (fichier de "
                        "verdicts absent, vide ou jamais signalé). Écris d'abord le fichier "
                        "de verdicts complet, PUIS la sentinelle, dans cet ordre.")
                print(f"⏱️  L'auditeur n'a pas signalé la fin de la passe. Nouvelle tentative.")
                reset_agent_session()
                continue

            # Plancher FORT après coup : le parseur de verdicts. Ses erreurs deviennent le
            # feedback de la tentative suivante (le format verrouillé est réexpliqué).
            _data, fatal, soft = parse_findings_file(findings_file, pack)
            if fatal and _data is not None and bilan_only_fatals(fatal) \
                    and repair_bilan_line(findings_file, _data["verdicts"]):
                # Seule anomalie : le Bilan (checksum sans information). Python le
                # réécrit depuis les verdicts parsés et re-parse : la passe n'est pas
                # rejouée — aucun risque, les verdicts ont passé tous les autres contrôles.
                print(f"   🔧 Bilan réparé mécaniquement ({' ; '.join(fatal[:2])}) : "
                      f"les verdicts ont passé tous les autres contrôles, la passe "
                      f"n'est pas rejouée.")
                _data, fatal, soft = parse_findings_file(findings_file, pack)
            if fatal:
                error_history.append(
                    f"Ton fichier '{findings_file}' ne passe pas le contrôle mécanique : "
                    + " ; ".join(fatal[:6])
                    + f". Rappels : la section '## Verdicts' liste EXACTEMENT ces critères "
                      f"({', '.join(pack['criteres'])}) avec un statut C, NC, NA ou AVM ; "
                      f"chaque NC a un constat '### K<i> — <critère> — <titre>' avec son champ "
                      f"'- **Extrait :**' recopiant EXACTEMENT une ligne du fichier cité ; le Bilan "
                      f"est verrouillé '- Verdicts : C : <a>, NC : <b>, NA : <c>, AVM : <d>'. "
                      f"Réécris le fichier entièrement.")
                try:
                    os.remove(findings_file)
                except OSError:
                    pass
                print(f"⚠️  [REJET] Tentative {attempts} : verdicts hors format "
                      f"({len(fatal)} anomalie(s) : {' ; '.join(fatal[:3])}).")
                reset_agent_session()
                continue
            if soft:
                print(f"   ℹ️  Imperfections tolérées ({len(soft)}) : {' ; '.join(soft[:3])}"
                      + ("…" if len(soft) > 3 else ""))
            if audit_pass.get("declenche") and findings_all_c(_data):
                print(f"   ⚠️  PASSE SUSPECTE (C) : verdicts massivement C sans le moindre "
                      f"constat ni AVM sur un pack DÉCLENCHÉ — fausse conformité possible. "
                      f"Pas de retry automatique (l'arbitrage reste humain) : relis "
                      f"'{findings_file}' ; le rapport la signale en annexe.")
            if audit_pass.get("declenche") and findings_all_na(_data):
                print(f"   ⚠️  PASSE SUSPECTE : ce pack a été routé par DÉCLENCHEUR (ses motifs "
                      f"existent dans les fichiers du compartiment) mais 100 % des verdicts "
                      f"sont NA — incohérent par construction. Pas de retry automatique "
                      f"(l'arbitrage reste humain) : relis '{findings_file}' et supprime-le "
                      f"pour rejouer la passe si besoin ; le rapport la signale en annexe.")

            success = True

        if not success:
            # Les passes sont indépendantes : on MARQUE l'échec et on CONTINUE (les
            # critères de cette passe retomberont en AVM prudent à l'agrégation, le
            # rapport final sera marqué PARTIEL) — sauf circuit breaker ci-dessous.
            reason = compose_retry_feedback(error_history)
            failed.append({"label": audit_pass["label"],
                           "findings_path": findings_file, "reason": reason})
            consecutive_failures += 1
            print_pass_failure(audit_pass["label"], reason)
            if pass_failure_breaker(consecutive_failures, len(failed), position):
                cleanup_all_a11y_sentinels()
                fail_a11y(f"❌ Circuit breaker : {len(failed)} passe(s) non abouties "
                          f"(dont {consecutive_failures} consécutive(s)) sur {position} "
                          f"traitée(s) — le modèle cale systématiquement, arrêt avant les "
                          f"{total - position} passe(s) restante(s).",
                          details="\n".join(f"- {f['label']} : {f['reason']}" for f in failed),
                          title="Circuit breaker de l'audit")
            print(f"⚠️  Passe {position}/{total} non aboutie : le run CONTINUE (passes "
                  f"indépendantes). Ses critères sortiront en AVM prudent ; relance le "
                  f"pipeline après le run pour la rejouer.")
            cleanup_slot_sentinels(slot)
            continue

        consecutive_failures = 0
        print(f"✅ Passe {position}/{total} terminée : verdicts dans '{findings_file}'.")
        cleanup_slot_sentinels(slot)
        reset_agent_session()
    return failed


# ─── ÉTAPE É4 (CALCUL) : AGRÉGATION 100 % PYTHON ──────────────────────────────
# Le « compilateur » final : consolidation des verdicts par critère (NC > AVM > C > NA),
# taux en fourchette, constats recopiés — zéro LLM, zéro perte, écriture atomique.

CONSOLIDATION_ORDER = {"NC": 0, "AVM": 1, "C": 2, "NA": 3}


def criteria_sort_key(critere: str):
    return [int(x) for x in critere.split(".")]


def aggregate(passes: list, packs: list) -> dict:
    """Relit tous les fichiers de verdicts (déjà validés par le parseur) et consolide.

    Renvoie {"criteria": {critère: {...}}, "topics": [...], "totals": {...},
             "rate_floor"/"rate_central"/"rate_ceiling", "top_ncs": [...],
             "unreadable": [...]} — la matière unique du rapport et de la synthèse.
    """
    criteria = {}
    for pack in packs:
        for crit in pack["criteres"]:
            criteria[crit] = {"statut": None, "notes": [], "constats": [],
                              "impact_max": None, "pack": pack, "passes": 0}

    unreadable = []
    requalified_manual_c = 0   # C requalifiés AVM par la règle de testabilité (H4)
    seen_constats = set()   # (critère, localisation) déjà rapportés : un même défaut vu
                            # par deux passes (composant partagé) n'apparaît qu'une fois
    for audit_pass in passes:
        pack = audit_pass["pack"]
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"], pack)
        if fatal or data is None:
            # Ne devrait pas arriver (les passes garantissent le parseur) : un fichier
            # dégradé APRÈS coup est signalé, ses critères retombent en AVM prudent.
            unreadable.append(audit_pass["label"])
            for crit in pack["criteres"]:
                entry = criteria[crit]
                entry["passes"] += 1
                if entry["statut"] is None or CONSOLIDATION_ORDER["AVM"] < CONSOLIDATION_ORDER[entry["statut"]]:
                    entry["statut"] = "AVM"
                entry["notes"].append(f"verdicts illisibles à l'agrégation ({audit_pass['label']}) : "
                                      f"supprime '{audit_pass['findings_path']}' et relance")
            continue
        for crit, verdict in data["verdicts"].items():
            entry = criteria[crit]
            entry["passes"] += 1
            statut = verdict["statut"]
            # Règle de fer de la grille, appliquée par le CODE (H4) : un C sur un
            # critère à testabilité « manuelle » n'est pas démontrable statiquement —
            # requalifié AVM prudent, avec note visible. NC (constaté, localisé) et NA
            # restent acceptés tels quels.
            if statut == "C" and pack["testabilite"].get(crit) == "manuelle":
                statut = "AVM"
                requalified_manual_c += 1
                entry["notes"].append(f"C non démontrable statiquement (testabilité : "
                                      f"manuelle) : requalifié AVM "
                                      f"({audit_pass['bucket']['label']})")
            if entry["statut"] is None or CONSOLIDATION_ORDER[statut] < CONSOLIDATION_ORDER[entry["statut"]]:
                entry["statut"] = statut
            if verdict["note"] and statut in ("AVM", "NA"):
                entry["notes"].append(f"{verdict['note']} ({audit_pass['bucket']['label']})")
        for constat in data["constats"]:
            entry = criteria[constat["critere"]]
            key = (constat["critere"], constat["localisation"])
            if constat["localisation"] and key in seen_constats:
                continue  # même défaut déjà rapporté par une autre passe (composant partagé)
            seen_constats.add(key)
            constat = dict(constat)
            constat["origine"] = audit_pass["bucket"]["name"]
            entry["constats"].append(constat)
            if constat["impact"] is not None:
                entry["impact_max"] = max(entry["impact_max"] or 0, constat["impact"])

    # Critères jamais couverts par une passe : NA mécanique (pack non déclenché ou
    # compartiments vides) — la raison apparaît dans le rapport, jamais un trou silencieux.
    for crit, entry in criteria.items():
        if entry["statut"] is None:
            if entry["pack"]["toujours"]:
                # H10 (a minima) : la garantie 'toujours' ne vit que sur le socle — un
                # socle vide la laissait muette, et ces critères STRUCTURELS (lang,
                # <title>, lien d'évitement) sortaient « NA » à tort alors que
                # l'ABSENCE des motifs est précisément le défaut potentiel.
                entry["statut"] = "AVM"
                entry["notes"].append("pack structurel jamais exécuté (socle vide ou sans "
                                      "déclencheur) : vérifier le document hôte")
            else:
                entry["statut"] = "NA"
                entry["notes"].append("aucun déclencheur de ce pack détecté dans le périmètre "
                                      "(routage déterministe) : contenu absent")

    topics = []
    for pack in packs:
        counts = {s: 0 for s in STATUSES}
        for crit in pack["criteres"]:
            counts[criteria[crit]["statut"]] += 1
        topics.append({"id": pack["id"], "nom": pack["nom"], **counts})

    totals = {s: sum(t[s] for t in topics) for s in STATUSES}

    def rate(numerator, denominator):
        return round(100.0 * numerator / denominator, 1) if denominator else 100.0

    c, nc, avm = totals["C"], totals["NC"], totals["AVM"]
    stats = {
        "criteria": criteria,
        "topics": topics,
        "totals": totals,
        "rate_central": rate(c, c + nc),
        "rate_floor": rate(c, c + nc + avm),
        "rate_ceiling": rate(c + avm, c + nc + avm),
        "unreadable": unreadable,
        "requalified_manual_c": requalified_manual_c,
    }

    ncs = [{"critere": crit, "nom_pack": entry["pack"]["nom"],
            "impact": entry["impact_max"],
            "titre": (entry["constats"][0]["titre"] if entry["constats"] else "(constat non titré)")}
           for crit, entry in criteria.items() if entry["statut"] == "NC"]
    ncs.sort(key=lambda n: (-(n["impact"] or 0), criteria_sort_key(n["critere"])))
    stats["top_ncs"] = ncs[:10]
    return stats


# ─── ÉTAPE É3 : SYNTHÈSE EXÉCUTIVE (LLM COURT, FALLBACK MÉCANIQUE) ────────────

def mechanical_synthesis(stats: dict) -> str:
    """Fallback 100 % Python de la synthèse : l'échec du chapeau ne doit jamais
    invalider N passes réussies — le contenu de valeur est déjà agrégé."""
    totals = stats["totals"]
    lines = ["## Synthèse exécutive", "",
             f"Pré-audit statique RGAA 4.1.2 : {totals['C']} critère(s) conforme(s), "
             f"{totals['NC']} non conforme(s), {totals['NA']} non applicable(s) et "
             f"{totals['AVM']} à vérifier manuellement. Conformité démontrable : "
             f"{stats['rate_central']} % (fourchette {stats['rate_floor']} % à "
             f"{stats['rate_ceiling']} % selon l'issue des vérifications manuelles).",
             ""]
    if stats["top_ncs"]:
        lines.append("Chantiers prioritaires (non-conformités à plus fort impact) :")
        for n in stats["top_ncs"][:3]:
            lines.append(f"- Critère {n['critere']} ({n['nom_pack']}) : {n['titre']}")
        lines.append("")
    lines.append("(Synthèse générée mécaniquement : la passe de rédaction n'a pas abouti. "
                 "Ce document est un pré-audit statique, pas une attestation de conformité.)")
    return "\n".join(lines) + "\n"


def run_synthesis(stats: dict):
    """Étape É3 : le seul contenu du rapport qui demande une vraie rédaction transverse —
    court, donc confiable à un agent sans risque de saturation. TOUJOURS rejouée (elle
    doit refléter les verdicts à jour) ; NON bloquante (fallback mécanique)."""
    print(f"\n{'='*50}\n🪧 ÉTAPE É3 : SYNTHÈSE EXÉCUTIVE (CHAPEAU DU RAPPORT)\n{'='*50}")

    if os.path.exists(SYNTHESIS_FILE):
        try:
            os.remove(SYNTHESIS_FILE)
            print(f"   🧹 '{SYNTHESIS_FILE}' résiduel supprimé (la synthèse est régénérée).")
        except OSError:
            pass

    attempts = 0
    success = False
    error_history = []   # échecs des tentatives précédentes (feedback cumulatif)
    while not success and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Rattrapage d'un livrable TARDIF (même logique que les autres passes).
        if attempts > 1 and os.path.exists(SYNTHESIS_FILE) \
                and os.path.getsize(SYNTHESIS_FILE) > 0 \
                and synthesis_structural_check(SYNTHESIS_FILE):
            print(f"   ♻️  '{SYNTHESIS_FILE}' est finalement arrivé (livrable tardif) : accepté.")
            success = True
            break

        cleanup_slot_sentinels("synthese")
        print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Lancement du Rédacteur de la synthèse...")

        prompt = build_synthesis_prompt(stats, stats["top_ncs"],
                                        compose_retry_feedback(error_history), attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable, wait_reason = wait_for_deliverable(
            SYNTHESIS_FILE, a11y_sentinel("synthese", attempts),
            structural_check=synthesis_structural_check)
        enforce_readonly("Synthèse")

        if not got_deliverable or not synthesis_structural_check(SYNTHESIS_FILE):
            if os.path.exists(SYNTHESIS_FILE) and not synthesis_structural_check(SYNTHESIS_FILE):
                try:
                    os.remove(SYNTHESIS_FILE)
                except OSError:
                    pass
            if wait_reason == "sentinelle_sans_livrable":
                error_history.append(
                    f"Au passage précédent, tu as créé la sentinelle SANS la synthèse : "
                    f"écris '{SYNTHESIS_FILE}' D'ABORD, la sentinelle en toute DERNIÈRE "
                    f"action.")
            else:
                error_history.append(
                    f"Au passage précédent, la synthèse était absente ou hors format : "
                    f"le fichier '{SYNTHESIS_FILE}' doit commencer EXACTEMENT par la ligne "
                    f"'## Synthèse exécutive' (8 à 15 lignes au total).")
            print("⏱️  Synthèse absente ou hors format. Nouvelle tentative.")
            reset_agent_session()
            continue
        success = True

    cleanup_slot_sentinels("synthese")
    if not success:
        # DÉGRADATION GRACIEUSE (même contrat que la vue d'ensemble de la documentation) :
        # l'échec du chapeau ne doit pas invalider N passes réussies — fallback mécanique.
        print(f"⚠️  Synthèse non aboutie après {MAX_ATTEMPTS} tentatives : fallback MÉCANIQUE. "
              f"Le contenu de valeur est déjà dans les verdicts agrégés.")
        with open(SYNTHESIS_FILE, "w", encoding="utf-8") as f:
            f.write(mechanical_synthesis(stats))
        reset_agent_session()
        return

    print(f"✅ Synthèse exécutive prête : '{SYNTHESIS_FILE}'.")


# ─── ÉTAPE É4 (LIVRABLES) : RAPPORT & SYNTHÈSE (PYTHON, ÉCRITURE ATOMIQUE) ───

IMPACT_LABELS = {4: "Bloquant", 3: "Majeur", 2: "Modéré", 1: "Mineur", None: "Impact non renseigné"}


def escape_md_cell(text: str) -> str:
    """Neutralise les barres verticales dans une cellule de tableau Markdown."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def atomic_write(path: str, content: str):
    """Écriture ATOMIQUE : fichier temporaire DANS le projet (pas /tmp — contrainte
    3 OS) puis os.replace — un Ctrl+C pendant l'écriture ne laisse jamais un livrable
    tronqué."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def manual_deliverable_exists(path: str) -> bool:
    """Un livrable existe-t-il SANS le marqueur d'usine (écrit à la main) ?"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            _a11y_txt = f.read()
            return A11Y_MARKER not in _a11y_txt and A11Y_MARKER_LEGACY not in _a11y_txt
    except OSError:
        return True


def read_synthesis_or_fallback(stats: dict) -> str:
    """Contenu du chapeau (il porte déjà son titre '## Synthèse exécutive'), ou
    fallback mécanique si le fichier manque/est hors format."""
    if os.path.exists(SYNTHESIS_FILE) and synthesis_structural_check(SYNTHESIS_FILE):
        with open(SYNTHESIS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return mechanical_synthesis(stats).strip()


def assemble_report(stats: dict, a11y_map: dict, passes: list, packs: list,
                    contrasts: list, scope_files: list, failed_passes: list = None,
                    trigger_hits: dict = None, trigger_sondes: dict = None) -> None:
    """Le « compilateur » final du rapport. TOUJOURS rejoué en fin de run (reflète les
    verdicts à jour). Recopie, calcule, n'invente rien."""
    print(f"\n{'='*50}\n🧩 ÉTAPE É4 : ASSEMBLAGE MÉCANIQUE → '{A11Y_REPORT_FILE}'\n{'='*50}")
    criteria = stats["criteria"]
    totals = stats["totals"]
    failed_passes = failed_passes or []
    project = str(a11y_map.get("project") or os.path.basename(os.getcwd()))
    buckets = build_buckets(a11y_map)
    skipped = skipped_packs(passes, packs)
    mechanical = mechanical_na_passes(passes)
    paid = [p for p in passes if pass_needs_agent(p)]

    parts = [f"# Pré-audit d'accessibilité (RGAA 4.1.2) — {project}", "", A11Y_MARKER, "",
             f"*Généré le {time.strftime('%Y-%m-%d')} par `Pre-Audit-A11Y-RGAA.py` — "
             f"pré-audit STATIQUE automatisé : {len(paid)} passe(s) d'audit "
             f"(pack × compartiment)"
             + (f" + {len(mechanical)} tranche(s) NA par routage déterministe" if mechanical else "")
             + f", {len(scope_files)} fichier(s) UI, "
             f"{len(criteria)} critères RGAA évalués.*", "",
             f"> ⚠️ **Statut de ce document** : pré-audit réalisé par analyse statique du "
             f"code source. Il ne remplace PAS un audit de conformité RGAA (tests au "
             f"clavier, lecteurs d'écran, zoom 200 %, rendu réel) : {totals['AVM']} "
             f"critère(s) restent « à vérifier manuellement » (AVM), listés en annexe. "
             f"Le taux de conformité est donné en fourchette pour cette raison.", ""]
    if failed_passes:
        parts += [f"> 🚧 **Rapport PARTIEL** : {len(failed_passes)} passe(s) d'audit non "
                  f"abouties après {MAX_ATTEMPTS} tentatives (leurs critères sont consolidés "
                  f"en AVM prudent) : "
                  + " ; ".join(escape_md_cell(f["label"]) for f in failed_passes)
                  + ". Relance le pipeline pour les rejouer : les passes exploitables "
                  "sont reprises telles quelles.", ""]

    # Synthèse exécutive (chapeau LLM ou fallback mécanique).
    parts.append(read_synthesis_or_fallback(stats))
    parts.append("")

    # Taux de conformité + tableau par thématique.
    parts += ["## Taux de conformité", "",
              f"- Verdicts consolidés : **{totals['C']} C** (conformes), **{totals['NC']} NC** "
              f"(non conformes), **{totals['NA']} NA** (non applicables), **{totals['AVM']} AVM** "
              f"(à vérifier manuellement).",
              f"- **Conformité démontrable : {stats['rate_central']} %** "
              f"(critères conformes / (conformes + non conformes)).",
              f"- **Fourchette selon l'issue des vérifications manuelles : "
              f"{stats['rate_floor']} % à {stats['rate_ceiling']} %** (AVM comptés non "
              f"conformes pour le plancher, conformes pour le plafond).", "",
              "| Thématique | C | NC | NA | AVM |",
              "|---|---|---|---|---|"]
    for topic in stats["topics"]:
        parts.append(f"| T{topic['id']:02d} {escape_md_cell(topic['nom'])} "
                     f"| {topic['C']} | {topic['NC']} | {topic['NA']} | {topic['AVM']} |")
    parts.append(f"| **Total** | **{totals['C']}** | **{totals['NC']}** "
                 f"| **{totals['NA']}** | **{totals['AVM']}** |")
    parts.append("")
    if stats["unreadable"]:
        parts.append(f"*(⚠️ {len(stats['unreadable'])} passe(s) illisible(s) à l'agrégation — "
                     f"critères retombés en AVM prudent : {', '.join(stats['unreadable'])}. "
                     f"Supprime leurs fichiers dans '{A11Y_DIR}/' et relance pour les rejouer.)*")
        parts.append("")

    # Non-conformités, groupées par impact décroissant, constats recopiés tels quels.
    parts += ["## Non-conformités", ""]
    nc_criteria = [(crit, entry) for crit, entry in criteria.items() if entry["statut"] == "NC"]
    if not nc_criteria:
        parts += ["Aucune non-conformité démontrée sur le périmètre statique audité.", ""]
    for impact_level in (4, 3, 2, 1, None):
        level_entries = [(c, e) for c, e in nc_criteria if e["impact_max"] == impact_level]
        if not level_entries:
            continue
        title = IMPACT_LABELS[impact_level]
        prefix = f"Impact {impact_level} — " if impact_level else ""
        parts += [f"### {prefix}{title}", ""]
        for crit, entry in sorted(level_entries, key=lambda x: criteria_sort_key(x[0])):
            parts.append(f"#### Critère {crit} — {entry['pack']['nom']}")
            for constat in entry["constats"]:
                loc = constat["localisation"] or "(non renseignée)"
                if not constat.get("localisation_verifiee", True):
                    loc += " — ⚠️ fichier non trouvé dans le projet : à vérifier"
                parts += [f"- **{escape_md_cell(constat['titre'])}** "
                          f"(impact {constat['impact'] if constat['impact'] else '?'}, "
                          f"périmètre : {constat['origine']})",
                          f"  - Localisation : {loc}",
                          ("  - Extrait : `" + constat["extrait"] + "` — "
                           + ("✓ vérifié (retrouvé dans le fichier cité)"
                              if constat.get("extrait_verifie")
                              else "⚠️ NON retrouvé : constat à vérifier")
                           if constat.get("extrait")
                           else "  - Extrait : (absent — matérialité non vérifiée)"),
                          f"  - Constat : {constat['constat'] or '(non renseigné)'}",
                          f"  - Impact utilisateur : {constat['impact_utilisateur'] or '(non renseigné)'}",
                          f"  - Correction : {constat['correction'] or '(non renseignée)'}"]
            parts.append("")

    # Vérifications manuelles restantes : la dette de vérification, explicite et actionnable.
    parts += ["## Vérifications manuelles restantes (AVM)", "",
              "Ces critères ne peuvent pas être tranchés depuis le code seul (rendu visuel, "
              "lecteur d'écran, clavier, zoom). À couvrir lors d'une vérification manuelle "
              "pour transformer la fourchette en taux ferme :", ""]
    avm_rows = [(crit, entry) for crit, entry in sorted(criteria.items(), key=lambda x: criteria_sort_key(x[0]))
                if entry["statut"] == "AVM"]
    if avm_rows:
        parts += ["| Critère | Thématique | À vérifier |", "|---|---|---|"]
        for crit, entry in avm_rows:
            note = " ; ".join(dict.fromkeys(entry["notes"]))[:220] or "(voir le pack de la thématique)"
            parts.append(f"| {crit} | {escape_md_cell(entry['pack']['nom'])} | {escape_md_cell(note)} |")
    else:
        parts.append("Aucune : tous les critères applicables ont été tranchés statiquement.")
    parts.append("")

    # Conformes et non applicables : lecture rapide, une ligne chacun. Les NA sont
    # séparés en deux familles : « non détecté statiquement » ≠ « absent » — un NA de
    # ROUTAGE (aucune passe : aucun déclencheur du pack dans le périmètre) peut cacher
    # du contenu généré hors des sources (CMS, dynamique), un NA de PASSE a été constaté.
    conformes = sorted((c for c, e in criteria.items() if e["statut"] == "C"), key=criteria_sort_key)
    nas_passe = sorted((c for c, e in criteria.items()
                        if e["statut"] == "NA" and e["passes"] > 0), key=criteria_sort_key)
    nas_routage = sorted((c for c, e in criteria.items()
                          if e["statut"] == "NA" and e["passes"] == 0), key=criteria_sort_key)
    parts += ["## Critères conformes et non applicables", "",
              f"- **Conformes ({len(conformes)})** : {', '.join(conformes) or '(aucun)'}",
              f"- **NA constatés en passe ({len(nas_passe)})** : {', '.join(nas_passe) or '(aucun)'}",
              f"- **NA non détectés par le routage statique — contenu dynamique/CMS possible "
              f"({len(nas_routage)})** : {', '.join(nas_routage) or '(aucun)'}", ""]

    # Annexe périmètre & routage : ce qui a été audité, ce qui a été sauté et POURQUOI.
    parts += ["## Annexe — Périmètre et routage", "",
              f"Périmètre : {len(scope_files)} fichier(s) UI découverts mécaniquement "
              f"(extensions d'interface, tests et outillage exclus). Carte : '{A11Y_MAP_FILE}' "
              f"(éditable, validée en cours de run). Verdicts détaillés : un fichier par passe "
              f"dans '{A11Y_DIR}/'.", "",
              "| Compartiment | Fichiers | Packs audités |", "|---|---|---|"]
    per_bucket = {}
    for p in paid:
        per_bucket.setdefault(p["bucket"]["label"], []).append(f"T{p['pack']['id']:02d}")
    for bucket in buckets:
        packs_label = " ".join(per_bucket.get(bucket["label"], [])) or "(aucun)"
        parts.append(f"| {escape_md_cell(bucket['name'])} | {len(bucket['files'])} | {packs_label} |")
    parts.append("")
    if mechanical:
        parts.append(f"Tranches déclarées NA par le routage déterministe ({len(mechanical)}, aucun "
                     f"agent sollicité : aucun déclencheur du pack dans leurs fichiers ; fichier de "
                     f"verdicts écrit mécaniquement dans '{A11Y_DIR}/') : "
                     + ", ".join(escape_md_cell(p["label"]) for p in mechanical[:40])
                     + (f" … (+ {len(mechanical) - 40})" if len(mechanical) > 40 else "") + ".")
        parts.append("")
    if SCOPE_EXCLUSIONS["vendor"]:
        parts += [f"Hors périmètre — assets tiers livrés ({len(SCOPE_EXCLUSIONS['vendor'])} fichier(s) : "
                  f"public/, static/, assets/, dsfr/, bundles legacy — la bibliothèque n'est pas le "
                  f"projet ; ses surcharges dans les sources restent auditées) :", "",
                  summarize_by_directory(SCOPE_EXCLUSIONS["vendor"], 30), ""]
    if SCOPE_EXCLUSIONS["logic"]:
        parts += [f"Hors périmètre — logique pure sans signal d'interface "
                  f"({len(SCOPE_EXCLUSIONS['logic'])} fichier(s) .ts/.js sans balise, composant, "
                  f"ARIA ni accès au DOM) :", "",
                  summarize_by_directory(SCOPE_EXCLUSIONS["logic"], 30), ""]
    if skipped:
        parts.append("Packs jamais déclenchés (aucun motif détecté dans le périmètre — leurs "
                     "critères sont déclarés NA par le routage déterministe) : "
                     + ", ".join(f"T{p['id']:02d} {p['nom']}" for p in skipped) + ".")
        parts.append("")
    suspicious = suspicious_all_na_passes(passes)
    if suspicious:
        parts.append("⚠️ Passe(s) SUSPECTE(s) — pack routé par déclencheur (motifs présents "
                     "dans les fichiers du compartiment) mais 100 % des verdicts NA, "
                     "incohérent par construction : à relire ; pour rejouer une passe, "
                     "supprime son fichier de verdicts et relance. Passes concernées : "
                     + ", ".join(p["label"] for p in suspicious) + ".")
        parts.append("")
        # Les hits du scan sous les yeux de l'arbitre humain : la contradiction
        # (motifs présents / 100 % NA) se juge sur pièces.
        for suspect in suspicious:
            shown = []
            for path in suspect["bucket"]["files"]:
                hit = (trigger_hits or {}).get((suspect["pack"]["id"], path))
                if hit:
                    shown.append(f"  - `{path}:{hit[0]}` — motif « {escape_md_cell(hit[1])} »")
            if shown:
                parts.append(f"Motifs détectés pour {escape_md_cell(suspect['label'])} :")
                parts += shown[:MAX_TRIGGER_HITS_IN_PROMPT]
                parts.append("")
    all_c = suspicious_all_c_passes(passes)
    if all_c:
        parts.append("⚠️ Passe(s) SUSPECTE(s) côté C — pack déclenché mais verdicts "
                     "massivement C sans le moindre constat ni AVM (fausse conformité "
                     "possible) : à relire ; pour rejouer une passe, supprime son fichier "
                     "de verdicts et relance. Passes concernées : "
                     + ", ".join(p["label"] for p in all_c) + ".")
        parts.append("")
    c_suspects = suspicious_c_verdicts(passes, trigger_sondes)
    if c_suspects:
        parts.append("⚠️ Verdict(s) C SUSPECT(s) — une sonde mécanique détecte un motif de "
                     "non-conformité quasi certain, mais la passe a répondu C sur le critère "
                     "sondé. Jamais de verdict automatique : à relire, l'arbitrage est humain :")
        for suspect in c_suspects[:MAX_TRIGGER_HITS_IN_PROMPT]:
            parts.append(f"  - critère {suspect['critere']} (C) contre `{suspect['path']}:"
                         f"{suspect['line']}` — motif « {escape_md_cell(suspect['motif'])} » "
                         f"(NC {suspect['confiance']}) — passe "
                         f"{escape_md_cell(suspect['pass']['label'])}")
        parts.append("")
    if contrasts:
        parts += ["Mesures de contraste mécaniques (paires color/fond littérales d'un même "
                  "bloc CSS ; indice fourni à la passe Couleurs, jamais un verdict "
                  "automatique) :", ""]
        parts += [f"- {c['ratio']}:1 — `{c['file']}:{c['line']}` ({escape_md_cell(c['selector'])})"
                  for c in contrasts]
        parts.append("")

    # Annexe méthode & limites : l'honnêteté du livrable, en toutes lettres.
    # La ligne Référentiel affiche le décompte RÉELLEMENT audité : un manifeste de packs
    # édité (union ≠ 106 critères) n'est jamais silencieux dans le rapport.
    if len(criteria) == 106:
        referentiel_line = ("- Référentiel : RGAA 4.1.2 (DINUM, Licence Ouverte 2.0), "
                            "106 critères, 13 thématiques — équivalences WCAG 2.1 indiquées "
                            "dans les grilles de packs.")
    else:
        referentiel_line = (f"- Référentiel : RGAA 4.1.2 (DINUM, Licence Ouverte 2.0) — audit "
                            f"sur {len(criteria)} critère(s) du référentiel, "
                            f"{len(packs)} thématique(s) (manifeste de packs édité : les 106 "
                            f"critères ne sont pas tous couverts) — équivalences WCAG 2.1 "
                            f"indiquées dans les grilles de packs.")
    parts += ["## Annexe — Méthode et limites", "",
              "- Méthode : audit statique du code source, découpé en passes indépendantes "
              "(un pack thématique RGAA × un compartiment de la carte d'interface), contexte "
              "réinitialisé entre chaque passe ; verdicts contrôlés mécaniquement (set exact "
              "des critères, statuts, constats localisés) ; agrégation et taux calculés par "
              "code, sans intervention du modèle.",
              referentiel_line]
    if stats.get("requalified_manual_c"):
        parts.append(f"- Testabilité appliquée par le code : {stats['requalified_manual_c']} "
                     f"verdict(s) C requalifié(s) AVM (critère à testabilité « manuelle » : "
                     f"C non démontrable statiquement — la note figure sur chaque critère "
                     f"concerné).")
    parts += [
              "- Routage : les packs thématiques sont activés par détection de motifs dans "
              "les fichiers sources ; un contenu généré hors de ces sources (CMS, back-office, "
              "données dynamiques) peut échapper à cette détection. Un critère « NA non détecté "
              "par le routage statique » signifie « motif non détecté dans les sources », pas "
              "« contenu garanti absent du service ».",
              "- Limites : l'analyse statique ne voit ni le rendu, ni le DOM généré, ni le "
              "comportement au clavier ou au lecteur d'écran ; tout critère non décidable "
              "depuis le code est marqué AVM plutôt que deviné. Un statut C signifie "
              "« démontré sur les fichiers audités », pas « garanti sur la page rendue ».",
              "- Angles morts STRUCTURELS du découpage par compartiments (aucun contrôle "
              "croisé n'existe entre eux) : cohérence des étiquettes entre zones (11.3), "
              "héritage CSS trans-compartiments (10.5 : couleur posée dans une zone, fond "
              "hérité du socle), navigation redéfinie écran par écran (12.2). Ces critères "
              "restent au mieux AVM : à couvrir lors de la vérification manuelle.",
              "- Ce document est un OUTIL DE PRÉ-AUDIT ET DE REMÉDIATION. Il ne constitue ni "
              "un audit de conformité opposable ni une attestation, et ne suffit pas à publier "
              "une déclaration d'accessibilité.", ""]

    atomic_write(A11Y_REPORT_FILE, "\n".join(parts))
    print(f"✅ '{A11Y_REPORT_FILE}' assemblé : {totals['NC']} NC, {totals['AVM']} AVM, "
          f"conformité démontrable {stats['rate_central']} % "
          f"({stats['rate_floor']}–{stats['rate_ceiling']} %).")


def write_summary(stats: dict, a11y_map: dict, scope_files: list, passes: list) -> None:
    """Synthèse courte des résultats, 100 % Python (recopie et compte, n'invente rien) :
    chiffres clés, non-conformités démontrées (une ligne + correction chacune), reste à
    vérifier par thématique. Ni cadre réglementaire ni champs administratifs : c'est un
    pré-audit statique, pas une déclaration d'accessibilité."""
    totals = stats["totals"]
    criteria = stats["criteria"]
    project = str(a11y_map.get("project") or os.path.basename(os.getcwd()))
    paid = [p for p in passes if pass_needs_agent(p)]
    ncs = sorted((c for c, e in criteria.items() if e["statut"] == "NC"),
                 key=lambda c: (-(criteria[c]["impact_max"] or 0), criteria_sort_key(c)))

    parts = [f"# Synthèse du pré-audit d'accessibilité (RGAA 4.1.2) — {project}", "",
             A11Y_MARKER, "",
             f"*Pré-audit statique automatisé du {time.strftime('%Y-%m-%d')} : "
             f"{len(scope_files)} fichier(s) UI, {len(paid)} passe(s), {len(criteria)} critères "
             f"évalués. Détail (constats, localisations, corrections, annexes) : "
             f"'{A11Y_REPORT_FILE}'.*", "",
             "## Chiffres clés", "",
             "| Conformes | Non conformes | Non applicables | À vérifier manuellement |",
             "|---|---|---|---|",
             f"| {totals['C']} | {totals['NC']} | {totals['NA']} | {totals['AVM']} |", "",
             f"- Conformité démontrable : **{stats['rate_central']} %** "
             f"(conformes / (conformes + non conformes)).",
             f"- Fourchette selon l'issue des vérifications manuelles : "
             f"**{stats['rate_floor']} % à {stats['rate_ceiling']} %**.", "",
             f"## Non-conformités démontrées ({totals['NC']})", ""]
    if not ncs:
        parts += ["Aucune sur le périmètre statique audité.", ""]
    for crit in ncs:
        entry = criteria[crit]
        constat = entry["constats"][0] if entry["constats"] else None
        impact = f", impact {entry['impact_max']}" if entry["impact_max"] is not None else ""
        titre = constat["titre"] if constat else "(constat non titré)"
        line = f"- **Critère {crit}** ({entry['pack']['nom']}{impact}) : {titre}"
        if constat and constat.get("localisation"):
            line += f" — {constat['localisation']}"
        parts.append(line)
        if constat and constat.get("correction"):
            parts.append(f"  - Correction : {constat['correction']}")
    parts.append("")

    avm_topics = sorted((t for t in stats["topics"] if t["AVM"]), key=lambda t: -t["AVM"])
    parts += [f"## Reste à vérifier manuellement ({totals['AVM']} critère(s))", ""]
    if avm_topics:
        parts += ["Par thématique : "
                  + ", ".join(f"{t['nom']} ({t['AVM']})" for t in avm_topics) + ".",
                  "Moyens : clavier, lecteur d'écran, zoom 200 %, contrastes au rendu. "
                  f"La liste critère par critère est en annexe de '{A11Y_REPORT_FILE}'.", ""]
    else:
        parts += ["Aucun : tous les critères ont pu être tranchés depuis le code.", ""]
    parts += ["*Pré-audit statique : ni audit de conformité, ni déclaration d'accessibilité.*", ""]

    atomic_write(A11Y_SUMMARY_FILE, "\n".join(parts))
    print(f"✅ Synthèse des résultats : '{A11Y_SUMMARY_FILE}' "
          f"({totals['NC']} NC, {totals['AVM']} AVM).")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "pre-audit-a11y", RUNNER.name,
                   model=RUNNER.configured_model())
    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du
    # run courant : on le purge au démarrage (même contrat que l'usine).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # Les grilles et le manifeste sont le référentiel de TOUT l'audit : leur absence est
    # un échec immédiat (sans eux, les auditeurs improviseraient — l'usine l'interdit).
    trunk_text = load_grid(A11Y_TRUNK_SKILL_FILE)
    map_grid = load_grid(A11Y_MAP_SKILL_FILE)
    missing_grids = [path for path, text in ((A11Y_TRUNK_SKILL_FILE, trunk_text),
                                             (A11Y_MAP_SKILL_FILE, map_grid))
                     if not text.strip()]
    if missing_grids:
        print(f"❌ Grille(s) manquante(s) ou vide(s) : {', '.join(missing_grids)}.")
        write_fail_report("Grille d'audit manquante",
                          f"Introuvable(s) ou vide(s) : {', '.join(missing_grids)} — impossible "
                          f"d'auditer sans référentiel.")
        sys.exit(1)
    packs, manifest_fatal = load_packs_manifest()
    if manifest_fatal:
        print(f"❌ Manifeste des packs invalide ('{A11Y_PACKS_FILE}') :")
        for problem in manifest_fatal:
            print(f"   - {problem}")
        write_fail_report("Manifeste des packs invalide", "\n".join(manifest_fatal))
        sys.exit(1)

    # Étape É0 : périmètre + routage par PYTHON (déterministe), montrés à l'humain AVANT
    # de payer le moindre tour d'agent.
    scope_files = discover_ui_scope()
    if not scope_files:
        print("❌ Aucun fichier d'interface trouvé dans ce répertoire (extensions cherchées : "
              + ", ".join(sorted(UI_EXTENSIONS)) + ").")
        print("   → Lance l'audit depuis la racine du projet qui contient l'interface à évaluer.")
        write_fail_report("Périmètre d'audit vide",
                          "Aucun fichier d'interface détecté dans le répertoire courant.")
        sys.exit(1)

    print("🧮 Scan déterministe du périmètre (déclencheurs de packs, contrastes)...")
    triggers, trigger_hits = scan_triggers(scope_files, packs)
    sonde_hits = scan_sondes(scope_files, packs)
    if sonde_hits:
        total_sondes = sum(len(v) for v in sonde_hits.values())
        print(f"   Sondes NC : {total_sondes} indice(s) mécanique(s) détecté(s) — "
              f"injectés aux passes concernées, confrontés aux verdicts (annexe).")
    contrasts = measure_css_contrasts(scope_files)
    contrast_block = build_contrast_block(contrasts)

    existing_map = peek_a11y_map()
    files_per_pack = {p["id"]: sum(1 for hits in triggers.values() if p["id"] in hits)
                      for p in packs}
    preview = scope_files[:20]

    print(f"\n{'='*50}")
    print(f"♿ PRÉ-AUDIT D'ACCESSIBILITÉ (RGAA 4.1.2) — Périmètre découvert :")
    print(f"   Répertoire : {os.getcwd()}")
    print(f"   {len(scope_files)} fichier(s) UI à auditer. Aperçu :")
    for f in preview:
        print(f"      - {f}")
    if len(scope_files) > len(preview):
        print(f"      … et {len(scope_files) - len(preview)} autre(s).")
    if SCOPE_EXCLUSIONS["vendor"] or SCOPE_EXCLUSIONS["logic"]:
        print(f"   Hors périmètre mécanique (tracé en annexe du rapport) : "
              f"{len(SCOPE_EXCLUSIONS['vendor'])} asset(s) tiers (public/, static/, dsfr/, legacy…), "
              f"{len(SCOPE_EXCLUSIONS['logic'])} fichier(s) de logique pure sans signal d'interface.")
    print(f"   Routage des 13 packs thématiques (déclencheurs détectés) :")
    for pack in packs:
        hits = files_per_pack[pack["id"]]
        always = " + passe socle garantie" if pack["toujours"] else ""
        if hits:
            print(f"      - T{pack['id']:02d} {pack['nom']} : {hits} fichier(s) déclencheur(s){always}")
        elif pack["toujours"]:
            print(f"      - T{pack['id']:02d} {pack['nom']} : aucun déclencheur{always}")
        else:
            print(f"      - T{pack['id']:02d} {pack['nom']} : aucun déclencheur → critères NA (pack sauté)")
    if contrasts:
        print(f"   Contrastes : {len(contrasts)} paire(s) CSS littérale(s) mesurée(s) "
              f"(pire ratio : {contrasts[0]['ratio']}:1) — fournies à la passe Couleurs.")
    else:
        print(f"   Contrastes : aucune paire color/fond littérale mesurable (l'agent traitera "
              f"la thématique Couleurs sans indice chiffré).")
    context = business_context_file()
    if context:
        print(f"   Contexte métier : '{context}' détecté (pointé aux auditeurs en lecture optionnelle).")
    else:
        print(f"   Contexte métier : aucun ('{SPEC_FILE}'/'{NEED_FILE}' absents) — l'interface est "
              f"auditée telle qu'elle se présente.")
    if existing_map:
        print(f"   Reprise : carte existante ({len(existing_map['zones'])} zone(s)) — le décompte "
              f"exact des passes sera affiché avec la carte ; les passes déjà exploitables "
              f"dans '{A11Y_DIR}/' seront sautées.")
    else:
        print(f"   Déroulé : 1 cartographie (sautée si '{A11Y_MAP_FILE}' valide) + N passes "
              f"d'audit (pack × compartiment, contexte réinitialisé entre chaque ; décompte "
              f"exact affiché avec la carte AVANT de payer) + 1 synthèse + agrégation Python "
              f"→ '{A11Y_REPORT_FILE}' + '{A11Y_SUMMARY_FILE}' (racine).")
    for deliverable in (A11Y_REPORT_FILE, A11Y_SUMMARY_FILE):
        if manual_deliverable_exists(deliverable):
            print(f"   ⚠️  ATTENTION : un '{deliverable}' SANS marqueur d'usine existe à la racine "
                  f"(écrit à la main ?). L'assemblage final l'ÉCRASERA — sauvegarde-le avant de "
                  f"valider si tu veux le conserver.")
    print(f"{'='*50}")

    confirm = input("\n▶️  Lancer le pré-audit d'accessibilité sur ce périmètre ? (y/n) : ")
    mm_audit.event("gate", id="scope", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print("⏹️  Annulé par l'utilisateur.")
        sys.exit(0)

    # Garde read-only : baseline capturée AVANT le premier agent.
    init_readonly_guard()

    # Étape É1 : cartographie (LLM seulement si nécessaire — reprise par fichiers),
    # doublement validée (schéma Python + y/n humain, carte éditable avant de valider).
    a11y_map = run_cartography(map_grid, scope_files, packs, triggers)

    # La matrice des passes est figée par la carte validée : c'est elle qui indexe la
    # reprise, l'avancement et le rapport d'échec.
    buckets = build_buckets(a11y_map)
    passes = build_pass_list(buckets, packs, triggers)
    _RUN_STATE["passes"] = passes
    # L8 (git UNIQUEMENT) : --rejouer-modifiees <ref> invalide toute passe dont un
    # fichier du compartiment a changé depuis <ref> — après un cycle de remédiation,
    # choisir à la main les fichiers de verdicts à supprimer est propice à l'oubli
    # (un verdict périmé resterait au rapport comme s'il était à jour).
    if "--rejouer-modifiees" in sys.argv:
        flag_idx = sys.argv.index("--rejouer-modifiees")
        since_ref = sys.argv[flag_idx + 1] if flag_idx + 1 < len(sys.argv) else "HEAD"
        if shutil.which("git") is None or not os.path.isdir(".git"):
            fail_a11y("❌ --rejouer-modifiees exige un dépôt git : sans lui, supprime "
                      f"manuellement les fichiers de verdicts à rejouer dans '{A11Y_DIR}/' "
                      "puis relance.", title="Reprise diff-aware sans git")
        ok_diff, diff_out = run_git(["diff", "--name-only", since_ref])
        if not ok_diff:
            fail_a11y(f"❌ 'git diff --name-only {since_ref}' a échoué (réf invalide ?).",
                      title="Reprise diff-aware : réf illisible")
        changed_files = [l.strip() for l in diff_out.splitlines() if l.strip()]
        stale = invalidated_passes(passes, changed_files)
        if stale:
            print(f"♻️  --rejouer-modifiees {since_ref} : {len(stale)} passe(s) invalidée(s) "
                  f"(fichiers du compartiment modifiés depuis la réf) :")
            for stale_pass in stale:
                print(f"   - {stale_pass['label']}")
                try:
                    os.remove(stale_pass["findings_path"])
                except OSError:
                    pass
            mm_audit.event("guard", name="rejouer_modifiees", action="invalidation",
                           ref=since_ref, passes=len(stale))
        else:
            print(f"♻️  --rejouer-modifiees {since_ref} : aucune passe à invalider.")

    if not passes:
        fail_a11y("❌ Aucune passe d'audit à lancer : aucun pack n'est déclenché sur les "
                  "compartiments de la carte (périmètre sans contenu d'interface reconnu ?).",
                  title="Matrice de passes vide")

    # 🚀 Boot du harness Data Center dans tmux (no-op si la cartographie l'a déjà lancé).
    RUNNER.start()

    # Étape É2 : les passes d'audit (une session neuve par passe). Les passes non
    # abouties ne tuent plus le run (sauf circuit breaker) : elles reviennent ici.
    failed_passes = run_audit_passes(passes, trunk_text, contrast_block, trigger_hits,
                                     sonde_hits)

    # Étape É4 (calcul) : agrégation mécanique des verdicts.
    stats = aggregate(passes, packs)

    # Étape É3 : synthèse exécutive (non bloquante : fallback mécanique après 3 échecs).
    run_synthesis(stats)

    # Étape É4 (livrables) : rapport + synthèse des résultats.
    assemble_report(stats, a11y_map, passes, packs, contrasts, scope_files, failed_passes,
                    trigger_hits, sonde_hits)
    write_summary(stats, a11y_map, scope_files, passes)

    # Dernier passage de la garde read-only : couvre la fenêtre entre le dernier enforce
    # d'une passe et la fin du run (notamment le chemin « livrable tardif accepté »).
    enforce_readonly("final")

    # Nettoyage des fichiers temporaires et sentinelles, puis fermeture propre.
    for tmp_f in [TMP_A11Y_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_a11y_sentinels()
    RUNNER.kill()
    if failed_passes:
        # Rapport PARTIEL : le failReport persiste et liste les passes à rejouer
        # (la reprise par fichiers fait le reste au relancement). Sortie 0 assumée :
        # le livrable existe et il est honnête (AVM + bandeau + annexe).
        write_fail_report(f"Pré-audit partiel : {len(failed_passes)} passe(s) non abouties",
                          "Le run a continué (passes indépendantes) : le rapport est généré "
                          "mais PARTIEL — les critères des passes manquantes sont consolidés "
                          "en AVM prudent. Relance le pipeline pour rejouer ces passes.",
                          details="\n".join(f"- {f['label']} : {f['reason']}"
                                            for f in failed_passes))
    elif os.path.exists(FAIL_REPORT_FILE):
        # Run réellement nominal : aucun rapport d'échec ne doit subsister.
        os.remove(FAIL_REPORT_FILE)

    totals = stats["totals"]
    print(f"""
🏁 [CONGRATULATIONS] Pré-audit d'accessibilité terminé !
   📄 Rapport consolidé : '{A11Y_REPORT_FILE}' — conformité démontrable {stats['rate_central']} %
      (fourchette {stats['rate_floor']}–{stats['rate_ceiling']} %), {totals['NC']} NC, {totals['AVM']} AVM.
   📄 Synthèse des résultats : '{A11Y_SUMMARY_FILE}' (chiffres clés, non-conformités, reste à vérifier).
   🗂️  Verdicts détaillés par passe : '{A11Y_DIR}/' ; carte d'interface : '{A11Y_MAP_FILE}'.
   ♿ Prochaine étape recommandée : couvrir les {totals['AVM']} critère(s) AVM par une
      vérification manuelle (clavier, lecteur d'écran, zoom) pour transformer la fourchette
      en taux ferme.
   ♻️  Pour rejouer UNE passe (après correction du code, p. ex.) : supprime son fichier dans
      '{A11Y_DIR}/' et relance — seul le manquant est rejoué, l'agrégation est refaite.
      Pour tout refaire (carte comprise) : supprime '{A11Y_DIR}/' et '{A11Y_MAP_FILE}' puis relance.""")
    # Clôture du journal de run (chemin capturé AVANT end, qui remet l'état à zéro).
    # Un run aux passes manquantes se clôt en « partial » : le journal dit la vérité.
    journal_dir = mm_audit.run_dir()
    mm_audit.end("partial" if failed_passes else "success")
    if journal_dir:
        print(f"   📁 Journal du run : {os.path.relpath(journal_dir)}/")
    if failed_passes:
        print(f"""
⚠️  RAPPORT PARTIEL : {len(failed_passes)} passe(s) non abouties sur {len(passes)} — leurs critères
   sont consolidés en AVM prudent. Relance le pipeline pour les rejouer (les passes
   exploitables sont reprises telles quelles). Détail : '{FAIL_REPORT_FILE}'.""")


mm_core.configure(
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
