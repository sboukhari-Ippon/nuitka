#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_runner — abstraction du HARNESS d'agent IA pour les orchestrateurs MAIsterMind
─────────────────────────────────────────────────────────────────────────────────
Ce module porte, et lui seul, tout ce que les orchestrateurs savent de l'agent IA
qu'ils pilotent : le nom de sa session tmux, la commande qui le lance, la façon de
lui coller un prompt, de repartir d'un contexte vierge, de capturer son écran et
de le tuer. Avant lui, cette couche était recopiée dans les 10 orchestrateurs et
chaque changement de harness imposait un fork complet du produit.

Ce que ce module ne fait PAS, et ne doit jamais faire :
  - juger la réussite d'une tâche. Le verdict d'une phase reste l'EXÉCUTION de la
    commande de vérification par l'orchestrateur, et la fin d'une tâche d'agent
    reste signalée par une SENTINELLE FICHIER. Ces deux mécaniques sont déjà
    agnostiques du harness : les garder dehors est ce qui permettra plus tard un
    runner headless ('opencode run', 'codex exec') sans toucher aux pipelines —
    send_task() rend la main dès la soumission, exactement comme aujourd'hui.
  - connaître le pipeline (spec, plan, blackboard, phases) : il n'en sait rien.

Deux implémentations, un registre FERMÉ (pas de découverte dynamique de plugins) :
  - OpenCodeTuiRunner : TUI 'opencode --agent factory', session 'oc-<rôle>-<hash>'
  - CodexTuiRunner    : TUI 'codex', session 'cx-<rôle>-<hash>', validation de
                        l'écran « trust » au premier boot dans un dossier

Ajouter un 3ᵉ harness = écrire une classe ici + une entrée dans RUNNERS. Rien
d'autre à toucher dans les orchestrateurs.

Sélection du harness (resolve_runner), par ordre de priorité :
  1. variable d'environnement MM_AGENT_HARNESS (override explicite) ;
  2. marqueur d'équipement du projet ('.mm-equip.json', écrit par l'app) ;
  3. artefacts présents dans le projet ('.codex/' → codex, '.opencode/' → opencode)
     — repli pour les projets équipés avant l'abstraction du harness ;
  4. un SEUL des deux binaires présent dans le PATH → celui-là, avec information ;
  5. sinon : arrêt propre avec un message actionnable.

L'app cockpit n'importe PAS ce module (elle reste mono-fichier stdlib compilable
Nuitka) : sa connaissance du harness se limite au préflight, aux artefacts
d'équipement, aux libellés et aux préfixes de session.
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import importlib
import subprocess

# Nom du marqueur d'équipement écrit par l'app à la racine du projet. Il porte
# déjà 'distro_version' et 'engine' ; l'app y ajoute désormais 'harness'.
EQUIP_MARKER   = ".mm-equip.json"

# Variable d'environnement d'override. Valeurs : les clés de RUNNERS, plus 'mock'
# (runner de test fourni par tools/, JAMAIS distribué — voir _load_mock_runner).
HARNESS_ENV    = "MM_AGENT_HARNESS"

# ─── TIMEOUTS PARAMÉTRABLES ───────────────────────────────────────────────────
# Deux timeouts sont réglables par l'utilisateur (les filets de résilience — retries,
# backstops de mutation — restent en dur : les ouvrir inviterait à casser la logique
# « un timeout d'infra n'est pas un verdict rouge »). Résolution, par priorité :
#   1. variable d'environnement (override ponctuel, même esprit que MM_AGENT_HARNESS) ;
#   2. section 'timeouts' du marqueur '.mm-equip.json' (écrite par l'app, panneau
#      ⏱ Timeouts de la carte projet) ;
#   3. défaut en dur de l'orchestrateur.
# Valeurs en secondes, bornées : une valeur hors bornes ou illisible est ignorée
# (repli sur la source suivante), jamais une erreur — un marqueur corrompu ne doit
# pas empêcher un run.
TIMEOUT_ENV  = {"phase": "MM_PHASE_TIMEOUT", "verify": "MM_VERIFY_TIMEOUT"}
TIMEOUT_MIN  = 60
TIMEOUT_MAX  = 7200


