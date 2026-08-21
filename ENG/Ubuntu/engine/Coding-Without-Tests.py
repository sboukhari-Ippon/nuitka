#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - Code factory with an agent harness + tmux (Full TUI Data Center Version)
─────────────────────────────────────────────────────────────────────────────
"CODE ONLY" VARIANT: plan without tests (plan-no-test skill), each phase verified by
an LLM Verifier Agent (no test execution).

PO → Architect pipeline:
  - Step 1: a PO Agent refines 'need.md' into a business specification 'spec.md' (user
    stories, acceptance criteria, out-of-scope, assumptions), VALIDATED by the human.
  - Step 2: an Architect Agent (code-only mode) converts 'spec.md' into an implementation plan.
  - Step 3: the blackboard conversion is a MECHANICAL copy of the plan's decisions.

Data Center & TUI Strategy:
  - The tmux session is initialized DIRECTLY at startup.
  - We directly launch the chosen harness TUI (Cloud / Data Center model).
  - Steps 1 (PO Spec), 2 (Plan) and 3 (Blackboard) are executed directly in the TUI.
  - Production: each phase goes through a Coder Agent then an independent
    Verifier Agent that actually RE-READS the produced code. Agents communicate
    via sentinel files ('.phase_<id>.done' / '.phase_<id>.verdict'); the sole
    owner of the blackboard is the Python orchestrator (no concurrent writes).
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
    build_skills_dictionary, collect_spec_us_ids, done_sentinel, git_head_sha,
    inject_skills_dictionary, is_orchestration_file, load_blackboard, load_skills,
    signal_handler, validate_all_skills, wait_for_file_creation,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="factory", messages={
    "follow":   "   👀 Follow live in another terminal: tmux attach -t {session}",
    "new_warn": "   ⚠️  The TUI may not have reset (literal '/new' still on screen): "
                "if the run drifts, check with tmux attach.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-no-test/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Pipeline system skills: never routed to production phases.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-to-blackboard", "refacto"}

# Temporary context routing files
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_VERIFIER_FILE     = RUNNER.tmp_file("verifier")
TMP_REFACTO_FILE      = RUNNER.tmp_file("refacto")
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: the
# only valid choice on all 3 OSes (Windows has no /tmp), and unifying it removes the last
# CODE diff between variants of the same language (one script = one language).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# End-of-deliverable sentinels for the pipeline steps (1 to 3): same contract as
# production (the agent creates the .done file AFTER saving the deliverable). Replaces
# the "size stable for 1.5s" detection, which could read a half-written file if the
# agent paused between two writes.
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"
PLAN_DONE_SENTINEL       = ".pipeline_plan.done"
BLACKBOARD_DONE_SENTINEL = ".pipeline_blackboard.done"
REFACTO_DONE_SENTINEL    = ".pipeline_refacto.done"

# HUMAN approval of the spec, materialized: the mere EXISTENCE of spec.md proves nothing
# (a timeout can leave a never-validated spec behind, see fail_pipeline). Deliberately
# outside the '.pipeline_*.done' pattern purged by cleanup_all_sentinels: the approval
# must survive a resume.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session (prompts of project B would
# land in the agent of project A). Resuming the SAME project reuses its session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per phase (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net: pipeline deliverable accepted if it
                                       # stayed stable for N consecutive checks (N × POLL_INTERVAL seconds).
                                       # 30s: a slow local model pausing between two writes must not get
                                       # its half-written deliverable accepted (see structural_check too)


def fail_pipeline(message: str):
    """Single exit point for pipeline step failures (steps 1 to 3).

    Always kills the tmux session BEFORE exiting: an exit that leaves the agent alive
    lets it finish writing its deliverable AFTER the orchestrator gave up — on relaunch,
    that half-validated file would be mistaken for a valid resume state (this is how a
    never-approved spec used to become the source of truth). RUNNER.kill() is a no-op when
    no session exists, so this helper is safe everywhere.
    """
    mm_audit.end("failed")
    print(message)
    RUNNER.kill()
    sys.exit(1)


# ─── PHASE SENTINELS (CODER / VERIFIER → ORCHESTRATOR CHANNEL) ────────────────

def verdict_sentinel(phase_id: int, attempt: int) -> str:
    """File written by the Verifier (signal 'OK' or 'REJECTED' + reasons)."""
    return f".phase_{phase_id}.attempt{attempt}.verdict"


