#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrateur IA - Usine à DOCUMENTATION COMPORTEMENTALE avec un harness d'agent + tmux
─────────────────────────────────────────────────────────────────────────────
VARIANTE « DOCUMENTATION » : elle n'écrit AUCUN code — elle documente TOUT le
comportement d'un projet EXISTANT (features et tests d'acceptance possibles, couverts par
des tests existants ou proposés), et livre un fichier consolidé 'documentation.md' à la
RACINE du projet documenté, agréable à lire pour un humain.

C'est l'application directe de la logique MAIsterMind — trancher la fenêtre de contexte
par phase pour rendre les modèles petits ou moyens fiables sur la durée — à un travail de
DOCUMENTATION : demander « documente tout le projet d'un coup » sature le contexte et
produit une doc superficielle ; ici chaque ZONE FONCTIONNELLE est une phase dédiée,
exécutée dans une session neuve (/new), qui ne reçoit QUE sa tranche (grille + SA zone +
SES fichiers) et n'écrit QUE son fichier de zone. L'assemblage final est MÉCANIQUE
(Python) : zéro perte, zéro paraphrase, quel que soit le volume.

Pipeline :
  - Étape 0 : PÉRIMÈTRE — fichiers de code et de tests découverts par PYTHON (déterministe,
    zéro LLM), affichés puis confirmés par l'humain (y/n) AVANT de payer le moindre agent.
  - Étape 1 : CARTOGRAPHIE — contrairement à l'audit (10 heuristiques fixes), le découpage
    en phases n'est pas connu d'avance : un agent cartographe ASSIGNE les fichiers du
    périmètre à des zones fonctionnelles nommées et ordonnées ('doc_map.yaml', l'équivalent
    du blackboard). Double validation : schéma Python (couverture totale, zone « Divers »
    ajoutée mécaniquement au besoin) puis humaine (y/n, YAML éditable avant de valider).
  - Étape 2 : DOCUMENTATION — N passes, une par zone, session neuve à chaque fois. Chaque
    documentaliste écrit 'doc_zones/Zxx_<slug>.md' (features + tests d'acceptance
    Couvert/Proposé) puis signale sa fin par sentinelle. Pas de verdict exécutable, mais
    trois GARDES DE CONTENU mécaniques (Python, zéro LLM) en plus du filet de vivacité
    (3 tentatives) et du plancher STRUCTUREL : tout chemin cité entre backticks doit
    EXISTER (une source hallucinée = rejet avec l'écart exact), tout « Couvert par » doit
    s'adosser à un fichier de TEST réel (sinon l'AT est « Proposé »), et les compteurs du
    Bilan doivent égaler le comptage RÉEL du fichier (features et AT recomptés par
    Python). Un signal de complétude (warn-only) liste les fichiers de la zone jamais
    cités en source. On a volontairement renoncé à un vérificateur LLM par zone : les
    écarts OBJECTIFS sont tous attrapables mécaniquement, et une opinion de LLM de plus
    serait un risque d'hallucination de plus.
  - Étape 3 : VUE D'ENSEMBLE — un agent rédige le chapeau de lecture (produit, parcours,
    guide) à partir des seuls intents et bilans (jamais les zones entières). Non bloquante :
    après 3 échecs, un fallback mécanique Python prend le relais.
  - Étape 4 : ASSEMBLAGE — Python déterministe : concaténation dans l'ordre du doc_map,
    titres décalés, sommaire, carte des zones et annexe de couverture générés
    mécaniquement → 'documentation.md' à la racine (écriture atomique).

Reprise par fichiers, comme les autres variantes : un 'doc_map.yaml' valide saute la
cartographie ; un fichier de zone exploitable saute sa passe ; la vue d'ensemble et
l'assemblage sont TOUJOURS rejoués. FRAÎCHEUR : une zone sautée dont des fichiers ont été
modifiés APRÈS l'écriture de sa documentation est signalée « périmée » (mtime, best-effort,
warn-only) — à l'écran de périmètre comme au moment du saut. Pour re-documenter une zone :
supprimer son fichier dans 'doc_zones/' et relancer. Pour tout refaire : supprimer
'doc_zones/' et 'doc_map.yaml'.

Garde READ-ONLY (best-effort, si le projet est déjà un dépôt git) : documenter ne modifie
pas le projet documenté. Tout fichier suivi modifié par un agent est restauré
(git checkout) et signalé ; tout fichier créé hors des livrables est signalé (jamais
supprimé). Sans git, l'interdiction reste portée par les prompts (dégradation gracieuse).