def _timeout_candidate(raw) -> int | None:
    """int dans [TIMEOUT_MIN, TIMEOUT_MAX], ou None si illisible / hors bornes."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if TIMEOUT_MIN <= value <= TIMEOUT_MAX else None


def resolve_timeout(key: str, default: int, project_dir: str = ".") -> int:
    """Timeout effectif (secondes) pour 'phase' ou 'verify' : env > marqueur > défaut.

    Les orchestrateurs tournent avec cwd = racine du projet (contrat de lancement de
    l'app, et consigne d'usage manuel) : project_dir='.' lit le bon marqueur."""
    env_name = TIMEOUT_ENV.get(key)
    if env_name:
        value = _timeout_candidate(os.environ.get(env_name))
        if value is not None:
            return value
    marked = _read_marker(project_dir).get("timeouts")
    if isinstance(marked, dict):
        value = _timeout_candidate(marked.get(key))
        if value is not None:
            return value
    return default

# Repli du nom de modèle dans les messages d'échec, quand aucune config ne le fixe.
MODEL_FALLBACK = "le modèle actuel"

# ─── MESSAGES DE LA COUCHE HARNESS ────────────────────────────────────────────
# Les orchestrateurs des forks OpenCode/Codex d'origine ne formulent PAS leur couche tmux de la même
# façon : « Mode Data Center » ici, « réparation » là ; « Suis le run en direct » /
# « Suis l'audit en direct » / rien du tout. Ces écarts sont d'époque et sans logique,
# mais ce sont les messages que les utilisateurs lisent : la migration ne les réécrit
# pas. Chaque ligne devient donc une entrée de cette table, et un orchestrateur ne
# passe QUE celles où il s'écarte du gabarit majoritaire.
#
# Champs disponibles : {session} (nom de session tmux), {tui} (nom du harness tel
# qu'il apparaît dans les messages : « opencode », « Codex »), {label} (libellé
# propre : « OpenCode », « Codex »), {wait} (secondes d'attente du boot).
# Valeur None = ligne NON affichée (deux orchestrateurs sont muets sur la
# réutilisation de session, Guided-Fix.py sur « prêt et chaud »).
MESSAGES = {
    "reuse":     "♻️  Session tmux '{session}' déjà active. Réutilisation.",
    "start":     "🖥️  Démarrage de la session tmux '{session}' (Mode Data Center)...",
    "boot":      "⏳ Attente du boot du TUI {tui} cloud ({wait}s)...",
    "ready":     "✓ {label} prêt et chaud dans tmux.",
    "follow":    "   👀 Suis le run en direct dans un autre terminal : tmux attach -t {session}",
    "new_reset": "🔄 Réinitialisation du contexte {tui} (/new)...",
    "new_warn":  "   ⚠️  La TUI n'a peut-être pas été réinitialisée ('/new' littéral encore "
                 "à l'écran) : si le run dérive, vérifie avec tmux attach.",
    "kill":      "🛑 Session tmux '{session}' fermée.",
}


class AgentRunner:
    """Interface du harness. Les mécaniques tmux communes aux deux harness vivent
    ici (elles sont OCTET POUR OCTET les mêmes dans les forks OpenCode et
    Codex d'origine) ; tout ce qui diffère est porté par les attributs de classe et les
    quelques méthodes redéfinies par chaque implémentation."""

    # ─── Identité du harness (chaque implémentation redéfinit tout ce bloc) ────
    name           = ""      # clé du registre RUNNERS
    label          = ""      # libellé « propre » (UI, message « prêt et chaud »)
    tui_name       = ""      # nom tel qu'il apparaît dans les messages tmux existants
    binary         = ""      # exécutable attendu dans le PATH
    launch_cmd     = ""      # ligne de commande tapée dans le pane tmux
    task_preamble  = ""      # rappel collé en tête de CHAQUE tâche (vide : rien n'est ajouté)
    session_prefix = ""      # préfixe de session : "oc-" / "cx-"
    buffer_prefix  = ""      # préfixe du tampon de prompt : "oc" / "cx"
    tmp_prefix     = ""      # préfixe des fichiers de routage : "opencode" / "codex"
    equip_dir      = ""      # dossier d'équipement copié par l'app : ".opencode" / ".codex"
    equip_files    = ()      # fichiers d'équipement copiés par l'app (ex. AGENTS.md)
    config_file    = ""      # config du harness, RELATIVE au projet
    global_configs = ()      # configs globales, dans l'ordre de repli (~ accepté)
    install_hint   = ""      # comment l'installer
    auth_cmd       = ()       # commande de contrôle d'authentification
    auth_hint      = ""      # comment s'authentifier

    # ─── Réglages tmux (valeurs des forks d'origine, inchangées) ────
    boot_wait        = 6     # temps de boot standard pour le TUI cloud
    width            = 120   # largeur du terminal virtuel
    height           = 40
    new_session_wait = 2
    # Readiness APRÈS le boot fixe : un TUI qui s'auto-met à jour au premier lancement
    # (OpenCode 1.17 → 1.18, 22/08/2026) avale le prompt collé pendant le téléchargement
    # — 19 min sans qu'aucune session soit créée. On attend donc, au plus ready_timeout s,
    # que la TUI ait pris l'écran et qu'aucune installation ne soit en cours.
    ready_timeout      = 45
    ready_busy_markers = ("upgrad", "updating", "installing", "downloading")

    def __init__(self, project_dir: str, role: str,
                 new_context_check: bool = True,
                 messages: dict | None = None):
        """`role` suffixe la session tmux (factory, spec, techplan, tdd, proto, doc,
        audit, a11y, fix) : deux orchestrateurs lancés sur le MÊME projet ne doivent
        jamais partager une session, sinon les prompts de l'un atterrissent dans
        l'agent de l'autre.

        `messages` surcharge les entrées de MESSAGES où CE script s'écarte du gabarit
        majoritaire (voir la table). `new_context_check` désactive la vérification
        warn-only du /new (Guided-Fix.py ne l'a jamais faite)."""
        # project_dir est haché TEL QUEL (pas de realpath) : les orchestrateurs
        # passent os.getcwd(), déjà résolu, et l'app retrouve la session par le
        # même hash. Normaliser ici casserait cette correspondance.
        self.project_dir       = project_dir
        self.role              = role
        self.session           = (self.session_prefix + role + "-"
                                  + hashlib.sha1(project_dir.encode("utf-8")).hexdigest()[:8])
        self.prompt_buffer     = "." + self.buffer_prefix + "_short_prompt.txt"
        self.new_context_check = new_context_check
        self.messages          = dict(MESSAGES, **(messages or {}))

    def say(self, key: str):
        """Affiche une ligne de la couche harness, ou rien si le script la tait."""
        template = self.messages.get(key)
        if template is None:
            return
        print(template.format(session=self.session, tui=self.tui_name,
                              label=self.label, wait=self.boot_wait))

    # ─── Noms de fichiers dérivés du harness ──────────────────────────────────

    def tmp_file(self, kind: str) -> str:
        """Fichier de routage de contexte (prompt déporté) : '.opencode_po.md',
        '.codex_task.md'… Le nom a toujours porté le harness ; le garder évite
        de changer le contenu des .gitignore et des gardes git des projets déjà
        équipés."""
        return "." + self.tmp_prefix + "_" + kind + ".md"

    @property
    def tmp_glob(self) -> str:
        """Motif .gitignore couvrant tous les fichiers de routage."""
        return "." + self.tmp_prefix + "_*.md"

    @property
    def tmp_dot_prefix(self) -> str:
        """Préfixe testé par is_orchestration_artifact() des orchestrateurs."""
        return "." + self.tmp_prefix + "_"

    # ─── COUCHE TMUX (DATA CENTER DIRECT) ─────────────────────────────────────

    def is_running(self) -> bool:
        """Vérifie si la session tmux de l'usine existe déjà."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True
        )
        return result.returncode == 0

    def start(self):
        """Crée une session tmux détachée et lance le harness version Data Center."""
        if self.is_running():
            self.say("reuse")
            return

        self.say("start")
        subprocess.run([
            "tmux", "new-session", "-d",
            "-s", self.session,
            "-x", str(self.width),
            "-y", str(self.height)
        ], check=True)

        # Lance le harness en mode interactif classique directement
        subprocess.run([
            "tmux", "send-keys", "-t", self.session,
            self.launch_cmd
        ], check=True)
        time.sleep(0.2)
        subprocess.run([
            "tmux", "send-keys", "-t", self.session,
            "Enter"
        ], check=True)

        self.say("boot")
        time.sleep(self.boot_wait)
        self.wait_ready()
        self.after_boot()
        self.say("ready")
        self.say("follow")

    def wait_ready(self):
        """Après le boot_wait fixe : attend (au plus ready_timeout s) que la TUI ait pris
        l'écran — la commande tapée au shell n'en est plus la dernière ligne — et qu'aucune
        mise à jour/installation ne soit affichée. Best-effort : au-delà du délai, on
        continue en le disant (le premier prompt peut alors être perdu)."""
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            screen = self.capture() or ""
            low = screen.lower()
            still_shell = screen.rstrip().endswith(self.launch_cmd)
            busy = any(marker in low for marker in self.ready_busy_markers)
            if screen.strip() and not still_shell and not busy:
                return
            time.sleep(1)
        print(f"   ⚠️  {self.label} : TUI toujours en démarrage ou en mise à jour après "
              f"{self.ready_timeout}s — on continue (le premier prompt peut être perdu).")

    def after_boot(self):
        """Crochet post-boot, avant la première sollicitation. Sans objet pour un
        harness qui démarre directement sur son invite (OpenCode) ; Codex y valide
        son écran « trust »."""
        return

    def send_task(self, prompt: str):
        """Envoie un prompt texte dans le TUI du harness via le buffer tmux.

        Rend la main dès la soumission : la fin de la tâche est signalée par la
        SENTINELLE FICHIER nommée dans le prompt, jamais par un retour d'ici."""
        with open(self.prompt_buffer, "w", encoding="utf-8") as f:
            f.write(self.task_preamble + prompt if self.task_preamble else prompt)

        # Buffer NOMMÉ : les buffers tmux sont globaux au serveur, deux usines utilisant le
        # buffer par défaut se marcheraient dessus (le projet A collerait le prompt chargé
        # par le projet B). '-d' supprime le buffer juste après le collage.
        subprocess.run(["tmux", "load-buffer", "-b", self.session, self.prompt_buffer], check=True)
        subprocess.run(["tmux", "paste-buffer", "-d", "-b", self.session, "-t", self.session], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)

    def new_context(self):
        """Envoie la commande /new dans le harness pour réinitialiser le contexte."""
        self.say("new_reset")
        # Escape D'ABORD : si l'agent précédent génère encore (il a simplement raté sa
        # sentinelle), un '/new' aveugle serait avalé comme TEXTE de prompt au lieu de
        # s'exécuter comme commande : contexte non réinitialisé, prompts qui s'empilent,
        # tout le run qui dérive.
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Escape"], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.session, "/new"], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)
        time.sleep(self.new_session_wait)
        if not self.new_context_check:
            return
        # Vérification du reset en WARN-ONLY (heuristique, à calibrer face à la vraie TUI ;
        # ne JAMAIS bloquer le run dessus) : après un /new réussi, le texte littéral '/new'
        # ne devrait plus être à l'écran ; sa présence suggère qu'il a été tapé comme texte
        # de prompt.
        tail = self.capture()[-2000:]
        if "/new" in tail:
            self.say("new_warn")

    def capture(self) -> str:
        """Capture le contenu texte actuel du terminal tmux.

        Alimente la vérification warn-only du reset après '/new' et le diagnostic d'échec
        du scaffold (un problème d'appels d'outils devient visible sans s'attacher à la
        session).
        """
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.session, "-p", "-S", "-10000"],
            capture_output=True, text=True
        )
        return result.stdout

    def kill(self):
        """Tue proprement la session tmux."""
        if self.is_running():
            subprocess.run(["tmux", "kill-session", "-t", self.session])
            self.say("kill")

    # ─── CONFIGURATION DU HARNESS ─────────────────────────────────────────────

    def configured_model(self) -> str:
        """Modèle configuré pour ce projet, sinon globalement (message d'échec).
        Chaque implémentation lit son propre format de config."""
        raise NotImplementedError

    def _config_candidates(self) -> list:
        """Config du projet d'abord, configs globales ensuite."""
        return [self.config_file] + [os.path.expanduser(p) for p in self.global_configs]

    # ─── PRÉFLIGHT (diagnostic ; le pipeline ne l'appelle pas) ────────────────

    def preflight(self) -> list:
        """[{ok, label, detail, fix_hint}] : binaire présent, authentification,
        modèle configuré. Utilisé par le diagnostic en ligne de commande
        (`python3 mm_runner.py`) et par les outils de tools/. L'app cockpit refait
        ce contrôle de son côté : elle n'importe pas ce module (invariant mono-fichier)."""
        checks = []
        path = shutil.which(self.binary)
        version = None
        if path:
            version = self._first_line([self.binary, "--version"])
        checks.append({
            "ok": bool(path),
            "label": f"binaire '{self.binary}'",
            "detail": (version or path) if path else "absent du PATH",
            "fix_hint": "" if path else self.install_hint,
        })
        if not path:
            return checks
        authed, detail = self._auth_state()
        checks.append({
            "ok": authed,
            "label": "authentification",
            "detail": detail,
            "fix_hint": "" if authed else self.auth_hint,
        })
        model = self.configured_model()
        checks.append({
            "ok": True,               # informatif : aucun modèle épinglé n'est pas une erreur
            "label": "modèle configuré",
            "detail": model,
            "fix_hint": "",
        })
        return checks

    def _auth_state(self) -> tuple:
        """(authentifié ?, détail lisible). Jamais d'exception : un préflight qui
        casse serait pire qu'un préflight imprécis."""
        try:
            proc = subprocess.run(list(self.auth_cmd), capture_output=True,
                                  text=True, timeout=20)
        except Exception as exc:
            return False, f"contrôle impossible ({exc.__class__.__name__})"
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        first = out.splitlines()[0].strip() if out else ""
        return proc.returncode == 0, (first[:120] or "sans réponse")

    @staticmethod
    def _first_line(cmd: list) -> str:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except Exception:
            return ""
        out = (proc.stdout or proc.stderr or "").strip()
        return out.splitlines()[0][:60] if out else ""