def cleanup_sentinels(phase_id: int):
    """Remove all sentinels (every attempt) of a phase."""
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
                or (name.startswith(".pipeline_") and name.endswith(".done")):
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
    """Read the list of files declared by the Coder in its .done sentinel.

    Small models often format the list as bullets ('- src/foo.ts', '* a.py', '1. b.go'):
    leading list markers are stripped so the verifier receives real paths instead of
    decorated lines.
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


def read_verdict(phase_id: int, attempt: int) -> tuple:
    """Read the Verifier's verdict. Returns (is_ok: bool, feedback: str).

    Tolerant parsing: leading blank lines and markdown fences are skipped, then
    the first word of the first meaningful line is read. 'OK', 'OK.',
    'OK, compliant'... validate; everything else (including 'REJECTED') rejects.
    """
    path = verdict_sentinel(phase_id, attempt)
    if not os.path.exists(path):
        return False, "The verifier produced no verdict."
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return False, "Empty verdict produced by the verifier."

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
    return False, body or "The verifier rejected the phase without giving a reason."


# ─── GIT LANDMARKS (BEST-EFFORT) ──────────────────────────────────────────────
# BEST-EFFORT: without git (binary absent, init failure), the factory runs identically
# but without landmarks — graceful degradation, never block the run over tooling. In
# this code-only variant (LLM verifier, no rollback trigger), git buys an audit trail:
# baseline at production start, one commit per validated phase, one after the refacto —
# a manual rollback point per step for the human.

_GIT = {"enabled": False}

# Identity passed per command: the factory must not depend on the machine's git config.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

GITIGNORE_BODY = f"""# MAIster-Mind orchestration artifacts (ephemeral)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.spec_approved
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

    --allow-empty: a validated phase that changed nothing still gets its landmark
    commit, so per-phase shas stay reliable for manual diffs and rollbacks.
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
    """Set of files modified/created since a reference sha (the factory perimeter, RUN
    scale). Empty without git or without a sha → the caller falls back.

    No intermediate commit is made during a phase: the work lives in the working tree. So we
    compare the tree to the reference sha ('git diff <sha>', tracked files) and add the
    untracked files ('ls-files --others').
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


# Orchestrator artifacts (never produced code): excluded from the refactoring perimeter.
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_VERIFIER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


def ensure_phase_repo():
    """Per-phase git landmarks, set up before production (best-effort).

    If the project is already a git repo (human-managed), it is reused AS IS. Otherwise
    'git init' + a minimal .gitignore (ephemeral orchestration files only) + a baseline
    commit. Without git: warn once and run without landmarks.
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
    commit_phase("baseline: factory start")


# ─── FILE MONITOR SYNCHRONIZATION ────────────────────────────────────────────

def spec_structural_check(path: str) -> bool:
    """Minimal structural floor for a spec accepted WITHOUT sentinel: its mandatory
    "Out of scope" section must be present (a half-written spec stops before it)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "out of scope" in f.read().lower()
    except OSError:
        return False


def plan_structural_check(path: str) -> bool:
    """Minimal structural floor for a plan accepted WITHOUT sentinel: the mandatory
    "Stack & Verification" header block must be present."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "stack & verification" in f.read().lower()
    except OSError:
        return False


