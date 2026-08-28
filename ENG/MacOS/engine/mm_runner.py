#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_runner — AI agent HARNESS abstraction for the MAIsterMind orchestrators
─────────────────────────────────────────────────────────────────────────
This module holds, and holds alone, everything the orchestrators know about the AI
agent they drive: the name of its tmux session, the command that launches it, how
to paste a prompt into it, how to start from a fresh context, how to capture its
screen and how to kill it. Before it, that layer was copied into all 10
orchestrators and every harness change forced a full fork of the product.

What this module does NOT do, and must never do:
  - judge whether a task succeeded. A phase verdict remains the EXECUTION of the
    verification command by the orchestrator, and the end of an agent task remains
    signalled by a FILE SENTINEL. Both mechanisms are already harness-agnostic:
    keeping them out is what will later allow a headless runner ('opencode run',
    'codex exec') without touching the pipelines — send_task() returns as soon as
    the prompt is submitted, exactly as today.
  - know anything about the pipeline (spec, plan, blackboard, phases).

Two implementations, one CLOSED registry (no dynamic plugin discovery):
  - OpenCodeTuiRunner: 'opencode --agent factory' TUI, session 'oc-<role>-<hash>'
  - CodexTuiRunner   : 'codex' TUI, session 'cx-<role>-<hash>', confirms the
                       « trust » screen on the first boot in a folder

Adding a 3rd harness = writing a class here + one entry in RUNNERS. Nothing else
to touch in the orchestrators.

Harness selection (resolve_runner), by order of priority:
  1. MM_AGENT_HARNESS environment variable (explicit override);
  2. the project's equipment marker ('.mm-equip.json', written by the app);
  3. artefacts present in the project ('.codex/' → codex, '.opencode/' → opencode)
     — fallback for projects equipped before the harness abstraction;
  4. exactly ONE of the two binaries on PATH → that one, with a notice;
  5. otherwise: clean stop with an actionable message.

The cockpit app does NOT import this module (it stays a single-file stdlib script,
Nuitka-compilable): its knowledge of the harness is limited to preflight,
equipment artefacts, labels and session prefixes.
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

# Name of the equipment marker the app writes at the project root. It already
# carries 'distro_version' and 'engine'; the app now also writes 'harness'.
EQUIP_MARKER   = ".mm-equip.json"

# Override environment variable. Values: the keys of RUNNERS, plus 'mock' (the test
# runner provided by tools/, NEVER distributed — see _load_mock_runner).
HARNESS_ENV    = "MM_AGENT_HARNESS"

# ─── CONFIGURABLE TIMEOUTS ────────────────────────────────────────────────────
# Two timeouts are user-adjustable (the resilience nets — retries, mutation
# backstops — stay hardcoded: opening them would invite breaking the
# "an infra timeout is not a red verdict" logic). Resolution, by priority:
#   1. environment variable (one-off override, same spirit as MM_AGENT_HARNESS);
#   2. 'timeouts' section of the '.mm-equip.json' marker (written by the app,
#      ⏱ Timeouts panel of the project card);
#   3. the orchestrator's hardcoded default.
# Values in seconds, bounded: an out-of-bounds or unreadable value is ignored
# (fall back to the next source), never an error — a corrupted marker must
# not prevent a run.
TIMEOUT_ENV  = {"phase": "MM_PHASE_TIMEOUT", "verify": "MM_VERIFY_TIMEOUT"}
TIMEOUT_MIN  = 60
TIMEOUT_MAX  = 7200