class OpenCodeTuiRunner(AgentRunner):
    """OpenCode piloté dans son TUI via tmux — comportement du fork OpenCode d'origine, extrait tel quel."""

    name           = "opencode"
    label          = "OpenCode"
    tui_name       = "opencode"
    binary         = "opencode"
    launch_cmd     = "opencode --agent factory"
    session_prefix = "oc-"
    buffer_prefix  = "oc"
    tmp_prefix     = "opencode"
    equip_dir      = ".opencode"
    equip_files    = ()
    config_file    = "./.opencode/opencode.json"
    global_configs = ("~/.config/opencode/opencode.json", "~/.config/opencode/config.json")
    install_hint   = "installe OpenCode : https://opencode.ai/docs"
    auth_cmd       = ("opencode", "auth", "list")
    auth_hint      = "authentifie-toi : opencode auth login"
    # Journaux d'OpenCode (Linux/WSL, puis macOS) : source du modèle OBSERVÉ quand aucune
    # config ne le fixe (le modèle choisi dans la TUI via /model n'est écrit nulle part
    # ailleurs) — sinon run.json et failReport disaient « le modèle actuel ».
    log_dirs       = ("~/.local/share/opencode/log",
                      "~/Library/Application Support/opencode/log")

    def configured_model(self) -> str:
        """Modèle configuré dans .opencode/opencode.json ; sinon le modèle de la dernière
        session OpenCode ouverte dans CE projet (journal d'OpenCode) ; sinon le repli."""
        for path in self._config_candidates():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    model = (json.load(f) or {}).get("model")
            except Exception:
                continue
            if model:
                return model
        observed = self._observed_model()
        if observed:
            return f"{observed} (observé : dernière session OpenCode de ce projet)"
        return MODEL_FALLBACK

    def _observed_model(self) -> str:
        """'<providerID>/<model.id>' de la dernière ligne 'message=created id=ses_…' du
        journal d'OpenCode dont directory= est CE projet ; '' si introuvable. Best-effort :
        le format du journal peut changer, les trois journaux les plus récents suffisent."""
        pattern = re.compile(r"message=created id=ses_\S+.*?\bdirectory=(\S+).*?"
                             r"\bmodel\.id=(\S+)\s+model\.providerID=(\S+)")
        project = os.path.realpath(self.project_dir)
        for log_dir in self.log_dirs:
            directory = os.path.expanduser(log_dir)
            try:
                logs = sorted((os.path.join(directory, n) for n in os.listdir(directory)
                               if n.endswith(".log")), key=os.path.getmtime, reverse=True)[:3]
            except OSError:
                continue
            for log_path in logs:
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except OSError:
                    continue
                for line in reversed(lines):
                    match = pattern.search(line)
                    if match and os.path.realpath(match.group(1)) == project:
                        return f"{match.group(3)}/{match.group(2)}"
        return ""

    def _auth_state(self) -> tuple:
        """'opencode auth list' sort 0 même sans identifiant : c'est le compte de
        la dernière ligne (« N credentials ») qui tranche."""
        try:
            proc = subprocess.run(list(self.auth_cmd), capture_output=True,
                                  text=True, timeout=20)
        except Exception as exc:
            return False, f"contrôle impossible ({exc.__class__.__name__})"
        if proc.returncode != 0:
            return False, "opencode auth list en échec"
        out = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"(\d+)\s+credential", out)
        if not match:
            # Format inattendu (nouvelle version d'OpenCode) : on ne bloque pas.
            return True, "identifiants non dénombrables (format inattendu)"
        count = int(match.group(1))
        return count > 0, f"{count} identifiant(s) enregistré(s)"