Protection d'une doc manuelle : le 'documentation.md' généré porte un marqueur HTML
invisible ; un 'documentation.md' préexistant SANS ce marqueur (écrit à la main) est
annoncé explicitement à l'écran de confirmation AVANT le y/n — on ne détruit jamais
silencieusement du travail humain.
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil
import fnmatch
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
RUNNER = resolve_runner(os.getcwd(), role="doc", messages={
    "reuse":  None,
    "follow": "   👀 Suis la documentation en direct dans un autre terminal : tmux attach -t {session}",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
DOC_DIR               = "doc_zones"                # constats intermédiaires (un fichier par zone)
DOC_MAP_FILE          = "doc_map.yaml"             # carte des zones (l'équivalent du blackboard)
DOC_FILE              = "documentation.md"         # livrable final consolidé, à la RACINE
OVERVIEW_FILE         = f"{DOC_DIR}/_overview.md"  # chapeau de lecture (vue d'ensemble)
FAIL_REPORT_FILE      = "failReport.md"            # rapport d'arrêt persistant (même contrat que l'usine)
DOC_MAP_SKILL_FILE    = "./.agents/pipeline/doc-map/SKILL.md"
DOC_ZONE_SKILL_FILE   = "./.agents/pipeline/doc-zone/SKILL.md"
AGENT_CONFIG_FILE     = RUNNER.config_file

# Fichier temporaire de routage de contexte (prompt déporté, nommé par le harness)
TMP_DOC_FILE          = RUNNER.tmp_file("doc")

# Fichier tampon du prompt envoyé au TUI via tmux. Chemin RELATIF au projet : c'est le
# seul choix valable sur les 3 OS (Windows n'a pas de /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Nom de la session tmux, suffixé d'une empreinte du répertoire du projet : deux usines
# tournant sur la même machine ne doivent JAMAIS partager une session. Préfixe DISTINCT
# des autres variantes (rôles 'factory' / 'proto' / 'audit') : une documentation peut
# coexister avec une production ou un audit sur un AUTRE projet sans collision.
TMUX_SESSION          = RUNNER.session

# Marqueur HTML invisible du livrable généré : c'est lui qui distingue une doc d'usine
# (écrasable) d'une doc écrite à la main (annoncée avant le y/n, décision D6).
DOC_MARKER            = "<!-- généré par Documentation -->"
DOC_MARKER_LEGACY     = "<!-- généré par MAIsterMind_documentation -->"

MAX_ATTEMPTS          = 3              # Tentatives par passe (filet de vivacité + plancher structurel)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max par passe (filet de sécurité)
STABLE_POLLS_FALLBACK = 15             # filet sans sentinelle : livrable accepté s'il est resté
                                       # stable pendant N contrôles consécutifs (N × POLL_INTERVAL secondes)

# Bornes de fenêtre de contexte (mêmes familles que MAX_SCOPE_FILES_IN_PROMPT de l'audit) :
MAX_ZONE_FILES_IN_PROMPT  = 150   # au-delà, la liste des fichiers d'une zone est tronquée dans le prompt
MAX_SCOPE_FILES_IN_CARTO  = 400   # au-delà, le surplus du périmètre est résumé par répertoire
                                  # (les non listés finiront en zone « Divers » via la couverture,
                                  # sauf si le cartographe les assigne PAR RÉPERTOIRE : entrée
                                  # de la carte terminée par '/')
DIVERS_RETRY_THRESHOLD    = 100   # au-delà de N fichiers en « Divers », la carte est REJOUÉE
                                  # (tant qu'il reste des tentatives) : un résiduel qui contient
                                  # l'essentiel du projet n'est pas une cartographie
SOFT_MAX_FILES_PER_ZONE   = 25    # warn (non bloquant) au-delà : la passe de zone risque de saturer
                                  # (harmonisé avec la borne « 25 fichiers max par zone » de la
                                  # grille doc-map : le cartographe est censé sous-découper avant)


# ─── SENTINELLES (CANAL AGENT → ORCHESTRATEUR) ────────────────────────────────
# Préfixe '.doc_' DISTINCT des '.phase_' / '.pipeline_' / '.audit_' des autres variantes :
# un résidu d'un ancien run d'un autre pipeline ne peut pas être pris pour un signal de
# documentation, et réciproquement.

def doc_sentinel(slot: str, attempt: int) -> str:
    """Fichier écrit par l'agent en toute fin de passe (signal 'j'ai terminé').

    'slot' identifie la passe ('map', 'z1'…'zN', 'overview'). Le numéro de tentative est
    inclus dans le nom : une sentinelle écrite tardivement par l'agent d'une tentative
    précédente ne peut pas être prise pour le signal de la tentative courante.
    """
    return f".doc_{slot}.attempt{attempt}.done"


def cleanup_slot_sentinels(slot: str):
    """Supprime toutes les sentinelles (toutes tentatives) d'une passe."""
    prefix = f".doc_{slot}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_doc_sentinels():
    """Nettoyage final de toutes les sentinelles de documentation résiduelles."""
    for name in os.listdir("."):
        if name.startswith(".doc_") and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


# ─── SYNCHRONISATION VIA MONITEUR DE FICHIERS ─────────────────────────────────

def wait_for_deliverable(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> bool:
    """Attend un livrable signalé par SENTINELLE (même contrat que le pipeline des autres
    variantes : l'agent crée le .done APRÈS avoir sauvegardé le livrable).

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


# ─── PLANCHERS STRUCTURELS (MÊME FAMILLE QUE L'AUDIT) ─────────────────────────

def zone_structural_check(path: str) -> bool:
    """Plancher structurel minimal d'un fichier de zone : ses sections obligatoires
    '## Features' et '## Bilan' doivent être présentes (un fichier à moitié écrit — ou
    du bavardage hors format — s'arrête avant)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return "## features" in content and "## bilan" in content
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


def overview_structural_check(path: str) -> bool:
    """Plancher structurel minimal de la vue d'ensemble : elle commence par son titre."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip().lower().startswith("## vue d'ensemble")
    except OSError:
        return False


def zone_ok(path: str) -> bool:
    """Un fichier de zone est-il exploitable (présent, non vide, structurellement valide) ?
    Sert à la reprise (passe sautée), à l'affichage d'avancement et au rapport d'échec."""
    return os.path.exists(path) and os.path.getsize(path) > 0 and zone_structural_check(path)


def slugify(name: str) -> str:
    """Slug de fichier dérivé par PYTHON (jamais par le modèle — une source d'erreur de
    moins) : minuscules, accents translittérés, kebab-case."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "zone"


def zone_path(zone: dict) -> str:
    """Chemin du fichier de zone (zéro-paddé pour le tri à l'œil). Le modèle ne fournit
    JAMAIS ce chemin : il est calculé ici depuis l'id et le nom de la carte."""
    try:
        zid = int(zone.get("id"))
    except (TypeError, ValueError):
        zid = 0
    return f"{DOC_DIR}/Z{zid:02d}_{slugify(str(zone.get('name') or 'zone'))}.md"


# ─── GARDES DE CONTENU D'UNE ZONE (MÉCANIQUES, ZÉRO LLM) ──────────────────────
# Le plancher structurel prouve que le fichier a la bonne FORME ; ces gardes prouvent que
# tout ce qui y est VÉRIFIABLE ne ment pas : un chemin cité existe sur le disque, un
# statut « Couvert » s'adosse à un fichier de test réel, les compteurs du Bilan égalent le
# comptage réel du contenu. Le reste (fidélité des comportements décrits, falsifiabilité
# des AT) est RÉDACTIONNEL : aucun verdict mécanique possible — et on a volontairement
# renoncé à un vérificateur LLM ici (une opinion de plus = un risque d'hallucination de
# plus) : c'est l'humain qui lit, avec des chiffres et des sources garantis exacts.

CITED_TOKEN_RE       = re.compile(r"`([^`\n]+)`")
CITED_LINE_SUFFIX_RE = re.compile(r":L?\d+(?:-\d+)?$")
AT_STATUS_RE         = re.compile(r"^\s*-\s*\*\*AT\d+\s*[—–-]\s*(Couvert|Propos[ée])", re.IGNORECASE)
COVERED_AT_RE        = re.compile(r"\*\*AT\d+\s*[—–-]\s*Couvert par\s+`([^`\n]+)`", re.IGNORECASE)


def clean_cited(token: str) -> str:
    """Normalise un chemin cité (backslashes, suffixe ':ligne', './') vers le format du
    périmètre — le format que le disque peut confirmer ou infirmer."""
    return norm_rel(CITED_LINE_SUFFIX_RE.sub("", str(token).strip().replace("\\", "/")))


# Caractères qui font d'un token un MOTIF (glob, placeholder, flèche) et non un chemin.
PATTERN_CHARS = "<>*?{}$|→"


def looks_like_path(token: str) -> bool:
    """Un token entre backticks est-il une CITATION DE FICHIER (et non un identifiant de
    code) ? Volontairement strict : '/' ou extension code connue exigés — `canActivate`,
    `npm test` ou `--flag` ne déclenchent jamais la garde (mieux vaut rater une citation
    exotique que rejeter une zone sur un identifiant)."""
    t = str(token).strip()
    if not t or " " in t or "(" in t or t.startswith("-"):
        return False
    if any(ch in t for ch in PATTERN_CHARS):
        # Glob, placeholder ou flèche : un MOTIF, pas la citation d'un fichier
        # (`docs/*.md`, `epic/<KEY>`, `tick_*_agent_<TICKET>.json`, `epic/<KEY> → main`).
        return False
    t = clean_cited(t)
    if "/" in t:
        return True
    return os.path.splitext(t)[1].lower() in CODE_EXTENSIONS


def is_project_rooted(cited: str) -> bool:
    """Un chemin cité avec '/' n'est une SOURCE du projet que si son premier segment est
    une entrée réelle de la racine (`scripts/…`, `src/…`, `.claude/…`). Sinon c'est un
    chemin d'exécution ou une référence git (`epic/<KEY>`, `origin/main`, `docs/` créé
    par un script, `/report`) : la documentation a le droit d'en parler, la garde des
    sources inventées ne le concerne pas. Constat du 28/08 : 8 faux positifs sur 11 dans
    une zone de scripts d'orchestration, trois tentatives brûlées."""
    if "/" not in cited:
        return True
    first = cited.split("/", 1)[0]
    return bool(first) and os.path.exists(first)


def suggest_zone_file(cited: str, zone_files) -> str | None:
    """Un basename nu (`dispatch_plan.sh`) qui correspond à UN seul fichier de la zone :
    le chemin exact à renvoyer au documentaliste, pour qu'il corrige du premier coup."""
    if "/" in cited or not zone_files:
        return None
    matches = sorted({norm_rel(f) for f in zone_files
                      if os.path.basename(norm_rel(f)) == cited})
    return matches[0] if len(matches) == 1 else None


def cited_paths(content: str) -> set:
    """Chemins cités entre backticks dans un fichier de zone (suffixe :ligne retiré,
    fences ignorées) : la matière première des gardes source et complétude."""
    out = set()
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        for token in CITED_TOKEN_RE.findall(line):
            if looks_like_path(token):
                out.add(clean_cited(token))
    return out


def covered_test_citations(content: str) -> list:
    """Citations brutes des « AT<i> — Couvert par `…` » (fences ignorées)."""
    out = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if not in_fence:
            out.extend(COVERED_AT_RE.findall(line))
    return out


def count_zone_content(content: str) -> dict:
    """Compteurs MÉCANIQUES d'un fichier de zone (features, AT, couverts, proposés),
    fences ignorées : la vérité que le Bilan déclaré doit égaler — et celle que
    l'assemblage affiche, quoi que le Bilan déclare."""
    features = ats = covered = 0
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        if FEATURE_HEADING_RE.match(line.strip()):
            features += 1
            continue
        match = AT_STATUS_RE.match(line)
        if match:
            ats += 1
            if match.group(1).lower().startswith("couvert"):
                covered += 1
    return {"features": features, "ats": ats, "covered": covered, "proposed": ats - covered}


def zone_content_issues(path: str, test_scope: set, zone_files=None) -> list:
    """Écarts VÉRIFIABLES d'un fichier de zone (liste vide = conforme). Chaque écart est
    formulé pour être renvoyé TEL QUEL au documentaliste (feedback exact, jamais vague)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return [f"'{path}' est illisible."]
    issues = []
    covered_raw = covered_test_citations(content)
    covered_norm = {clean_cited(raw) for raw in covered_raw}
    # 1. Sources hallucinées : tout chemin cité doit exister (les citations « Couvert »
    #    sont traitées à part, avec un message plus spécifique).
    for cited in sorted(cited_paths(content) - covered_norm):
        if os.path.exists(cited) or not is_project_rooted(cited):
            continue
        exact = suggest_zone_file(cited, zone_files)
        if exact:
            issues.append(f"Le chemin cité `{cited}` est un nom de fichier nu : cite le chemin "
                          f"exact depuis la racine du projet, `{exact}`.")
        else:
            issues.append(f"Le chemin cité `{cited}` n'existe pas dans le projet : cite "
                          f"UNIQUEMENT des fichiers que tu as réellement lus (chemin exact "
                          f"recopié depuis ta zone), ou retire cette source.")
    # 2. « Couvert » sans test réel : le statut le plus précieux du livrable ne repose
    #    jamais sur la parole du modèle.
    for raw in covered_raw:
        p = clean_cited(raw)
        if not os.path.exists(p):
            issues.append(f"AT « Couvert par `{raw}` » : ce fichier n'existe pas — passe "
                          f"l'AT en « Proposé » ou cite le VRAI fichier de test qui vérifie "
                          f"ce scénario.")
        elif p not in test_scope and not is_test_file(p):
            issues.append(f"AT « Couvert par `{raw}` » : ce chemin n'est pas un fichier de "
                          f"TEST du projet — « Couvert » cite un test existant ; sinon l'AT "
                          f"est « Proposé ».")
    # 3. Bilan ≠ contenu réel : les compteurs sont recomptés par Python — l'écart exact
    #    (avec les bons chiffres) est renvoyé au modèle.
    declared = parse_zone_bilan(content)
    mech = count_zone_content(content)
    if any(declared[key] is None for key in mech):
        issues.append(f"Le '## Bilan' ne respecte pas le format verrouillé. Écris EXACTEMENT "
                      f"ces deux lignes (comptage réel de ton fichier) : "
                      f"« - Features : {mech['features']} » puis « - Tests d'acceptance : "
                      f"{mech['ats']} (couverts : {mech['covered']}, proposés : "
                      f"{mech['proposed']}) ».")
    elif any(declared[key] != mech[key] for key in mech):
        issues.append(f"Les compteurs du '## Bilan' ne correspondent pas au contenu réel du "
                      f"fichier ({mech['features']} feature(s), {mech['ats']} AT dont "
                      f"{mech['covered']} couvert(s) et {mech['proposed']} proposé(s)) : "
                      f"accorde le Bilan et le contenu.")
    return issues


def warn_uncited_zone_files(deliverable: str, zone: dict):
    """Signal de COMPLÉTUDE (warn-only, jamais bloquant) : les fichiers de code de la zone
    jamais cités en source. Tous ne portent pas de feature — mais un angle mort doit se
    VOIR, pas se deviner."""
    try:
        with open(deliverable, "r", encoding="utf-8") as f:
            cited = cited_paths(f.read())
    except OSError:
        return
    files = [str(f) for f in (zone.get("files") or [])]
    uncited = [f for f in files if norm_rel(f) not in cited]
    if uncited:
        shown = ", ".join(uncited[:8]) + ("…" if len(uncited) > 8 else "")
        print(f"   ℹ️  Complétude : {len(uncited)}/{len(files)} fichier(s) de la zone jamais "
              f"cité(s) en source ({shown}) — angle mort possible, à vérifier à la lecture.")


def stale_zone_sources(zone: dict, deliverable: str) -> list:
    """Fichiers de la zone modifiés APRÈS l'écriture de sa documentation : la doc de zone
    est probablement PÉRIMÉE. Best-effort mtime (DrvFs/WSL2 tronque parfois) : un signal
    pour l'humain, jamais un verdict — c'est lui qui décide de rejouer la passe."""
    try:
        doc_mtime = os.path.getmtime(deliverable)
    except OSError:
        return []
    stale = []
    for entry in list(zone.get("files") or []) + list(zone.get("tests") or []):
        p = norm_rel(entry)
        try:
            if os.path.exists(p) and os.path.getmtime(p) > doc_mtime:
                stale.append(p)
        except OSError:
            continue
    return sorted(stale)


# ─── GRILLES : CHARGEMENT ─────────────────────────────────────────────────────
# Contrairement à la grille Nielsen (tronc commun + sections à trancher), les deux grilles
# de ce pipeline sont envoyées ENTIÈRES : la « tranche » de contexte vient du doc_map
# (SA zone, SES fichiers), pas de la grille.

def load_grid(path: str) -> str:
    """Charge une grille (SKILL.md). Son absence est un échec IMMÉDIAT : sans grille,
    les agents improviseraient — exactement ce que l'usine interdit."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ─── DÉCOUVERTE DU PÉRIMÈTRE (PYTHON, DÉTERMINISTE, ZÉRO LLM) ─────────────────
# Le périmètre est établi par l'orchestrateur, jamais par un agent : liste stable,
# reproductible, affichée à l'humain AVANT de payer le moindre tour de LLM.

# Extensions UI de l'audit + extensions back/scripts : la documentation couvre TOUT le
# comportement du projet, pas seulement son interface. Constante ÉDITABLE : ajoute ici
# les extensions propres à ta stack si le périmètre affiché en oublie.
UI_EXTENSIONS   = {".html", ".htm", ".css", ".scss", ".sass", ".less",
                   ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                   ".vue", ".svelte", ".astro", ".ejs", ".hbs", ".njk", ".twig"}
CODE_EXTENSIONS = UI_EXTENSIONS | {".py", ".java", ".kt", ".kts", ".go", ".rb", ".php",
                                   ".cs", ".rs", ".c", ".h", ".cpp", ".hpp", ".swift",
                                   ".scala", ".sql", ".sh", ".ps1", ".bat"}

# Répertoires exclus par NOM ; tout répertoire caché ('.git', '.agents', '.opencode'/'.codex',
# '.venv', '.next'…) est exclu d'office par le filtre startswith('.') du walk.
EXCLUDED_DIR_NAMES = {"node_modules", "dist", "build", "out", "coverage", "target",
                      "vendor", "__pycache__", DOC_DIR}

# L'usine ne se documente pas elle-même quand elle est posée dans un projet cible : les
# orchestrateurs sont exclus du périmètre. (Leurs .md/.yaml sont déjà hors périmètre :
# extensions non code.)
ORCHESTRATION_BASENAME_PATTERN = "MAIsterMind*.py"
ORCHESTRATOR_SCRIPTS = frozenset({
    "Coding.py", "Coding-Without-Tests.py", "Test-First.py", "Acceptance-First.py",
    "Design-Prototype.py",
    "Spec.py", "Technical-Plan.py", "Audit-Design.py", "Pre-Audit-A11Y-RGAA.py",
    "Documentation.py", "Guided-Fix.py", "Skills-Adaptation.py", "mm_runner.py",
})


def is_test_file(path: str) -> bool:
    """Heuristique de nommage best-effort : 'path' ressemble-t-il à un fichier de test ?

    Contrairement à l'audit (tests hors périmètre), les tests sont ICI une source de
    vérité comportementale : ils sont routés vers un bucket séparé pour permettre de
    distinguer un test d'acceptance « Couvert » d'un test « Proposé ».
    Mêmes conventions multi-langages que les variantes de production.
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


def is_code_file(name: str) -> bool:
    """'name' (nom de fichier nu) est-il une source de comportement à documenter ?

    Volontairement pragmatique : extensions code connues, MOINS l'outillage qui partage
    ces extensions sans porter de comportement produit — bundles minifiés (illisibles,
    générés), déclarations TypeScript, fichiers de configuration (vite/webpack/tailwind…),
    stories Storybook (démo, pas produit), dotfiles, et les orchestrateurs MAIsterMind
    eux-mêmes.
    """
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext not in CODE_EXTENSIONS:
        return False
    if low.startswith("."):
        return False
    if low.endswith(".d.ts") or ".min." in low or ".config." in low or ".stories." in low:
        return False
    if name in ORCHESTRATOR_SCRIPTS or fnmatch.fnmatch(name, ORCHESTRATION_BASENAME_PATTERN):
        return False
    return True


def discover_code_scope() -> tuple:
    """Listes triées (chemins relatifs, séparateur '/') des fichiers du périmètre :
    (code_files, test_files). Même walk que l'audit, avec routage des tests vers le
    second bucket au lieu de leur exclusion."""
    code_files, test_files = [], []
    for root, dirs, files in os.walk(".", topdown=True):
        dirs[:] = sorted(d for d in dirs
                         if d not in EXCLUDED_DIR_NAMES and not d.startswith("."))
        for name in files:
            if not is_code_file(name):
                continue
            rel = os.path.normpath(os.path.join(root, name)).replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            if is_test_file(rel):
                test_files.append(rel)
            else:
                code_files.append(rel)
    return sorted(code_files), sorted(test_files)


def business_context_file() -> str:
    """Fichier de contexte métier disponible ('spec.md' prioritaire, sinon 'need.md'),
    ou chaîne vide. La documentation n'en a PAS besoin pour tourner : c'est un plus optionnel."""
    for candidate in (SPEC_FILE, NEED_FILE):
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


def business_context_hint() -> str:
    """Pointeur OPTIONNEL vers le contexte métier : on n'inline jamais la spec dans les
    prompts (fenêtre de contexte), on indique seulement où la trouver."""
    context = business_context_file()
    if context:
        return (f"Le fichier '{context}' (contexte métier) existe à la racine : consulte-le "
                f"UNIQUEMENT si un parcours t'est incompréhensible sans lui (économise ton contexte).")
    return "(aucun fichier de contexte métier détecté : documente le comportement tel que le code le montre)"


# ─── GARDE READ-ONLY (GIT, BEST-EFFORT) ───────────────────────────────────────
# « Python vérifie ce qui est vérifiable » : l'interdiction de modifier le projet documenté
# est portée par les prompts (invérifiable seule) ET par ce diff mécanique quand un dépôt
# git préexiste. Comme l'audit : JAMAIS de 'git init' ni de commit — documenter ne doit
# laisser AUCUNE trace dans le projet en dehors des livrables.

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


# Livrables et artefacts de la DOCUMENTATION elle-même : les seuls fichiers que les agents
# ont le droit de produire — jamais restaurés ni signalés par la garde read-only.
_DOC_BASENAMES = {DOC_FILE, DOC_MAP_FILE, FAIL_REPORT_FILE, TMP_DOC_FILE,
                  TMP_PROMPT_BUFFER, f"{DOC_FILE}.tmp", f"{DOC_MAP_FILE}.tmp",
                  os.path.basename(__file__)}


def is_doc_artifact(path: str) -> bool:
    """'path' est-il un livrable/artefact de la documentation (et non un fichier du projet) ?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _DOC_BASENAMES:
        return True
    if segments[0] == DOC_DIR:
        return True
    # Sentinelles et tampons éphémères, où qu'ils se trouvent dans l'arbre.
    if base.startswith(".doc_") and base.endswith(".done"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Caches Python, environnement virtuel et répertoires d'outillage : hors projet documenté.
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
        l'agent » (faux positif permanent) ;
      - les fichiers suivis DÉJÀ MODIFIÉS (worktree sale) : sans cette baseline, la
        restauration 'git checkout' DÉTRUIRAIT du travail humain non commité antérieur
        au run — inacceptable. Ces fichiers sortent de la garde pour tout le run
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
          "un agent sera restauré).")
    dirty_project = sorted(f for f in _GIT["baseline_dirty"] if not is_doc_artifact(f))
    if dirty_project:
        print(f"   ⚠️  {len(dirty_project)} fichier(s) déjà modifié(s) AVANT le run (travail en "
              f"cours ?) : ils sont exclus de la garde (jamais restaurés d'office) — "
              f"{', '.join(dirty_project[:10])}{'…' if len(dirty_project) > 10 else ''}")


def enforce_readonly(label: str):
    """Restaure les fichiers SUIVIS modifiés pendant une passe et signale les fichiers créés
    hors livrables (best-effort, après CHAQUE passe).

    Restauration d'office pour les modifications (documenter ne corrige pas) ; simple
    SIGNALEMENT pour les créations (on ne supprime jamais un fichier qu'on n'a pas créé :
    décision laissée à l'humain).
    """
    if not _GIT["enabled"]:
        return
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    # 'baseline_dirty' exclu de la restauration : un fichier déjà modifié AVANT le run
    # porte du travail humain non commité — le restaurer le DÉTRUIRAIT (cf. init).
    touched = sorted(f for f in diff_out.splitlines()
                     if f.strip() and not is_doc_artifact(f.strip())
                     and f.strip() not in _GIT["baseline_dirty"]) if ok_diff else []
    if touched:
        run_git(["checkout", "--"] + touched)
        print(f"🛡️  [{label}] DOCUMENTATION = LECTURE SEULE : {len(touched)} fichier(s) du projet "
              f"modifié(s) par l'agent — restauré(s) : {', '.join(touched)}")
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        strays = sorted(
            f for f in ({line.strip() for line in others_out.splitlines() if line.strip()}
                        - _GIT["baseline_untracked"])
            if not is_doc_artifact(f))
        if strays:
            print(f"⚠️  [{label}] Fichier(s) créé(s) hors livrables de documentation (non supprimés, "
                  f"à inspecter) : {', '.join(strays)}")


# ─── RAPPORT D'ÉCHEC & MESSAGE D'ÉCHEC ────────────────────────────────────────

# Carte courante (posée dès qu'elle est validée) : les rapports d'échec indexent
# l'avancement sur les zones — quand la carte n'existe pas encore, ils le disent.
_DOC_MAP_STATE = {"map": None}


def documented_count(doc_map: dict) -> int:
    """Nombre de zones dont le fichier est déjà exploitable."""
    if not isinstance(doc_map, dict) or not isinstance(doc_map.get("zones"), list):
        return 0
    return sum(1 for zone in doc_map["zones"]
               if isinstance(zone, dict) and zone_ok(zone_path(zone)))


def write_fail_report(title: str, reason: str, details: str = ""):
    """Écrit un rapport d'arrêt persistant à la racine (même contrat que l'usine :
    tout arrêt NON nominal en produit un). Best-effort : ne lève JAMAIS."""
    # Chokepoint des arrêts non nominaux : le journal de run se clôt ici (chaque
    # appelant sort en sys.exit(1) juste après). Idempotent : end() après end() est no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Rapport d'échec — MAIsterMind (documentation)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "", "### Avancement"]
        doc_map = _DOC_MAP_STATE["map"]
        if isinstance(doc_map, dict) and isinstance(doc_map.get("zones"), list) and doc_map["zones"]:
            lines.append(f"- Zones documentées : {documented_count(doc_map)}/{len(doc_map['zones'])}")
            for zone in doc_map["zones"]:
                if not isinstance(zone, dict):
                    continue
                mark = "✅" if zone_ok(zone_path(zone)) else "⏳"
                lines.append(f"  - {mark} Z{zone.get('id')} : {zone.get('name')}")
        else:
            lines.append("- Cartographie : non établie ('doc_map.yaml' absent ou invalide).")
        lines.append("")
        if details.strip():
            lines.append("### Détails")
            lines.append(details.strip()[:4000])
            lines.append("")
        lines.append("### Action recommandée")
        lines.append("Corrige la cause ci-dessus (ou monte le modèle d'un cran via /model ou "
                     f"'{AGENT_CONFIG_FILE}'), puis relance : la carte et les zones déjà "
                     "documentées seront reprises automatiquement.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Rapport d'échec écrit dans '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


def fail_doc(message: str, details: str = "", title: str = "Échec d'une passe de documentation"):
    """Point de sortie unique des échecs. Tue toujours la session tmux AVANT de quitter :
    un exit qui laisse l'agent vivant le laisse finir d'écrire son livrable APRÈS
    l'abandon de l'orchestrateur (état de reprise trompeur au relancement)."""
    print(message)
    write_fail_report(title, message, details)
    RUNNER.kill()
    sys.exit(1)


def print_pass_failure(label: str, reason: str):
    model = RUNNER.configured_model()
    done = documented_count(_DOC_MAP_STATE["map"]) if _DOC_MAP_STATE["map"] else 0
    print(f"""
{'='*60}
❌ La passe « {label} » n'a pas abouti après {MAX_ATTEMPTS} tentatives.

   Cause : {reason}

💡 Le modèle actuel ({model}) cale sur cette passe (souvent un problème d'appels
   d'outils : le livrable ou la sentinelle ne sont jamais créés, ou le format
   demandé n'est pas respecté).
   Le plus efficace : relance après avoir amené un modèle un cran au-dessus,
   soit via /model dans le TUI, soit dans '{AGENT_CONFIG_FILE}'.

   Pas de stress : la carte validée et les {done} zone(s) déjà documentée(s) seront
   reprises automatiquement, tu ne repars pas de zéro. À tout de suite ! 🚀
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


def validate_and_normalize_doc_map(doc_map, code_files: list, test_files: list) -> tuple:
    """Contrôle et normalise la carte. Renvoie (fatal, soft) et MUTE doc_map en place.

    La carte sort d'un petit LLM faillible ; deux classes de problèmes (même famille que
    validate_blackboard_schema dans l'usine) :
      - fatal : manques STRUCTURANTS (pas un mapping, zones absentes, zone sans id/nom,
        ids dupliqués — sentinelles partagées —, zone dont AUCUN fichier listé n'existe).
        L'orchestrateur DOIT s'arrêter ou rejouer la passe dessus.
      - soft : manques rattrapés MÉCANIQUEMENT ici (chemins inventés retirés, doublons
        d'assignation dédupliqués — première zone gagne —, couverture complétée par une
        zone « Divers », intent/project comblés) : signalés à l'humain, jamais bloquants.
    Le modèle propose, le code vérifie, l'humain tranche (au y/n qui suit).
    """
    fatal, soft = [], []
    if not isinstance(doc_map, dict):
        return ["La carte n'est pas un mapping YAML valide."], []
    zones = doc_map.get("zones")
    if not isinstance(zones, list) or not zones:
        return ["Bloc 'zones' manquant ou vide : rien à documenter."], []

    if not str(doc_map.get("project") or "").strip():
        doc_map["project"] = os.path.basename(os.getcwd()) or "Projet"
        soft.append(f"Champ 'project' manquant : comblé avec « {doc_map['project']} » (affichage seul).")

    scope = set(code_files) | set(test_files)
    seen_paths = {}   # chemin -> id de la première zone qui l'assigne
    seen_ids = set()

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
            fatal.append(f"zones[].id dupliqué ({zone['id']}) : les sentinelles "
                         f"'.doc_z{zone['id']}.attemptM.done' seraient PARTAGÉES entre deux zones.")
        seen_ids.add(zone["id"])
        if not str(zone.get("name") or "").strip():
            fatal.append(f"zones[{idx}].name manquant.")
            continue
        zone["name"] = str(zone["name"]).strip()
        if not str(zone.get("intent") or "").strip():
            zone["intent"] = "(non renseigné)"
            soft.append(f"Zone Z{zone['id']} « {zone['name']} » : 'intent' manquant (comblé).")

        removed, kept = [], {"files": [], "tests": []}
        declared = 0
        for bucket in ("files", "tests"):
            entries = zone.get(bucket) or []
            if not isinstance(entries, list):
                entries = []
            for entry in entries:
                declared += 1
                p = norm_rel(entry)
                # Entrée RÉPERTOIRE (chemin terminé par '/') : tous les fichiers du périmètre
                # qu'il contient, non encore assignés — code ET tests, chacun dans son bucket.
                # C'est ce qui permet de cartographier un monorepo sans recopier des milliers
                # de chemins (et sans que le surplus tombe mécaniquement en « Divers »).
                expanded_code = expand_dir_entry(p, code_files, seen_paths)
                expanded_tests = expand_dir_entry(p, test_files, seen_paths)
                if expanded_code or expanded_tests:
                    for f in expanded_code:
                        seen_paths[f] = zone["id"]
                        kept["files"].append(f)
                    for f in expanded_tests:
                        seen_paths[f] = zone["id"]
                        kept["tests"].append(f)
                    continue
                if p not in scope:
                    removed.append(p)
                    continue
                if p in seen_paths:
                    soft.append(f"'{p}' assigné à plusieurs zones : conservé dans la zone "
                                f"Z{seen_paths[p]} (première assignation), retiré de Z{zone['id']}.")
                    continue
                seen_paths[p] = zone["id"]
                kept[bucket].append(p)
        zone["files"], zone["tests"] = kept["files"], kept["tests"]
        if removed:
            shown = ", ".join(removed[:10]) + ("…" if len(removed) > 10 else "")
            soft.append(f"Zone Z{zone['id']} « {zone['name']} » : {len(removed)} chemin(s) hors "
                        f"périmètre retiré(s) mécaniquement ({shown}).")
        if declared and not (zone["files"] or zone["tests"]):
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
        if len(zone["files"]) + len(zone["tests"]) > SOFT_MAX_FILES_PER_ZONE:
            soft.append(f"Zone Z{zone['id']} « {zone['name']} » : "
                        f"{len(zone['files']) + len(zone['tests'])} fichiers "
                        f"(> {SOFT_MAX_FILES_PER_ZONE}) — la passe risque de saturer sa fenêtre ; "
                        f"redécoupe la carte avant de valider si possible.")

    if fatal:
        return fatal, soft

    ids = sorted(seen_ids)
    if ids != list(range(1, len(ids) + 1)):
        soft.append(f"zones[].id n'est pas une séquence contiguë 1..N "
                    f"({', '.join(str(i) for i in ids)}) : toléré, l'ordre du YAML fait foi.")

    # COUVERTURE TOTALE (symétrique du contrôle check_spec_coverage de l'usine) : tout
    # fichier du périmètre absent de la carte est ajouté MÉCANIQUEMENT à une zone
    # « Divers » (créée au besoin) — la documentation ne laisse aucun angle mort silencieux.
    missing_code = [f for f in code_files if f not in seen_paths]
    missing_tests = [f for f in test_files if f not in seen_paths]
    if missing_code or missing_tests:
        divers = next((z for z in zones if isinstance(z, dict)
                       and slugify(str(z.get("name") or "")) == "divers"), None)
        if divers is None:
            divers = {"id": max(seen_ids) + 1, "name": "Divers",
                      "intent": "Résiduel technique et transverse "
                                "(complété mécaniquement par le contrôle de couverture).",
                      "files": [], "tests": []}
            zones.append(divers)
        divers["files"] = list(divers.get("files") or []) + missing_code
        divers["tests"] = list(divers.get("tests") or []) + missing_tests
        soft.append(f"Couverture : {len(missing_code) + len(missing_tests)} fichier(s) du "
                    f"périmètre absent(s) de la carte — ajouté(s) mécaniquement à la zone "
                    f"« Divers » (Z{divers['id']}).")

    # Une « Divers » déclarée vide et restée vide après couverture n'a plus de raison d'être
    # (une passe de documentation sur zéro fichier n'aurait aucun sens).
    zones[:] = [z for z in zones
                if not (isinstance(z, dict) and slugify(str(z.get("name") or "")) == "divers"
                        and not (z.get("files") or z.get("tests")))]

    return fatal, soft


def divers_size(doc_map: dict) -> int:
    """Nombre de fichiers (code + tests) rangés en zone « Divers » — 0 si absente."""
    for zone in doc_map.get("zones") or []:
        if isinstance(zone, dict) and slugify(str(zone.get("name") or "")) == "divers":
            return len(zone.get("files") or []) + len(zone.get("tests") or [])
    return 0


def divers_files(doc_map: dict) -> list:
    """Fichiers (code + tests) de la zone « Divers » — [] si absente."""
    for zone in doc_map.get("zones") or []:
        if isinstance(zone, dict) and slugify(str(zone.get("name") or "")) == "divers":
            return list(zone.get("files") or []) + list(zone.get("tests") or [])
    return []


def save_doc_map(doc_map: dict):
    """Persiste la carte NORMALISÉE (écriture atomique) : ce que l'humain valide au y/n
    est exactement ce qui est sur disque — et donc ce qu'un run de reprise rechargera."""
    tmp = f"{DOC_MAP_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc_map, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp, DOC_MAP_FILE)


def peek_doc_map():
    """Chargement best-effort de la carte pour l'affichage É0 (jamais bloquant)."""
    try:
        with open(DOC_MAP_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and isinstance(data.get("zones"), list) and data["zones"]:
            return data
    except Exception:
        pass
    return None


# ─── PROMPTS DÉPORTÉS PAR FICHIER ─────────────────────────────────────────────

def summarize_by_directory(files: list, max_lines: int = 60) -> str:
    """Résumé par répertoire des fichiers NON listés dans le prompt de cartographie
    (assignables PAR RÉPERTOIRE : entrée de la carte terminée par '/' ; sinon ils finiront
    en zone « Divers » via la couverture). Borné à `max_lines` répertoires, les plus
    peuplés d'abord."""
    counts = {}
    for f in files:
        d = os.path.dirname(f) or "."
        counts[d] = counts.get(d, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"- {d}/ : {n} fichier(s) non listé(s)" for d, n in ordered[:max_lines]]
    if len(ordered) > max_lines:
        lines.append(f"- (+ {len(ordered) - max_lines} autre(s) répertoire(s))")
    return "\n".join(lines)


def build_carto_scope_blocks(code_files: list, test_files: list) -> tuple:
    """Blocs « fichiers à assigner » du prompt cartographe, bornés à
    MAX_SCOPE_FILES_IN_CARTO au total. Renvoie (bloc_code, bloc_tests, bloc_surplus).

    Les fichiers listés sont un ÉCHANTILLON représentatif de tous les répertoires (code
    applicatif d'abord), pas les N premiers par ordre alphabétique — sur un monorepo, ces
    N premiers étaient 300 feuilles de style d'icônes et zéro fichier de src/. Le surplus
    est résumé par répertoire et assignable PAR RÉPERTOIRE."""
    listed_code = select_carto_sample(code_files, MAX_SCOPE_FILES_IN_CARTO)
    remaining = MAX_SCOPE_FILES_IN_CARTO - len(listed_code)
    listed_tests = select_carto_sample(test_files, max(0, remaining))
    listed = set(listed_code) | set(listed_tests)
    overflow = [f for f in code_files + test_files if f not in listed]
    code_block = "\n".join(f"- {f}" for f in listed_code) or "(aucun)"
    tests_block = "\n".join(f"- {f}" for f in listed_tests) or "(aucun)"
    overflow_block = ""
    if overflow:
        overflow_block = (f"\n(⚠️ Périmètre de {len(code_files) + len(test_files)} fichiers : "
                          f"{len(listed)} listés ci-dessus (échantillon représentatif de tous les "
                          f"répertoires), {len(overflow)} non listé(s), résumés par répertoire "
                          f"ci-dessous. Assigne-les PAR RÉPERTOIRE : une entrée de files: dont le "
                          f"chemin se termine par '/' couvre tous les fichiers du périmètre qu'il "
                          f"contient (récursivement, code et tests). Ce que tu n'assignes pas ira "
                          f"mécaniquement en zone « Divers », qui doit rester un résiduel — pas "
                          f"l'essentiel du projet.)\n"
                          + summarize_by_directory(overflow))
    return code_block, tests_block, overflow_block


def build_carto_prompt(grid_text: str, code_files: list, test_files: list,
                       feedback: str, attempt: int) -> str:
    sentinel = doc_sentinel("map", attempt)
    code_block, tests_block, overflow_block = build_carto_scope_blocks(code_files, test_files)
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es le Cartographe fonctionnel d'un pipeline de documentation découpé par zones : tu
ASSIGNES chaque fichier fourni ci-dessous à une zone fonctionnelle nommée et ordonnée.
Tu ne documentes RIEN (une passe dédiée par zone s'en charge ensuite) et tu ne lis pas le
projet en profondeur : survole les seuls fichiers dont le nom ne permet pas de trancher.
DOCUMENTATION = LECTURE SEULE : tu ne modifies, ne corriges, ne crées AUCUN fichier du projet.
Tu n'écris QUE deux fichiers : '{DOC_MAP_FILE}' à la racine, puis ta sentinelle de fin.

--- GRILLE DU CARTOGRAPHE ---
{grid_text}

--- FICHIERS À ASSIGNER (découverts par l'orchestrateur ; chemins à RECOPIER tels quels) ---
Une entrée de files: ou tests: peut aussi être un RÉPERTOIRE (chemin terminé par '/', ex.
"src/cart/") : elle assigne à la zone tous les fichiers du périmètre qu'il contient et qui
ne sont pas déjà assignés ailleurs. La zone « Divers » peut être omise ou déclarée vide :
l'orchestrateur y range mécaniquement ce que tu n'auras pas assigné.
FICHIERS DE CODE ({len(code_files)}) :
{code_block}

FICHIERS DE TESTS ({len(test_files)}) :
{tests_block}
{overflow_block}

--- CONTEXTE MÉTIER (optionnel) ---
{business_context_hint()}

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- LIVRABLE OBLIGATOIRE ---
Écris la carte dans '{DOC_MAP_FILE}' à la racine du projet : YAML PUR conforme à la grille
ci-dessus (AUCUNE balise ```, toutes les valeurs textuelles entre guillemets doubles,
chemins recopiés depuis les listes fournies). Fais-le directement via tes outils d'édition
de fichier, sans bavardage inutile dans la console.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{DOC_MAP_FILE}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur. Ne le crée que lorsque la carte est VRAIMENT terminée.
"""
    with open(TMP_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_DOC_FILE}' à la racine du projet et réalise "
            f"la passe de cartographie fonctionnelle.")


def build_zone_files_block(zone: dict) -> str:
    """Bloc « ta zone » du prompt documentaliste : listes bornées (fenêtre de contexte)."""
    files = [str(f) for f in (zone.get("files") or [])]
    tests = [str(t) for t in (zone.get("tests") or [])]
    listed_files = files[:MAX_ZONE_FILES_IN_PROMPT]
    remaining = MAX_ZONE_FILES_IN_PROMPT - len(listed_files)
    listed_tests = tests[:max(0, remaining)]
    lines = [f"Fichiers de code de ta zone ({len(files)}) :"]
    lines += [f"- {f}" for f in listed_files] or ["(aucun fichier de code)"]
    lines.append("")
    lines.append(f"Fichiers de TESTS existants de ta zone ({len(tests)}) — ta source de "
                 f"vérité pour le statut « Couvert » :")
    if listed_tests:
        lines += [f"- {t}" for t in listed_tests]
    else:
        lines.append("(aucun test existant : tous les tests d'acceptance de cette zone "
                     "seront « Proposé ».)")
    overflow = (len(files) - len(listed_files)) + (len(tests) - len(listed_tests))
    if overflow > 0:
        lines.append(f"(+ {overflow} autre(s) fichier(s) non listé(s) : concentre-toi sur "
                     f"les parcours principaux ci-dessus.)")
    return "\n".join(lines)


def build_zone_prompt(grid_text: str, zone: dict, position: int, total: int,
                      doc_map: dict, feedback: str, attempt: int) -> str:
    zone_id = zone["id"]
    deliverable = zone_path(zone)
    sentinel = doc_sentinel(f"z{zone_id}", attempt)
    other_zones = "\n".join(f"- Z{z['id']} : {z['name']}"
                            for z in doc_map["zones"] if z is not zone) or "(aucune autre zone)"
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es un Agent Documentaliste comportemental ultra-spécialisé, affecté à UNE SEULE zone
fonctionnelle : Z{zone_id} « {zone['name']} ». C'est la passe {position}/{total} d'une
documentation découpée par zones.
DOCUMENTATION = LECTURE SEULE : tu ne modifies, ne corriges, ne crées AUCUN fichier du projet.
Tu n'écris QUE deux fichiers : ton fichier de zone, puis ta sentinelle de fin.
Ignore tout comportement relevant d'une AUTRE zone que la tienne : une passe dédiée s'en
charge (le documenter ici créerait des doublons) — au plus un renvoi d'une ligne « voir Z<n> ».

--- GRILLE DU DOCUMENTALISTE ---
{grid_text}

--- TA ZONE (assignée par la cartographie validée par l'humain) ---
Zone Z{zone_id} : {zone['name']}
Rôle annoncé (intent) : {zone.get('intent', '(non renseigné)')}

{build_zone_files_block(zone)}

--- LES AUTRES ZONES (pour les renvois « voir Z<n> » ; NE documente PAS leur contenu) ---
{other_zones}

--- CONTEXTE MÉTIER (optionnel) ---
{business_context_hint()}

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- LIVRABLE OBLIGATOIRE ---
Écris ta documentation de zone dans '{deliverable}' (crée le dossier '{DOC_DIR}/' au besoin)
en respectant STRICTEMENT le format de la grille ci-dessus : première ligne
'# Z{zone_id} : {zone['name']}', sections '## Features' et '## Bilan' obligatoires (Bilan au
format verrouillé) ; « Aucune feature utilisateur. » explicite si la zone est purement
technique. Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile
dans la console.

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{deliverable}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur. Ne le crée que lorsque le fichier de zone est VRAIMENT terminé.
"""
    with open(TMP_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_DOC_FILE}' à la racine du projet et documente "
            f"la zone Z{zone_id}.")


def extract_bilan_block(path: str) -> str:
    """Extrait les lignes du '## Bilan' d'un fichier de zone (quelques lignes par zone :
    la seule matière chiffrée dont la vue d'ensemble a besoin — jamais la zone entière)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return "(bilan illisible)"
    kept, in_bilan = [], False
    for line in lines:
        low = line.strip().lower()
        if low.startswith("## bilan"):
            in_bilan = True
            continue
        if in_bilan and (line.startswith("## ") or line.startswith("# ")):
            break
        if in_bilan and line.strip():
            kept.append(line.strip())
    return "\n".join(kept) or "(bilan vide)"


def build_overview_prompt(doc_map: dict, feedback: str, attempt: int) -> str:
    sentinel = doc_sentinel("overview", attempt)
    zones = doc_map["zones"]
    zone_lines = "\n".join(f"- Z{z['id']} « {z['name']} » : {z.get('intent', '')}" for z in zones)
    bilan_lines = "\n\n".join(f"Z{z['id']} « {z['name']} » — Bilan :\n{extract_bilan_block(zone_path(z))}"
                              for z in zones)
    full_context = f"""--- CONTRAT COMPORTEMENTAL ---
Tu es le Rédacteur de la vue d'ensemble d'une documentation comportementale réalisée en
{len(zones)} passes indépendantes (une par zone fonctionnelle). Tu rédiges le CHAPEAU de
lecture, et rien d'autre : tu ne relis PAS les fichiers de zone, tu ne relis PAS le code
du projet. ZÉRO invention : uniquement ce que les intitulés, intents et bilans ci-dessous
portent. Tu ne modifies aucun fichier du projet ; tu n'écris QUE la vue d'ensemble, puis
ta sentinelle.

--- LE PROJET ---
Nom : {doc_map.get('project', '(non renseigné)')}

--- LES ZONES (dans l'ordre de lecture de la documentation finale) ---
{zone_lines}

--- LES BILANS CHIFFRÉS PAR ZONE (extraits par l'orchestrateur) ---
{bilan_lines}

--- LIVRABLE OBLIGATOIRE : '{OVERVIEW_FILE}' ---
Un texte de 15 à 30 lignes, commençant EXACTEMENT par la ligne '## Vue d'ensemble', qui
donne au lecteur : ce que fait le produit et pour qui, les parcours majeurs (déduits des
seuls intents), et comment lire la documentation (les zones dans l'ordre ci-dessus ;
« Couvert » = tests d'acceptance vérifiés par un test existant cité, « Proposé » = tests à
écrire). Langage utilisateur, aucun jargon interne, aucune liste de fichiers.
Fais-le directement via tes outils d'édition de fichier, sans bavardage inutile dans la console.

--- RETOUR DE L'ORCHESTRATEUR À CORRIGER (le cas échéant) ---
{feedback}

--- INSTRUCTION DE FIN OBLIGATOIRE ---
En toute DERNIÈRE action, après avoir sauvegardé '{OVERVIEW_FILE}', crée le fichier
sentinelle '{sentinel}' à la racine (contenu : le seul mot done) : c'est le signal de fin
pour l'orchestrateur.
"""
    with open(TMP_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Lis le fichier de consignes '{TMP_DOC_FILE}' à la racine du projet et rédige "
            f"la vue d'ensemble de la documentation.")


# ─── ÉTAPE É1 : CARTOGRAPHIE (1 PASSE LLM + DOUBLE VALIDATION) ────────────────

def print_doc_map_recap(doc_map: dict, soft: list):
    """Récapitulatif humain de la carte : le tableau que l'on valide au y/n."""
    zones = doc_map["zones"]
    print(f"\n{'='*60}")
    print(f"🗺️  CARTE FONCTIONNELLE — {doc_map.get('project', '(sans nom)')} : {len(zones)} zone(s)")
    print(f"{'Id':>4} | {'Zone':<30} | {'Code':>4} | {'Tests':>5} | Intent")
    print(f"{'-'*4}-+-{'-'*30}-+-{'-'*4}-+-{'-'*5}-+-{'-'*30}")
    for zone in zones:
        name = str(zone["name"])[:30]
        intent = str(zone.get("intent", ""))[:60]
        print(f"{zone['id']:>4} | {name:<30} | {len(zone.get('files') or []):>4} | "
              f"{len(zone.get('tests') or []):>5} | {intent}")
    if soft:
        print(f"\n⚠️  Points d'attention (non bloquants) :")
        for warning in soft:
            print(f"   - {warning}")
    print(f"\n   ✏️  La carte est ÉDITABLE : '{DOC_MAP_FILE}' (l'ordre des zones = l'ordre de "
          f"lecture de la documentation finale).")
    print(f"{'='*60}")


def confirm_doc_map(doc_map: dict, soft: list):
    """Validation humaine de la carte (le y/n qui arbitre AVANT de payer N passes)."""
    print_doc_map_recap(doc_map, soft)
    confirm = input("\n▶️  Valider cette carte et lancer la documentation zone par zone ? (y/n) : ")
    mm_audit.event("gate", id="map", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Arrêt. Édite '{DOC_MAP_FILE}' puis relance (il sera repris tel quel), "
              f"ou supprime-le pour rejouer la cartographie.")
        RUNNER.kill()
        sys.exit(0)


def load_and_validate_map_file(code_files: list, test_files: list) -> tuple:
    """Charge + valide 'doc_map.yaml'. Renvoie (doc_map, fatal, soft, parse_error)."""
    try:
        with open(DOC_MAP_FILE, "r", encoding="utf-8") as f:
            doc_map = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, [], [], str(e)
    except OSError as e:
        return None, [], [], str(e)
    fatal, soft = validate_and_normalize_doc_map(doc_map, code_files, test_files)
    return doc_map, fatal, soft, ""


def run_cartography(grid_text: str, code_files: list, test_files: list) -> dict:
    """Étape É1 : produit (ou reprend) la carte des zones, doublement validée.

    Reprise : un 'doc_map.yaml' existant et valide saute la passe LLM (récapitulatif +
    y/n de nouveau affichés — c'est là que l'édition manuelle du YAML est prise en
    compte) ; un fichier existant mais structurellement invalide arrête le run avec
    consigne (corriger ou supprimer), même contrat que le blackboard.
    """
    if os.path.exists(DOC_MAP_FILE):
        doc_map, fatal, soft, parse_error = load_and_validate_map_file(code_files, test_files)
        if parse_error:
            fail_doc(f"❌ '{DOC_MAP_FILE}' existant mais non parsable : corrige-le ou "
                     f"supprime-le (la cartographie sera rejouée), puis relance.",
                     details=parse_error[:1500], title="Carte existante invalide")
        if fatal:
            fail_doc(f"❌ '{DOC_MAP_FILE}' existant mais structurellement invalide :\n   - "
                     + "\n   - ".join(fatal)
                     + f"\n   → Corrige-le ou supprime-le (la cartographie sera rejouée), puis relance.",
                     details="\n".join(fatal), title="Carte existante invalide")
        save_doc_map(doc_map)
        _DOC_MAP_STATE["map"] = doc_map
        print(f"♻️  '{DOC_MAP_FILE}' existant et valide : cartographie sautée (reprise).")
        # Carte écrite APRÈS l'arrêt d'un run resté sans clôture : livrable d'un agent
        # orphelin, à relire avant de la reprendre comme valide.
        residual = residual_deliverable_warning(DOC_MAP_FILE, "documentation")
        if residual:
            soft = list(soft) + [residual]
        confirm_doc_map(doc_map, soft)
        return doc_map

    print(f"\n{'='*50}\n🗺️  ÉTAPE É1 : CARTOGRAPHIE FONCTIONNELLE (1 passe LLM)\n{'='*50}")
    total_scope = len(code_files) + len(test_files)
    if total_scope > MAX_SCOPE_FILES_IN_CARTO:
        print(f"   ⚠️  Périmètre de {total_scope} fichiers > {MAX_SCOPE_FILES_IN_CARTO} : le "
              f"surplus sera résumé par répertoire dans le prompt et rangé en zone « Divers » "
              f"par le contrôle de couverture (dégradation assumée).")
    RUNNER.start()

    attempts = 0
    doc_map, soft = None, []
    feedback = "Premier passage — aucun retour précédent."

    while doc_map is None and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Rattrapage d'un livrable TARDIF : l'agent de la tentative précédente a pu finir
        # d'écrire APRÈS le timeout de l'orchestrateur. Si sa carte est devenue valide
        # entre-temps, on la prend telle quelle plutôt que de payer un tour pour tout refaire.
        if attempts > 1 and os.path.exists(DOC_MAP_FILE):
            late_map, late_fatal, late_soft, late_err = load_and_validate_map_file(code_files, test_files)
            if not late_err and not late_fatal:
                print(f"   ♻️  '{DOC_MAP_FILE}' est finalement arrivé (livrable tardif) : accepté.")
                doc_map, soft = late_map, late_soft
                break

        cleanup_slot_sentinels("map")
        print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Lancement du Cartographe fonctionnel...")

        prompt = build_carto_prompt(grid_text, code_files, test_files, feedback, attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable = wait_for_deliverable(DOC_MAP_FILE, doc_sentinel("map", attempts),
                                               structural_check=map_structural_check)
        # Garde read-only après CHAQUE tentative (aboutie ou non) : un cartographe qui a
        # « corrigé » du code en cours de route est restauré immédiatement.
        enforce_readonly("Carto")

        if not got_deliverable:
            feedback = (f"Au passage précédent, aucun livrable n'a été reçu ('{DOC_MAP_FILE}' "
                        f"absent, vide ou jamais signalé). Écris d'abord la carte YAML complète, "
                        f"PUIS la sentinelle, dans cet ordre.")
            print(f"⏱️  Le cartographe n'a pas signalé la fin de sa passe. Nouvelle tentative.")
            if os.path.exists(DOC_MAP_FILE) and not map_structural_check(DOC_MAP_FILE):
                try:
                    os.remove(DOC_MAP_FILE)
                except OSError:
                    pass
            RUNNER.new_context()
            continue

        candidate, fatal, cand_soft, parse_error = load_and_validate_map_file(code_files, test_files)
        if parse_error:
            feedback = (f"Ton '{DOC_MAP_FILE}' n'est pas du YAML parsable "
                        f"(erreur : {parse_error[:400]}). Rappels : AUCUNE balise ```, toutes "
                        f"les valeurs textuelles entre guillemets doubles, guillemets internes "
                        f"échappés (\\\"). Réécris le fichier entièrement.")
            print(f"⚠️  [REJET] Tentative {attempts} : YAML non parsable.")
        elif fatal:
            feedback = ("Ta carte ne respecte pas le schéma de la grille : "
                        + " ; ".join(fatal)
                        + " Rappels : chemins RECOPIÉS depuis les listes fournies (jamais "
                          "inventés), chaque zone avec un id entier unique, un name et au "
                          "moins un fichier existant. Réécris le fichier entièrement.")
            print(f"⚠️  [REJET] Tentative {attempts} : carte structurellement invalide "
                  f"({len(fatal)} anomalie(s)).")
        elif divers_size(candidate) > DIVERS_RETRY_THRESHOLD and attempts < MAX_ATTEMPTS:
            # Une « Divers » qui contient l'essentiel du projet n'est pas une cartographie :
            # on rejoue tant qu'il reste des tentatives, en nommant les répertoires à assigner.
            overflow = divers_size(candidate)
            feedback = (f"Ta carte laisse {overflow} fichiers en zone « Divers » (résiduel), soit "
                        f"l'essentiel du projet : ce n'est pas un découpage fonctionnel. Assigne-les "
                        f"à des zones fonctionnelles nommées, PAR RÉPERTOIRE (entrée de files: "
                        f"terminée par '/'). Répertoires concernés :\n"
                        + summarize_by_directory(divers_files(candidate)))
            print(f"⚠️  [REJET] Tentative {attempts} : {overflow} fichiers en « Divers » "
                  f"(> {DIVERS_RETRY_THRESHOLD}) — la carte ne découpe pas le projet.")
        else:
            doc_map, soft = candidate, cand_soft
            break

        try:
            os.remove(DOC_MAP_FILE)
        except OSError:
            pass
        RUNNER.new_context()

    if doc_map is None:
        cleanup_all_doc_sentinels()
        print_pass_failure("Cartographie", feedback)
        fail_doc(f"❌ Cartographie non aboutie après {MAX_ATTEMPTS} tentatives.", details=feedback)

    cleanup_slot_sentinels("map")
    save_doc_map(doc_map)
    _DOC_MAP_STATE["map"] = doc_map
    # Contexte réinitialisé avant la première passe de zone : la conversation du
    # cartographe ne doit pas fuiter dans les passes suivantes.
    RUNNER.new_context()
    confirm_doc_map(doc_map, soft)
    return doc_map


# ─── ÉTAPE É2 : N PASSES DE DOCUMENTATION (UNE PAR ZONE) ──────────────────────

def warn_orphan_zone_files(doc_map: dict):
    """Fichiers de 'doc_zones/' ne correspondant à aucune zone de la carte (carte rééditée
    à la main, p. ex.) : signalés en début d'étape, JAMAIS supprimés (décision humaine)."""
    if not os.path.isdir(DOC_DIR):
        return
    expected = {os.path.basename(zone_path(zone)) for zone in doc_map["zones"]}
    orphans = sorted(name for name in os.listdir(DOC_DIR)
                     if name.startswith("Z") and name.endswith(".md") and name not in expected)
    if orphans:
        print(f"⚠️  Fichier(s) orphelin(s) dans '{DOC_DIR}/' (aucune zone de la carte ne les "
              f"produit — carte rééditée ?) : {', '.join(orphans)}. Non supprimés ; ils ne "
              f"seront PAS assemblés.")


def run_doc_passes(grid_text: str, doc_map: dict, test_scope: set):
    """Le cœur MAIsterMind : une session neuve par zone, une tranche de contexte par passe."""
    zones = doc_map["zones"]
    total = len(zones)
    warn_orphan_zone_files(doc_map)

    for position, zone in enumerate(zones, start=1):
        zone_id = zone["id"]
        deliverable = zone_path(zone)

        # Reprise par fichiers : un fichier de zone exploitable saute sa passe. Deux
        # signaux warn-only accompagnent le saut (jamais de rejeu silencieux ni payant) :
        # la FRAÎCHEUR (des fichiers de la zone ont changé depuis) et les écarts de
        # contenu d'un fichier issu d'un ancien run (produit avant les gardes).
        if zone_ok(deliverable):
            stale = stale_zone_sources(zone, deliverable)
            if stale:
                print(f"⏭️  Passe Z{zone_id} ({position}/{total}) déjà documentée MAIS PÉRIMÉE : "
                      f"{len(stale)} fichier(s) de la zone modifié(s) depuis "
                      f"('{stale[0]}'{'…' if len(stale) > 1 else ''}). Sautée quand même — "
                      f"supprime '{deliverable}' et relance pour la re-documenter.")
            else:
                print(f"⏭️  Passe Z{zone_id} ({position}/{total}) déjà documentée ('{deliverable}') : sautée.")
            legacy_issues = zone_content_issues(deliverable, test_scope, zone.get("files"))
            if legacy_issues:
                print(f"   ⚠️  Écart(s) vérifiable(s) dans ce fichier repris (ancien run ?) : "
                      f"{len(legacy_issues)} — supprime '{deliverable}' et relance pour le "
                      f"régénérer sous garde. Premier écart : {legacy_issues[0]}")
            continue
        if os.path.exists(deliverable):
            # Résidu à moitié écrit d'un run interrompu : on repart proprement.
            try:
                os.remove(deliverable)
                print(f"🧹 '{deliverable}' résiduel (incomplet) supprimé : la passe est rejouée.")
            except OSError:
                pass

        print(f"\n{'='*50}\n📝 PASSE Z{zone_id} ({position}/{total}) : {zone['name']}\n{'='*50}")

        attempts = 0
        success = False
        feedback = "Premier passage — aucun retour précédent."

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1

            # Rattrapage d'un livrable TARDIF (même logique que l'audit) — accepté aux
            # MÊMES conditions qu'un livrable nominal : structure ET gardes de contenu.
            if attempts > 1 and zone_ok(deliverable) \
                    and not zone_content_issues(deliverable, test_scope, zone.get("files")):
                print(f"   ♻️  '{deliverable}' est finalement arrivé (livrable tardif) : accepté.")
                success = True
                break

            cleanup_slot_sentinels(f"z{zone_id}")
            print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Passe Z{zone_id} — lancement du "
                  f"Documentaliste comportemental...")

            prompt = build_zone_prompt(grid_text, zone, position, total, doc_map,
                                       feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(prompt))
            RUNNER.send_task(prompt)

            got_deliverable = wait_for_deliverable(deliverable,
                                                   doc_sentinel(f"z{zone_id}", attempts),
                                                   structural_check=zone_structural_check)
            # Garde read-only après CHAQUE tentative (aboutie ou non) : un documentaliste
            # qui a « corrigé » du code en le lisant est restauré immédiatement.
            enforce_readonly(f"Z{zone_id}")

            if not got_deliverable:
                feedback = ("Au passage précédent, aucun livrable n'a été reçu (fichier de "
                            "zone absent, vide ou jamais signalé). Écris d'abord le fichier "
                            "de zone complet, PUIS la sentinelle, dans cet ordre.")
                print(f"⏱️  Le documentaliste n'a pas signalé la fin de la passe Z{zone_id}. "
                      f"Nouvelle tentative.")
                RUNNER.new_context()
                continue

            # Plancher structurel APRÈS coup, même quand la sentinelle est arrivée : le
            # chemin sentinelle de wait_for_deliverable ne vérifie pas la structure, et un
            # fichier hors format serait inassemblable (Bilan illisible, sections absentes).
            if not zone_structural_check(deliverable):
                feedback = (f"Ton fichier '{deliverable}' ne respecte pas le format demandé : "
                            f"les sections '## Features' (avec des features au format de la "
                            f"grille, ou la seule ligne « Aucune feature utilisateur. ») et "
                            f"'## Bilan' (deux lignes au format verrouillé) sont OBLIGATOIRES. "
                            f"Réécris-le entièrement au bon format.")
                try:
                    os.remove(deliverable)
                except OSError:
                    pass
                print(f"⚠️  [REJET] Tentative {attempts} : fichier de zone hors format "
                      f"(sections obligatoires absentes).")
                RUNNER.new_context()
                continue

            # ── GARDES DE CONTENU (mécaniques, zéro LLM) ── : sources citées existantes,
            # « Couvert » adossé à un test réel, Bilan égal au comptage réel. L'écart
            # EXACT est renvoyé au documentaliste — pas un jugement, un fait vérifiable.
            issues = zone_content_issues(deliverable, test_scope, zone.get("files"))
            if issues:
                feedback = ("Ton fichier de zone contient des écarts VÉRIFIABLES :\n- "
                            + "\n- ".join(issues)
                            + "\nCorrige-les puis réécris le fichier entièrement (zéro "
                              "invention : ne cite que des chemins réels de ta zone).")
                try:
                    os.remove(deliverable)
                except OSError:
                    pass
                print(f"🛡️  [REJET] Tentative {attempts} : {len(issues)} écart(s) vérifiable(s) "
                      f"dans le fichier de zone (source citée introuvable, « Couvert » sans "
                      f"test réel ou Bilan faux).")
                RUNNER.new_context()
                continue

            success = True
            warn_uncited_zone_files(deliverable, zone)

        if not success:
            reason = feedback
            cleanup_all_doc_sentinels()
            print_pass_failure(f"Z{zone_id} : {zone['name']}", reason)
            fail_doc(f"❌ Passe Z{zone_id} non aboutie après {MAX_ATTEMPTS} tentatives.",
                     details=reason)

        print(f"✅ Passe Z{zone_id} terminée : documentation dans '{deliverable}'.")
        cleanup_slot_sentinels(f"z{zone_id}")
        RUNNER.new_context()


# ─── ÉTAPE É3 : VUE D'ENSEMBLE (CHAPEAU DE LECTURE) ───────────────────────────

def mechanical_overview(doc_map: dict) -> str:
    """Fallback 100 % Python de la vue d'ensemble : l'échec du chapeau ne doit jamais
    invalider N passes réussies — le contenu de valeur est déjà dans les zones."""
    zones = doc_map["zones"]
    lines = ["## Vue d'ensemble", "",
             f"Le projet « {doc_map.get('project', '(sans nom)')} » est documenté en "
             f"{len(zones)} zones fonctionnelles, présentées dans l'ordre de lecture "
             f"ci-dessous. (Vue d'ensemble générée mécaniquement : la passe de rédaction "
             f"n'a pas abouti.)", ""]
    for zone in zones:
        lines.append(f"- **Z{zone['id']} — {zone['name']}** : {zone.get('intent', '')}")
    lines += ["",
              "Chaque zone décrit ses features puis leurs tests d'acceptance : « Couvert » "
              "signifie vérifié par un test existant du projet (cité) ; « Proposé » signifie "
              "un test d'acceptance à écrire."]
    return "\n".join(lines) + "\n"


def run_overview(doc_map: dict):
    """Étape É3 : le seul contenu du livrable final qui demande une vraie rédaction
    transverse — court, donc confiable à un agent sans risque de saturation.
    TOUJOURS rejouée (elle doit refléter les zones à jour)."""
    print(f"\n{'='*50}\n🪧 ÉTAPE É3 : VUE D'ENSEMBLE (CHAPEAU DE LECTURE)\n{'='*50}")

    if os.path.exists(OVERVIEW_FILE):
        try:
            os.remove(OVERVIEW_FILE)
            print(f"   🧹 '{OVERVIEW_FILE}' résiduel supprimé (la vue d'ensemble est régénérée).")
        except OSError:
            pass

    attempts = 0
    success = False
    feedback = "Premier passage — aucun retour précédent."
    while not success and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Rattrapage d'un livrable TARDIF (même logique que les autres passes).
        if attempts > 1 and os.path.exists(OVERVIEW_FILE) \
                and os.path.getsize(OVERVIEW_FILE) > 0 \
                and overview_structural_check(OVERVIEW_FILE):
            print(f"   ♻️  '{OVERVIEW_FILE}' est finalement arrivé (livrable tardif) : accepté.")
            success = True
            break

        cleanup_slot_sentinels("overview")
        print(f"\n🚀 [TENTATIVE {attempts}/{MAX_ATTEMPTS}] Lancement du Rédacteur de la vue d'ensemble...")

        prompt = build_overview_prompt(doc_map, feedback, attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable = wait_for_deliverable(OVERVIEW_FILE,
                                               doc_sentinel("overview", attempts),
                                               structural_check=overview_structural_check)
        enforce_readonly("Overview")

        if not got_deliverable or not overview_structural_check(OVERVIEW_FILE):
            if os.path.exists(OVERVIEW_FILE) and not overview_structural_check(OVERVIEW_FILE):
                try:
                    os.remove(OVERVIEW_FILE)
                except OSError:
                    pass
            feedback = (f"Au passage précédent, la vue d'ensemble était absente ou hors format : "
                        f"le fichier '{OVERVIEW_FILE}' doit commencer EXACTEMENT par la ligne "
                        f"'## Vue d'ensemble' (15 à 30 lignes au total).")
            print("⏱️  Vue d'ensemble absente ou hors format. Nouvelle tentative.")
            RUNNER.new_context()
            continue
        success = True

    cleanup_slot_sentinels("overview")
    if not success:
        # DÉGRADATION GRACIEUSE (différence assumée avec la synthèse d'audit, bloquante) :
        # l'échec du chapeau ne doit pas invalider N passes réussies — fallback mécanique.
        print(f"⚠️  Vue d'ensemble non aboutie après {MAX_ATTEMPTS} tentatives : fallback "
              f"MÉCANIQUE (liste des zones et intents). Le contenu de valeur est dans les zones.")
        with open(OVERVIEW_FILE, "w", encoding="utf-8") as f:
            f.write(mechanical_overview(doc_map))
        RUNNER.new_context()
        return

    print(f"✅ Vue d'ensemble prête : '{OVERVIEW_FILE}'.")


# ─── ÉTAPE É4 : ASSEMBLAGE (PYTHON DÉTERMINISTE, ZÉRO LLM, ZÉRO PERTE) ────────
# Le contrat « tu recopies, tu n'inventes pas » du compilateur blackboard, devenu du CODE :
# concaténation dans l'ordre du doc_map, sommaire et compteurs générés mécaniquement.
# Aucune perte possible, quel que soit le volume (décision D2).

# Les deux lignes du Bilan, verrouillées par la grille doc-zone (parse TOLÉRANT ;
# apostrophes droite/typographique acceptées). Depuis les gardes de contenu, le Bilan
# déclaré est CONFRONTÉ au comptage mécanique (count_zone_content) : à la production
# l'écart est un rejet, à l'assemblage ce sont TOUJOURS les compteurs recomptés qui
# font foi (un fichier d'ancien run ne fausse jamais l'annexe de couverture).
BILAN_FEATURES_RE = re.compile(r"^\s*-\s*\**Features\**\s*:\s*(\d+)", re.IGNORECASE)
BILAN_ATS_RE = re.compile(
    r"^\s*-\s*\**Tests d.accept(?:ance|ation)\**\s*:\s*(\d+)\s*"
    r"\(\s*couverts?\s*:\s*(\d+)\s*[,;]\s*propos[ée]s?\s*:\s*(\d+)\s*\)", re.IGNORECASE)

FEATURE_HEADING_RE = re.compile(r"^###\s+(F\d+\s*[—–-].+)$")


def iter_lines_with_fence_state(content: str):
    """Itère (ligne, in_fence) : les lignes à l'intérieur des blocs ``` / ~~~ sont marquées
    pour que ni le décalage de titres ni les extractions ne s'y appliquent."""
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            yield line, True
            continue
        yield line, in_fence


def shift_headings(content: str) -> str:
    """Décale tous les titres d'un niveau ('# ' → '## ', etc.) pour garder un unique H1
    dans le document final. Les lignes dans les fences sont ignorées."""
    out = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if not in_fence and line.startswith("#"):
            out.append("#" + line)
        else:
            out.append(line)
    return "\n".join(out)


def linkify_cited_paths(content: str) -> str:
    """Rend cliquables les chemins cités entre backticks (`src/x.ts:42` →
    [`src/x.ts:42`](src/x.ts#L42)) quand le fichier existe. Purement cosmétique et
    best-effort : jamais dans les fences, jamais sur un token déjà lié, jamais sur un
    chemin introuvable (le lien mort serait pire que pas de lien). 'documentation.md'
    vivant à la racine, les chemins relatifs du périmètre sont directement les bons."""
    def replace(match):
        token = match.group(1)
        if not looks_like_path(token):
            return match.group(0)
        clean = clean_cited(token)
        if not os.path.exists(clean):
            return match.group(0)
        line_match = re.search(r":L?(\d+)", token)
        anchor = f"#L{line_match.group(1)}" if line_match else ""
        return f"[`{token}`]({clean}{anchor})"

    out = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence or "`" not in line:
            out.append(line)
            continue
        out.append(re.sub(r"(?<!\[)`([^`\n]+)`(?!\]\()", replace, line))
    return "\n".join(out)


def parse_zone_bilan(content: str) -> dict:
    """Compteurs du '## Bilan' d'une zone. Valeur manquante → None (affichée « ? »)."""
    result = {"features": None, "ats": None, "covered": None, "proposed": None}
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        match = BILAN_FEATURES_RE.match(line)
        if match:
            result["features"] = int(match.group(1))
            continue
        match = BILAN_ATS_RE.match(line)
        if match:
            result["ats"] = int(match.group(1))
            result["covered"] = int(match.group(2))
            result["proposed"] = int(match.group(3))
    return result


def extract_feature_titles(content: str) -> list:
    """Titres des features ('### F1 — …') d'un fichier de zone, pour le sommaire."""
    titles = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        match = FEATURE_HEADING_RE.match(line.strip())
        if match:
            titles.append(match.group(1).strip())
    return titles


def extract_zone_heading(content: str, zone: dict) -> str:
    """Titre H1 réel du fichier de zone (celui que porte le document assemblé), avec
    repli sur le titre calculé depuis la carte."""
    for line, in_fence in iter_lines_with_fence_state(content):
        if not in_fence and line.startswith("# ") :
            return line[2:].strip()
    return f"Z{zone.get('id')} : {zone.get('name')}"


def github_anchor(heading: str) -> str:
    """Ancre de titre façon GitHub, best-effort (purement cosmétique, jamais bloquant) :
    minuscules, espaces → tirets, ponctuation retirée."""
    out = []
    for ch in str(heading).strip().lower():
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch == " ":
            out.append("-")
    return "".join(out)


def fmt_count(value) -> str:
    """Affichage tolérant d'un compteur de Bilan : None → « ? »."""
    return "?" if value is None else str(value)


def escape_md_cell(text: str) -> str:
    """Neutralise les barres verticales dans une cellule de tableau Markdown."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def assemble_documentation(doc_map: dict) -> dict:
    """Étape É4 : le « compilateur » final. TOUJOURS rejoué en fin de run (reflète les
    zones à jour). Renvoie les stats pour la bannière finale."""
    print(f"\n{'='*50}\n🧩 ÉTAPE É4 : ASSEMBLAGE MÉCANIQUE → '{DOC_FILE}'\n{'='*50}")
    zones = doc_map["zones"]
    project = str(doc_map.get("project") or os.path.basename(os.getcwd()))

    entries = []
    for zone in zones:
        path = zone_path(zone)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            content = ""
        if not content:
            # Ne devrait pas arriver (les passes garantissent les fichiers) : placeholder
            # explicite plutôt qu'un trou silencieux.
            content = (f"# Z{zone.get('id')} : {zone.get('name')}\n\n"
                       f"*(Fichier de zone manquant ou vide au moment de l'assemblage : "
                       f"supprime '{path}' et relance pour rejouer cette passe.)*")
            print(f"   ⚠️  '{path}' manquant ou vide : section remplacée par un placeholder.")
        # Les compteurs affichés sont TOUJOURS recomptés mécaniquement depuis le contenu
        # (count_zone_content) : un Bilan déclaré faux — fichier d'un ancien run, édition
        # manuelle — est signalé mais ne fausse jamais la carte ni l'annexe.
        mech = count_zone_content(content)
        declared = parse_zone_bilan(content)
        if any(declared[key] is not None and declared[key] != mech[key] for key in mech):
            print(f"   ⚠️  '{path}' : Bilan déclaré ≠ contenu réel — l'assemblage affiche les "
                  f"compteurs RECOMPTÉS ({mech['features']} feature(s), {mech['ats']} AT dont "
                  f"{mech['covered']} couvert(s)).")
        entries.append({
            "zone": zone,
            "content": content,
            "heading": extract_zone_heading(content, zone),
            "features": extract_feature_titles(content),
            "bilan": mech,
        })

    def total_of(key):
        return sum(e["bilan"][key] for e in entries if e["bilan"][key] is not None)

    stats = {"zones": len(zones), "features": total_of("features"), "ats": total_of("ats"),
             "covered": total_of("covered"), "proposed": total_of("proposed")}

    annexe_title = "Annexe — Couverture des tests d'acceptance"
    parts = [f"# Documentation comportementale — {project}", "", DOC_MARKER, "",
             f"*Générée le {time.strftime('%Y-%m-%d')} par `Documentation.py` — "
             f"{stats['zones']} zone(s), {stats['features']} feature(s), {stats['ats']} test(s) "
             f"d'acceptance ({stats['covered']} couvert(s), {stats['proposed']} proposé(s)) ; "
             f"compteurs recomptés mécaniquement à l'assemblage.*", ""]

    # Vue d'ensemble : contenu du chapeau (il porte déjà son titre '## Vue d'ensemble'),
    # ou fallback mécanique si le fichier manque/est hors format.
    if os.path.exists(OVERVIEW_FILE) and overview_structural_check(OVERVIEW_FILE):
        with open(OVERVIEW_FILE, "r", encoding="utf-8") as f:
            parts.append(f.read().strip())
    else:
        parts.append(mechanical_overview(doc_map).strip())
    parts.append("")

    # Sommaire : zones → features, liens d'ancre best-effort (règle de slug GitHub).
    parts += ["## Sommaire", "",
              "- [Carte des zones](#carte-des-zones)"]
    for entry in entries:
        parts.append(f"- [{entry['heading']}](#{github_anchor(entry['heading'])})")
        for feat in entry["features"]:
            parts.append(f"  - [{feat}](#{github_anchor(feat)})")
    parts.append(f"- [{annexe_title}](#{github_anchor(annexe_title)})")
    parts.append("")

    # Carte des zones : le tableau de lecture rapide (compteurs parsés des Bilans).
    parts += ["## Carte des zones", "",
              "| Zone | Rôle | Features | Tests d'acceptance (couverts / proposés) |",
              "|---|---|---|---|"]
    for entry in entries:
        zone, bilan = entry["zone"], entry["bilan"]
        parts.append(f"| [{escape_md_cell(entry['heading'])}](#{github_anchor(entry['heading'])}) "
                     f"| {escape_md_cell(zone.get('intent', ''))} "
                     f"| {fmt_count(bilan['features'])} "
                     f"| {fmt_count(bilan['ats'])} ({fmt_count(bilan['covered'])} / "
                     f"{fmt_count(bilan['proposed'])}) |")
    parts.append("")

    # Le corps : les zones décalées d'un niveau de titre, dans l'ordre du doc_map
    # (c'est le tri de niveau zone, décidé en É1 et validé par l'humain), avec les
    # sources citées rendues cliquables (cosmétique, best-effort).
    for entry in entries:
        parts += ["---", "", shift_headings(linkify_cited_paths(entry["content"])), ""]

    # Annexe de couverture : totaux par zone + total général.
    parts += ["---", "", f"## {annexe_title}", "",
              "« Couvert » : le test d'acceptance est vérifié par un test EXISTANT du projet "
              "(cité dans la zone). « Proposé » : test d'acceptance restant À ÉCRIRE.", "",
              "| Zone | Tests d'acceptance | Couverts | Proposés |",
              "|---|---|---|---|"]
    for entry in entries:
        bilan = entry["bilan"]
        parts.append(f"| {escape_md_cell(entry['heading'])} | {fmt_count(bilan['ats'])} "
                     f"| {fmt_count(bilan['covered'])} | {fmt_count(bilan['proposed'])} |")
    parts.append(f"| **Total** | **{stats['ats']}** | **{stats['covered']}** "
                 f"| **{stats['proposed']}** |")
    parts.append("")

    # Écriture ATOMIQUE : fichier temporaire DANS le projet (pas /tmp — contrainte 3 OS)
    # puis os.replace — un Ctrl+C pendant l'écriture ne laisse jamais un livrable tronqué.
    tmp = f"{DOC_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    os.replace(tmp, DOC_FILE)
    print(f"✅ '{DOC_FILE}' assemblé : {stats['zones']} zone(s), {stats['features']} feature(s), "
          f"{stats['ats']} test(s) d'acceptance.")
    return stats


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Journal de run (boîte noire) : trace auto-suffisante dans .mm-runs/.
    mm_audit.start(os.getcwd(), "documentation", RUNNER.name,
                   model=RUNNER.configured_model())
    # Un failReport.md résiduel d'un run précédent ne doit pas être pris pour celui du
    # run courant : on le purge au démarrage (même contrat que l'usine).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # Les grilles sont le référentiel de TOUT le pipeline : leur absence est un échec
    # immédiat (sans elles, les agents improviseraient — exactement ce que l'usine interdit).
    map_grid = load_grid(DOC_MAP_SKILL_FILE)
    zone_grid = load_grid(DOC_ZONE_SKILL_FILE)
    missing_grids = [path for path, text in ((DOC_MAP_SKILL_FILE, map_grid),
                                             (DOC_ZONE_SKILL_FILE, zone_grid))
                     if not text.strip()]
    if missing_grids:
        print(f"❌ Grille(s) manquante(s) ou vide(s) : {', '.join(missing_grids)}.")
        write_fail_report("Grille de documentation manquante",
                          f"Introuvable(s) ou vide(s) : {', '.join(missing_grids)} — impossible "
                          f"de documenter sans référentiel.")
        sys.exit(1)

    # Étape É0 : périmètre découvert par PYTHON (déterministe), montré à l'humain AVANT
    # de payer le moindre tour d'agent.
    code_files, test_files = discover_code_scope()
    if not code_files and not test_files:
        print("❌ Aucun fichier de code trouvé dans ce répertoire (extensions cherchées : "
              + ", ".join(sorted(CODE_EXTENSIONS)) + ").")
        print("   → Lance la documentation depuis la racine du projet à documenter.")
        write_fail_report("Périmètre de documentation vide",
                          "Aucun fichier de code détecté dans le répertoire courant.")
        sys.exit(1)

    existing_map = peek_doc_map()
    manual_doc = False
    if os.path.exists(DOC_FILE):
        try:
            with open(DOC_FILE, "r", encoding="utf-8") as f:
                _doc_txt = f.read()
                manual_doc = DOC_MARKER not in _doc_txt and DOC_MARKER_LEGACY not in _doc_txt
        except OSError:
            manual_doc = True

    preview_code = code_files[:15]
    preview_tests = test_files[:5]

    print(f"\n{'='*50}")
    print(f"📚 DOCUMENTATION COMPORTEMENTALE — Périmètre découvert :")
    print(f"   Répertoire : {os.getcwd()}")
    print(f"   {len(code_files)} fichier(s) de code + {len(test_files)} fichier(s) de tests. Aperçu :")
    for f in preview_code:
        print(f"      - {f}")
    if len(code_files) > len(preview_code):
        print(f"      … et {len(code_files) - len(preview_code)} autre(s) fichier(s) de code.")
    if preview_tests:
        print(f"   Tests (source de vérité pour le statut « Couvert ») :")
        for f in preview_tests:
            print(f"      - {f}")
        if len(test_files) > len(preview_tests):
            print(f"      … et {len(test_files) - len(preview_tests)} autre(s) fichier(s) de tests.")
    context = business_context_file()
    if context:
        print(f"   Contexte métier : '{context}' détecté (pointé aux agents en lecture optionnelle).")
    else:
        print(f"   Contexte métier : aucun ('{SPEC_FILE}'/'{NEED_FILE}' absents) — le comportement "
              f"est documenté tel que le code le montre.")
    if existing_map:
        done = documented_count(existing_map)
        print(f"   Reprise : carte existante ({len(existing_map['zones'])} zone(s)), "
              f"{done}/{len(existing_map['zones'])} zone(s) déjà documentée(s) dans '{DOC_DIR}/'.")
        # FRAÎCHEUR (warn-only) : une zone documentée dont le code a changé depuis est
        # probablement périmée. L'humain décide — il peut supprimer les fichiers listés
        # MAINTENANT, avant de répondre y : seules les zones manquantes sont rejouées.
        stale_zones = [(zone, stale_zone_sources(zone, zone_path(zone)))
                       for zone in existing_map["zones"]
                       if isinstance(zone, dict) and zone_ok(zone_path(zone))]
        stale_zones = [(zone, stale) for zone, stale in stale_zones if stale]
        if stale_zones:
            print(f"   ⚠️  {len(stale_zones)} zone(s) documentée(s) probablement PÉRIMÉE(S) "
                  f"(code modifié après leur documentation) :")
            for zone, stale in stale_zones:
                print(f"      - Z{zone.get('id')} « {zone.get('name')} » : {len(stale)} "
                      f"fichier(s) modifié(s) (ex. {stale[0]}) → supprime "
                      f"'{zone_path(zone)}' pour la re-documenter.")
            print(f"      Tu peux les supprimer maintenant, avant de valider : seules les "
                  f"zones manquantes seront rejouées.")
    zones_label = f"{len(existing_map['zones'])}" if existing_map else "N (déterminé par la carte)"
    print(f"   Déroulé : 1 cartographie (sautée si '{DOC_MAP_FILE}' valide) + {zones_label} passe(s) "
          f"de documentation (une par zone, contexte réinitialisé entre chaque) + 1 vue "
          f"d'ensemble + assemblage Python → '{DOC_FILE}' (racine).")
    if manual_doc:
        print(f"   ⚠️  ATTENTION : un '{DOC_FILE}' SANS marqueur d'usine existe à la racine "
              f"(documentation écrite à la main ?). L'assemblage final l'ÉCRASERA — sauvegarde-le "
              f"avant de valider si tu veux le conserver.")
    print(f"{'='*50}")

    confirm = input("\n▶️  Lancer la documentation sur ce périmètre ? (y/n) : ")
    mm_audit.event("gate", id="scope", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print("⏹️  Annulé par l'utilisateur.")
        sys.exit(0)

    # Garde read-only : baseline capturée AVANT le premier agent.
    init_readonly_guard()

    # Étape É1 : cartographie (LLM seulement si nécessaire — reprise par fichiers),
    # doublement validée (schéma Python + y/n humain, carte éditable avant de valider).
    doc_map = run_cartography(map_grid, code_files, test_files)

    # 🚀 Boot du harness Data Center dans tmux (no-op si la cartographie l'a déjà lancé).
    RUNNER.start()

    # Étape É2 : les N passes de documentation (une session neuve par zone). Le périmètre
    # des tests est passé aux gardes de contenu (un « Couvert » doit citer un test réel).
    run_doc_passes(zone_grid, doc_map, set(test_files))

    # Étape É3 : vue d'ensemble (non bloquante : fallback mécanique après 3 échecs).
    run_overview(doc_map)

    # Étape É4 : assemblage mécanique du livrable final.
    stats = assemble_documentation(doc_map)

    # Dernier passage de la garde read-only : couvre la fenêtre entre le dernier enforce
    # d'une passe et la fin du run (notamment le chemin « livrable tardif accepté »).
    enforce_readonly("final")

    # Nettoyage des fichiers temporaires et sentinelles, puis fermeture propre.
    for tmp_f in [TMP_DOC_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_doc_sentinels()
    RUNNER.kill()
    # Run réussi : aucun rapport d'échec ne doit subsister.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    print(f"""
🏁 [CONGRATULATIONS] Documentation comportementale terminée !
   📄 Livrable : '{DOC_FILE}' (racine) — {stats['zones']} zone(s), {stats['features']} feature(s),
      {stats['ats']} test(s) d'acceptance ({stats['covered']} couvert(s), {stats['proposed']} proposé(s)).
   🗂️  Détail par zone : '{DOC_DIR}/' ; carte des zones : '{DOC_MAP_FILE}'.
   ♻️  Pour re-documenter UNE zone (après évolution du code, p. ex.) : supprime son fichier
      dans '{DOC_DIR}/' et relance — seul le manquant est rejoué, l'assemblage est refait.
      Pour tout refaire (carte comprise) : supprime '{DOC_DIR}/' et '{DOC_MAP_FILE}' puis relance.""")
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