def blackboard_structural_check(path: str) -> bool:
    """Minimal structural floor for a blackboard accepted WITHOUT sentinel: the YAML
    must at least parse (a half-written mapping almost never does)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) is not None
    except (OSError, yaml.YAMLError):
        return False


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Wait for a pipeline deliverable (spec/plan/blackboard) signaled by a SENTINEL.

    Same contract as production: the agent creates a .done file AFTER saving the
    deliverable — an unambiguous signal, robust to writing pauses (the "size stable for
    1.5s" heuristic alone could accept a half-written file if the agent paused between
    two writes). SAFETY NET for an agent that forgets the sentinel: if the deliverable
    exists, is non-empty and has not changed for STABLE_POLLS_FALLBACK consecutive
    checks, it is accepted with a warning (graceful degradation — never block for
    10 minutes over a mere missing signal). The optional 'structural_check' hardens
    this fallback ONLY: a stable but structurally incomplete deliverable keeps waiting
    (the agent may pause longer than the stability window) until the global timeout.
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


# ─── BLACKBOARD READ / WRITE ─────────────────────────────────────────────────

# Last journaled phase statuses (TRANSITION detection by save_blackboard).
_PHASE_STATUS_SEEN = {}


def save_blackboard(data: dict):
    with open(BLACKBOARD_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
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


def validate_phase_ids(blackboard: dict) -> tuple:
    """Uniqueness/sequence guards on phases[].id (produced by a fallible small LLM).
    Returns (fatal, soft).

    A duplicated id makes two phases SHARE their '.phase_N.attemptM.done' /
    '.verdict' sentinels (false completion signals): fatal. A non-contiguous
    sequence is merely reported.
    """
    fatal, soft = [], []
    phases = blackboard.get("phases") if isinstance(blackboard, dict) else None
    if not isinstance(phases, list) or not phases:
        return ["Missing or empty 'phases' block: nothing to produce."], []
    ids = [str(phase.get("id")) for phase in phases if isinstance(phase, dict) and "id" in phase]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        fatal.append(
            f"Duplicated phases[].id ({', '.join(duplicated)}): the '.phase_N.attemptM.*' "
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



# ─── PER-PHASE SPEC SLICING (CONTEXT WINDOW) ──────────────────────────────────

# Heading of a user story in the PO spec (e.g. "### US-1: Balance computation").
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Slice of the spec limited to the phase's covered US (+ everything outside US sections).

    The prompts used to embed the WHOLE spec at every phase: on a large spec, every phase
    paid the full context cost. We only keep here the '### US-n' sections listed in
    'covers' (field copied from the plan by the blackboard compiler), plus everything that
    is not a US section (business goal, constraints, out-of-scope, assumptions). Small-model
    prudence: if 'covers' is empty, if the spec does not follow the US format, or if no
    covered US is found in it, return the WHOLE spec (graceful degradation — never starve
    the agent of context out of zeal).
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
    """NON-blocking WARNINGS on spec → phases traceability via 'covers'.

    Two directions: a US referenced by a phase but absent from the spec (probable
    hallucination of the blackboard compiler), and a US of the spec covered by no phase
    (requirement potentially FORGOTTEN by the Architect). Warn-only: 'covers' is optional;
    the human eye at the y/n decides.
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
                        f"{', '.join(unknown)} (probable compiler hallucination).")
    uncovered = sorted(spec_us - referenced)
    if uncovered:
        warnings.append(f"US of the spec covered by NO phase: {', '.join(uncovered)} "
                        f"(requirement forgotten by the Architect? Check the plan).")
    return warnings


# ─── DYNAMIC SKILLS DICTIONARY ────────────────────────────────────────────────

def parse_skill_frontmatter(skill_path: str) -> tuple:
    """Extract (name, description) from a SKILL.md YAML frontmatter."""
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


# ─── INTERACTIVE STEPS 1 TO 3 IN TUI (CLOUD) ────────────────────────────────

def generate_spec_from_need_tui():
    print("\n📖 [STEP 1: PO AGENT] Refining the need into a business specification in Cloud TUI...")

    if not os.path.exists(PO_SKILL_FILE):
        fail_pipeline(f"❌ Missing PO skill: '{PO_SKILL_FILE}'")
    with open(PO_SKILL_FILE, "r", encoding="utf-8") as f:
        po_spec = f.read()
    with open(TMP_PO_FILE, "w", encoding="utf-8") as f:
        f.write(po_spec)

    po_prompt = f"""Read the file '{NEED_FILE}' at the root of our project, as well as the Product Owner guidelines in the file '{TMP_PO_FILE}'.
You are a Senior Product Owner. Strictly APPLYING the guidelines in '{TMP_PO_FILE}', refine the raw need into a business specification and save it DIRECTLY in a new file named '{SPEC_FILE}' at the project root.

Directives for '{SPEC_FILE}':
- Zero invention: every requirement must derive from the need expressed in '{NEED_FILE}'.
- Every user story carries TESTABLE acceptance criteria (Given / When / Then).
- Every ambiguity of the need becomes an explicit assumption in "Assumptions & Questions".
- The "Out of scope" section is mandatory (the lock against over-engineering).
Do it directly via your file editing tools, without unnecessary chatter in the console.
As your very LAST action, after saving '{SPEC_FILE}', create the sentinel file '{SPEC_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
"""
    cleanup_pipeline_sentinel(SPEC_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(po_prompt))
    RUNNER.send_task(po_prompt)

    if wait_for_pipeline_file(SPEC_FILE, SPEC_DONE_SENTINEL, structural_check=spec_structural_check):
        print(f"✅ [STEP 1] Specification '{SPEC_FILE}' created successfully!")
    else:
        fail_pipeline(f"❌ [STEP 1] Timeout or failed to create '{SPEC_FILE}'.")