class CodexTuiRunner(AgentRunner):
    """Codex CLI piloté dans son TUI via tmux — comportement du fork Codex d'origine, extrait tel quel."""

    name           = "codex"
    label          = "Codex"
    tui_name       = "Codex"
    binary         = "codex"
    launch_cmd     = "codex"
    session_prefix = "cx-"
    buffer_prefix  = "cx"
    tmp_prefix     = "codex"
    equip_dir      = ".codex"
    equip_files    = ("AGENTS.md",)
    config_file    = "./.codex/config.toml"
    global_configs = ("~/.codex/config.toml",)
    install_hint   = "installe Codex CLI : npm install -g @openai/codex"
    auth_cmd       = ("codex", "login", "status")
    auth_hint      = "authentifie-toi : codex login"
    # Codex n'a pas d'équivalent au « question: deny » de l'agent OpenCode : la consigne ne
    # tient qu'au texte. AGENTS.md la pose au démarrage ; ce rappel la répète en tête de
    # chaque tâche, pour qu'elle survive à la longueur de la session et aux /new.
    task_preamble  = ("CONSIGNE DE SESSION (usine automatisée, personne ne lit l'écran) : ne pose AUCUNE question et "
                      "ne demande AUCUNE confirmation ; en cas de doute, choisis l'option la plus prudente et continue. "
                      "Signale la fin de la tâche uniquement par le fichier sentinelle indiqué ci-dessous.\n\n")

    def after_boot(self):
        """Valide l'écran « Do you trust this directory? » du premier boot de Codex
        dans ce dossier. Le trust est mémorisé par projet (~/.codex/config.toml) et
        conditionne la lecture de la config locale '.codex/config.toml' (posture
        usine : approbations et sandbox désactivées, modèle épinglé). Sans cette
        validation, le premier prompt collé serait avalé par l'écran de choix.
        Dossier déjà trusted (l'écran n'apparaît pas) : ne fait rien."""
        result = subprocess.run(["tmux", "capture-pane", "-p", "-t", self.session],
                                capture_output=True, text=True)
        if "Do you trust" not in (result.stdout or ""):
            return
        print("🔐 Premier lancement de Codex dans ce dossier : validation du trust...")
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)
        time.sleep(self.boot_wait)

    def configured_model(self) -> str:
        """Lit le modèle configuré dans .codex/config.toml (pour le message d'échec).

        Parse TOML MINIMAL (regex sur `model = "…"`) : correctif hérité du fork Codex — un
        json.load hérité de l'opencode.json échouait systématiquement sur du TOML,
        et le message d'échec sortait sans le nom du modèle."""
        for path in self._config_candidates():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    match = re.search(r'^\s*model\s*=\s*"([^"]*)"', f.read(), re.MULTILINE)
            except Exception:
                continue
            if match and match.group(1):
                return match.group(1)
        return MODEL_FALLBACK


