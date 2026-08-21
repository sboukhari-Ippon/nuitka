#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - PROTOTYPE factory with an agent harness + tmux (Designer / UX Version)
─────────────────────────────────────────────────────────────────────────────
"PROTOTYPE" VARIANT aimed at designers: generates clickable prototypes in
VANILLA HTML/CSS/JavaScript (no framework, no build, no test). Quality is checked at
TWO levels: at EVERY phase (mechanical guards + an independent Verifier Agent that
re-reads the produced files against the checklist and the design system), then ONCE
at the end by a global Reviewer that verifies three things: (A) compliance with the
UX rubric (skill 'ux'), (B) conformance to the blackboard (each phase done, each
user story covered) and (C) the application of the design system, end to end.

DESIGN SYSTEM (anti-hallucination guard):
  - The design system is declared by the HUMAN in 'need.md' ("## Design system"
    section: name + how to find it — MCP server, library/CDN, local folder,
    doc URL). If it is not there, a y/n gate asks to confirm the prototype's default
    tokens (agreement materialized in '.design_system_ack', which survives a
    resume) — a design system is NEVER invented by an agent.
  - The PO transcribes it into 'spec.md' ("Design system" section, re-read at gate 1),
    the Architect carries it into the plan ("Stack & Deliverables → Design system"),
    the compiler emits it in 'global_rules.design_system', and every production,
    verification and review prompt restates it.
  - Two MECHANICAL guards per phase (Python, zero LLM): anti "ghost designer" (the
    declared files actually changed) and anti "hallucinated tokens" (every var(--x)
    consumed by the produced files is defined in a CSS of the project — an invented
    design system betrays itself first through its tokens).

Pipeline (human gates: design system → spec → blackboard):
  - Step 1: a PO/UX Agent refines 'need.md' into a screen-, journey- and observable-
    UX-criteria-oriented 'spec.md' specification, VALIDATED by the human.
  - Step 2: an Architect Agent (prototype mode) converts 'spec.md' into an
    implementation plan of bounded micro-phases (deliverables = .html/.css/.js files):
    foundations (design system tokens) → shared components (grouped by family)
    → screens that ASSEMBLE without creating any new component.
  - Step 3: the blackboard conversion is a mechanical COPY of the plan's decisions
    (no verification command: a prototype has neither build nor test).
  - Step 4: per-phase PRODUCTION in sliced instances (sliced context, /new between
    phases). Each phase goes through the mechanical guards THEN the verdict of a
    fresh-context Verifier Agent (OK / REJECTED + gaps, forwarded to the designer-dev,
    loop bounded by MAX_ATTEMPTS). A mute verifier does not block the run: one
    relaunch, then acceptance with a warning (the mechanical guards already ran,
    the final review remains). An LLM's verdict stays an opinion: the mechanical
    guards run first, the final review re-checks everything.
  - Step 5: single global REVIEW. The Reviewer returns a verdict (OK / REJECTED + gaps)
    and writes 'review_report.md'. On gaps, a bounded correction loop
    (MAX_REVIEW_ATTEMPTS passes) fixes then re-verifies.

The agents communicate via sentinel files; the sole owner of the blackboard is the
Python orchestrator (no concurrent writes).
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil
import yaml

from mm_runner import resolve_runner, resolve_timeout

# Run journal (black box .mm-runs/, plan-big-last Lot 2): purely additive,
# full no-op if MM_AUDIT=0, NEVER makes a run fail.
import mm_audit