def _timeout_candidate(raw) -> int | None:
    """int within [TIMEOUT_MIN, TIMEOUT_MAX], or None if unreadable / out of bounds."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if TIMEOUT_MIN <= value <= TIMEOUT_MAX else None


def resolve_timeout(key: str, default: int, project_dir: str = ".") -> int:
    """Effective timeout (seconds) for 'phase' or 'verify': env > marker > default.

    Orchestrators run with cwd = project root (the app's launch contract, and the
    manual-usage instruction): project_dir='.' reads the right marker."""
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

# Fallback model name in failure messages, when no config pins one.
MODEL_FALLBACK = "the current model"

# ─── HARNESS LAYER MESSAGES ───────────────────────────────────────────────────
# The orchestrators of the original OpenCode/Codex forks do NOT word their tmux layer the same way:
# « Data Center Mode » here, « repair » there; « Follow live » / « Follow the audit
# live » / nothing at all. Those discrepancies are historical and follow no logic,
# but they are the messages users read: the migration does not rewrite them. Each
# line therefore becomes an entry in this table, and an orchestrator only passes the
# ones where it departs from the majority template.
#
# Available fields: {session} (tmux session name), {tui} (harness name as it appears
# in the messages: « opencode », « Codex »), {label} (clean label: « OpenCode »,
# « Codex »), {wait} (boot wait, in seconds).
# Value None = line NOT printed (two orchestrators say nothing about session reuse,
# Guided-Fix.py nothing about « ready and warm »).
MESSAGES = {
    "reuse":     "♻️  tmux session '{session}' already active. Reusing.",
    "start":     "🖥️  Starting tmux session '{session}' (Data Center Mode)...",
    "boot":      "⏳ Waiting for {tui} cloud TUI boot ({wait}s)...",
    "ready":     "✓ {label} ready and warm in tmux.",
    "follow":    "   👀 Follow live in another terminal: tmux attach -t {session}",
    "new_reset": "🔄 Resetting {tui} context (/new)...",
    "new_warn":  "   ⚠️  The TUI may not have reset (literal '/new' still on screen): "
                 "if the run drifts, check with tmux attach.",
    "kill":      "🛑 tmux session '{session}' closed.",
}


class AgentRunner:
    """The harness interface. The tmux mechanics shared by both harnesses live here
    (they are BYTE FOR BYTE the same in the original OpenCode and Codex forks);
    everything that differs is carried by class attributes and the few methods each
    implementation overrides."""

    # ─── Harness identity (every implementation redefines this whole block) ────
    name           = ""      # key in the RUNNERS registry
    label          = ""      # clean label (UI, « ready and warm » message)
    tui_name       = ""      # name as it appears in the existing tmux messages
    binary         = ""      # executable expected on PATH
    launch_cmd     = ""      # command line typed into the tmux pane
    session_prefix = ""      # session prefix: "oc-" / "cx-"
    buffer_prefix  = ""      # prompt buffer prefix: "oc" / "cx"
    tmp_prefix     = ""      # context-routing file prefix: "opencode" / "codex"
    equip_dir      = ""      # equipment folder copied by the app: ".opencode" / ".codex"
    equip_files    = ()      # equipment files copied by the app (e.g. AGENTS.md)
    config_file    = ""      # harness config, RELATIVE to the project
    global_configs = ()      # global configs, in fallback order (~ accepted)
    install_hint   = ""      # how to install it
    auth_cmd       = ()       # authentication check command
    auth_hint      = ""      # how to authenticate

    # ─── tmux settings (values of the original forks, unchanged) ──
    boot_wait        = 6     # standard boot time for the cloud TUI
    width            = 120   # virtual terminal width
    height           = 40
    new_session_wait = 2
    # Readiness AFTER the fixed boot: a TUI that self-updates on first launch
    # (OpenCode 1.17 → 1.18, 2026-08-22) swallows the prompt pasted during the download
    # — 19 min without any session being created. So we wait, at most ready_timeout s,
    # for the TUI to have taken the screen and for no installation to be in progress.
    ready_timeout      = 45
    ready_busy_markers = ("upgrad", "updating", "installing", "downloading")

    def __init__(self, project_dir: str, role: str,
                 new_context_check: bool = True,
                 messages: dict | None = None):
        """`role` suffixes the tmux session (factory, spec, techplan, tdd, proto, doc,
        audit, a11y, fix): two orchestrators started on the SAME project must never
        share a session, otherwise one's prompts land in the other's agent.

        `messages` overrides the MESSAGES entries where THIS script departs from the
        majority template (see the table). `new_context_check` disables the warn-only
        /new verification (Guided-Fix.py never did it)."""
        # project_dir is hashed AS IS (no realpath): the orchestrators pass
        # os.getcwd(), already resolved, and the app finds the session back through
        # the same hash. Normalising here would break that correspondence.
        self.project_dir       = project_dir
        self.role              = role
        self.session           = (self.session_prefix + role + "-"
                                  + hashlib.sha1(project_dir.encode("utf-8")).hexdigest()[:8])
        self.prompt_buffer     = "." + self.buffer_prefix + "_short_prompt.txt"
        self.new_context_check = new_context_check
        self.messages          = dict(MESSAGES, **(messages or {}))

    def say(self, key: str):
        """Prints one line of the harness layer, or nothing if the script mutes it."""
        template = self.messages.get(key)
        if template is None:
            return
        print(template.format(session=self.session, tui=self.tui_name,
                              label=self.label, wait=self.boot_wait))

    # ─── File names derived from the harness ───────────────────────────────────

    def tmp_file(self, kind: str) -> str:
        """Context-routing file (offloaded prompt): '.opencode_po.md',
        '.codex_task.md'… The name has always carried the harness; keeping it
        avoids changing the .gitignore contents and the git guards of already
        equipped projects."""
        return "." + self.tmp_prefix + "_" + kind + ".md"

    @property
    def tmp_glob(self) -> str:
        """.gitignore pattern covering every context-routing file."""
        return "." + self.tmp_prefix + "_*.md"

    @property
    def tmp_dot_prefix(self) -> str:
        """Prefix tested by the orchestrators' is_orchestration_artifact()."""
        return "." + self.tmp_prefix + "_"

    # ─── TMUX LAYER (DIRECT DATA CENTER) ──────────────────────────────────────

    def is_running(self) -> bool:
        """Check whether the factory tmux session already exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True
        )
        return result.returncode == 0

    def start(self):
        """Create a detached tmux session and launch the Data Center harness."""
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

        # Launch the harness straight in classic interactive mode
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
        """After the fixed boot_wait: waits (at most ready_timeout s) for the TUI to have
        taken the screen — the command typed at the shell is no longer its last line — and
        for no update/installation to be displayed. Best-effort: past the delay, we carry
        on and say so (the first prompt may then be lost)."""
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            screen = self.capture() or ""
            low = screen.lower()
            still_shell = screen.rstrip().endswith(self.launch_cmd)
            busy = any(marker in low for marker in self.ready_busy_markers)
            if screen.strip() and not still_shell and not busy:
                return
            time.sleep(1)
        print(f"   ⚠️  {self.label}: TUI still booting or updating after "
              f"{self.ready_timeout}s — carrying on (the first prompt may be lost).")

    def after_boot(self):
        """Post-boot hook, before the first solicitation. Pointless for a harness
        that starts straight on its prompt (OpenCode); Codex confirms its « trust »
        screen there."""
        return

    def send_task(self, prompt: str):
        """Send a text prompt into the harness TUI through the tmux buffer.

        Returns as soon as the prompt is submitted: the end of the task is signalled
        by the FILE SENTINEL named in the prompt, never by a return value here."""
        with open(self.prompt_buffer, "w", encoding="utf-8") as f:
            f.write(prompt)

        # NAMED buffer: tmux buffers are global to the server, so two factories using
        # the default buffer would race each other (project A would paste the prompt
        # loaded by project B). '-d' deletes the buffer right after pasting.
        subprocess.run(["tmux", "load-buffer", "-b", self.session, self.prompt_buffer], check=True)
        subprocess.run(["tmux", "paste-buffer", "-d", "-b", self.session, "-t", self.session], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)

    def new_context(self):
        """Send the /new command to the harness to reset the context."""
        self.say("new_reset")
        # Escape FIRST: if the previous agent is still generating (it merely missed
        # its sentinel), a blind '/new' would be swallowed as prompt TEXT instead of
        # running as a command — context not reset, prompts piling up, the whole run
        # drifting.
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Escape"], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.session, "/new"], check=True)
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)
        time.sleep(self.new_session_wait)
        if not self.new_context_check:
            return
        # WARN-ONLY reset check (heuristic, to calibrate against the real TUI; NEVER
        # block the run on it): after a successful /new, the literal '/new' text
        # should no longer be on screen; its presence suggests it was typed as prompt
        # text.
        tail = self.capture()[-2000:]
        if "/new" in tail:
            self.say("new_warn")

    def capture(self) -> str:
        """Capture the current text content of the tmux terminal.

        Feeds the warn-only reset check after '/new' and the scaffold failure
        diagnosis (a tool-calling problem becomes visible without attaching to the
        session).
        """
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", self.session, "-p", "-S", "-10000"],
            capture_output=True, text=True
        )
        return result.stdout

    def kill(self):
        """Kill the tmux session cleanly."""
        if self.is_running():
            subprocess.run(["tmux", "kill-session", "-t", self.session])
            self.say("kill")

    # ─── HARNESS CONFIGURATION ────────────────────────────────────────────────

    def configured_model(self) -> str:
        """Model configured for this project, otherwise globally (failure message).
        Each implementation reads its own config format."""
        raise NotImplementedError

    def _config_candidates(self) -> list:
        """Project config first, global configs next."""
        return [self.config_file] + [os.path.expanduser(p) for p in self.global_configs]

    # ─── PREFLIGHT (diagnosis; the pipeline never calls it) ───────────────────

    def preflight(self) -> list:
        """[{ok, label, detail, fix_hint}]: binary present, authentication,
        configured model. Used by the command-line diagnosis (`python3 mm_runner.py`)
        and by the tools in tools/. The cockpit app runs this check on its own side:
        it does not import this module (single-file invariant)."""
        checks = []
        path = shutil.which(self.binary)
        version = None
        if path:
            version = self._first_line([self.binary, "--version"])
        checks.append({
            "ok": bool(path),
            "label": f"'{self.binary}' binary",
            "detail": (version or path) if path else "not on PATH",
            "fix_hint": "" if path else self.install_hint,
        })
        if not path:
            return checks
        authed, detail = self._auth_state()
        checks.append({
            "ok": authed,
            "label": "authentication",
            "detail": detail,
            "fix_hint": "" if authed else self.auth_hint,
        })
        model = self.configured_model()
        checks.append({
            "ok": True,               # informative: no pinned model is not an error
            "label": "configured model",
            "detail": model,
            "fix_hint": "",
        })
        return checks

    def _auth_state(self) -> tuple:
        """(authenticated?, readable detail). Never raises: a preflight that breaks
        would be worse than an imprecise one."""
        try:
            proc = subprocess.run(list(self.auth_cmd), capture_output=True,
                                  text=True, timeout=20)
        except Exception as exc:
            return False, f"check failed ({exc.__class__.__name__})"
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        first = out.splitlines()[0].strip() if out else ""
        return proc.returncode == 0, (first[:120] or "no answer")

    @staticmethod
    def _first_line(cmd: list) -> str:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except Exception:
            return ""
        out = (proc.stdout or proc.stderr or "").strip()
        return out.splitlines()[0][:60] if out else ""


class OpenCodeTuiRunner(AgentRunner):
    """OpenCode driven in its TUI through tmux — original OpenCode fork behaviour, extracted as is."""

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
    install_hint   = "install OpenCode: https://opencode.ai/docs"
    auth_cmd       = ("opencode", "auth", "list")
    auth_hint      = "authenticate: opencode auth login"
    # OpenCode logs (Linux/WSL, then macOS): source of the OBSERVED model when no config
    # sets it (the model chosen in the TUI via /model is written nowhere else) — otherwise
    # run.json and failReport said "the current model".
    log_dirs       = ("~/.local/share/opencode/log",
                      "~/Library/Application Support/opencode/log")

    def configured_model(self) -> str:
        """Model configured in .opencode/opencode.json; otherwise the model of the last
        OpenCode session opened in THIS project (OpenCode log); otherwise the fallback."""
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
            return f"{observed} (observed: last OpenCode session of this project)"
        return MODEL_FALLBACK

    def _observed_model(self) -> str:
        """'<providerID>/<model.id>' of the last 'message=created id=ses_…' line of the
        OpenCode log whose directory= is THIS project; '' if not found. Best-effort: the
        log format may change, the three most recent logs are enough."""
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
        """'opencode auth list' exits 0 even with no credential: the count on its
        last line (« N credentials ») is what settles it."""
        try:
            proc = subprocess.run(list(self.auth_cmd), capture_output=True,
                                  text=True, timeout=20)
        except Exception as exc:
            return False, f"check failed ({exc.__class__.__name__})"
        if proc.returncode != 0:
            return False, "opencode auth list failed"
        out = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"(\d+)\s+credential", out)
        if not match:
            # Unexpected format (new OpenCode version): we do not block.
            return True, "credentials not countable (unexpected format)"
        count = int(match.group(1))
        return count > 0, f"{count} credential(s) stored"


class CodexTuiRunner(AgentRunner):
    """Codex CLI driven in its TUI through tmux — original Codex fork behaviour, extracted as is."""

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
    install_hint   = "install Codex CLI: npm install -g @openai/codex"
    auth_cmd       = ("codex", "login", "status")
    auth_hint      = "authenticate: codex login"

    def after_boot(self):
        """Confirm the « Do you trust this directory? » screen of Codex's first boot
        in this folder. Trust is remembered per project (~/.codex/config.toml) and
        gates the reading of the local '.codex/config.toml' config (factory posture:
        approvals and sandbox disabled, model pinned). Without this confirmation the
        first pasted prompt would be swallowed by the choice screen.
        Folder already trusted (the screen does not appear): does nothing."""
        result = subprocess.run(["tmux", "capture-pane", "-p", "-t", self.session],
                                capture_output=True, text=True)
        if "Do you trust" not in (result.stdout or ""):
            return
        print("🔐 First Codex launch in this folder: confirming trust...")
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)
        time.sleep(self.boot_wait)

    def configured_model(self) -> str:
        """Read the model configured in .codex/config.toml (for the failure message).

        MINIMAL TOML parsing (regex on `model = "…"`): fix inherited from the Codex fork — a json.load
        inherited from opencode.json failed systematically on TOML, and the failure
        message came out without the model name."""
        for path in self._config_candidates():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    match = re.search(r'^\s*model\s*=\s*"([^"]*)"', f.read(), re.MULTILINE)
            except Exception:
                continue
            if match and match.group(1):
                return match.group(1)
        return MODEL_FALLBACK


# CLOSED, hard-coded registry: adding a harness = adding a class + one entry.
# No dynamic discovery (no entry points, no folder scan): the distribution is a
# onefile binary, an implicit import would be invisible there.
RUNNERS = {
    OpenCodeTuiRunner.name: OpenCodeTuiRunner,
    CodexTuiRunner.name:    CodexTuiRunner,
}


# ─── HARNESS SELECTION ────────────────────────────────────────────────────────

def _read_marker(project_dir: str) -> dict:
    """The project's equipment marker. Missing or unreadable → {}."""
    try:
        with open(os.path.join(project_dir, EQUIP_MARKER), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def detect_harness(project_dir: str) -> tuple:
    """(harness name | None, origin of the decision). Decides NOTHING else: no
    message, no exit — resolve_runner takes care of that.

    Origins: 'env', 'marker', 'artifacts', 'path', 'ambiguous', 'none'."""
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
    """Actionable stop message: the user must know what to TYPE."""
    if origin == "ambiguous":
        head = ("❌ Two possible harnesses, and nothing to choose between them.\n"
                "   OpenCode and Codex CLI are both available, but this project does\n"
                "   not say which one to use (no equipment marker).")
    else:
        head = ("❌ No AI agent harness found.\n"
                "   MAIsterMind drives a CLI agent: OpenCode or Codex CLI. Neither is\n"
                "   installed on this machine.")
    return f"""
{'='*60}
{head}

   Project: {project_dir}

   Three ways to settle it:
   1. Equip the project from the MAIsterMind app: it writes the chosen harness
      into '{EQUIP_MARKER}' at the project root.
   2. Force it for this launch:
         {HARNESS_ENV}=opencode  python3 <orchestrator>.py
         {HARNESS_ENV}=codex     python3 <orchestrator>.py
   3. Install one of the two, then authenticate:
         OpenCode: https://opencode.ai/docs      then  opencode auth login
         Codex   : npm install -g @openai/codex  then  codex login
{'='*60}
"""


def _load_mock_runner():
    """Load the TEST runner (tools/mm_mock_runner.py), outside the distribution, or None.

    The engine's ONLY test hook, and it only opens when MM_AGENT_HARNESS=mock: in
    production the module is nowhere to be found and the value is rejected like any
    unknown name — cleanly, not as a traceback. Imported by string (importlib) so
    that the Nuitka compilation of the distribution never tries to embed a module
    from tools/."""
    try:
        return importlib.import_module("mm_mock_runner").MockRunner
    except Exception:
        return None


def resolve_runner(project_dir: str, role: str = "factory", **options) -> AgentRunner:
    """The active harness runner for this project. Stops cleanly (exit 1) if no
    harness can be determined: an actionable message beats a 'tmux: command not
    found' in the middle of a run.

    `role` suffixes the tmux session; `options` is passed straight to the runner
    (`messages`, `new_context_check` — see AgentRunner.__init__)."""
    name, origin = detect_harness(project_dir)

    if name == "mock":
        mock = _load_mock_runner()
        if mock is not None:
            return mock(project_dir, role, **options)
        print(f"\n❌ {HARNESS_ENV}='mock': the test runner cannot be found. It lives "
              f"in tools/ and is NOT distributed — put tools/ on PYTHONPATH, or pick "
              f"a real harness.\n")
        sys.exit(1)

    if name is None:
        print(_no_harness_message(origin, project_dir))
        sys.exit(1)

    if name not in RUNNERS:
        known = ", ".join(sorted(RUNNERS))
        print(f"\n❌ {HARNESS_ENV}='{name}': unknown harness. Accepted values: {known}.\n")
        sys.exit(1)

    runner = RUNNERS[name](project_dir, role, **options)

    # Implicit decision: we SAY it. An 'oc-…' session where the user expected a
    # 'cx-…' one is the kind of surprise that costs half an hour.
    if origin == "artifacts":
        print(f"ℹ️  Harness inferred from the project artefacts: {runner.label} "
              f"('{runner.equip_dir}/' present).")
    elif origin == "path":
        print(f"ℹ️  Harness inferred from PATH: {runner.label} (only harness installed). "
              f"Equip the project from the app to pin that choice.")

    return runner


# ─── COMMAND-LINE DIAGNOSIS ───────────────────────────────────────────────────

def _print_diagnostic(project_dir: str):
    """`python3 mm_runner.py`: what an orchestrator started here would see, and the
    state of both harnesses. No side effect (nothing is launched)."""
    name, origin = detect_harness(project_dir)
    print(f"Project         : {project_dir}")
    print(f"{HARNESS_ENV:<16}: {os.environ.get(HARNESS_ENV) or '(not set)'}")
    print(f"Marker          : {_read_marker(project_dir).get('harness') or '(none)'}")
    print(f"Harness chosen  : {name or '(none)'}  [origin: {origin}]")
    for key in sorted(RUNNERS):
        cls = RUNNERS[key]
        runner = cls(project_dir, "factory")
        print(f"\n── {cls.label} ({key})")
        print(f"   session       : {runner.session}")
        print(f"   prompt buffer : {runner.prompt_buffer}")
        print(f"   project config: {cls.config_file}")
        for check in runner.preflight():
            mark = "✓" if check["ok"] else "✗"
            hint = f"   → {check['fix_hint']}" if check["fix_hint"] else ""
            print(f"   {mark} {check['label']:<18}: {check['detail']}{hint}")


if __name__ == "__main__":
    _print_diagnostic(os.getcwd())