# Registre FERMÉ, en dur : ajouter un harness = ajouter une classe + une entrée.
# Aucune découverte dynamique (pas d'entry points, pas de scan de dossier) : la
# distribution est un binaire onefile, un import implicite y serait invisible.
RUNNERS = {
    OpenCodeTuiRunner.name: OpenCodeTuiRunner,
    CodexTuiRunner.name:    CodexTuiRunner,
}


# ─── SÉLECTION DU HARNESS ─────────────────────────────────────────────────────

def _read_marker(project_dir: str) -> dict:
    """Marqueur d'équipement du projet. Absent ou illisible → {}."""
    try:
        with open(os.path.join(project_dir, EQUIP_MARKER), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def detect_harness(project_dir: str) -> tuple:
    """(nom du harness | None, origine de la décision). Ne décide RIEN d'autre :
    ni message, ni sortie — resolve_runner en est chargé.

    Origines : 'env', 'marker', 'artifacts', 'path', 'ambiguous', 'none'."""
    forced = (os.environ.get(HARNESS_ENV) or "").strip().lower()
    if forced:
        return forced, "env"

    marked = str(_read_marker(project_dir).get("harness") or "").strip().lower()
    if marked in RUNNERS:
        return marked, "marker"

    equipped = [name for name, cls in RUNNERS.items()
                if os.path.isdir(os.path.join(project_dir, cls.equip_dir))]
    if len(equipped) == 1:
        return equipped[0], "artifacts"

    installed = [name for name, cls in RUNNERS.items() if shutil.which(cls.binary)]
    if len(installed) == 1:
        return installed[0], "path"

    return None, ("ambiguous" if (equipped or installed) else "none")


def _no_harness_message(origin: str, project_dir: str) -> str:
    """Message d'arrêt actionnable : l'utilisateur doit savoir quoi TAPER."""
    if origin == "ambiguous":
        head = ("❌ Deux harness possibles, et rien pour choisir.\n"
                "   OpenCode et Codex CLI sont tous les deux disponibles, mais ce projet\n"
                "   n'indique pas lequel utiliser (pas de marqueur d'équipement).")
    else:
        head = ("❌ Aucun harness d'agent IA trouvé.\n"
                "   MAIsterMind pilote un agent CLI : OpenCode ou Codex CLI. Aucun des deux\n"
                "   n'est installé sur cette machine.")
    return f"""
{'='*60}
{head}

   Projet : {project_dir}

   Trois façons de trancher :
   1. Équipe le projet depuis l'app MAIsterMind : elle écrit le harness choisi
      dans '{EQUIP_MARKER}' à la racine du projet.
   2. Impose-le pour ce lancement :
         {HARNESS_ENV}=opencode  python3 <orchestrateur>.py
         {HARNESS_ENV}=codex     python3 <orchestrateur>.py
   3. Installe l'un des deux, puis authentifie-toi :
         OpenCode : https://opencode.ai/docs      puis  opencode auth login
         Codex    : npm install -g @openai/codex  puis  codex login
{'='*60}
"""


def _load_mock_runner():
    """Charge le runner de TEST (tools/mm_mock_runner.py), hors distribution, ou None.

    UNIQUE crochet de test du moteur, et il ne s'ouvre que si MM_AGENT_HARNESS=mock :
    en production le module est introuvable et la valeur est refusée comme n'importe
    quel nom inconnu — proprement, pas en traceback. Import par chaîne (importlib)
    pour que la compilation Nuitka de la distribution ne cherche jamais à embarquer
    un module de tools/."""
    try:
        return importlib.import_module("mm_mock_runner").MockRunner
    except Exception:
        return None


def resolve_runner(project_dir: str, role: str = "factory", **options) -> AgentRunner:
    """Runner du harness actif pour ce projet. S'arrête proprement (exit 1) si
    aucun harness ne peut être déterminé : mieux vaut un message actionnable qu'un
    'tmux: command not found' au milieu d'un run.

    `role` suffixe la session tmux ; `options` est passé tel quel au runner
    (`messages`, `new_context_check` — voir AgentRunner.__init__)."""
    name, origin = detect_harness(project_dir)

    if name == "mock":
        mock = _load_mock_runner()
        if mock is not None:
            return mock(project_dir, role, **options)
        print(f"\n❌ {HARNESS_ENV}='mock' : le runner de test est introuvable. Il vit "
              f"dans tools/ et n'est PAS distribué — pose tools/ sur PYTHONPATH, ou "
              f"choisis un harness réel.\n")
        sys.exit(1)

    if name is None:
        print(_no_harness_message(origin, project_dir))
        sys.exit(1)

    if name not in RUNNERS:
        known = ", ".join(sorted(RUNNERS))
        print(f"\n❌ {HARNESS_ENV}='{name}' : harness inconnu. Valeurs acceptées : {known}.\n")
        sys.exit(1)

    runner = RUNNERS[name](project_dir, role, **options)

    # Décision implicite : on le DIT. Une session 'oc-…' là où l'utilisateur
    # attendait 'cx-…' est le genre de surprise qui coûte une demi-heure.
    if origin == "artifacts":
        print(f"ℹ️  Harness déduit des artefacts du projet : {runner.label} "
              f"('{runner.equip_dir}/' présent).")
    elif origin == "path":
        print(f"ℹ️  Harness déduit du PATH : {runner.label} (seul harness installé). "
              f"Équipe le projet depuis l'app pour figer ce choix.")

    return runner


# ─── DIAGNOSTIC EN LIGNE DE COMMANDE ──────────────────────────────────────────

def _print_diagnostic(project_dir: str):
    """`python3 mm_runner.py` : que verrait un orchestrateur lancé ici, et dans
    quel état sont les deux harness. Aucun effet de bord (rien n'est lancé)."""
    name, origin = detect_harness(project_dir)
    print(f"Projet          : {project_dir}")
    print(f"{HARNESS_ENV:<16}: {os.environ.get(HARNESS_ENV) or '(non posé)'}")
    print(f"Marqueur        : {_read_marker(project_dir).get('harness') or '(aucun)'}")
    print(f"Harness retenu  : {name or '(aucun)'}  [origine : {origin}]")
    for key in sorted(RUNNERS):
        cls = RUNNERS[key]
        runner = cls(project_dir, "factory")
        print(f"\n── {cls.label} ({key})")
        print(f"   session       : {runner.session}")
        print(f"   tampon prompt : {runner.prompt_buffer}")
        print(f"   config projet : {cls.config_file}")
        for check in runner.preflight():
            mark = "✓" if check["ok"] else "✗"
            hint = f"   → {check['fix_hint']}" if check["fix_hint"] else ""
            print(f"   {mark} {check['label']:<18}: {check['detail']}{hint}")


if __name__ == "__main__":
    _print_diagnostic(os.getcwd())