# Shared functions extracted at Lot 4a (plan-big-last): see mm_core.py.
# The configuration (THIS module's constants/objects) is injected at the end
# of the file via mm_core.configure(...) — all names are defined by then.
import mm_core
from mm_core import (
    collect_spec_us_ids, git_head_sha, load_blackboard, load_skills,
    signal_handler, wait_for_file_creation,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="proto", messages={
    "follow":   "   👀 Follow live in another terminal: tmux attach -t {session}",
    "new_warn": "   ⚠️  The TUI may not have reset (literal '/new' still on screen): "
                "if the run drifts, check with tmux attach.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REVIEW_REPORT_FILE    = "review_report.md"
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard-proto/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-proto/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Prototype system skills: applied AUTOMATICALLY to every production phase AND used as the
# rubric by the final reviewer. 'ux' = experience quality, 'proto-coding' = vanilla
# HTML/CSS/JS code conventions.
UX_SKILL              = "ux"
PROTO_CODING_SKILL    = "proto-coding"
PROTO_SYSTEM_SKILLS   = [UX_SKILL, PROTO_CODING_SKILL]

# Pipeline system skills: never treated as produced code.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-proto",
                         "plan-to-blackboard", "plan-to-blackboard-proto", "refacto"}

# Temporary context routing files
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_REVIEW_FILE       = RUNNER.tmp_file("review")
TMP_VERIF_FILE        = RUNNER.tmp_file("verif")
TMP_FIX_FILE          = RUNNER.tmp_file("fix")
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")

# ─── DESIGN SYSTEM (DECLARED BY THE HUMAN, NEVER INVENTED BY AN AGENT) ─────────
# The source of truth is 'need.md' ("## Design system" section: name + how to find
# it — MCP server, library/CDN, local folder, doc URL). Without a declaration, a y/n
# gate makes the human CONFIRM the default tokens: the agreement is MATERIALIZED (it
# survives a resume) and deliberately outside the '.pipeline_*' pattern purged by
# cleanup_all_sentinels. The pipeline then CARRIES the declaration (spec → plan →
# global_rules.design_system) without ever completing it.
DS_ACK_SENTINEL       = ".design_system_ack"
DS_DEFAULT            = "(none — the prototype's default tokens)"
# Section heading (## Design system / ### Design-système…) and free-text mention: the
# detection is deliberately BROAD — a false positive costs a faithful transcription by
# the PO, a false negative costs a mere y/n gate where the human decides.
DS_HEADING_RE         = re.compile(r"^#{1,4}\s*design[ -]?syst[eè]me?s?\b\s*:?\s*(.*)$", re.IGNORECASE)
DS_KEYWORD_RE         = re.compile(r"design[ -]?system|syst[eè]me\s+de\s+design|design\s+syst[eè]me", re.IGNORECASE)

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: the
# only valid choice on all 3 OSes (Windows has no /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# End-of-deliverable sentinels for the pipeline (steps 1 to 3): the agent creates the .done
# AFTER saving the deliverable, an unambiguous signal robust to writing pauses.
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"
PLAN_DONE_SENTINEL       = ".pipeline_plan.done"
BLACKBOARD_DONE_SENTINEL = ".pipeline_blackboard.done"

# HUMAN approval of the spec, materialized: the mere EXISTENCE of spec.md proves nothing
# (a timeout can leave a never-validated spec behind, see fail_pipeline). Deliberately
# outside the '.pipeline_*' pattern purged by cleanup_all_sentinels: the approval must
# survive a resume.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3              # Attempts for a phase (production + guards + verifier verdict)
MAX_REVIEW_ATTEMPTS   = 3             # Passes of the review -> correction -> re-review loop
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per phase / per review (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less net: pipeline deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds).


def fail_pipeline(message: str):
    """Single exit point for pipeline step failures (steps 1 to 3).

    Always kills the tmux session BEFORE exiting: an exit that leaves the agent alive
    lets it finish writing its deliverable AFTER the orchestrator gave up — on relaunch,
    that half-validated file would be mistaken for a valid resume state.
    """
    mm_audit.end("failed")
    print(message)
    RUNNER.kill()
    sys.exit(1)


# ─── PHASE SENTINELS (DESIGNER-DEV → ORCHESTRATOR CHANNEL) ─────────────────

def done_sentinel(phase_id: int, attempt: int) -> str:
    """File written by the designer-dev at the very end of a phase (signal 'I'm done').

    The attempt number is part of the name: a sentinel written late by the agent of a
    previous attempt cannot be mistaken for the current attempt's signal (no false
    positive on phase completion).
    """
    return f".phase_{phase_id}.attempt{attempt}.done"


def verdict_sentinel(phase_id: int, attempt: int) -> str:
    """File written by the per-phase Verifier Agent (OK/REJECTED verdict + gaps).

    Same principle as the .done sentinel: the attempt number in the name prevents a
    late verdict from a previous attempt from being mistaken for the current one.
    """
    return f".phase_{phase_id}.attempt{attempt}.verdict"


def cleanup_sentinels(phase_id: int):
    """Remove all sentinels (every attempt) of a phase (.done AND .verdict)."""
    prefix = f".phase_{phase_id}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and (name.endswith(".done") or name.endswith(".verdict")):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_sentinels():
    """Final cleanup of all residual sentinels (phases AND pipeline)."""
    for name in os.listdir("."):
        if (name.startswith(".phase_") and (name.endswith(".done") or name.endswith(".verdict"))) \
                or (name.startswith(".pipeline_") and (name.endswith(".done") or name.endswith(".verdict"))):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_pipeline_sentinel(sentinel: str):
    """Remove a residual pipeline sentinel (previous interrupted run)."""
    try:
        os.remove(sentinel)
    except OSError:
        pass


def read_touched_files(phase_id: int, attempt: int) -> list:
    """Read the list of files declared by the designer-dev in its .done sentinel.

    Small models often format the list as bullets ('- a.html', '* b.css', '1. c.js'):
    leading list markers are stripped to keep only paths.
    """
    path = done_sentinel(phase_id, attempt)
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


def no_declared_file_touched(files: list, since_ts: float, changed_since_phase: set = None) -> bool:
    """True if NO declared file actually changed SINCE THE START OF THE PHASE.

    Signature of the "ghost designer": a sentinel written without real work. No LLM
    verdict can reliably catch this case: this cheap, mechanical check handles it BEFORE
    paying for a verifier. Reference = the PHASE, not the attempt: a file produced in one
    attempt and re-declared unchanged in the next is still recognized as real work
    (deliberately LENIENT — ONE file actually touched WITHIN THE PHASE is enough to
    pass). Two signals: 'changed_since_phase' (git diff since the phase start, robust and
    primary — insensitive to the truncated mtimes of DrvFs/WSL2) then, as a git-less
    fallback, the mtime since the phase start ('since_ts').
    """
    changed_since_phase = changed_since_phase or set()
    for path in files:
        clean = path.strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue
        if clean in changed_since_phase:
            return False
        try:
            if os.path.exists(clean) and os.path.getmtime(clean) >= since_ts:
                return False
        except OSError:
            continue
    return True


# ─── "HALLUCINATED TOKENS" MECHANICAL GUARD (A DESIGN SYSTEM BETRAYS ITSELF THROUGH ITS TOKENS) ──
# An invented (or misapplied) design system shows up first through tokens that exist
# nowhere: var(--color-brand-500) consumed while no CSS of the project defines it. This
# check is PURELY mechanical (regex, zero LLM): it does not judge the design, it proves
# that a consumed identifier has a definition. Accepted false negative: invented tokens
# BUT defined locally by the same agent pass the guard — it is the Verifier Agent's role
# (and part C of the review) to compare against the declared design system.

CSS_VAR_USE_RE = re.compile(r"var\(\s*--([A-Za-z0-9_-]+)")
CSS_VAR_DEF_RE = re.compile(r"--([A-Za-z0-9_-]+)\s*:")

_TOKEN_SCAN_SKIP_DIRS = {".git", ".agents", ".venv", "node_modules", "__pycache__",
                         RUNNER.equip_dir}


def collect_defined_css_tokens() -> set:
    """Set of the CSS custom properties defined in the project (.css and .html files,
    excluding orchestration artifacts and tooling). Best-effort: unreadable = ignored."""
    defined = set()
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in _TOKEN_SCAN_SKIP_DIRS]
        for name in files:
            if not name.lower().endswith((".css", ".html", ".htm")):
                continue
            path = os.path.relpath(os.path.join(root, name)).replace("\\", "/")
            if is_orchestration_file(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    defined.update(CSS_VAR_DEF_RE.findall(f.read()))
            except OSError:
                continue
    return defined


def undefined_css_tokens(touched_files: list) -> list:
    """var(--x) tokens CONSUMED by the phase's declared files but defined in NO CSS/HTML
    of the project. Returns sorted (token, file) pairs, empty if everything is defined.
    Only looks at the existing declared files (.css/.html/.js)."""
    defined = None  # computed lazily: most phases use no var() at all
    missing = []
    for raw in touched_files:
        clean = raw.strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean or not clean.lower().endswith((".css", ".html", ".htm", ".js")):
            continue
        if not os.path.exists(clean) or is_orchestration_file(clean):
            continue
        try:
            with open(clean, "r", encoding="utf-8") as f:
                used = set(CSS_VAR_USE_RE.findall(f.read()))
        except OSError:
            continue
        if not used:
            continue
        if defined is None:
            defined = collect_defined_css_tokens()
        for token in sorted(used - defined):
            missing.append((token, clean))
    return sorted(missing)


def read_review_verdict(path: str) -> tuple:
    """Read the final Reviewer's verdict. Returns (is_ok: bool, gaps: str).

    Tolerant parsing: leading blank lines and markdown fences are ignored, then the first
    word of the first useful line is read. 'OK', 'OK.', 'OK, compliant'... validate; anything
    else (including 'REJECTED') rejects, the body becoming the list of gaps.
    """
    if not os.path.exists(path):
        return False, "The reviewer produced no verdict."
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return False, "Empty verdict produced by the reviewer."

    lines = raw.splitlines()
    idx = 0
    while idx < len(lines) and (not lines[idx].strip() or lines[idx].strip().startswith("```")):
        idx += 1
    head_line = lines[idx].strip() if idx < len(lines) else ""

    token = ""
    for ch in head_line.upper():
        if ch.isalpha():
            token += ch
        else:
            break

    if token == "OK":
        return True, ""
    body = "\n".join(lines[idx + 1:]).strip() if token == "REJECTED" else raw
    return False, body or "The reviewer rejected the prototype without specifying the gaps."


# ─── GIT LANDMARKS (BEST-EFFORT) ─────────────────────────────────────────────────
# BEST-EFFORT: without git (binary absent, init failure), the factory runs identically but
# without landmarks — graceful degradation. This prototype variant has no mechanical phase
# verdict: git provides an audit trail (baseline, one commit per signaled phase, one after
# the review) — a manual per-step rollback point for the human.

_GIT = {"enabled": False}

# Identity passed per command: the factory must not depend on the local git config.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

GITIGNORE_BODY = f"""# MAIster-Mind orchestration artifacts (ephemeral)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.spec_approved
.design_system_ack
.mm-runs/
__pycache__/
"""


def run_git(args: list, timeout: int = 60) -> tuple:
    """Run a git command. Returns (ok, stdout stripped). Never raises."""
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


def commit_phase(label: str) -> bool:
    """Commit the whole working tree (best-effort; failure → warn and keep going).

    --allow-empty: a signaled phase that changed nothing still gets its landmark commit,
    so per-phase shas stay reliable for diffs and manual rollbacks.
    """
    if not _GIT["enabled"]:
        return False
    ok_add, _ = run_git(["add", "-A"])
    ok_commit = False
    if ok_add:
        ok_commit, _ = run_git(["commit", "-q", "--allow-empty", "-m", label])
    if not ok_commit:
        print(f"⚠️  git commit failed for '{label}' (continuing without this landmark).")
    return ok_commit


def files_changed_since_phase_start(start_sha: str) -> set:
    """Set of files modified/created since a reference sha (the factory's scope, RUN scale).
    Empty without git or without a sha → the caller falls back to the fallback.
    """
    if not _GIT["enabled"] or not start_sha:
        return set()
    changed = set()
    ok_diff, diff_out = run_git(["diff", "--name-only", start_sha])
    if ok_diff:
        changed.update(line.strip() for line in diff_out.splitlines() if line.strip())
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        changed.update(line.strip() for line in others_out.splitlines() if line.strip())
    return changed


# Orchestrator artifacts (never produced code): excluded from the review scope.
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REVIEW_REPORT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_REVIEW_FILE, TMP_VERIF_FILE, TMP_FIX_FILE,
    TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, DS_ACK_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


def is_orchestration_file(path: str) -> bool:
    """Is 'path' an orchestrator artifact (and not the produced prototype)?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _ORCH_BASENAMES:
        return True
    # Ephemeral sentinels and buffers, wherever they sit in the tree.
    if base.startswith(".phase_") or base.startswith(".pipeline_"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Python caches, virtual environment and tooling directories: never produced proto.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def ensure_phase_repo():
    """Per-phase git landmarks, set up before production (best-effort).

    If the project is already a git repo (human-managed), it is reused AS IS. Otherwise
    'git init' + a minimal .gitignore + a baseline commit.
    """
    if shutil.which("git") is None:
        print("⚠️  git not found: per-phase commits are disabled for this run.")
        return
    if os.path.isdir(".git"):
        _GIT["enabled"] = True
        print("✓ Existing git repo reused (per-phase commits enabled).")
        return
    ok, _ = run_git(["init", "-q"])
    if not ok:
        print("⚠️  'git init' failed: per-phase commits are disabled for this run.")
        return
    if not os.path.exists(".gitignore"):
        with open(".gitignore", "w", encoding="utf-8") as f:
            f.write(GITIGNORE_BODY)
    _GIT["enabled"] = True
    commit_phase("baseline: proto factory start")


# ─── FILE MONITOR SYNCHRONIZATION ─────────────────────────────────────────────

def spec_structural_check(path: str) -> bool:
    """Minimal structural floor for a spec accepted WITHOUT sentinel: its mandatory
    "Out of scope" section must be present."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "out of scope" in f.read().lower()
    except OSError:
        return False


def plan_structural_check(path: str) -> bool:
    """Minimal structural floor for a plan accepted WITHOUT sentinel: the mandatory
    "Stack & Deliverables" header block must be present."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "stack & deliverables" in f.read().lower()
    except OSError:
        return False


def blackboard_structural_check(path: str) -> bool:
    """Minimal structural floor for a blackboard accepted WITHOUT sentinel: the YAML
    must at least parse."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) is not None
    except (OSError, yaml.YAMLError):
        return False


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Wait for a pipeline deliverable (spec/plan/blackboard/report) signaled by a SENTINEL.

    The agent creates a .done file AFTER saving the deliverable. SAFETY NET for an agent that
    forgets the sentinel: if the deliverable exists, is non-empty and has not changed for
    STABLE_POLLS_FALLBACK consecutive checks, it is accepted with a warning. The optional
    'structural_check' parameter hardens this net ONLY.
    """
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}' (completion signal: '{sentinel}')...")
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
                        print(f"   ⏳ '{filepath}' is stable but structurally incomplete: "
                              f"still waiting (the agent may still be writing).")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinel '{sentinel}' missing but '{filepath}' has been stable for "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s: deliverable accepted (safety net).")
                return True
    return False


# ─── BLACKBOARD READ / WRITE ──────────────────────────────────────────────────

# Last journaled phase statuses (TRANSITION detection by save_blackboard).
_PHASE_STATUS_SEEN = {}


def save_blackboard(data: dict):
    """Write the blackboard ATOMICALLY (temporary file + os.replace).

    The blackboard is the ONLY resume state (which phases are DONE/OK). A kill right in
    the middle of a classic 'w'-mode dump (which truncates then rewrites in place) would
    leave a half-written YAML → resume impossible, the whole run lost. So we write to a
    temporary file, force the flush to disk, then rename atomically. os.replace is atomic
    on the same filesystem (POSIX as well as Windows).
    """
    tmp_path = BLACKBOARD_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, BLACKBOARD_FILE)
    # Run journal: a phase-status TRANSITION triggers an event + a frozen copy of
    # the blackboard (saves without a transition journal nothing).
    statuses = {str(p.get("id")): str(p.get("status"))
                for p in (data.get("phases") or []) if isinstance(p, dict)}
    if statuses != _PHASE_STATUS_SEEN:
        for pid, status in statuses.items():
            if _PHASE_STATUS_SEEN.get(pid) != status:
                mm_audit.event("phase_status", id=pid, status=status)
        _PHASE_STATUS_SEEN.clear()
        _PHASE_STATUS_SEEN.update(statuses)
        mm_audit.snapshot(BLACKBOARD_FILE)


def global_rule(blackboard: dict, key: str) -> str:
    """Read a global rule with an honest fallback '(unspecified)' (blackboard produced by a
    fallible small LLM: a field may be missing)."""
    value = (blackboard.get("global_rules") or {}).get(key)
    return value if value else "(unspecified)"


def design_system_rule(blackboard: dict) -> str:
    """The design system carried down to the blackboard, with an honest fallback to the
    default tokens (a missing field must never let an agent invent one)."""
    value = (blackboard.get("global_rules") or {}).get("design_system")
    return value if value else DS_DEFAULT


def ensure_design_system(blackboard: dict, declared: str):
    """MECHANICAL transport guard: a design system declared by the HUMAN (need.md,
    transcribed into the spec they validated) must NEVER get lost along the way.

    If the compiler omitted global_rules.design_system, design_system_rule would fall
    back in SILENCE to the default tokens while a design system exists: so we COPY the
    declaration (human text, never an invention) and save — the human gate then re-reads
    the repaired value in the recap and in blackboard.yaml.
    The "(mentioned in need.md …)" placeholder of the keyword fallback is not a
    description: it repairs nothing (the field written by the compiler prevails).
    """
    if not declared or declared.startswith("(mentioned"):
        return
    rules = blackboard.get("global_rules")
    if not isinstance(rules, dict):
        rules = {}
        blackboard["global_rules"] = rules
    if rules.get("design_system"):
        return
    rules["design_system"] = declared
    save_blackboard(blackboard)
    print(f"🎨 Transport repaired: 'global_rules.design_system' was missing from "
          f"'{BLACKBOARD_FILE}' — declaration copied verbatim from the validated spec.")


def validate_phase_ids(blackboard: dict) -> tuple:
    """Uniqueness/sequence guards on phases[].id. Returns (fatal, soft).

    A duplicated id makes two phases SHARE their '.phase_N.attemptM.done' sentinels
    (false completion signals): fatal. A non-contiguous sequence is merely reported.
    """
    fatal, soft = [], []
    phases = blackboard.get("phases") if isinstance(blackboard, dict) else None
    if not isinstance(phases, list) or not phases:
        return ["Missing or empty 'phases' block: nothing to produce."], []
    ids = [str(phase.get("id")) for phase in phases if isinstance(phase, dict) and "id" in phase]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        fatal.append(
            f"Duplicated phases[].id ({', '.join(duplicated)}): the '.phase_N.attemptM.done' "
            f"sentinels would be SHARED between two phases (false completion signals)."
        )
    elif ids and ids != [str(i) for i in range(1, len(ids) + 1)]:
        soft.append(
            f"phases[].id is not a contiguous 1..N sequence ({', '.join(ids)}): tolerated, "
            f"but check that the compiler did not skip or renumber a phase."
        )
    return fatal, soft


def check_need_file():
    if not os.path.exists(NEED_FILE):
        print(f"❌ Critical error: '{NEED_FILE}' is missing.")
        sys.exit(1)
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print(f"❌ Critical error: '{NEED_FILE}' is empty.")
        sys.exit(1)
    print("✓ Need file (need.md) validation: OK")


# ─── DESIGN SYSTEM GATE (HUMAN-IN-THE-LOOP, BEFORE ANY AGENT) ──────────────────

def read_design_system_from_need(need_text: str) -> str:
    """Description of the design system declared in need.md, otherwise an empty string.

    Two recognized forms: a titled section ("## Design system" — its body is the
    description, later transcribed by the PO) or a mere free-text mention (the PO is
    then told to transcribe from need.md). NO inference beyond that: if nothing is
    found, the y/n gate decides — never an agent.
    """
    lines = need_text.splitlines()
    for i, line in enumerate(lines):
        match = DS_HEADING_RE.match(line.strip())
        if match:
            level = len(line.strip()) - len(line.strip().lstrip("#"))
            body = []
            for follower in lines[i + 1:]:
                stripped = follower.strip()
                if stripped.startswith("#") and (len(stripped) - len(stripped.lstrip("#"))) <= level:
                    break
                body.append(follower)
            text = (match.group(1).strip() + "\n" + "\n".join(body)).strip()
            if text:
                return text
    if DS_KEYWORD_RE.search(need_text):
        return "(mentioned in need.md — to transcribe faithfully from need.md)"
    return ""


def confirm_default_design_system():
    """y/n gate BEFORE any agent: without a design system declared in need.md, the human
    confirms the default tokens or stops to declare their own.

    It is the mechanical answer to the "hallucinated design system" risk: the declaration
    can ONLY come from the human (need.md), never from an agent. The agreement is
    MATERIALIZED (like the spec approval): a resume does not ask again. No agent nor tmux
    session exists yet at this stage: refusing is free.
    """
    print(f"\n{'='*50}")
    print(f"🎨 DESIGN SYSTEM — no mention detected in '{NEED_FILE}'.")
    print(f"   If you use a design system: answer n, then describe it in '{NEED_FILE}'")
    print(f"   (“## Design system” section: its name, and how to find it — MCP server")
    print(f"   to mention to the agents, library/CDN, local folder, doc URL) and relaunch.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Continue with the prototype's default tokens? (y/n): ")
    mm_audit.event("gate", id="design-system", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Cancelled by the user. Declare your design system in '{NEED_FILE}' "
              f"(“## Design system” section), then relaunch.")
        RUNNER.kill()
        sys.exit(0)
    with open(DS_ACK_SENTINEL, "w", encoding="utf-8") as f:
        f.write("default tokens acknowledged\n")


# ─── PER-PHASE SPEC SLICING (CONTEXT WINDOW) ───────────────────────

# Heading of a user story in the PO spec (e.g. "### US-1: Home screen").
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Slice of the spec limited to the US covered by the phase (+ everything outside US).

    We only keep the '### US-n' sections listed in 'covers', plus everything that is not a
    US section (goal, constraints, out-of-scope, assumptions). Small-model prudence: if
    'covers' is empty, if the spec does not follow the US format, or if no covered US is found
    in it, return the WHOLE spec (graceful degradation).
    """
    wanted = {c.strip().upper() for c in (covers or [])
              if isinstance(c, str) and c.strip()}
    if not wanted:
        return spec_text
    spec_us_ids = collect_spec_us_ids(spec_text)
    if not spec_us_ids or not (wanted & spec_us_ids):
        return spec_text
    kept = []
    current_us = None  # id of the current US section, None = common trunk
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            current_us = match.group(1).upper()
        elif current_us is not None and line.startswith("## "):
            current_us = None  # end of the US zone: back to the common trunk
        if current_us is None or current_us in wanted:
            kept.append(line)
    return "\n".join(kept)


def check_spec_coverage(blackboard: dict, spec_text: str) -> list:
    """(Non-blocking) WARNINGS of spec → phases traceability via 'covers'.

    Two directions: a US referenced by a phase but absent from the spec (likely compiler
    hallucination), and a US of the spec covered by no phase (screen possibly FORGOTTEN by the
    Architect). Warn-only: the human eye at the y/n decides.
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
        warnings.append(f"US referenced by phases but ABSENT from the spec: "
                        f"{', '.join(unknown)} (likely compiler hallucination).")
    uncovered = sorted(spec_us - referenced)
    if uncovered:
        warnings.append(f"US of the spec covered by NO phase: {', '.join(uncovered)} "
                        f"(screen forgotten by the Architect? Check the plan).")
    return warnings


# ─── SKILLS LOADING ────────────────────────────────────────────────────────────

def present_system_skills() -> list:
    """System skills ACTUALLY present in the project (disk scan, never an agent
    declaration): the only source of truth of what will be applied."""
    return [s for s in PROTO_SYSTEM_SKILLS
            if os.path.exists(os.path.join(SKILLS_DIR, s, "SKILL.md"))]


def inject_system_skills(blackboard: dict) -> list:
    """MECHANICALLY materializes 'skills_required' on every phase of the blackboard.

    The LLM compiler NEVER emits this field in prototype mode (explicit instruction):
    the orchestrator writes it, from the disk scan of the system skills
    (ux, proto-coding). The blackboard thus becomes the VISIBLE trace, from the human
    gate onwards, of what will be applied to each phase — and the 'ux' skill, if it
    exists, is ALWAYS on board: re-injected even after a manual edit that would have
    removed it. Additional skills added by hand are preserved.
    Returns the list of system skills present.
    """
    system = present_system_skills()
    changed = False
    for phase in blackboard.get("phases", []) or []:
        if not isinstance(phase, dict):
            continue
        declared = phase.get("skills_required") or []
        merged = system + [s for s in declared if s not in system]
        if declared != merged:
            phase["skills_required"] = merged
            changed = True
    if changed:
        save_blackboard(blackboard)
    return system


def phase_skills(phase: dict) -> list:
    """Skills to load for THIS phase: the system skills present (orchestrator
    guarantee, whatever the blackboard says), then the additional ones the blackboard
    declares — if they exist on disk and are not pipeline skills (never routed to
    production)."""
    extras = [s for s in (phase.get("skills_required") or [])
              if s not in PROTO_SYSTEM_SKILLS and s not in PIPELINE_SKILLS
              and os.path.exists(os.path.join(SKILLS_DIR, s, "SKILL.md"))]
    return present_system_skills() + extras


def check_proto_skills():
    """Check the presence of the prototype's system skills (ux, proto-coding).

    They are applied automatically to every phase and serve as the reviewer's rubric: their
    absence strongly degrades quality, so we warn the human without blocking.
    """
    present = present_system_skills()
    missing = [s for s in PROTO_SYSTEM_SKILLS if s not in present]
    if missing:
        print(f"\n⚠️  System skill(s) not found: {', '.join(missing)}")
        print(f"   Expected path: {SKILLS_DIR}/<skill>/SKILL.md")
        print("   → Phases and the review will run without them (degraded quality).\n")
    else:
        print(f"✅ System skills present: {', '.join(PROTO_SYSTEM_SKILLS)}.\n")


# ─── INTERACTIVE STEPS 1 TO 3 IN THE TUI (CLOUD) ────────────────────────────

def generate_spec_from_need_tui(ds_declared: str):
    print("\n📖 [STEP 1: PO/UX AGENT] Refining the need into a specification (screens & journeys)...")

    if not os.path.exists(PO_SKILL_FILE):
        fail_pipeline(f"❌ Missing PO skill: '{PO_SKILL_FILE}'")
    with open(PO_SKILL_FILE, "r", encoding="utf-8") as f:
        po_spec = f.read()
    with open(TMP_PO_FILE, "w", encoding="utf-8") as f:
        f.write(po_spec)

    # The design system is a HUMAN DECLARATION (need.md or y/n gate) that the PO
    # TRANSCRIBES — never an agent decision. The directive therefore depends on what the
    # human declared: faithful transcription, or an explicit "(none)" section.
    if ds_declared:
        ds_directive = (f"- DESIGN SYSTEM: the need declares one. Transcribe it FAITHFULLY into a "
                        f"“## Design system” section of '{SPEC_FILE}': its name and how to access it "
                        f"(MCP server, library/CDN, local folder, doc URL), as '{NEED_FILE}' "
                        f"gives them. Copy, zero invention, zero addition.")
    else:
        ds_directive = (f"- DESIGN SYSTEM: no design system is declared (choice confirmed by "
                        f"the human). Add to '{SPEC_FILE}' a “## Design system” section "
                        f"containing exactly: “{DS_DEFAULT}”. NEVER invent one.")

    po_prompt = f"""Read the file '{NEED_FILE}' at the root of our project, as well as the Product Owner instructions in the file '{TMP_PO_FILE}'.
You are a design/UX-oriented Product Owner. Applying the instructions of '{TMP_PO_FILE}' SCRUPULOUSLY, refine the raw need into a PROTOTYPE specification and save it DIRECTLY in a new file named '{SPEC_FILE}' at the project root.

Directives specific to this prototype:
- Zero invention: every requirement must derive from the need expressed in '{NEED_FILE}'.
- Think in SCREENS and JOURNEYS: each user story describes a screen or a step of the user journey.
- Acceptance criteria are behaviors OBSERVABLE ON SCREEN (Given / When / Then): what is displayed, the states (empty, error, loading), the possible actions.
{ds_directive}
- Any ambiguity in the need becomes an explicit assumption in "Assumptions & Questions".
- The "Out of scope" section is mandatory (anti over-engineering lock: a prototype does not implement everything).
- Make NO technical choice: this is an HTML/CSS/JS prototype, the architect will decide the breakdown.
Do it directly via your file-editing tools, without needless chatter in the console.
As your very LAST action, after saving '{SPEC_FILE}', create the sentinel file '{SPEC_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
"""
    cleanup_pipeline_sentinel(SPEC_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(po_prompt))
    RUNNER.send_task(po_prompt)

    if wait_for_pipeline_file(SPEC_FILE, SPEC_DONE_SENTINEL, structural_check=spec_structural_check):
        print(f"✅ [STEP 1] Specification '{SPEC_FILE}' created successfully!")
    else:
        fail_pipeline(f"❌ [STEP 1] Timeout or failure creating '{SPEC_FILE}'.")


def confirm_spec_with_human():
    """Human validation of the spec (UPSTREAM human-in-the-loop).

    This is where fixing costs the least: a misunderstood requirement rejected at this stage
    avoids paying for (and redoing) a plan, a blackboard and production phases.
    """
    print(f"\n{'='*50}")
    print(f"📋 SPECIFICATION READY: re-read '{SPEC_FILE}' (assumptions and out-of-scope first).")
    print(f"   You can edit it directly in another terminal before validating.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Validate the specification and start the architecture? (y/n): ")
    mm_audit.event("gate", id="spec", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Cancelled by the user. Refine '{NEED_FILE}', delete '{SPEC_FILE}', then relaunch.")
        RUNNER.kill()
        sys.exit(0)
    # The approval is MATERIALIZED (not inferred from the file's existence): on resume, a
    # spec without this sentinel goes back through the y/n instead of being taken on trust.
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # frozen copy of the spec AS APPROVED


def generate_plan_from_need_tui():
    print("\n📖 [STEP 2: PROTO ARCHITECT AGENT] Generating the implementation plan...")

    if not os.path.exists(PLAN_SKILL_FILE):
        fail_pipeline(f"❌ Missing planning skill: '{PLAN_SKILL_FILE}'")
    with open(PLAN_SKILL_FILE, "r", encoding="utf-8") as f:
        plan_spec = f.read()
    with open(TMP_PLAN_FILE, "w", encoding="utf-8") as f:
        f.write(plan_spec)

    planning_prompt = f"""Read the file '{SPEC_FILE}' at the root of our project (validated specification), as well as the architecture instructions in the file '{TMP_PLAN_FILE}'.
You are a prototype Architect. Applying the instructions of '{TMP_PLAN_FILE}' SCRUPULOUSLY, generate a sequential implementation plan in Markdown format and save it DIRECTLY in a new file named '{PLAN_FILE}' at the project root.

Directives for the file '{PLAN_FILE}':
- Imposed stack: HTML5 + CSS + VANILLA JavaScript. NO framework, NO build, NO test, NO verification command.
- The plan MUST start with the "Stack & Deliverables" block and EVERY phase MUST declare its "Covers" field (US-x): the following pipeline steps copy these decisions without inferring them.
- DESIGN SYSTEM: the "Stack & Deliverables" block MUST carry the line "**Design system:** …" COPIED from the "Design system" section of '{SPEC_FILE}' (name + access source — MCP server, library/CDN, local folder, URL), or "{DS_DEFAULT}" if the spec says so. You NEVER complete nor invent a design system.
- Lay the foundations in the first phase (design system tokens materialized in assets/css/tokens.css — the SINGLE source of the tokens —, base, index.html), then the shared COMPONENTS in one or more bounded phases (grouped by family: forms, navigation, data display — only those the spec's screens require), then one screen (or coherent group of screens) per phase, which ASSEMBLES the existing components without creating new ones.
- Break down into BOUNDED micro-phases (1 to 5 tasks, at most 5 files created/modified, at most 3 files to read per phase); the indicative range of 3 to 10 phases always yields to these size bounds.
- YAGNI principle: plan ONLY what the specification asks for; its "Out of scope" section is a prohibition.
Do it directly via your file-editing tools, without needless chatter in the console.
As your very LAST action, after saving '{PLAN_FILE}', create the sentinel file '{PLAN_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
"""
    cleanup_pipeline_sentinel(PLAN_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(planning_prompt))
    RUNNER.send_task(planning_prompt)

    if wait_for_pipeline_file(PLAN_FILE, PLAN_DONE_SENTINEL, structural_check=plan_structural_check):
        print(f"✅ [STEP 2] Plan '{PLAN_FILE}' created successfully!")
    else:
        fail_pipeline(f"❌ [STEP 2] Timeout or failure creating '{PLAN_FILE}'.")


def transform_plan_to_blackboard_tui():
    if not os.path.exists(BLACKBOARD_SKILL_FILE):
        fail_pipeline(f"❌ Missing blackboard compiler skill: '{BLACKBOARD_SKILL_FILE}'")

    print("\n📖 [STEP 3: PROTO BLACKBOARD COMPILER] Generating the YAML Blackboard...")

    with open(BLACKBOARD_SKILL_FILE, "r", encoding="utf-8") as f:
        compiler_spec = f.read()
    with open(TMP_ARCHITECT_FILE, "w", encoding="utf-8") as f:
        f.write(compiler_spec)

    prompt = f"""You are a Blackboard Compiler: you COPY the plan's decisions, you make none. Read the plan just generated in '{PLAN_FILE}' as well as the structure instructions in the file '{TMP_ARCHITECT_FILE}'.
Generate the file '{BLACKBOARD_FILE}' at the root of our project, scrupulously respecting the requested YAML format.

PROTOTYPE MODE REMINDER: emit NO verify_cmd, build_cmd, mutation_cmd, skills_required or nature field (an HTML/JS prototype has neither build nor test; the system skills — ux, proto-coding — are materialized into skills_required by the ORCHESTRATOR itself after your compilation, from a disk scan: you NEVER emit that field). Do emit, however, global_rules.design_system: the COPY of the plan's "Stack & Deliverables → Design system" line ("{DS_DEFAULT}" if the plan declares it that way — never invented).

Write the clean YAML directly into the file '{BLACKBOARD_FILE}', without wrapping it in markdown fences like ```yaml.
As your very LAST action, after saving '{BLACKBOARD_FILE}', create the sentinel file '{BLACKBOARD_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
"""
    cleanup_pipeline_sentinel(BLACKBOARD_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)

    if wait_for_pipeline_file(BLACKBOARD_FILE, BLACKBOARD_DONE_SENTINEL,
                              structural_check=blackboard_structural_check):
        try:
            parsed_data = load_blackboard()
            print(f"🏆 [STEP 3] Blackboard initialized and validated in '{BLACKBOARD_FILE}'!\n")
            return parsed_data
        except Exception as err:
            fail_pipeline(f"❌ [STEP 3] YAML parsing failure: {err}")
    else:
        fail_pipeline(f"❌ [STEP 3] Timeout or failure creating '{BLACKBOARD_FILE}'.")


# ─── STEPS 4 & 5: FILE-DEPORTED PROMPTS ──────────────────────────────

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    # Architect context and reading list, carried from the plan: GUIDANCE that spares the
    # designer-dev a free re-exploration of the project.
    context_block = ""
    if str(phase.get("context") or "").strip():
        context_block = f"""--- YOUR PLACE IN THE PLAN (architect context) ---
{str(phase.get("context")).strip()}

"""
    files_to_read = [str(p).strip() for p in (phase.get("files_to_read") or []) if str(p).strip()]
    files_block = ""
    if files_to_read:
        files_block = ("--- FILES TO READ FIRST ---\n"
                       "Read these files BEFORE coding (the Architect selected them for this "
                       "phase); do not explore the rest of the project unless strictly necessary:\n"
                       + "\n".join(files_to_read) + "\n\n")

    full_context = f"""--- SYSTEM RULES ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Visual direction: {global_rule(blackboard, 'styling')}
Constraints: {global_rule(blackboard, 'constraints')}
Accessibility: {global_rule(blackboard, 'accessibility')}

{skills_context}--- BEHAVIORAL CONTRACT ---
You are a Designer-Dev Agent hyper-specialized for Phase {phase['id']} ONLY.
You implement ONLY the tasks of this phase. Stop as soon as it is done.
YAGNI principle: you do nothing that is not explicitly requested.

--- DELIVERABLE NATURE: PROTOTYPE ---
You produce a clickable prototype in VANILLA HTML/CSS/JavaScript: no framework, no
bundler, no `npm install`, no build step. Each screen must open directly in a browser
(double-click on the `.html`). Data is hard-mocked (JS objects); no backend nor real
network call. Scrupulously apply the skills above (`ux` and `proto-coding`): interface
states, accessibility, BEM, CSS tokens.

--- DESIGN SYSTEM (ANTI-HALLUCINATION RULE) ---
The design system of this prototype is: {design_system_rule(blackboard)}.
If a design system is declared above: your tokens, components and classes come from it
EXCLUSIVELY — the tokens live in assets/css/tokens.css (single source, materialized by
the foundations phase from the declared source: MCP server, library, folder, doc), the
components in the shared CSS. You NEVER invent a token (var(--…)) nor a component that
does not exist in that design system; the orchestrator mechanically checks that every
var(--…) consumed is defined. In a SCREEN phase, you ASSEMBLE the existing components
without creating new ones. Without a declared design system, you use the prototype's
default tokens (already in tokens.css) — without ever claiming to follow a named design
system.

{context_block}{files_block}--- NEED / SPECIFICATION ---
{user_need}

--- PHASE {phase['id']} OBJECTIVE: {phase['name']} ---
Checklist:
{chr(10).join([f'- [ ] {t}' for t in phase.get('tasks', [])])}

--- VERIFIER FEEDBACK TO FIX (if any) ---
{critic_feedback}

--- MANDATORY END-OF-PHASE INSTRUCTION ---
You NEVER touch the file {BLACKBOARD_FILE}: the orchestrator manages it.
When all the tasks of the phase are ACTUALLY done in the files, and as your very last
action, create the sentinel file '{done_sentinel(phase['id'], attempt)}' at the project root.
It must contain the list of files you created or modified (one path per line), and nothing else.
This file is the phase-completion signal: only create it when you are REALLY done.
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the instruction file '{TMP_CODER_FILE}' at the project root. Scrupulously follow its instructions to carry out Phase {phase['id']}."


def build_phase_verifier_prompt(phase: dict, blackboard: dict, phase_need: str,
                                touched_files: list, attempt: int) -> str:
    """Instructions of the PER-PHASE Verifier Agent (fresh context, independent of the designer-dev).

    Deliberately NARROW scope (context window): the files declared by the phase + the
    spec slice covered + the design system — never the whole prototype (that is the
    final review's role). Its verdict is an LLM OPINION: it comes AFTER the mechanical
    guards (anti-ghost, hallucinated tokens) and BEFORE the final review, which
    re-checks everything.
    """
    files_block = "\n".join(f"   - {f}" for f in touched_files) or "   (no file declared)"
    tasks_block = "\n".join(f"   - {t}" for t in (phase.get("tasks") or [])) or "   (no task listed)"
    full_context = f"""You are a strict and independent phase Verifier (Lead Designer-Dev QA). You did NOT produce this code: you judge it. You verify ONLY Phase {phase['id']} "{phase['name']}", nothing else.

--- GLOBAL PROJECT RULES ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Visual direction: {global_rule(blackboard, 'styling')}
Constraints: {global_rule(blackboard, 'constraints')}
Accessibility: {global_rule(blackboard, 'accessibility')}

--- NEED COVERED BY THIS PHASE (spec extract) ---
{phase_need}

--- PHASE CHECKLIST (to confront with the real code) ---
{tasks_block}

--- FILES DECLARED BY THE DESIGNER-DEV (your reading scope) ---
{files_block}

--- MANDATORY METHOD ---
1. Open and actually READ each declared file with your tools. Do not rely on any summary.
2. CHECKLIST: is each task above CONCRETELY done in these files?
3. DESIGN SYSTEM: if a design system is declared in the global rules, verify that it is ACTUALLY applied and never hallucinated: the consumed tokens (var(--…)) and the used components/classes come from the declared design system (materialized in assets/css/tokens.css and the shared CSS) — no invented token or component, no "in the style of" class. If this phase is a SCREEN phase, it ASSEMBLES the existing components: flag any new component created in a screen phase.
4. DELIVERABLE: the delivered .html file(s) open autonomously (consistent relative paths, no framework, no undeclared network dependency, mocked data).
5. Only report gaps you have ACTUALLY observed in the files; cite the offending file. Demand NOTHING beyond THIS phase's checklist (the other screens, the global UX review and the rest of the plan are not your scope).

--- MANDATORY VERDICT ---
As your very LAST action, write your verdict in the sentinel file '{verdict_sentinel(phase['id'], attempt)}' at the root:
- If the checklist is honored AND the design system correctly applied: the FIRST line contains EXACTLY the word "OK" (nothing else).
- Otherwise: the FIRST line contains EXACTLY the word "REJECTED", then the following lines list precisely and briefly the gaps to fix (file + problem + expected correction): they will be forwarded as is to the designer-dev.
You modify NO file of the prototype and you NEVER touch the file {BLACKBOARD_FILE}.
"""
    with open(TMP_VERIF_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instruction file '{TMP_VERIF_FILE}' at the project root and verify "
            f"Phase {phase['id']} as it requires.")


def serialize_phases_for_review(blackboard: dict) -> str:
    """Compact list of phases (id, name, covered US, tasks) for the reviewer's conformance
    part: it verifies that EACH task is concretely done and each US covered."""
    blocks = []
    for phase in blackboard.get("phases", []) or []:
        covers = ", ".join(phase.get("covers") or []) or "(unspecified)"
        tasks = "\n".join(f"      - {t}" for t in (phase.get("tasks") or [])) or "      (no task listed)"
        blocks.append(f"   Phase {phase.get('id')} — {phase.get('name')} (covers: {covers})\n{tasks}")
    return "\n".join(blocks)


def build_review_scope_block(blackboard: dict) -> tuple:
    """Build (scope_block, scope_files): the scope of prototype files to re-read, limited to
    the run's diff (never the legacy). Without git, we ask to re-read the whole proto."""
    baseline_sha = blackboard.get("_run_baseline_sha", "")
    scope_files = sorted(
        f for f in files_changed_since_phase_start(baseline_sha)
        if not is_orchestration_file(f) and os.path.exists(f)
    )
    if scope_files:
        scope_block = (
            "Re-read ONLY the files below, produced by the factory (everything else — "
            "legacy, dependencies — is OUT OF SCOPE):\n"
            + "\n".join(f"   - {f}" for f in scope_files)
            + "\n   Open each screen and confront it with the rubric and the blackboard."
        )
    else:
        scope_block = ("Re-read all the produced prototype files (index.html, screens, "
                       "CSS, JS). Open each screen and confront it with the rubric and the blackboard.")
    return scope_block, scope_files


def build_review_prompt(blackboard: dict, user_need: str, grille: str,
                        scope_block: str, verdict_path: str) -> str:
    phases_block = serialize_phases_for_review(blackboard)
    full_context = f"""You are a Lead Product Designer + QA, strict and independent. You perform the GLOBAL quality review of this prototype, at the very end of fabrication. Your mission has THREE parts, equally important:
  (A) UX QUALITY: does the prototype comply with the UX rubric below (interface states, accessibility, visual hierarchy, responsive, feedback)?
  (B) CONFORMANCE TO THE BLACKBOARD: was each phase actually done, and is each user story of the specification covered by a screen/journey of the produced prototype?
  (C) DESIGN SYSTEM: is the design system declared in the global rules ACTUALLY applied end to end (tokens and components coming from the design system, assets/css/tokens.css as the single source, no invented token or component, consistency from one screen to the next)? If it reads "{DS_DEFAULT}", verify that no screen claims to follow a named design system.

--- REFERENCE RUBRIC (UX + code conventions) ---
{grille}--- GLOBAL PROJECT RULES ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Visual direction: {global_rule(blackboard, 'styling')}
Constraints: {global_rule(blackboard, 'constraints')}
Accessibility: {global_rule(blackboard, 'accessibility')}

--- SPECIFICATION (source of truth for the need) ---
{user_need}

--- BLACKBOARD TO HONOR (phase by phase, to confront with the real code) ---
{phases_block}

--- SCOPE TO RE-READ ---
{scope_block}

--- MANDATORY METHOD ---
1. Open and actually READ each file in the scope with your tools. Do not rely on any summary.
2. Part B: for EACH phase above, verify that its tasks are concretely done in the files; for EACH user story of the spec, verify that a screen/journey covers it.
3. Part A: confront the prototype with the UX checklist (missing states, keyboard focus, contrasts, semantics, responsive, feedback...).
4. Part C: confront the actually used tokens and components with the declared design system (tokens.css = single source; no invented token or component; consistency across screens).
5. Only report gaps you have ACTUALLY observed in the code.

--- TWO MANDATORY DELIVERABLES ---
1. Write a readable report in '{REVIEW_REPORT_FILE}' at the root, structured as follows:
   - Summary (overall assessment + verdict)
   - Conformance to the blackboard (phase by phase, US by US: compliant or precise gap)
   - UX quality (compliant points / gaps, screen by screen)
   - Application of the design system (compliant or gaps: tokens/components outside the design system)
   - Gaps to fix as a priority (actionable list)
2. As your very LAST action, write your machine verdict in the sentinel file '{verdict_path}' at the root:
   - If the prototype honors the blackboard AND complies with the UX rubric AND applies the design system without a blocking gap: the FIRST line contains EXACTLY the word "OK" (nothing else).
   - Otherwise: the FIRST line contains EXACTLY the word "REJECTED", then the following lines list precisely and briefly the gaps to fix (these will be forwarded for correction).
You NEVER touch the file {BLACKBOARD_FILE}.
"""
    with open(TMP_REVIEW_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the audit file '{TMP_REVIEW_FILE}' at the project root and carry out the complete final review of the prototype."


def build_fix_prompt(blackboard: dict, grille: str, scope_block: str,
                     gaps: str, fix_done: str) -> str:
    full_context = f"""You are a Designer-Dev Agent. The quality reviewer flagged gaps between the prototype and (A) the UX rubric, (B) the blackboard, (C) the declared design system. Fix ONLY these gaps.

--- REFERENCE RUBRIC (UX + code conventions) ---
{grille}--- GLOBAL RULES ---
Stack: {global_rule(blackboard, 'target')}
Design system: {design_system_rule(blackboard)}
Visual direction: {global_rule(blackboard, 'styling')}
Constraints: {global_rule(blackboard, 'constraints')}
Accessibility: {global_rule(blackboard, 'accessibility')}

--- SCOPE (prototype files) ---
{scope_block}

--- GAPS TO FIX (flagged by the reviewer) ---
{gaps}

--- CORRECTION RULES ---
- Fix the listed gaps, and nothing else (no over-engineering, no gratuitous rework).
- Stay in vanilla HTML/CSS/JS, without framework or build; each screen stays directly openable.
- Stay within the design system declared above: no invented token (var(--…)) nor component, tokens.css stays the single source.
- Create NO regression on the already-compliant screens.

--- MANDATORY END INSTRUCTION ---
You NEVER touch the file {BLACKBOARD_FILE}.
When the gaps are fixed, and as your very last action, create the sentinel file '{fix_done}' at the project root (content: the list of modified files, one path per line).
"""
    with open(TMP_FIX_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the instruction file '{TMP_FIX_FILE}' at the project root and fix the gaps flagged by the reviewer."


# ─── FAILURE MESSAGE ──────────────────────────────────────────────────────────


def print_failure_message(phase: dict, blackboard: dict, reason: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    print(f"""
{'='*60}
❌ Phase {phase['id']} "{phase['name']}" did not converge after {MAX_ATTEMPTS} attempts.

   Last blocking point (mechanical guard or verifier):
   "{reason}"

💡 The current model ({model}) stalls on this precise step (sentinel never created =
   often a tool-calling problem; repeated gaps = design system or checklist not
   honored). Most effective: relaunch after bringing in a model one notch above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the {done_count} phase(s) already produced will be resumed
   automatically, you do not start from scratch. See you soon! 🚀
{'='*60}
""")


# ─── MAIN PRODUCTION LOOP (MECHANICAL GUARDS + PER-PHASE VERIFIER) ──

def run_production_phases(blackboard: dict, user_need: str, need_is_spec: bool = False):
    total = len(blackboard["phases"])

    # The skills come from the BLACKBOARD (skills_required, materialized by
    # inject_system_skills before the human gate), with an orchestrator guarantee:
    # the system skills present (ux, proto-coding) are ALWAYS applied —
    # if a 'ux' skill exists, every phase uses it, whatever the file says.
    system_skills = inject_system_skills(blackboard)
    if system_skills:
        print(f"   📦 System skills injected into every phase: {', '.join(system_skills)}")
    else:
        print(f"   ⚠️  No system skill found in {SKILLS_DIR}: "
              f"phases and the review will run without a rubric (degraded quality).")

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} already produced: {phase['name']}")
            continue

        print(f"\n{'='*50}\n🎨 PHASE {phase['id']}/{total}: {phase['name']}\n{'='*50}")

        # Context window: the designer-dev only receives the spec slice covered by ITS phase
        # (the 'covers' field), never the whole spec — except on graceful degradation.
        phase_need = extract_spec_slice(user_need, phase.get("covers")) if need_is_spec else user_need
        if need_is_spec and len(phase_need) < len(user_need):
            print(f"   ✂️  Spec sliced for the phase: {len(phase_need)}/{len(user_need)} characters "
                  f"(covers {', '.join(phase.get('covers') or [])}).")

        # Skills of THIS phase: the blackboard's skills_required, with the system
        # skills guaranteed first (phase_skills filters out the pipeline skills and
        # those absent from disk — a hallucinated skill loads nothing).
        skills_for_phase = phase_skills(phase)
        extras = [s for s in skills_for_phase if s not in system_skills]
        if extras:
            print(f"   📦 Additional skills from the blackboard: {', '.join(extras)}")
        skills_context = load_skills(skills_for_phase)

        attempts = 0
        success = False
        critic_feedback = "First draft — no previous critique."
        # Per-phase diff landmark (anti-ghost guard): empty without git → mtime fallback.
        phase_start_sha = git_head_sha()
        # Time reference of the PHASE, captured ONCE (a per-attempt reference would
        # wrongly reclassify as "ghost" a file written in a previous attempt and
        # re-declared unchanged afterwards).
        phase_started = time.time()

        phase["status"]  = "IN_PROGRESS"
        phase["verdict"] = "PENDING"
        save_blackboard(blackboard)
        cleanup_sentinels(phase["id"])

        # Per-phase quality loop ("loop engineering"): designer-dev → MECHANICAL
        # guards (anti-ghost, hallucinated tokens — free, before any LLM) →
        # independent Verifier Agent (OK/REJECTED verdict + gaps, forwarded to the
        # designer-dev on the next attempt). The global control (full UX rubric,
        # end-to-end conformance) stays with the final review.
        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1
            cleanup_sentinels(phase["id"])
            print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Phase {phase['id']} — launching the Designer-Dev...")

            coder_prompt = build_coder_prompt(phase, blackboard, phase_need, skills_context,
                                              critic_feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(coder_prompt))
            RUNNER.send_task(coder_prompt)

            if not wait_for_file_creation(done_sentinel(phase["id"], attempts)):
                print(f"⏱️  The designer-dev did not signal completion (sentinel '{done_sentinel(phase['id'], attempts)}' missing). New attempt.")
                RUNNER.new_context()
                continue

            touched_files = read_touched_files(phase["id"], attempts)

            # ── ANTI "GHOST DESIGNER" GUARD (mechanical, free) ──: sentinel written
            # without real work. We reject BEFORE paying for a verifier.
            changed_in_phase = files_changed_since_phase_start(phase_start_sha)
            if no_declared_file_touched(touched_files, phase_started, changed_in_phase):
                critic_feedback = (
                    f"Your sentinel declares {len(touched_files)} file(s), but NONE was "
                    "actually created or modified since the start of this phase. CONCRETELY "
                    "carry out the checklist tasks (create/modify the files), then only "
                    "recreate the sentinel with the real list of touched files."
                )
                phase["critic_feedback"] = critic_feedback
                save_blackboard(blackboard)
                print(f"👻 [REJECTED] Attempt {attempts}: sentinel written but no declared "
                      f"file was touched (ghost designer).")
                RUNNER.new_context()
                continue

            # ── "HALLUCINATED TOKENS" GUARD (mechanical, free) ──: every var(--x)
            # consumed by the produced files must be defined in a CSS of the project.
            # An invented design system betrays itself here first — exact feedback, zero LLM.
            missing_tokens = undefined_css_tokens(touched_files)
            if missing_tokens:
                details = "\n".join(f"- var(--{token}) consumed in {path} but defined nowhere"
                                    for token, path in missing_tokens)
                critic_feedback = (
                    f"CSS tokens consumed by your files are DEFINED nowhere in the "
                    f"project:\n{details}\nUse ONLY existing tokens "
                    f"(assets/css/tokens.css is the single source — design system: "
                    f"{design_system_rule(blackboard)}); if a token is genuinely missing "
                    f"for THIS phase's checklist, define it in tokens.css from the "
                    f"declared source, never by inventing it."
                )
                phase["critic_feedback"] = critic_feedback
                save_blackboard(blackboard)
                print(f"🎨 [REJECTED] Attempt {attempts}: {len(missing_tokens)} CSS token(s) "
                      f"consumed but defined nowhere (hallucinated design system?).")
                RUNNER.new_context()
                continue

            # ── PER-PHASE VERIFIER AGENT (fresh context, independent) ──: re-reads the
            # declared files against the checklist and the design system. A MUTE verifier
            # does not block the run: one relaunch (the designer's work has not changed),
            # then acceptance with a warning — the mechanical guards already ran and the
            # final review re-checks everything.
            print(f"  → Designer-dev finished ({len(touched_files)} declared file(s)). "
                  f"Verification by an independent agent...")
            verdict_path = verdict_sentinel(phase["id"], attempts)
            cleanup_pipeline_sentinel(verdict_path)
            verifier_prompt = build_phase_verifier_prompt(phase, blackboard, phase_need,
                                                          touched_files, attempts)
            RUNNER.new_context()
            mm_audit.event("agent_task", prompt_bytes=len(verifier_prompt))
            RUNNER.send_task(verifier_prompt)
            got_verdict = wait_for_file_creation(verdict_path)
            if not got_verdict:
                print("⏱️  The verifier returned no verdict within the allotted time: one relaunch...")
                RUNNER.new_context()
                mm_audit.event("agent_task", prompt_bytes=len(verifier_prompt))
                RUNNER.send_task(verifier_prompt)
                got_verdict = wait_for_file_creation(verdict_path)

            if not got_verdict:
                print("⚠️  Verifier mute after relaunch: phase accepted on the mechanical guards "
                      "alone (the final review will re-check everything).")
                is_ok, gaps = True, ""
            else:
                is_ok, gaps = read_review_verdict(verdict_path)
                cleanup_pipeline_sentinel(verdict_path)

            if is_ok:
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ Phase {phase['id']} produced and VERIFIED "
                      f"({len(touched_files)} declared file(s)).")
                commit_phase(f"phase {phase['id']}: {phase['name']}")
            else:
                critic_feedback = gaps
                phase["critic_feedback"] = gaps
                save_blackboard(blackboard)
                print(f"⚠️  [REJECTED] Attempt {attempts}: the verifier flagged gaps. "
                      f"Forwarded to the designer-dev:\n{gaps}")
                RUNNER.new_context()

        if not success:
            phase["status"]  = "TODO"
            phase["verdict"] = "REJECTED"
            phase["critic_feedback"] = critic_feedback
            save_blackboard(blackboard)
            cleanup_all_sentinels()
            print_failure_message(phase, blackboard, critic_feedback)
            RUNNER.kill()
            sys.exit(1)

        RUNNER.new_context()


# ─── STEP 5: SINGLE GLOBAL REVIEW (UX + BLACKBOARD CONFORMANCE) ──────────────

def review_verdict_sentinel(attempt: int) -> str:
    return f".pipeline_review.attempt{attempt}.verdict"


def fix_done_sentinel(attempt: int) -> str:
    return f".pipeline_fix.attempt{attempt}.done"


def execute_final_review(blackboard: dict, user_need: str) -> bool:
    """Single global Reviewer: verifies (A) the UX rubric and (B) conformance to the blackboard.

    Returns an actionable verdict; on gaps, a bounded correction loop
    (MAX_REVIEW_ATTEMPTS review -> correction -> re-review passes). Returns True if compliant.
    """
    print(f"\n{'='*50}\n🔎 STEP 5: GLOBAL REVIEW (UX + BLACKBOARD CONFORMANCE)\n{'='*50}")

    grille = load_skills(PROTO_SYSTEM_SKILLS)
    scope_block, scope_files = build_review_scope_block(blackboard)

    success = False
    last_gaps = ""
    for attempt in range(1, MAX_REVIEW_ATTEMPTS + 1):
        # Purge the residual report of a previous pass: the wait below must observe only
        # THIS pass's report.
        if os.path.exists(REVIEW_REPORT_FILE):
            os.remove(REVIEW_REPORT_FILE)
        verdict_path = review_verdict_sentinel(attempt)
        cleanup_pipeline_sentinel(verdict_path)

        print(f"\n🧪 [PASS {attempt}/{MAX_REVIEW_ATTEMPTS}] Launching the global Reviewer...")
        RUNNER.new_context()
        review_prompt = build_review_prompt(blackboard, user_need, grille, scope_block, verdict_path)
        mm_audit.event("agent_task", prompt_bytes=len(review_prompt))
        RUNNER.send_task(review_prompt)

        # The VERDICT (sentinel) is the completion signal that drives the loop; the report is
        # the human artifact, written just before. So we wait for the verdict (not the report,
        # whose name could be hallucinated); the report is best-effort.
        if not wait_for_file_creation(verdict_path):
            print("⏱️  The reviewer returned no verdict within the allotted time.")
            last_gaps = "The reviewer returned no verdict (timeout)."
            break

        is_ok, gaps = read_review_verdict(verdict_path)
        cleanup_pipeline_sentinel(verdict_path)
        if is_ok:
            success = True
            print("✅ [REVIEW] Prototype COMPLIANT: UX respected and blackboard honored.")
            break

        last_gaps = gaps
        print(f"⚠️  [REVIEW] Gaps flagged (pass {attempt}):\n{gaps}")

        if attempt == MAX_REVIEW_ATTEMPTS:
            # Last pass: we do not launch a correction that would not be re-verified.
            break

        print("🛠️  Launching a correction pass targeted at the gaps...")
        fix_done = fix_done_sentinel(attempt)
        cleanup_pipeline_sentinel(fix_done)
        RUNNER.new_context()
        fix_prompt = build_fix_prompt(blackboard, grille, scope_block, gaps, fix_done)
        mm_audit.event("agent_task", prompt_bytes=len(fix_prompt))
        RUNNER.send_task(fix_prompt)
        if not wait_for_file_creation(fix_done):
            print("⏱️  The correction pass did not signal its completion: new review anyway.")
        cleanup_pipeline_sentinel(fix_done)
        # The scope may have expanded (new files created by the correction).
        scope_block, scope_files = build_review_scope_block(blackboard)

    # Cleanup of temporary files, whatever the outcome.
    for tmp_f in [TMP_CODER_FILE, TMP_REVIEW_FILE, TMP_FIX_FILE, TMP_ARCHITECT_FILE,
                  TMP_PO_FILE, TMP_PLAN_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_sentinels()

    if success:
        commit_phase("review: compliant prototype (UX + blackboard)")
    else:
        commit_phase("review: persistent gaps after correction")
        print(f"\n⚠️  Gaps remain after {MAX_REVIEW_ATTEMPTS} pass(es). "
              f"The prototype is still delivered and usable.")
        print(f"   Details in '{REVIEW_REPORT_FILE}'. Last gaps flagged:\n{last_gaps}")

    print_open_hint(scope_files)
    return success


def print_open_hint(scope_files: list):
    """Tell the designer how to open the prototype (HTML entry point)."""
    htmls = [f for f in scope_files if f.lower().endswith(".html")]
    entry = next((f for f in htmls if os.path.basename(f).lower() == "index.html"), None)
    if entry is None and htmls:
        entry = sorted(htmls)[0]
    if entry is None and os.path.exists("index.html"):
        entry = "index.html"
    print(f"\n{'─'*50}")
    if entry:
        print(f"👀 Open the prototype: double-click on '{entry}' (or drag it into your browser).")
        print(f"   If needed, serve it locally: python3 -m http.server 8000  then http://localhost:8000")
    else:
        print("👀 Open the prototype's HTML entry file in your browser.")
    print(f"{'─'*50}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)

def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "proto", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan approval sentinel (spec.md deleted since) must never validate a FUTURE spec:
    # we purge it before anything else.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # 🎨 DESIGN SYSTEM GATE (before any agent, even before the harness boot): the
    # declaration can ONLY come from the human. Three states: declared in need.md
    # (a prior agreement made obsolete is purged); already confirmed "no design
    # system" during a previous run (silent resume); otherwise y/n gate.
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        ds_declared = read_design_system_from_need(f.read())
    if ds_declared:
        if os.path.exists(DS_ACK_SENTINEL):
            os.remove(DS_ACK_SENTINEL)
        print(f"🎨 Design system declared in '{NEED_FILE}': it will be transcribed into the "
              f"spec then carried to every agent.")
    elif os.path.exists(DS_ACK_SENTINEL):
        print(f"🔄 Default tokens already confirmed during a previous run "
              f"('{DS_ACK_SENTINEL}'). Design system gate skipped.")
    else:
        confirm_default_design_system()

    # 🚀 STEP ZERO: Immediate boot of the harness Data Center in Tmux
    RUNNER.start()

    # Step 1: PO/UX refinement via the TUI (need.md → spec.md), validated by the HUMAN.
    if not os.path.exists(SPEC_FILE):
        generate_spec_from_need_tui(ds_declared)
        confirm_spec_with_human()
        RUNNER.new_context()
    elif not os.path.exists(SPEC_APPROVED_SENTINEL):
        print(f"🔄 Existing '{SPEC_FILE}' found but NEVER approved (interrupted run?).")
        confirm_spec_with_human()
    else:
        print(f"🔄 Existing '{SPEC_FILE}' found (approved by the human). PO step skipped.")

    # Step 2: Implementation plan via the TUI (spec.md → plan.md)
    if not os.path.exists(PLAN_FILE):
        generate_plan_from_need_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 Existing '{PLAN_FILE}' found. Step skipped.")

    # Step 3: Blackboard configuration via the TUI
    if not os.path.exists(BLACKBOARD_FILE):
        blackboard = transform_plan_to_blackboard_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 Existing '{BLACKBOARD_FILE}' found. Loading...")
        blackboard = load_blackboard()

    # The "need" context injected into the agents is the refined and validated SPEC (observable
    # criteria); need.md only serves as a fallback (old runs). need_is_spec conditions the
    # per-US slicing (extract_spec_slice) in production.
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Design system as the pipeline carries it: re-read from the SPEC validated by the
    # human (or need.md as a fallback), for the blackboard transport guard.
    ds_transported = read_design_system_from_need(user_need)

    # Guard on phases[].id: a duplicated id silently corrupts the sentinel channel, we stop
    # BEFORE paying for a run.
    fatal_ids, soft_ids = validate_phase_ids(blackboard)
    for warning in soft_ids:
        print(f"⚠️  {warning}")
    if fatal_ids:
        for problem in fatal_ids:
            print(f"❌ {problem}")
        fail_pipeline(f"   → Fix '{BLACKBOARD_FILE}' then relaunch.")

    # The recap → y/n sequence LOOPS: the human can edit the blackboard in another terminal
    # while the prompt waits; any change triggers a reload and a new confirmation on the
    # re-displayed recap.
    while True:
        # Skills: materialized BY THE ORCHESTRATOR into blackboard.yaml (skills_required
        # field of every phase) BEFORE the human gate — the human reads in the file what
        # will actually be applied, and an edit that would remove a system skill is
        # re-injected on reload (the 'ux' skill, if it exists, is ALWAYS applied).
        system_skills = inject_system_skills(blackboard)
        # Same guarantee for the design system: mechanically repaired if it got lost
        # between the plan and the blackboard (copy of the human declaration, never more).
        ensure_design_system(blackboard, ds_transported)
        if need_is_spec:
            coverage_warnings = check_spec_coverage(blackboard, user_need)
            if coverage_warnings:
                print("\n⚠️  Spec → phases traceability:")
                for warning in coverage_warnings:
                    print(f"   - {warning}")

        print(f"\n{'='*50}")
        print(f"📋 BLACKBOARD READY — Recap:")
        print(f"   Project: {blackboard['project']}")
        print(f"   Stack (global_rules.target): {(blackboard.get('global_rules') or {}).get('target') or '(unspecified)'}")
        print(f"   Design system: {design_system_rule(blackboard)}")
        print(f"   Skills applied to every phase (skills_required): "
              f"{', '.join(system_skills) or '(none found)'}")
        if UX_SKILL not in system_skills:
            print(f"   ⚠️  Skill '{UX_SKILL}' not found ({SKILLS_DIR}/{UX_SKILL}/SKILL.md): "
                  f"the UX rubric will NOT be applied — degraded quality.")
        print(f"   Phases: {len(blackboard['phases'])}")
        for p in blackboard['phases']:
            covers = ', '.join(p.get('covers') or [])
            print(f"   Phase {p['id']}: {p['name']} "
                  f"({len(p.get('tasks', []))} task(s); covers: {covers or '?'})")
        print(f"{'='*50}")
        print(f"   Quality control: mechanical guards (ghost, tokens) + per-phase verifier, then a UX + conformance + design system review at the end.")
        print(f"   You can edit '{BLACKBOARD_FILE}' directly in another terminal before validating.")

        with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
            raw_at_prompt = f.read()
        confirm = input("\n▶️  Validate the blackboard and start production? (y/n): ")
        mm_audit.event("gate", id="blackboard", gate_kind="yn", answer=confirm.strip().lower())
        if confirm.strip().lower() != 'y':
            print("⏹️  Cancelled by the user.")
            RUNNER.kill()
            sys.exit(0)

        try:
            with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
                edited_during_prompt = f.read() != raw_at_prompt
            if not edited_during_prompt:
                break
            print(f"\n🔄 '{BLACKBOARD_FILE}' was modified while waiting for the prompt: reloading...")
            blackboard = load_blackboard()
        except Exception as err:
            print(f"❌ '{BLACKBOARD_FILE}' was modified during the prompt but is now unreadable "
                  f"(invalid or corrupted YAML): {err}")
            print(f"   → Fix '{BLACKBOARD_FILE}' then relaunch.")
            RUNNER.kill()
            sys.exit(1)

    check_proto_skills()

    # Git landmarks (best-effort): baseline now, then one commit per signaled phase.
    ensure_phase_repo()

    # Run reference: everything that differs from this sha is the factory's work, never the
    # legacy. Persisted because a RESUME would recapture an already-advanced HEAD, and the
    # review would then miss the files of earlier phases.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    print(f"\n🚀 Starting prototype production: {blackboard['project']}")

    # Step 4: Production loop (without per-phase verifier)
    run_production_phases(blackboard, user_need, need_is_spec)

    # Step 5: Single global review (UX + blackboard conformance)
    review_ok = execute_final_review(blackboard, user_need)

    # Clean shutdown
    RUNNER.kill()
    # Run finished: we purge the approval markers (spec, design system), kept outside
    # cleanup_all_sentinels because they must survive an INTERRUPTION.
    if os.path.exists(SPEC_APPROVED_SENTINEL):
        os.remove(SPEC_APPROVED_SENTINEL)
    if os.path.exists(DS_ACK_SENTINEL):
        os.remove(DS_ACK_SENTINEL)

    if review_ok:
        print("\n🏁 [CONGRATULATIONS] Prototype produced AND validated (UX + blackboard) in a single run!")
    else:
        print(f"\n🏁 Prototype produced. Final review: remaining gaps, see '{REVIEW_REPORT_FILE}'.")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    POLL_INTERVAL=POLL_INTERVAL,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    US_HEADING_RE=US_HEADING_RE,
    run_git=run_git,
)


if __name__ == "__main__":
    main()