def confirm_spec_with_human():
    """Human validation of the spec (UPSTREAM human-in-the-loop).

    This is where fixing costs the least: a misunderstood requirement rejected at this
    stage avoids paying for (and redoing) a plan, a blackboard and whole production phases.
    The human can edit the spec in another terminal before validating.
    """
    print(f"\n{'='*50}")
    print(f"📋 SPECIFICATION READY: review '{SPEC_FILE}' (assumptions and out-of-scope first).")
    print(f"   You can edit it directly in another terminal before validating.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Validate the specification and start the architecture? (y/n): ")
    mm_audit.event("gate", id="spec", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Cancelled by the user. Refine '{NEED_FILE}', delete '{SPEC_FILE}', then relaunch.")
        RUNNER.kill()
        sys.exit(0)
    # The approval is MATERIALIZED (not inferred from the file's existence): on resume,
    # a spec without this sentinel goes through the y/n again instead of being trusted.
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # frozen copy of the spec AS APPROVED


def generate_plan_from_need_tui():
    print("\n📖 [STEP 2: ARCHITECT AGENT] Generating the implementation plan in Cloud TUI...")

    if not os.path.exists(PLAN_SKILL_FILE):
        fail_pipeline(f"❌ Missing planning skill: '{PLAN_SKILL_FILE}'")
    with open(PLAN_SKILL_FILE, "r", encoding="utf-8") as f:
        plan_spec = f.read()
    # The REAL skills catalog goes to the Architect: the routing decision (each phase's
    # Skill field) belongs to the agent who has the full plan context, and is then
    # mechanically copied downstream by the blackboard compiler.
    plan_spec = inject_skills_dictionary(plan_spec)
    with open(TMP_PLAN_FILE, "w", encoding="utf-8") as f:
        f.write(plan_spec)

    print("   📚 Skills detected and offered to the architect:")
    for line in (build_skills_dictionary().splitlines() or ["(no phase skill detected)"]):
        print(f"      {line}")

    planning_prompt = f"""Read the file '{SPEC_FILE}' at the root of our project (validated business specification), as well as the architecture guidelines in the file '{TMP_PLAN_FILE}'.
You are a senior Software Architect. Strictly APPLYING the guidelines in '{TMP_PLAN_FILE}', generate a sequential implementation plan in Markdown format and save it DIRECTLY in a new file named '{PLAN_FILE}' at the project root.

Directives for '{PLAN_FILE}':
- The plan MUST start with the "Stack & Verification" block (with the COMPILATION command, the verdict of all phases in code-only mode) and EVERY phase MUST declare its Nature and its "Covers" field (US-x): the next pipeline steps copy these decisions without inferring them.
- Break the specification down into BOUNDED micro-phases (1 to 5 tasks, at most 5 files created/modified, at most 3 files to read per phase); the indicative range of 3 to 12 phases always yields to these size bounds. Do not add any phase for a requirement absent from '{SPEC_FILE}'.
- YAGNI principle: plan ONLY what the specification requests; its "Out of scope" section is a prohibition.
- Precise unit checklists, clear stack.
Do it directly via your file editing tools, without unnecessary chatter in the console.
As your very LAST action, after saving '{PLAN_FILE}', create the sentinel file '{PLAN_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
"""
    cleanup_pipeline_sentinel(PLAN_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(planning_prompt))
    RUNNER.send_task(planning_prompt)

    if wait_for_pipeline_file(PLAN_FILE, PLAN_DONE_SENTINEL, structural_check=plan_structural_check):
        print(f"✅ [STEP 2] Plan '{PLAN_FILE}' created successfully!")
    else:
        fail_pipeline(f"❌ [STEP 2] Timeout or failed to create '{PLAN_FILE}'.")


def transform_plan_to_blackboard_tui():
    if not os.path.exists(BLACKBOARD_SKILL_FILE):
        fail_pipeline(f"❌ Missing blackboard compiler skill: '{BLACKBOARD_SKILL_FILE}'")

    print("\n📖 [STEP 3: BLACKBOARD COMPILER] Generating Blackboard YAML in Cloud TUI...")

    # The compiler COPIES the plan's decisions (including each phase's Skill): the
    # skills dictionary now goes to the Architect (step 2), not here. The Python net
    # validate_all_skills still catches hallucinated keywords downstream.
    with open(BLACKBOARD_SKILL_FILE, "r", encoding="utf-8") as f:
        compiler_spec = f.read()
    with open(TMP_ARCHITECT_FILE, "w", encoding="utf-8") as f:
        f.write(compiler_spec)

    prompt = f"""You are a Blackboard Compiler: you COPY the plan's decisions, you make none. Read the plan that was just generated in '{PLAN_FILE}' as well as the structure instructions from '{TMP_ARCHITECT_FILE}'.
Generate the '{BLACKBOARD_FILE}' at the root of our project strictly following the requested YAML format.

Write the clean YAML directly in the file '{BLACKBOARD_FILE}', without wrapping it in markdown code blocks like ```yaml.
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
            fail_pipeline(f"❌ [STEP 3] YAML parsing failed: {err}")
    else:
        fail_pipeline(f"❌ [STEP 3] Timeout or failed to create '{BLACKBOARD_FILE}'.")


# ─── STEP 4 & 5: PROMPTS DELEGATED TO FILES ──────────────────────────────────

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    # Architect context and reading list, carried since the plan: GUIDANCE that spares
    # the coder a free re-exploration of the project. Nothing sandboxes its reads, so
    # the context-window gain is probabilistic, not guaranteed.
    context_block = ""
    if str(phase.get("context") or "").strip():
        context_block = f"""--- YOUR PLACE IN THE PLAN (Architect's context) ---
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
Architecture: {blackboard['global_rules']['target']}
Design & CSS: {blackboard['global_rules']['styling']}
Prohibitions: {blackboard['global_rules']['constraints']}
Accessibility: {blackboard['global_rules']['accessibility']}

{skills_context}
--- BEHAVIORAL CONTRACT ---
You are an ultra-specialized Coder Agent for Phase {phase['id']} ONLY.
You implement ONLY the tasks of this phase. Stop as soon as it's done.
YAGNI principle: you implement nothing that is not explicitly requested.

--- ABSOLUTE RULE ON TESTS ---
FORMAL prohibition to read, modify, correct or add test files.
Existing tests are out of bounds. Ignore them completely.
Focus only on production source code.

{context_block}{files_block}--- INITIAL NEED ---
{user_need}

--- PHASE {phase['id']} GOAL: {phase['name']} ---
Checklist:
{chr(10).join([f'- [ ] {t}' for t in phase['tasks']])}

--- VERIFIER FEEDBACK TO FIX (if any) ---
{critic_feedback}

--- MANDATORY END OF PHASE INSTRUCTION ---
You NEVER touch the file {BLACKBOARD_FILE}: the orchestrator manages it.
When all the phase tasks are ACTUALLY implemented in the code, and as your very
last action, create the sentinel file '{done_sentinel(phase['id'], attempt)}' at the project root.
It must contain the list of files you created or modified (one path per line), and nothing else.
This file is the signal that triggers verification: only create it once you are TRULY done.
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the task file '{TMP_CODER_FILE}' at the project root. Strictly follow its instructions to complete Phase {phase['id']}."


def build_verifier_prompt(phase: dict, blackboard: dict, user_need: str,
                          touched_files: list, attempt: int) -> str:
    files_block = "\n".join(f"- {p}" for p in touched_files) if touched_files \
        else "(no file declared — explore the project with your tools to find the coder's work)"

    full_context = f"""You are a strict and independent Senior QA Verifier Agent.
Your mission: verify that Phase '{phase['name']}' has been ACTUALLY implemented in the code,
in accordance with the checklist AND the project's global rules.

--- GLOBAL RULES TO ENFORCE ---
Architecture: {blackboard['global_rules']['target']}
Design & CSS: {blackboard['global_rules']['styling']}
Prohibitions: {blackboard['global_rules']['constraints']}
Accessibility: {blackboard['global_rules']['accessibility']}

--- INITIAL NEED ---
{user_need}

--- PHASE CHECKLIST TO VERIFY ---
{chr(10).join([f'- {t}' for t in phase['tasks']])}

--- FILES MODIFIED BY THE CODER ---
{files_block}

--- MANDATORY VERIFICATION METHOD ---
1. Open and ACTUALLY READ the content of each file above with your reading tools. Do not rely on any summary.
2. Confront the real code against EACH checklist task AND EACH global rule.
3. Only validate what you have actually observed in the code.

--- VERDICT ---
Write your conclusion in the sentinel file '{verdict_sentinel(phase['id'], attempt)}' at the project root:
  - If everything is implemented without flaw and compliant with the rules: the FIRST line contains EXACTLY the word "OK" (nothing else).
  - Otherwise: the FIRST line contains EXACTLY the word "REJECTED", then the following lines
    precisely list the gaps, errors or violations to fix.
You NEVER touch the file {BLACKBOARD_FILE}: the orchestrator updates it from your verdict.
"""
    with open(TMP_VERIFIER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the audit file '{TMP_VERIFIER_FILE}' at the project root. Follow its instructions to verify Phase {phase['id']}."


# ─── FAILURE MESSAGE ──────────────────────────────────────────────────────────


def print_failure_message(phase: dict, blackboard: dict, critic_feedback: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    print(f"""
{'='*60}
❌ Phase {phase['id']} "{phase['name']}" did not converge after {MAX_ATTEMPTS} attempts.

   Last blocking point raised by the verifier:
   "{critic_feedback}"

💡 The current model ({model}) is stuck on this specific step.
   Most effective: relaunch after bringing in a model one notch above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the {done_count} already-validated phase(s) will be resumed
   automatically, you don't start from scratch. See you soon! 🚀
{'='*60}
""")


# ─── MAIN PRODUCTION LOOP ─────────────────────────────────────────────────────

def run_production_phases(blackboard: dict, user_need: str, need_is_spec: bool = False):
    total = len(blackboard["phases"])

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} already validated: {phase['name']}")
            continue

        print(f"\n{'='*50}\n🛠️  PHASE {phase['id']}/{total} : {phase['name']}\n{'='*50}")

        skills_context = load_skills(phase.get("skills_required", []))
        loaded = [s for s in phase.get("skills_required", []) if os.path.exists(os.path.join(SKILLS_DIR, s, "SKILL.md"))]
        if loaded:
            print(f"   📦 Skills loaded: {', '.join(loaded)}")

        # Context window: coder AND verifier only receive the spec slice covered by THIS
        # phase ('covers' field), never the whole spec — except graceful degradation
        # (missing covers or spec without US format).
        phase_need = extract_spec_slice(user_need, phase.get("covers")) if need_is_spec else user_need
        if need_is_spec and len(phase_need) < len(user_need):
            print(f"   ✂️  Spec sliced for the phase: {len(phase_need)}/{len(user_need)} characters "
                  f"(covers {', '.join(phase.get('covers') or [])}).")

        attempts = 0
        success  = False
        critic_feedback = "First draft — no previous criticism."

        phase["status"]  = "IN_PROGRESS"
        phase["verdict"] = "PENDING"
        save_blackboard(blackboard)
        cleanup_sentinels(phase["id"])

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1
            cleanup_sentinels(phase["id"])
            print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Phase {phase['id']} — launching Coder Agent...")

            coder_prompt = build_coder_prompt(phase, blackboard, phase_need, skills_context, critic_feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(coder_prompt))
            RUNNER.send_task(coder_prompt)

            if not wait_for_file_creation(done_sentinel(phase["id"], attempts)):
                print(f"⏱️  The coder did not signal completion (sentinel '{done_sentinel(phase['id'], attempts)}' missing). Retrying.")
                RUNNER.new_context()
                continue

            touched_files = read_touched_files(phase["id"], attempts)
            print(f"  → Coder finished ({len(touched_files)} declared file(s)). Routing to the independent QA Verifier...")

            RUNNER.new_context()
            verifier_prompt = build_verifier_prompt(phase, blackboard, phase_need, touched_files, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(verifier_prompt))
            RUNNER.send_task(verifier_prompt)

            if not wait_for_file_creation(verdict_sentinel(phase["id"], attempts)):
                print("⏱️  The verifier returned no verdict. Retrying.")
                RUNNER.new_context()
                continue

            is_ok, feedback = read_verdict(phase["id"], attempts)

            if is_ok:
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCESS] Phase {phase['id']} validated by the QA Verifier.")
                commit_phase(f"phase {phase['id']}: {phase['name']}")
            else:
                critic_feedback = feedback
                phase["critic_feedback"] = feedback
                save_blackboard(blackboard)
                print(f"⚠️  [REJECT] Attempt {attempts}. Reason relayed to the coder:\n{feedback}")
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


def execute_final_refactoring(blackboard: dict, user_need: str):
    print(f"\n{'='*50}\n🛡️  STEP 5: REFACTORING & FINAL POLISH AGENT\n{'='*50}")

    refacto_skills = load_skills(["refacto"])

    # Factory perimeter: refactor ONLY what this run produced or modified (diff since the run
    # baseline), never pre-existing legacy. Without git the perimeter is empty → we fall back
    # to the old wording (degraded mode already assumed across the whole pipeline).
    baseline_sha = blackboard.get("_run_baseline_sha", "")
    scope = sorted(
        f for f in files_changed_since_phase_start(baseline_sha)
        if not is_orchestration_file(f) and os.path.exists(f)
    )
    if scope:
        scope_block = (
            "Analyze ONLY the files listed below, produced or modified by the factory "
            "(everything else — legacy, dependencies — is OUT OF SCOPE: do not read or "
            "modify it):\n"
            + "\n".join(f"   - {f}" for f in scope)
            + "\n   Work file by file; do not load the whole codebase at once."
        )
    else:
        scope_block = "Analyze all created or modified files."

    full_context = f"""You are a Craftsman Expert, Senior Refactoring Engineer and Code Auditor.
Perform a final audit and polish on all generated codebase.

--- SPECIALIZED SKILLS ---
{refacto_skills}
--- GLOBAL CONSTRAINTS ---
Stack: {blackboard['global_rules']['target']}
Styling: {blackboard['global_rules']['styling']}
Prohibitions: {blackboard['global_rules']['constraints']}
Accessibility: {blackboard['global_rules']['accessibility']}

--- INITIAL NEED ---
{user_need}

--- OBJECTIVES ---
1. {scope_block}
2. Identify anomalies (orphan imports, obsolete types, etc.).
3. Directly fix all inconsistencies by modifying the files.
4. You NEVER delete NOR weaken an existing test to make anything pass: if a test
   turns red, the production code is what must be fixed.
5. Write a technical report summarizing applied optimizations in {REFACTO_REPORT_FILE}.

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{REFACTO_REPORT_FILE}', create the sentinel file
'{REFACTO_DONE_SENTINEL}' at the root (content: the single word done): it is the completion
signal for the orchestrator.
"""
    with open(TMP_REFACTO_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    # A residual report from a previous interrupted run would be detected IMMEDIATELY
    # while the agent is still modifying code: purge it so the wait below observes THIS
    # run's report only.
    if os.path.exists(REFACTO_REPORT_FILE):
        os.remove(REFACTO_REPORT_FILE)
        print(f"   🧹 Residual '{REFACTO_REPORT_FILE}' from a previous run removed.")
    cleanup_pipeline_sentinel(REFACTO_DONE_SENTINEL)

    print("🤖 Sending refactoring order via file...")
    RUNNER.new_context()
    mm_audit.event("agent_task", prompt_bytes=len(f"Read the file '{TMP_REFACTO_FILE}' at the project root and execute the complete final audit."))
    RUNNER.send_task(f"Read the file '{TMP_REFACTO_FILE}' at the project root and execute the complete final audit.")

    # Same sentinel contract as steps 1-3 (with the stability safety net inherited from
    # wait_for_pipeline_file): the mere EXISTENCE of the report is not a completion
    # signal — the agent may create it then keep modifying code.
    if wait_for_pipeline_file(REFACTO_REPORT_FILE, REFACTO_DONE_SENTINEL):
        print(f"✅ Refactoring report generated in '{REFACTO_REPORT_FILE}'.")
    else:
        print(f"⚠️  Timeout: '{REFACTO_REPORT_FILE}' not generated (the refacto may have modified code anyway).")

    # Clean up temporary files, whatever the refacto outcome.
    for tmp_f in [TMP_CODER_FILE, TMP_VERIFIER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE,
                  TMP_PO_FILE, TMP_PLAN_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_sentinels()
    # Landmark commit: this variant has no mechanical refacto verdict (LLM verifier
    # only), so the pre/post-refacto pair in the git log is the human's rollback handle.
    commit_phase("refacto: final polish (no mechanical verdict in this variant)")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)

def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "code", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan approval sentinel (spec.md deleted since) must never validate a FUTURE
    # spec: purge it before anything else.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # 🚀 STEP ZERO: Immediate harness Data Center boot in Tmux
    RUNNER.start()

    # Step 1: PO refinement via TUI (need.md → spec.md), validated by the HUMAN.
    # The validated spec becomes the source of truth for everything downstream (plan,
    # production). Three resume states: no spec → generate + confirm; spec WITHOUT the
    # approval sentinel (interrupted run: timeout, Ctrl-C during the y/n) → re-ask the
    # human instead of trusting a possibly never-validated file; spec + sentinel → skip.
    if not os.path.exists(SPEC_FILE):
        generate_spec_from_need_tui()
        confirm_spec_with_human()
        RUNNER.new_context()
    elif not os.path.exists(SPEC_APPROVED_SENTINEL):
        print(f"🔄 Existing '{SPEC_FILE}' found but NEVER approved (interrupted run?).")
        confirm_spec_with_human()
    else:
        print(f"🔄 Existing '{SPEC_FILE}' found (human-approved). PO step skipped.")

    # Step 2: Implementation plan via TUI (spec.md → plan.md)
    if not os.path.exists(PLAN_FILE):
        generate_plan_from_need_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 Existing '{PLAN_FILE}' found. Step skipped.")

    # Step 3: Blackboard configuration via TUI
    if not os.path.exists(BLACKBOARD_FILE):
        blackboard = transform_plan_to_blackboard_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 Existing '{BLACKBOARD_FILE}' found. Loading...")
        blackboard = load_blackboard()

    # The "need" context injected into production agents is the refined, validated SPEC
    # (testable acceptance criteria); need.md is only a fallback (old runs).
    # need_is_spec conditions the per-US slicing (extract_spec_slice) during production.
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Guardrail on phases[].id (blackboard produced by a fallible small LLM): a
    # duplicated id silently corrupts the sentinel channel, stop BEFORE paying a run.
    fatal_ids, soft_ids = validate_phase_ids(blackboard)
    for warning in soft_ids:
        print(f"⚠️  {warning}")
    if fatal_ids:
        for problem in fatal_ids:
            print(f"❌ {problem}")
        fail_pipeline(f"   → Fix '{BLACKBOARD_FILE}' then relaunch.")

    # The summary → y/n sequence LOOPS: the human can edit the blackboard in another terminal
    # while the prompt waits, but production runs on this in-memory dict and save_blackboard()
    # rewrites the file from it — an edit not reloaded before the 'y' would be ignored then
    # silently overwritten. Any file change during the prompt therefore triggers a reload and
    # a fresh confirmation on the re-displayed summary.
    while True:
        # NON-blocking spec → phases traceability warnings ('covers'): US hallucinated by the
        # compiler, or spec requirements that no phase covers.
        if need_is_spec:
            coverage_warnings = check_spec_coverage(blackboard, user_need)
            if coverage_warnings:
                print("\n⚠️  Spec → phases traceability:")
                for warning in coverage_warnings:
                    print(f"   - {warning}")

        print(f"\n{'='*50}")
        print(f"📋 BLACKBOARD READY — Summary:")
        print(f"   Project: {blackboard['project']}")
        print(f"   Stack (global_rules.target): {(blackboard.get('global_rules') or {}).get('target') or '(unspecified)'}")
        print(f"   Phases: {len(blackboard['phases'])}")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers') or [])
            print(f"   Phase {p['id']}: {p['name']} [{skills}] "
                  f"({len(p.get('tasks', []))} tasks; covers: {covers or '?'})")
        print(f"{'='*50}")
        print(f"   You can edit '{BLACKBOARD_FILE}' directly in another terminal before validating.")

        with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
            raw_at_prompt = f.read()
        confirm = input("\n▶️  Validate blackboard and start production? (y/n): ")
        mm_audit.event("gate", id="blackboard", gate_kind="yn", answer=confirm.strip().lower())
        if confirm.strip().lower() != 'y':
            print("⏹️  Cancelled by user.")
            RUNNER.kill()
            sys.exit(0)

        try:
            with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
                edited_during_prompt = f.read() != raw_at_prompt
            if not edited_during_prompt:
                break
            print(f"\n🔄 '{BLACKBOARD_FILE}' was edited while the prompt was waiting: reloading...")
            blackboard = load_blackboard()
        except Exception as err:
            print(f"❌ '{BLACKBOARD_FILE}' was edited during the prompt but is now unreadable "
                  f"(invalid or corrupt YAML): {err}")
            print(f"   → Fix '{BLACKBOARD_FILE}' then relaunch.")
            RUNNER.kill()
            sys.exit(1)

    validate_all_skills(blackboard)

    # Git landmarks (best-effort): baseline now, then one commit per validated phase.
    ensure_phase_repo()

    # Run baseline: everything that differs from this sha is the factory's work, never
    # pre-existing legacy. Persisted because a RESUME would recapture an already-advanced
    # HEAD, and the refactoring would then miss earlier phases' files.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    print(f"\n🚀 Starting active production: {blackboard['project']}")

    # Step 4: Production loop
    run_production_phases(blackboard, user_need, need_is_spec)

    # Step 5: Final polish
    execute_final_refactoring(blackboard, user_need)

    # Clean shutdown
    RUNNER.kill()
    # Successful run: nothing left to resume, so purge the spec-approval marker. Kept out of
    # cleanup_all_sentinels (which also runs mid-flight) because it must survive an INTERRUPTION;
    # here we are on the success path, so removing it is safe.
    if os.path.exists(SPEC_APPROVED_SENTINEL):
        os.remove(SPEC_APPROVED_SENTINEL)
    print("\n🏁 [CONGRATULATIONS] Data Center code factory validated everything in a single run!")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    PIPELINE_SKILLS=PIPELINE_SKILLS,
    POLL_INTERVAL=POLL_INTERVAL,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    US_HEADING_RE=US_HEADING_RE,
    _ORCH_BASENAMES=_ORCH_BASENAMES,
    parse_skill_frontmatter=parse_skill_frontmatter,
    run_git=run_git,
)


if __name__ == "__main__":
    main()
