#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - Code factory with an agent harness + tmux (Full TUI Data Center Version)
─────────────────────────────────────────────────────────────────────────────
CODING — reference pipeline: universal verdict + overlay (impact review, LLM verifier, breakage triage).

"Universal verdict" base (ex-Coding, removed: this script is now the reference), enriched with the Yolo
overlay — the arbitration logic of Guided-Fix.py moved INTO the run:
  - Step 2bis (upstream): an Impact Review Agent crosses 'plan.md' with the project's
    EXISTING code and lists in 'impact.md' the current behaviors the plan is going to
    BREAK; the HUMAN endorses them at a dedicated gate BEFORE production (nobody
    discovers mid-run that the evolution blows the application up).
  - Green path (ALL phases): after the mechanical verdict, an independent LLM Verifier
    (fresh context) confronts the code actually produced with the phase's checklist in the
    blackboard — a green suite proves "nothing is broken", not "the phase delivered
    everything". It never stamps DONE: a rejection consumes one coder attempt.
  - Red path (ALL phases): a Triage Agent confronts the failing tests with 'impact.md' —
    ENDORSED breakage → the test is deleted by the ORCHESTRATOR (never by an agent,
    accounting adjusted) and the flow continues; UNPLANNED breakage → a Repairer Agent
    fixes the side effect (failing tests FROZEN, the phase's behavior still required);
    a TRUE conflict (the two behaviors exclude each other) → 'impact-phase-<id>.md' is
    arbitrated by the human at a mid-run gate (accepted → mechanical deletion; rejected →
    the historical behavior prevails, the fix is recorded). The REJECTED + failReport.md
    net after MAX_ATTEMPTS is unchanged — it should just become rare.

Reminder of the "UNIVERSAL VERDICT" base — difference with Coding-Without-Tests.py (which stays in Tier 1):
  - The verdict of EVERY phase is the global verification command 'verify_cmd'
    (compilation + FULL SUITE, declared by the Architect in the plan). Since the scaffold
    guarantees a non-empty suite from phase 1, a regression introduced by ANY phase is
    detected at THAT phase, with fresh feedback — no need to wait for the final tests
    phase. A phase may declare its own command ('phases[].verify_cmd') as a rare EXCEPTION.
  - The coder prompt is NEUTRAL, plan-driven: it neither forces NOR forbids tests.
    The agent only does the tasks of ITS phase (another phase may be dedicated to tests).

PO → Architect pipeline (new):
  - Step 1: a PO Agent refines 'need.md' into a business specification 'spec.md' (user
    stories, testable acceptance criteria, out-of-scope, assumptions), VALIDATED by the
    human. Fixing the need costs the least HERE, before paying for plan + blackboard +
    production.
  - Step 2: an Architect Agent converts 'spec.md' into an implementation plan where each
    phase EXPLICITLY declares its nature (feature/tests) and its verification command.
  - Step 3: the blackboard conversion becomes a MECHANICAL copy of these decisions
    (zero inference asked of the small model, which only compiles the format).

Data Center & TUI Strategy (unchanged):
  - The tmux session is initialized DIRECTLY at startup.
  - We directly launch the chosen harness TUI (Cloud / Data Center model).
  - Steps 1 (PO Spec), 2 (Plan) and 3 (Blackboard) are executed directly in the TUI.
  - Production: each phase goes through a Coder Agent, then the orchestrator RUNS the
    phase's verification command itself; the exit code IS the verdict (brick A). The
    coder communicates via a sentinel file ('.phase_<id>.attemptN.done'); the sole owner
    of the blackboard is the Python orchestrator (no concurrent writes).

Accepted residual risk: the verdict proves "nothing is broken", not "the phase did its
job" (a no-op coder would pass green). Guards: anti "ghost coder" check (at least one
file declared in the sentinel must have actually changed during the attempt) and
a-posteriori functional proof by the tests phases, derived from the spec's acceptance
criteria.
"""

import os
import re
import sys
import time
import signal
import subprocess
import shlex
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
    allowed_test_edits, remove_planned_obsolete_tests,
    fail_if_toolchain_environment_broken,
    append_arbitration, build_coder_prompt, build_correction_prompt, build_mutation_targets,
    build_phase_verifier_prompt, build_refacto_fix_prompt, build_repair_prompt, build_skills_dictionary,
    build_triage_prompt, collect_spec_us_ids, commit_phase, correction_sentinel,
    done_sentinel, ensure_phase_repo, fail_pipeline, files_changed_since_phase_start,
    generate_impact_review_tui, git_head_sha, impact_phase_file, inject_skills_dictionary,
    is_test_file, load_blackboard, load_skills, mutation_tool_available,
    no_declared_file_touched, print_failure_message, read_repair_outcome, read_touched_files,
    read_triage, read_verdict, record_test_count, repair_sentinel,
    resolve_mutation_cmd, resolve_verify_cmd, restore_test_files, run_mutation,
    run_verify, run_verify_resilient, save_blackboard, signal_handler,
    test_count_regression, triage_sentinel, truncate_output, validate_all_skills,
    validate_blackboard_schema, verdict_sentinel, wait_for_file_creation,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="coding")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
FAIL_REPORT_FILE      = "failReport.md"   # persistent stop report (part D, §6.8)
IMPACT_FILE           = "impact.md"       # Yolo: validated impact review (committed, audit trail)
IMPACT_PHASE_PREFIX   = "impact-phase-"   # Yolo: impact-phase-<id>.md, mid-run human arbitration
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Pipeline system skills: never routed to production phases.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-to-blackboard", "refacto"}

# Temporary context routing files
TMP_CODER_FILE        = RUNNER.tmp_file("task")
TMP_REFACTO_FILE      = RUNNER.tmp_file("refacto")
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")
# Yolo: offloaded instructions of the four new agents (impact review, phase verifier,
# breakage triage, side-effect repairer/corrector).
TMP_IMPACT_FILE       = RUNNER.tmp_file("impact")
TMP_VERIFIER_FILE     = RUNNER.tmp_file("verifier")
TMP_TRIAGE_FILE       = RUNNER.tmp_file("triage")
TMP_REPAIR_FILE       = RUNNER.tmp_file("repair")

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
IMPACT_DONE_SENTINEL     = ".pipeline_impact.done"    # Yolo: end of step 2bis (impact review)

# HUMAN approval of the spec, materialized: the mere EXISTENCE of spec.md proves nothing
# (a timeout can leave a never-validated spec behind, see fail_pipeline). Deliberately
# outside the '.pipeline_*.done' pattern purged by cleanup_all_sentinels: the approval
# must survive a resume.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# Yolo: HUMAN approval of the impact review, materialized — same contract as the spec
# (the existence of impact.md does not prove its validation; the approval survives a resume).
IMPACT_APPROVED_SENTINEL = ".impact_approved"

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session (prompts of project B would
# land in the agent of project A). Resuming the SAME project reuses its session.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
REFACTO_FIX_PHASE_ID  = -1             # dedicated sentinel id for post-refacto regression fixes (≠ phases ≥1, ≠ scaffold 0)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per phase (safety net)
VERIFY_TIMEOUT        = resolve_timeout("verify", 300)            # 5 min max for running the verification command
MAX_VERIFY_RETRIES_ON_TIMEOUT = 2      # immediate re-verifications on an infra timeout (the code did not change)
MAX_PHASE_VERIFY_TIMEOUTS     = 3      # persistent timeouts tolerated on a phase before aborting ("broken infra")
MUTATION_TIMEOUT      = 300            # CAUTIOUS: bounded budget for mutation testing (brick B). Brick B
                                       # NEVER lengthens the run without bound; any overrun degrades to a warn
MAX_PHASE_MUTATION_TIMEOUTS   = 2      # anti-cost backstop if the mutation tool/infra is durably slow
SCAFFOLD_TIMEOUT      = 300            # 5 min: the scaffold is the shortest task of the run — if it
                                       # does not complete, the model's tool calling is almost always
                                       # the culprit, and a fast diagnosis beats a long wait
VERIFY_FEEDBACK_LIMIT = 4000           # max size of the verification feedback sent back to the coder
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net: pipeline deliverable accepted if it
                                       # stayed stable for N consecutive checks (N × POLL_INTERVAL seconds).
                                       # 30s: a slow local model pausing between two writes must not get
                                       # its half-written deliverable accepted (see structural_check too)

# NO test/code toggle in this script (unlike Coding-Without-Tests.py).
# The verification command is ALWAYS read from the "verify_cmd" field: the phase's one if
# declared (rare exception), else the global one. The UNIVERSAL VERDICT (compilation +
# full suite) is carried by the global 'verify_cmd', declared by the Architect Agent and
# copied by the blackboard compiler, never by this script.


# ─── PHASE SENTINELS (CODER → ORCHESTRATOR CHANNEL) ────────────────

# Yolo: per-attempt sentinels of the new agents. Same contract as the coder (the attempt
# number in the name makes any confusion with a late signal from a previous attempt
# impossible), with distinct suffixes so each channel stays identifiable.

# Yolo: the purges also cover the .verdict and .triage suffixes (the .done of the new
# agents already share the coder's pattern).
_SENTINEL_SUFFIXES = (".done", ".verdict", ".triage")

def cleanup_sentinels(phase_id: int):
    """Remove all sentinels (every attempt, every agent) of a phase."""
    prefix = f".phase_{phase_id}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(_SENTINEL_SUFFIXES):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_sentinels():
    """Final cleanup of all residual sentinels (phases AND pipeline)."""
    for name in os.listdir("."):
        if (name.startswith(".phase_") or name.startswith(".pipeline_")) \
                and name.endswith(_SENTINEL_SUFFIXES):
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


def check_need_file():
    if not os.path.exists(NEED_FILE):
        print(f"❌ Critical error: '{NEED_FILE}' is missing.")
        write_fail_report("Need file missing", f"'{NEED_FILE}' is missing at the project root.")
        sys.exit(1)
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print(f"❌ Critical error: '{NEED_FILE}' is empty.")
        write_fail_report("Need file empty", f"'{NEED_FILE}' is present but empty.")
        sys.exit(1)
    print("✓ Need file (need.md) validation: OK")


# ─── GIT GUARDS (BEST-EFFORT) ─────────────────────────────────────────────────
# Everything here is BEST-EFFORT: without git (binary absent, init failure), the
# factory runs identically but WITHOUT mechanical guards — graceful degradation,
# never block the run over tooling. What git buys, in the "Python verifies what is
# verifiable" spirit: a commit per green phase (per-phase diff → mechanical detection
# of test tampering), a rollback point for the final refacto, and an audit trail
# (spec/plan/blackboard ARE committed).

_GIT = {"enabled": False}

# Identity passed per command: the factory must not depend on the machine's git config.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

GITIGNORE_BODY = f"""# MAIster-Mind orchestration artifacts (ephemeral)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.fix_*
.spec_approved
.impact_approved
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


# ─── TEST SUITE INTEGRITY (BEST-EFFORT §1.3 GUARDS) ───────────────────────────

_TEST_COUNT = {"warned": False}


# ─── VERIFICATION EXECUTION (BRICK A: EXECUTION = VERDICT) ────────────────────

# ─── BRICK B: TARGETED MUTATION TESTING (DOES THE SUITE BITE?) ────────────────
# Extension of brick A: the universal verdict proves "nothing is broken"; brick B proves
# "the suite turns RED when the code is wrong" (falsifiable tests). End-to-end mechanical —
# the mutation tool's exit code IS the verdict, no LLM judges. Driven by the Architect via an
# OPTIONAL 'mutation_cmd' field; absent → brick inactive (run identical to today). Graceful
# degradation everywhere (tool absent / timeout → warn, never a block).

# Files belonging to the ORCHESTRATOR itself (never code produced by the coder): buffer
# prompts, pipeline deliverables, blackboard, sentinels, Python caches, venv, agent configs
# and the MAIster-Mind script. They are rewritten every phase; NO 'git diff'-based guard must
# count them as "modified production code" nor restore them (git checkout) — otherwise the
# factory sabotages its own state, even its own script, and no 'tests' phase ever converges
# (cause of a systematic rejection when these artifacts are git-tracked, e.g. a human repo
# whose .gitignore did not cover them). Deliberately BROAD: when in doubt we protect (at worst
# we miss a false "touched code" on an orchestration file, never on real produced code).
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE, FAIL_REPORT_FILE, IMPACT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_IMPACT_FILE, TMP_VERIFIER_FILE, TMP_TRIAGE_FILE, TMP_REPAIR_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, IMPACT_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


def is_orchestration_file(path: str) -> bool:
    """Is 'path' an orchestrator artifact (and not produced code)? Cf. _ORCH_BASENAMES."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _ORCH_BASENAMES:
        return True
    # Ephemeral sentinels and buffers, wherever they sit in the tree ('.fix_*':
    # Guided-Fix.py sentinels, same family).
    if base.startswith(".phase_") or base.startswith(".pipeline_") or base.startswith(".fix_"):
        return True
    # Yolo: mid-run arbitration reports (impact-phase-<id>.md), written by the
    # orchestrator's agents — never produced code.
    if base.startswith(IMPACT_PHASE_PREFIX) and base.endswith(".md"):
        return True
    # Guided-Fix.py arbitration reports: orchestration deliverables COMMITTED as an
    # audit trail (same status as spec/plan/blackboard) — never produced code. Without
    # this pattern, they would enter the final refactoring scope and be counted as
    # "touched code" by the diff-based guards.
    if base.startswith("fix_report-") and base.endswith(".md"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Python caches, virtual environment and tooling directories: never produced code.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


# ─── BLACKBOARD SCHEMA VALIDATION (PRODUCED BY A FALLIBLE SMALL LLM) ───────────

REQUIRED_GLOBAL_RULES = ["target", "styling", "constraints", "accessibility"]


def apply_blackboard_defaults(blackboard: dict):
    """Fill in absent non-critical fields to avoid any KeyError in production.

    Structural gaps were already reported by validate_blackboard_schema; here we only
    guarantee that later direct accesses do not raise an exception.
    """
    if not isinstance(blackboard, dict):
        return
    blackboard.setdefault("status", "IN_PROGRESS")
    global_rules = blackboard.setdefault("global_rules", {})
    if isinstance(global_rules, dict):
        for key in REQUIRED_GLOBAL_RULES:
            global_rules.setdefault(key, "(unspecified)")
    for phase in blackboard.get("phases", []) or []:
        if isinstance(phase, dict):
            phase.setdefault("status", "TODO")
            phase.setdefault("verdict", "PENDING")
            phase.setdefault("critic_feedback", "")
            phase.setdefault("skills_required", [])
            phase.setdefault("tasks", [])
            phase.setdefault("covers", [])
            # Architect decisions carried since the plan (empty on old blackboards:
            # the coder prompt then falls back to its neutral wording).
            phase.setdefault("nature", "")
            phase.setdefault("context", "")
            phase.setdefault("files_to_read", [])


# ─── PER-PHASE SPEC SLICING (CONTEXT WINDOW) ──────────────────────────────────
# The old "regression coverage" heuristic (guessing on a free string whether the last
# tests phase ran the full suite) is REMOVED: the universal verdict (every phase =
# compilation + full suite) makes the coverage structural.


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
- The plan MUST start with the "Stack & Verification" block (with the UNIVERSAL VERDICT verification command: compilation + full suite) and EVERY phase MUST declare its Nature (feature/tests) and its "Covers" field (US-x): the next pipeline steps copy these decisions without inferring them.
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


# ─── YOLO · STEP 2BIS: IMPACT REVIEW (UPSTREAM, BETWEEN PLAN AND BLACKBOARD) ───

def confirm_impact_with_human():
    """Human validation of the impact review (UPSTREAM human-in-the-loop, Yolo gate).

    This is where the human endorses the existing behaviors the evolution is going to
    break: in production, a red test covered by this review will be deleted MECHANICALLY
    by the orchestrator, without another stop. The review can be edited in another
    terminal before validating (removing an impact = requiring its preservation).
    """
    print(f"\n{'='*50}")
    print(f"🔎 IMPACT REVIEW READY: review '{IMPACT_FILE}' — every impact validated here will be "
          f"broken WITHOUT another stop during production (test deleted by the orchestrator).")
    print(f"   You can edit it directly in another terminal before validating.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Validate the impact review and continue? (y/n): ")
    mm_audit.event("gate", id="impact", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Cancelled by the user. Adjust '{PLAN_FILE}' (or '{SPEC_FILE}') to "
              f"preserve these behaviors, delete '{IMPACT_FILE}', then relaunch.")
        RUNNER.kill()
        sys.exit(0)
    # The approval is MATERIALIZED (same contract as the spec): on resume, a review without
    # this sentinel goes through the y/n again instead of being trusted as validated.
    with open(IMPACT_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")


# ─── STEP 4 & 5: PROMPTS DELEGATED TO FILES ──────────────────────────────────

# ─── FAILURE MESSAGE ──────────────────────────────────────────────────────────


def write_fail_report(title: str, reason: str, blackboard: dict = None, details: str = "",
                      action: str = None):
    """Write a persistent stop report at the root (part D, §6.8). Best-effort: NEVER raises.

    Every NON-nominal stop of the run (each sys.exit(1)) produces one: cause, progress
    (validated vs remaining phases) and recommended action survive the (volatile) console
    message, which is precious for a long, unattended run. Reserved for REAL stops: brick B's
    graceful degradations (mutation tool absent, timeout, surviving mutants) do NOT stop the
    run and therefore write NO report.
    """
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — Yolo", "", f"## {title}", "", "### Cause", reason.strip(), ""]
        if isinstance(blackboard, dict) and isinstance(blackboard.get("phases"), list):
            phases = blackboard["phases"]
            done = sum(1 for p in phases if isinstance(p, dict)
                       and p.get("status") == "DONE" and p.get("verdict") == "OK")
            lines.append("### Progress")
            lines.append(f"- Validated phases: {done}/{len(phases)}")
            for p in phases:
                if not isinstance(p, dict):
                    continue
                ok = p.get("status") == "DONE" and p.get("verdict") == "OK"
                mark = "✅" if ok else "⏳"
                lines.append(f"  - {mark} Phase {p.get('id', '?')} : {p.get('name', '(no name)')} "
                             f"[{p.get('status', '?')}/{p.get('verdict', '?')}]")
            lines.append("")
        if details.strip():
            lines.append("### Details")
            lines.append(truncate_output(details))
            lines.append("")
        lines.append("### Recommended action")
        lines.append(action or "Fix the cause above (or bring in a model one notch higher via /model or "
                     f"'{AGENT_CONFIG_FILE}'), then relaunch: the already-validated phases will "
                     "be resumed automatically.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── SCAFFOLD STEP (EXECUTABLE SKELETON + HEALTH TEST) ────────────────────────

def ensure_executable_scaffold(blackboard: dict, user_need: str):
    """Guarantee an executable project BEFORE production (hard prerequisite of brick A).

    If the global verification command does not pass (missing toolchain/scaffold), a
    dedicated agent creates the minimal skeleton (build file + directory tree + a trivial
    health test), then we re-test. Early, readable failure rather than N red phases
    unrelated to their own logic.

    Idempotent: if verification already passes (resume after crash, or pre-bootstrapped
    project), the step is skipped without invoking any agent.
    """
    verify_cmd = (blackboard.get("verify_cmd") or "").strip()
    if not verify_cmd:
        print("⚠️  No global verification command: scaffold step skipped "
              "(execution-based verification will be inoperative).")
        return

    print(f"\n{'='*50}\n🏗️  STEP 0: EXECUTABLE SCAFFOLD\n{'='*50}")
    print("   ℹ️  This step also serves as a SMOKE TEST of the model: it is its simplest")
    print("      request (create 2-3 files + a sentinel). If it does not complete, suspect")
    print("      the configured model's tool calling first.")
    print("   Preliminary check of the verification chain...")
    ok, output, _ = run_verify(verify_cmd)
    if ok:
        print("✓ Verification chain already passes: skeleton present, step skipped.")
        record_test_count(output, blackboard)
        return
    fail_if_toolchain_environment_broken(verify_cmd, output, blackboard)

    print("   Chain not operational: generating the skeleton via a dedicated agent...")
    scaffold_done = done_sentinel(0, 1)
    cleanup_sentinels(0)

    scaffold_context = f"""You are a Platform Engineer. Create ONLY the minimal executable
skeleton of the project, without implementing any feature of the need.

--- TARGET STACK ---
{blackboard['global_rules']['target']}

--- INITIAL NEED (context only, DO NOT implement) ---
{user_need}

--- GOAL ---
1. Create the build / dependency management file suited to the stack (e.g. pom.xml,
   package.json, pyproject.toml) and the standard source/test directory tree, empty.
2. Add A SINGLE trivial health test that compiles and passes empty (e.g. a true assertion).
3. No business logic, no feature of the need: strictly the skeleton.
4. Once your work is done, the following command MUST succeed: « {verify_cmd} ».

--- MANDATORY END OF TASK ---
You NEVER touch the file {BLACKBOARD_FILE}. As your very last action, create the sentinel
file '{scaffold_done}' at the root, containing the list of created files (one per line).
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(scaffold_context)

    RUNNER.new_context()
    mm_audit.event("agent_task", prompt_bytes=len(f"Read the task file '{TMP_CODER_FILE}' at the project root and create the project's executable skeleton."))
    RUNNER.send_task(f"Read the task file '{TMP_CODER_FILE}' at the project root and create the project's executable skeleton.")

    if not wait_for_file_creation(scaffold_done, timeout=SCAFFOLD_TIMEOUT):
        print(f"""
{'='*60}
❌ The agent did not signal scaffold completion (sentinel missing after {SCAFFOLD_TIMEOUT}s).

   The scaffold is the FIRST and simplest request made to the model. If it fails
   here, suspect a TOOL CALLING problem of the configured model FIRST, before any
   code problem: some models (small local models especially) print the tool call
   as text instead of executing it, or never create the requested files.

   Diagnosis: attach to the session ('tmux attach -t {TMUX_SESSION}') and check
   whether the model writes text instead of using its editing tools. If so, switch
   to a model reliable at tool calling (/model in the TUI or
   '{AGENT_CONFIG_FILE}'), then relaunch.
{'='*60}
""")
        # Tool-calling diagnosis without attaching: the last TUI screen usually shows
        # whether the model printed its tool calls as text instead of executing them.
        tail = RUNNER.capture()[-1500:]
        if tail.strip():
            print(f"   Last TUI screen (diagnosis):\n{tail}")
        write_fail_report(
            "Scaffold did not complete",
            f"The agent did not signal scaffold completion (sentinel missing after {SCAFFOLD_TIMEOUT}s). "
            f"Suspect the configured model's tool calling first.",
            blackboard, details=tail)
        cleanup_sentinels(0)
        RUNNER.kill()
        sys.exit(1)

    RUNNER.new_context()
    ok, output, _ = run_verify(verify_cmd)
    cleanup_sentinels(0)
    if not ok:
        print(f"""
{'='*60}
❌ The execution chain is broken BEFORE production even starts.
   The command « {verify_cmd} » fails on the generated skeleton.

   Output (truncated):
{output}

💡 Fix the skeleton or the verification command in '{BLACKBOARD_FILE}',
   then relaunch. (We prefer this clean stop to N red phases unrelated to logic.)
{'='*60}
""")
        write_fail_report(
            "Verification chain broken on the scaffold",
            f"The command « {verify_cmd} » fails on the generated skeleton, before any production.",
            blackboard, details=output)
        RUNNER.kill()
        sys.exit(1)
    print("✓ Executable scaffold validated: the verification chain passes empty.\n")
    record_test_count(output, blackboard)
    commit_phase("scaffold: executable skeleton")


# ─── YOLO · GREEN PATH: PHASE LLM VERIFIER ────────────────────────────────────

# ─── YOLO · RED PATH: IMPACT TRIAGE, REPAIR, ARBITRATION ──────────────────────

def delete_planned_tests(paths: list, blackboard: dict, phase: dict, reason: str) -> list:
    """MECHANICAL deletion of test files whose breakage is endorsed (decision 1).

    It is the ORCHESTRATOR that deletes, never an agent: each path is validated (exists, is
    a test file, not an orchestration artifact), deleted (git rm if tracked), logged in
    impact.md, removed from protected_test_files and remembered in _yolo_deleted_tests (the
    freeze guards must not restore it). The last_test_count reference is reset: the next
    green re-baselines the non-decreasing guard (it would otherwise compare against a count
    including the deleted tests). The deletion is COMMITTED right away: the diff-since-HEAD
    guards must not mistake this orchestrator act for an agent's work.
    """
    deleted = []
    for raw in paths:
        p = str(raw).strip().strip("'\"`").replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        if not p or not os.path.isfile(p):
            print(f"   ⚠️  Deletion: path not found, ignored: '{raw}'")
            continue
        if not is_test_file(p) or is_orchestration_file(p):
            print(f"   ⚠️  Deletion: '{p}' is not a deletable test file, "
                  f"ignored (the repairer will take over).")
            continue
        tracked = False
        if _GIT["enabled"]:
            ok_tracked, tracked_out = run_git(["ls-files", "--", p])
            tracked = ok_tracked and bool(tracked_out.strip())
        if tracked:
            run_git(["rm", "-f", "--", p])
        else:
            try:
                os.remove(p)
            except OSError:
                continue
        deleted.append(p)
        print(f"   🗑️  Test deleted by the orchestrator (endorsed breakage): {p}")
    if not deleted:
        return deleted

    # Accounting: re-baseline of the non-decreasing guard + removal of the protections
    # + memory of the deletions (exclusion from the freeze guards).
    if "last_test_count" in blackboard:
        blackboard.pop("last_test_count", None)
        print("   ℹ️  Non-decreasing guard reset (re-baseline at the next green).")
    protected = set(blackboard.get("protected_test_files") or [])
    if protected & set(deleted):
        blackboard["protected_test_files"] = sorted(protected - set(deleted))
    already = set(blackboard.get("_yolo_deleted_tests") or [])
    blackboard["_yolo_deleted_tests"] = sorted(already | set(deleted))
    save_blackboard(blackboard)

    # Audit log in impact.md (“Deletion log” section, at the end of the file).
    try:
        with open(IMPACT_FILE, "a", encoding="utf-8") as f:
            for p in deleted:
                f.write(f"- Phase {phase['id']}: `{p}` deleted — {reason}\n")
    except OSError:
        pass
    commit_phase(f"phase {phase['id']}: endorsed test deletion ({reason})")
    return deleted


def repair_touched_tests(blackboard: dict, phase_start_sha: str, baseline: set) -> list:
    """Test files touched by a repair/correction pass (freeze violation).

    'baseline' is the snapshot of the files already modified BEFORE the pass (captured by
    the caller): only what shows up ON TOP is that pass's work — the attribution never
    blames the phase's earlier legitimate work, nor the orchestrator's deletions (committed
    and remembered in _yolo_deleted_tests).
    """
    deleted = set(blackboard.get("_yolo_deleted_tests") or [])
    return sorted(f for f in files_changed_since_phase_start(phase_start_sha) - set(baseline or ())
                  if f.strip() and is_test_file(f.strip())
                  and not is_orchestration_file(f.strip())
                  and f.strip() not in deleted)


def attempt_impact_resolution(phase: dict, blackboard: dict, phase_need: str,
                              failure_output: str, attempt: int, phase_cmd: str,
                              phase_start_sha: str) -> tuple:
    """Yolo red path (fig. 4 of the validated design): triage against impact.md, mechanical
    deletion of the endorsed breakages, repair of the side effects, human arbitration of the
    unplanned impacts. Returns (is_ok, output): is_ok=True if the suite came back to GREEN
    (the caller carries on with the green path, LLM verifier included); otherwise the freshest
    red output becomes the coder's feedback again (the attempt is consumed).
    Any agent or verification timeout degrades to (False, ...): never a deadlock, the
    REJECTED net after MAX_ATTEMPTS stays exactly the same as in the base.
    """
    phase_id = phase["id"]

    # 1. TRIAGE: which tests break, and does the validated impact review cover them?
    print(f"🔎 [IMPACT TRIAGE] Tests are failing: confronting them with '{IMPACT_FILE}'...")
    RUNNER.new_context()
    RUNNER.send_task(build_triage_prompt(phase, failure_output, attempt))
    if not wait_for_file_creation(triage_sentinel(phase_id, attempt)):
        print("⏱️  The triage returned no verdict: back to the normal flow (coder feedback).")
        return False, failure_output
    prevu, imprevu = read_triage(phase_id, attempt)
    print(f"   → Triage: {len(prevu)} PLANNED breakage(s), {len(imprevu)} UNPLANNED.")

    # 2. PLANNED breakages: mechanical deletion by the orchestrator + re-verification.
    if prevu:
        deleted = delete_planned_tests(prevu, blackboard, phase,
                                       "breakage planned by the validated impact review")
        if deleted:
            is_ok, output, timed_out = run_verify_resilient(phase_cmd)
            if timed_out:
                return False, failure_output
            if is_ok:
                print("✅ [IMPACT TRIAGE] Suite green after deleting the endorsed tests: "
                      "the flow continues.")
                return True, output
            failure_output = output  # red remains: head for the repair

    # 3. REPAIRER: unplanned side effect — failing tests FROZEN, phase behavior still required.
    print(f"🔧 [REPAIRER] Fixing the unplanned side effect (failing tests FROZEN)...")
    pre_repair = files_changed_since_phase_start(phase_start_sha)
    RUNNER.new_context()
    RUNNER.send_task(build_repair_prompt(phase, blackboard, failure_output, phase_cmd, attempt))
    if not wait_for_file_creation(repair_sentinel(phase_id, attempt)):
        print("⏱️  The repairer did not signal completion: back to the normal flow (coder feedback).")
        return False, failure_output

    touched = repair_touched_tests(blackboard, phase_start_sha, pre_repair)
    if touched:
        restore_test_files(touched)
        print(f"🛡️  [REJECTED] The repairer modified frozen tests ({', '.join(touched)}) — restored.")
        return False, (failure_output + "\n\n[Orchestrator] The repair pass was "
                       "reverted: the test files are frozen, fix the production code.")

    is_conflict, conflict_tests = read_repair_outcome(phase_id, attempt)

    if not is_conflict:
        is_ok, output, timed_out = run_verify_resilient(phase_cmd)
        if timed_out:
            return False, failure_output
        if is_ok:
            print("✅ [REPAIRER] Side effect absorbed: suite green, the flow continues.")
            return True, output
        print("⚠️  [REPAIRER] The suite stays red after the repair.")
        return False, output

    # 4. A REAL CONFLICT declared: human arbitration (mid-run gate impact-phase-<id>).
    if not os.path.exists(impact_phase_file(phase_id)):
        print("⚠️  The repairer declares a conflict but wrote no impact report: "
              "back to the normal flow (coder feedback).")
        return False, failure_output
    print(f"\n{'='*50}")
    print(f"⚖️  UNPLANNED IMPACT DETECTED (phase {phase_id}): review '{impact_phase_file(phase_id)}'.")
    print(f"   y → the impact is endorsed: the tests concerned will be deleted by the orchestrator.")
    print(f"   n → the old behavior prevails: an agent fixes the phase while preserving it.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Accept this new impact and continue? (y/n): ")
    mm_audit.event("gate", id="impact-phase", gate_kind="yn", answer=confirm.strip().lower())

    if confirm.strip().lower() == 'y':
        append_arbitration(phase_id, accepted=True)
        deleted = delete_planned_tests(
            conflict_tests, blackboard, phase,
            f"unplanned impact endorsed by the human (cf. {impact_phase_file(phase_id)})")
        if not deleted:
            print("⚠️  No deletable test in the conflict declaration: back to the normal "
                  "flow (coder feedback).")
            return False, failure_output
        is_ok, output, timed_out = run_verify_resilient(phase_cmd)
        if timed_out:
            return False, failure_output
        if is_ok:
            print("✅ [ARBITRATION] Impact endorsed, tests deleted: the flow continues.")
            return True, output
        return False, output

    # Rejected: the old behavior prevails — fix while preserving it (decision 5: a single
    # pass; a persistent red = a spec conflict, the attempt fails).
    append_arbitration(phase_id, accepted=False)
    print(f"↩️  [ARBITRATION] Impact rejected: fixing while preserving the historical behavior...")
    pre_fix = files_changed_since_phase_start(phase_start_sha)
    RUNNER.new_context()
    RUNNER.send_task(build_correction_prompt(phase, blackboard, failure_output, phase_cmd, attempt))
    if not wait_for_file_creation(correction_sentinel(phase_id, attempt)):
        print("⏱️  The corrector did not signal completion: back to the normal flow (coder feedback).")
        return False, failure_output
    touched = repair_touched_tests(blackboard, phase_start_sha, pre_fix)
    if touched:
        restore_test_files(touched)
        print(f"🛡️  [REJECTED] The corrector modified frozen tests ({', '.join(touched)}) — restored.")
        return False, failure_output
    is_ok, output, timed_out = run_verify_resilient(phase_cmd)
    if timed_out:
        return False, failure_output
    if is_ok:
        print(f"✅ [ARBITRATION] Historical behavior preserved, suite green — arbitration "
              f"recorded in '{impact_phase_file(phase_id)}'.")
        return True, output
    print("⚠️  [ARBITRATION] The suite stays red after the fix: probably a spec conflict "
          "(not a code bug). The attempt fails — see the arbitration report.")
    return False, output


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

        # Context window: the coder only receives the spec slice covered by ITS phase
        # ('covers' field), never the whole spec — except graceful degradation.
        phase_need = extract_spec_slice(user_need, phase.get("covers")) if need_is_spec else user_need
        if need_is_spec and len(phase_need) < len(user_need):
            print(f"   ✂️  Spec sliced for the phase: {len(phase_need)}/{len(user_need)} characters "
                  f"(covers {', '.join(phase.get('covers', []))}).")

        verify_cmd = resolve_verify_cmd(phase, blackboard)
        if not verify_cmd:
            print(f"❌ Phase {phase['id']}: no verification command "
                  f"('verify_cmd' on the phase or global). Fix '{BLACKBOARD_FILE}' then relaunch.")
            write_fail_report(
                f"Phase {phase['id']} \"{phase['name']}\" has no verification command",
                f"Neither the phase nor the blackboard declares a 'verify_cmd': this phase cannot "
                f"be verified. Fix '{BLACKBOARD_FILE}' then relaunch.",
                blackboard)
            RUNNER.kill()
            sys.exit(1)

        # ── POST-FIX REVALIDATION (handshake with Guided-Fix.py) ──: a 'FIXED'
        # phase was repaired and brought back to green by Guided-Fix.py after a
        # human arbitration (regression fixed or evolution endorsed). fix.py NEVER
        # stamps DONE/OK itself — it is a CLAIM, not a verdict: the orchestrator remains
        # the sole authority and RE-EXECUTES the verification here. Green → validated
        # WITHOUT relaunching a coder (replaying an already-complete phase would push
        # the agent into gratuitous changes to satisfy the anti-ghost guard). Red → the
        # phase falls back into the normal loop below, with the fresh output as first
        # feedback. The side bookkeeping (last_test_count, protected_test_files of a
        # 'tests' phase) and the COMMIT of the repaired work are already handled by
        # fix.py (an uncommitted fix would be taken for the next phase's work by the
        # diff-HEAD guards, and restored).
        fix_recheck_feedback = ""
        if str(phase.get("status") or "").strip().upper() == "FIXED":
            print(f"🔁 Phase {phase['id']} marked 'FIXED' by Guided-Fix.py: revalidation by execution...")
            recheck_ok, recheck_output, recheck_timed_out = run_verify_resilient(verify_cmd)
            if recheck_ok:
                record_test_count(recheck_output, blackboard)
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCESS] Phase {phase['id']} revalidated: the verification passes (exit code 0).")
                commit_phase(f"phase {phase['id']}: {phase['name']} (revalidated post-fix)")
                continue
            if recheck_timed_out:
                print(f"🛑 [INFRA TIMEOUT] The revalidation of phase {phase['id']} times out "
                      f"repeatedly: an INFRASTRUCTURE incident, not a code failure. The "
                      f"'FIXED' marker is kept: check the machine or the command, then relaunch.")
                write_fail_report(
                    f"Post-fix revalidation of phase {phase['id']} timed out",
                    f"The command \"{verify_cmd}\" times out repeatedly during the post-fix "
                    f"revalidation: an infrastructure incident, not a code failure. The "
                    f"'FIXED' marker is kept — fix the environment then relaunch.",
                    blackboard)
                RUNNER.kill()
                sys.exit(1)
            print(f"⚠️  The revalidation of phase {phase['id']} fails: the repaired state no longer "
                  f"passes (different environment or code changed since the repair). The phase goes "
                  f"back through the normal production loop with this output as first feedback.")
            fix_recheck_feedback = recheck_output

        attempts = 0
        verify_timeouts = 0
        mutation_timeouts = 0       # brick B: mutation timeouts on this phase (cost backstop)
        mutation_hardening_used = 0 # brick B: hardening passes consumed (bounded to 1)
        success  = False
        critic_feedback = fix_recheck_feedback or "First draft — no previous criticism."
        # Architect's decision (copied since the plan): drives the test-file guard below.
        nature = str(phase.get("nature") or "").strip().lower()
        # Landmark for the per-phase diff (3c): empty without git.
        remove_planned_obsolete_tests(phase, blackboard)   # tests obsolètes déclarés par le plan
        phase_start_sha = git_head_sha()
        # Temporal reference of the PHASE (mtime fallback of the anti-ghost guard), captured
        # ONCE here. Never per attempt: a per-attempt reference wrongly reclassified as "ghost"
        # a file written during a previous attempt.
        phase_started = time.time()

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

            # ── ANTI "GHOST CODER" GUARD ──: the full suite stays green if the agent did
            # NOTHING; the verdict alone cannot tell "nothing broken" from "nothing done".
            # If no declared file changed SINCE THE PHASE STARTED, reject BEFORE paying for a
            # verification. Reference = phase (not attempt): a file produced in one attempt and
            # re-declared unchanged later is still real work.
            changed_in_phase = files_changed_since_phase_start(phase_start_sha)
            if no_declared_file_touched(touched_files, phase_started, changed_in_phase):
                critic_feedback = (
                    f"Your sentinel declares {len(touched_files)} file(s), but NONE was "
                    "actually created or modified since this phase started. CONCRETELY perform "
                    "the checklist tasks (create/modify the files), and only then recreate "
                    "the sentinel with the real list of touched files."
                )
                phase["critic_feedback"] = critic_feedback
                save_blackboard(blackboard)
                print(f"👻 [REJECTED] Attempt {attempts}: sentinel written but no declared "
                      f"file was touched (ghost coder).")
                mm_audit.event("guard", name="codeur_fantome", action="rejet")
                RUNNER.new_context()
                continue

            # ── TEST-FILE PROTECTION (mechanical §1.3 guard, best-effort) ──: files
            # produced by green 'tests' phases are out of bounds during 'feature'
            # phases. The prompt-only prohibition is unverifiable; this diff is not.
            # Known false positive (a legitimately shared test helper): the feedback
            # names the files, the human arbitrates.
            if nature == "feature" and _GIT["enabled"]:
                protected = set(blackboard.get("protected_test_files") or []) - allowed_test_edits(phase, blackboard)
                if protected:
                    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                    touched_protected = sorted(set(diff_out.splitlines()) & protected) if ok_diff else []
                    if touched_protected:
                        run_git(["checkout", "--"] + touched_protected)
                        critic_feedback = (
                            f"You modified PROTECTED test files during a 'feature' phase: "
                            f"{', '.join(touched_protected)}. They have been restored. Test files "
                            f"are out of bounds in feature phases: implement this phase's "
                            f"checklist without touching them."
                        )
                        phase["critic_feedback"] = critic_feedback
                        save_blackboard(blackboard)
                        print(f"🛡️  [REJECTED] Attempt {attempts}: protected test files modified "
                              f"({', '.join(touched_protected)}) — restored.")
                        mm_audit.event("guard", name="tests_proteges",
                                       action="restauration", files=len(touched_protected))
                        RUNNER.new_context()
                        continue

            # ── TESTS-ONLY GUARD (mirror of protected_test_files, best-effort, §6.6) ──:
            # a 'tests' phase only modifies test files; production code is FROZEN. Any touched
            # production file is restored (git checkout) and the attempt rejected. Placed BEFORE
            # verification (like protected_test_files): we catch the cheat whether the attempt
            # ends green OR red, and avoid a wasted verify on a state we will reject. Anti-cheat
            # (the tester cannot tweak production to make its tests pass) AND foundation of brick B
            # (we mutate a stable production). Settled caveat: a real production bug revealed by a
            # test STALLS the phase, left to the human (no quiet production fix by the tester).
            if nature == "tests" and _GIT["enabled"]:
                ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                # Excludes the orchestrator's own files (prompts, blackboard, sentinels, .pyc,
                # its own script…), which it rewrites every phase: counting them as "modified
                # production code" would reject EVERY tests attempt and, worse, restoring them
                # (git checkout below) would sabotage the orchestrator's state — even its
                # script. Cf. is_orchestration_file.
                touched_prod = sorted(f for f in diff_out.splitlines()
                                      if f.strip() and not is_test_file(f.strip())
                                      and not is_orchestration_file(f.strip())) if ok_diff else []
                if touched_prod:
                    run_git(["checkout", "--"] + touched_prod)
                    critic_feedback = (
                        f"In a 'tests' phase, you only touch test files. You modified production "
                        f"code: {', '.join(touched_prod)}. These files have been restored. If a "
                        f"test reveals a real production bug, do NOT fix it: let the verification "
                        f"fail (a human will arbitrate)."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJECTED] Attempt {attempts}: production code modified in a 'tests' "
                          f"phase ({', '.join(touched_prod)}) — restored.")
                    RUNNER.new_context()
                    continue

            print(f"  → Coder finished ({len(touched_files)} declared file(s)). Verification by EXECUTION...")

            # ── BRICK A: the verdict IS the exit code. ──
            # Python runs the command itself; no LLM judges functional completeness.
            # An objective signal that neither the coder nor a verifier can hallucinate.
            # A TIMEOUT is NOT a red verdict (see the dedicated branch).
            is_ok, output, verify_timed_out = run_verify_resilient(verify_cmd)

            # ── YOLO · RED PATH ── : before handing a red back to the coder, the impact
            # triage tries to resolve it — breakage endorsed by the human → mechanical
            # deletion of the test and carry on; unplanned side effect → repairer (tests
            # frozen); a true conflict → human arbitration (impact-phase-<id>.md). If it
            # turns the suite green, the phase carries on below (LLM verifier included);
            # otherwise the freshest red output becomes the coder's feedback again. An
            # infra timeout keeps its dedicated branch (never treated as a red).
            if not is_ok and not verify_timed_out:
                is_ok, output = attempt_impact_resolution(
                    phase, blackboard, phase_need, output, attempts, verify_cmd, phase_start_sha)
            if is_ok:
                # ── NON-DECREASING TEST COUNT (mechanical §1.3 guard, best-effort) ──:
                # a green suite that LOST tests is a weakened suite, not a success.
                count_regression = test_count_regression(output, blackboard)
                if count_regression:
                    critic_feedback = count_regression
                    phase["critic_feedback"] = count_regression
                    save_blackboard(blackboard)
                    print(f"🛡️  [REJECTED] Attempt {attempts}: suite green but the passed-test "
                          f"count DECREASED.")
                    RUNNER.new_context()
                    continue

                # ── YOLO · PHASE LLM VERIFIER (green path) ── : a green suite proves
                # "nothing is broken", not "the phase delivered everything". An independent
                # fresh-context agent confronts the produced code with the phase's checklist
                # in the blackboard. It never stamps DONE: a rejection (or the absence of a
                # verdict) consumes the attempt and hands back to the coder — placed BEFORE
                # brick B so that no mutation run is paid for on an attempt that is going
                # to be rejected.
                print("🧐 Suite green: routing to the phase LLM Verifier (independent agent)...")
                RUNNER.new_context()
                RUNNER.send_task(build_phase_verifier_prompt(phase, blackboard, phase_need,
                                                             touched_files, attempts))
                if not wait_for_file_creation(verdict_sentinel(phase["id"], attempts)):
                    critic_feedback = ("The LLM verifier returned no verdict (timeout): the "
                                       "phase's compliance with its checklist could not be "
                                       "confirmed. Check that EVERY checklist task is actually "
                                       "delivered, then recreate the completion sentinel.")
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"⏱️  [REJECTED] Attempt {attempts}: no verdict from the LLM verifier.")
                    RUNNER.new_context()
                    continue
                verdict_ok, verdict_feedback = read_verdict(phase["id"], attempts)
                if not verdict_ok:
                    critic_feedback = verdict_feedback
                    phase["critic_feedback"] = verdict_feedback
                    save_blackboard(blackboard)
                    print(f"🧐 [REJECTED] Attempt {attempts}: suite green but the LLM verifier "
                          f"finds gaps against the blackboard checklist:\n{verdict_feedback}")
                    RUNNER.new_context()
                    continue
                print("🧐 LLM verifier: the phase delivered its whole checklist (compliant).")

                # ── BRICK B: does the suite BITE? (targeted mutation testing, §6.4) ──:
                # the suite is green; now we prove it turns RED when the code is wrong. Acts only
                # on 'tests' phases, and only after a green suite (mutating code whose tests are red
                # is pointless). Verdict = the tool's exit code; no LLM judges. Graceful degradation
                # everywhere (tool absent / timeout → warn, never a reject nor a stop). ONE single
                # hardening pass per phase (§5 pt 6): beyond that, we validate and signal (do not
                # relentlessly push a small model that cannot harden).
                if nature == "tests":
                    mcmd = resolve_mutation_cmd(phase, blackboard)
                    targets = build_mutation_targets(phase)
                    if not mcmd:
                        print("ℹ️  Brick B inactive (no 'mutation_cmd' declared).")
                    elif "{targets}" in mcmd and not targets:
                        print("⚠️  Brick B: no mutable target (files_to_read empty or missing) — skipped.")
                    elif not mutation_tool_available(mcmd):
                        print("⚠️  Brick B: mutation tool not found — skipped (graceful degradation).")
                    else:
                        run_cmd = mcmd.replace("{targets}", " ".join(shlex.quote(t) for t in targets)) if "{targets}" in mcmd else mcmd
                        print("🧬 Brick B: suite passes — checking that it BITES (targeted mutation)...")
                        mut_started = time.time()
                        ok_mut, mout, mut_timed_out = run_mutation(run_cmd)
                        print(f"   ⏱️  Brick B: mutation finished in {time.time() - mut_started:.0f}s.")
                        if mut_timed_out:
                            mutation_timeouts += 1
                            print(f"⏱️  Brick B: mutation timed out ({MUTATION_TIMEOUT}s) — ignored "
                                  f"({mutation_timeouts}/{MAX_PHASE_MUTATION_TIMEOUTS}), phase validated on "
                                  f"the universal verdict. We do NOT relaunch the coder (the suite is green, "
                                  f"only the tool stalled): graceful degradation, run never lengthened without bound.")
                        elif not ok_mut and mutation_hardening_used < 1:
                            mutation_hardening_used += 1
                            critic_feedback = (
                                "The suite PASSES but does not BITE: mutants survived (hollow tests). "
                                "Strengthen the ASSERTIONS to kill these mutations (test boundaries, "
                                "return values, branches), without adding I/O or a trivial test:\n"
                                + truncate_output(mout))
                            phase["critic_feedback"] = critic_feedback
                            save_blackboard(blackboard)
                            print(f"🧬 [REJECTED] Attempt {attempts}: mutants survive — "
                                  f"test hardening requested (single pass).")
                            RUNNER.new_context()
                            continue
                        elif not ok_mut:
                            print("⚠️  Brick B: mutants still survive after 1 pass — we validate and "
                                  "signal (the model cannot harden; do not block the run).")
                        else:
                            print("🧬 Brick B: the suite BITES (mutants killed). Phase truly validated.")

                record_test_count(output, blackboard, expect_growth=(nature == "tests"))
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCESS] Phase {phase['id']}: verification passes (exit code 0).")
                commit_phase(f"phase {phase['id']}: {phase['name']}")
                # Register this tests phase's deliverables as PROTECTED for later
                # feature phases (the diff covers the whole phase, every attempt).
                if nature == "tests" and _GIT["enabled"] and phase_start_sha:
                    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"])
                    if ok_diff:
                        protected = set(blackboard.get("protected_test_files") or [])
                        # Does NOT register orchestration artifacts committed during the phase
                        # (blackboard, prompts…): if protected, they would then stall every
                        # 'feature' phase via the protected_test_files guard.
                        protected.update(line.strip() for line in diff_out.splitlines()
                                         if line.strip() and not is_orchestration_file(line.strip()))
                        blackboard["protected_test_files"] = sorted(protected)
                        save_blackboard(blackboard)
            elif verify_timed_out:
                # Infra timeout, not a code failure: we do NOT consume the attempt (otherwise a
                # few machine slowdowns would exhaust the coder's MAX_ATTEMPTS). We replay the
                # same attempt after reset, with an anti-loop guard if the infra is durably broken.
                verify_timeouts += 1
                if verify_timeouts >= MAX_PHASE_VERIFY_TIMEOUTS:
                    critic_feedback = (
                        f"Verification \"{verify_cmd}\" timed out ({VERIFY_TIMEOUT}s) repeatedly "
                        f"({verify_timeouts}x): an INFRASTRUCTURE incident, not a code failure. "
                        f"Check the machine or the command, then relaunch."
                    )
                    print(f"🛑 [INFRA TIMEOUT] Giving up phase {phase['id']} after {verify_timeouts} "
                          f"persistent timeouts (not {MAX_ATTEMPTS} code failures).")
                    break
                attempts -= 1  # attempt not consumed: it was not a code red
                print(f"⏱️  [INFRA TIMEOUT] Inconclusive verification (time limit exceeded). Attempt NOT "
                      f"consumed ({verify_timeouts}/{MAX_PHASE_VERIFY_TIMEOUTS}) — relaunch after reset.")
                RUNNER.new_context()
            else:
                critic_feedback = output
                phase["critic_feedback"] = output
                save_blackboard(blackboard)
                print(f"⚠️  [REJECT] Attempt {attempts}: verification fails. Output relayed to the coder:\n{output}")
                RUNNER.new_context()

        if not success:
            phase["status"]  = "TODO"
            phase["verdict"] = "REJECTED"
            phase["critic_feedback"] = critic_feedback
            save_blackboard(blackboard)
            cleanup_all_sentinels()
            print_failure_message(phase, blackboard, critic_feedback)
            write_fail_report(
                f"Phase {phase['id']} \"{phase['name']}\" did not converge after {MAX_ATTEMPTS} attempts",
                f"Last blocking point raised by the verification:\n{critic_feedback}",
                blackboard, details=critic_feedback)
            RUNNER.kill()
            sys.exit(1)

        RUNNER.new_context()


def verify_and_fix_after_refacto(blackboard: dict, user_need: str, verify_cmd: str) -> tuple:
    """Re-runs the GLOBAL SUITE after the refacto; on regression, a CORRECTION loop
    (execution feedback → fixer agent → re-verification), bounded by MAX_ATTEMPTS.

    Returns (ok, output, timed_out, fixes): 'fixes' = number of correction attempts launched
    (0 if the suite already passed). A persistent infra timeout is NOT treated as a regression
    (no fix attempted); it is surfaced as-is to the caller.
    """
    print("\n🧪 Post-refacto re-verification (global suite): the polish must not have broken the code...")
    ok, output, timed_out = run_verify_resilient(verify_cmd)
    # A green suite that LOST tests counts as a regression too (same §1.3 guard as
    # production): the fixer gets the count feedback instead of a runner output.
    count_regression = test_count_regression(output, blackboard) if ok else None
    attempts = 0
    while (not ok or count_regression) and not timed_out and attempts < MAX_ATTEMPTS:
        attempts += 1
        cleanup_sentinels(REFACTO_FIX_PHASE_ID)
        print(f"\n🔧 [REGRESSION FIX {attempts}/{MAX_ATTEMPTS}] The refacto broke the suite — targeted fix...")
        fix_prompt = build_refacto_fix_prompt(blackboard, user_need,
                                              count_regression or output, verify_cmd, attempts)
        RUNNER.new_context()
        mm_audit.event("agent_task", prompt_bytes=len(fix_prompt))
        RUNNER.send_task(fix_prompt)
        if not wait_for_file_creation(done_sentinel(REFACTO_FIX_PHASE_ID, attempts)):
            print("⏱️  The fixer did not signal completion (sentinel missing). Retrying.")
            RUNNER.new_context()
            continue
        ok, output, timed_out = run_verify_resilient(verify_cmd)
        count_regression = test_count_regression(output, blackboard) if ok else None
    cleanup_sentinels(REFACTO_FIX_PHASE_ID)
    final_ok = ok and not count_regression
    if final_ok:
        record_test_count(output, blackboard)
    return final_ok, count_regression or output, timed_out, attempts


def execute_final_refactoring(blackboard: dict, user_need: str):
    print(f"\n{'='*50}\n🛡️  STEP 5: REFACTORING & FINAL POLISH AGENT\n{'='*50}")

    # Rollback point (3b): the refacto is the last hand on a fully-green codebase.
    pre_refacto_sha = git_head_sha()

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
4. You NEVER delete NOR weaken an existing test to make the suite pass: if a test
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
    # signal — the agent may create it then keep modifying code while we re-verify.
    if wait_for_pipeline_file(REFACTO_REPORT_FILE, REFACTO_DONE_SENTINEL):
        print(f"✅ Refactoring report generated in '{REFACTO_REPORT_FILE}'.")
    else:
        print(f"⚠️  Timeout: '{REFACTO_REPORT_FILE}' not generated (the refacto may have modified code anyway).")

    # Clean up temporary files, whatever the refacto outcome.
    for tmp_f in [TMP_CODER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE, TMP_PLAN_FILE,
                  TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_sentinels()

    # ── POST-REFACTO RE-VERIFICATION + REGRESSION FIX (brick A all the way) ──
    # The refacto MODIFIES the code: it is the last hand on the codebase, and the only
    # production action that escaped the objective verdict. We re-run the GLOBAL SUITE; if the
    # polish introduced a regression, we do NOT stop dead: we launch a CORRECTION loop (same
    # logic as production: execution feedback → agent → re-verify), bounded by MAX_ATTEMPTS.
    # Definitive failure only after that (regression or persistent infra timeout).
    final_cmd = (blackboard.get("verify_cmd") or "").strip()
    if not final_cmd:
        print("⚠️  No global 'verify_cmd': post-refacto re-verification impossible, step skipped.")
        return

    ok, output, timed_out, fixes = verify_and_fix_after_refacto(blackboard, user_need, final_cmd)
    if ok:
        if fixes:
            print(f"✅ Post-refacto regression fixed (attempt {fixes}): the global suite passes again.")
        else:
            print("✓ Post-refacto re-verification OK: the polish introduced no detectable regression.")
        commit_phase("refacto: final polish")
        return

    reason = ("timed out repeatedly (INFRASTRUCTURE incident, not the code)"
              if timed_out else f"stays RED after {MAX_ATTEMPTS} fix attempt(s)")
    print(f"""
{'='*60}
❌ After the refacto, the global suite {reason}.
   The polish modifies the code; the suite « {final_cmd} » no longer passes and the
   automatic correction was not enough.

   Last output (truncated):
{output}

💡 Inspect the latest changes (see '{REFACTO_REPORT_FILE}') or fix/re-run the suite
   manually before shipping.
{'='*60}
""")
    # Rollback (3b) ONLY on a PROVEN persistent regression: an infra timeout proves
    # nothing against the polish, so the code is kept in that case. reset --hard
    # restores every tracked file; files CREATED by the refacto (untracked, including
    # the report) survive for inspection.
    if _GIT["enabled"] and pre_refacto_sha and not timed_out:
        ok_rollback, _ = run_git(["reset", "--hard", pre_refacto_sha])
        if ok_rollback:
            print(f"↩️  Refacto rolled back to {pre_refacto_sha[:8]}: the delivered code is the "
                  f"all-phases-green state. '{REFACTO_REPORT_FILE}' (untracked) survives for inspection.")
            mm_audit.event("guard", name="rollback_refacto", action="reset_hard")
    write_fail_report(
        "Post-refacto regression not resolved",
        f"After the refacto, the global suite {reason}. The automatic correction was not enough.",
        blackboard, details=output)
    RUNNER.kill()
    sys.exit(1)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)

def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "coding", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan approval sentinel (spec.md deleted since) must never validate a FUTURE
    # validate a FUTURE spec: purge it before anything else. Same contract for the impact
    # review (Yolo).
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)
    if os.path.exists(IMPACT_APPROVED_SENTINEL) and not os.path.exists(IMPACT_FILE):
        os.remove(IMPACT_APPROVED_SENTINEL)

    # A residual failReport.md from a previous run must not be mistaken for the current
    # run's (part D, §6.8): purge it at startup, like the residual refactoring_report.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

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

    # Step 2bis (Yolo): impact review of the plan on the EXISTING code, validated by the HUMAN.
    # The breakages endorsed HERE will be handled without another stop during production (the
    # covered red test is deleted mechanically by the orchestrator). Same resume states as the
    # spec: no review → generation + confirmation; review never approved (run interrupted
    # during the y/n) → ask again; review approved → step skipped.
    if not os.path.exists(IMPACT_FILE):
        generate_impact_review_tui()
        confirm_impact_with_human()
        RUNNER.new_context()
    elif not os.path.exists(IMPACT_APPROVED_SENTINEL):
        print(f"🔄 Existing '{IMPACT_FILE}' found but NEVER approved (interrupted run?).")
        confirm_impact_with_human()
    else:
        print(f"🔄 Existing '{IMPACT_FILE}' found (approved by the human). Step skipped.")

    # Step 3: Blackboard configuration via TUI
    if not os.path.exists(BLACKBOARD_FILE):
        blackboard = transform_plan_to_blackboard_tui()
        RUNNER.new_context()
    else:
        print(f"🔄 Existing '{BLACKBOARD_FILE}' found. Loading...")
        try:
            blackboard = load_blackboard()
        except Exception as err:
            # The blackboard is the resume state: if it is unreadable (corrupt YAML, e.g. a kill
            # during an earlier write), we stop CLEANLY with a clear message rather than crash on
            # a raw traceback or resume from a dubious state.
            print(f"❌ '{BLACKBOARD_FILE}' present but unreadable (invalid or corrupt YAML): {err}")
            print(f"   → Fix or delete '{BLACKBOARD_FILE}', then relaunch "
                  f"(it will be regenerated from '{PLAN_FILE}').")
            write_fail_report(
                "Blackboard unreadable at startup",
                f"'{BLACKBOARD_FILE}' is present but unreadable (invalid or corrupt YAML): {err}. "
                f"Fix or delete this file then relaunch.")
            RUNNER.kill()
            sys.exit(1)

    # The "need" context injected into production agents is the refined, validated SPEC
    # (testable acceptance criteria); need.md is only a fallback (old runs).
    # need_is_spec conditions the per-US slicing (extract_spec_slice) during production.
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Guardrail: the blackboard is produced by a fallible small LLM. We validate the structure
    # BEFORE paying for a whole run. STRUCTURAL gaps (no phases, a phase without id/name/tasks,
    # no global 'verify_cmd') would crash production or false-green it: we stop on them (BLOCKING
    # error). Non-critical fields (global_rules & its keys, filled by apply_blackboard_defaults;
    # display-only 'project') are merely reported. The human y/n stays the net on command CONTENT.
    # The validate → summary → y/n sequence LOOPS: the human can edit the blackboard in another
    # terminal while the prompt waits, but production runs on this in-memory dict and
    # save_blackboard() rewrites the file from it — an edit not reloaded before the 'y' would be
    # ignored then silently overwritten. Any file change during the prompt therefore triggers a
    # reload, a re-validation and a fresh confirmation.
    while True:
        fatal, soft = validate_blackboard_schema(blackboard)
        if soft:
            print("\nℹ️  Non-critical fields absent (filled automatically):")
            for problem in soft:
                print(f"   - {problem}")
        if fatal:
            print("\n❌ The blackboard has STRUCTURAL anomalies:")
            for problem in fatal:
                print(f"   - {problem}")
            print(f"   → Fix '{BLACKBOARD_FILE}' then relaunch: starting production on an incoherent "
                  f"blackboard guarantees a failure or a false green.")
            write_fail_report(
                "Structurally invalid blackboard",
                "The blackboard has STRUCTURAL anomalies that would make the run fail or false-green.",
                blackboard, details="\n".join(f"- {p}" for p in fatal))
            RUNNER.kill()
            sys.exit(1)
        apply_blackboard_defaults(blackboard)

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
        print(f"   Project: {blackboard.get('project', '(untitled)')}")
        print(f"   Stack (global_rules.target): {blackboard['global_rules']['target']}")
        print(f"   Universal verdict (verify_cmd): {blackboard.get('verify_cmd') or '⚠️  MISSING'}")
        print(f"   Phases: {len(blackboard['phases'])}")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers', []))
            own_cmd = (p.get('verify_cmd') or '').strip()
            extra = f" — specific verify: {own_cmd}" if own_cmd else ""
            print(f"   Phase {p['id']}: {p['name']} [{skills}] "
                  f"({len(p.get('tasks', []))} tasks; covers: {covers or '?'}){extra}")
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
            write_fail_report(
                "Blackboard unreadable after manual edit",
                f"'{BLACKBOARD_FILE}' was edited during the prompt but is now unreadable "
                f"(invalid or corrupt YAML): {err}. Fix this file then relaunch.")
            RUNNER.kill()
            sys.exit(1)

    validate_all_skills(blackboard)

    # Git safety net (best-effort): baseline BEFORE the scaffold, then one commit per
    # green phase (per-phase diff, test-file protection, refacto rollback, audit trail).
    ensure_phase_repo()

    # Run baseline: everything that differs from this sha is the factory's work (scaffold +
    # phases), never pre-existing legacy. Persisted because a RESUME would recapture an
    # already-advanced HEAD, and the refactoring would then miss earlier phases' files.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    # Step 0: executable skeleton (hard prerequisite of execution-based verification).
    ensure_executable_scaffold(blackboard, user_need)

    print(f"\n🚀 Starting active production: {blackboard.get('project', '')}")

    # Step 4: Production loop
    run_production_phases(blackboard, user_need, need_is_spec)

    # Step 5: Final polish
    execute_final_refactoring(blackboard, user_need)

    # Clean shutdown
    RUNNER.kill()
    # Successful run: no failure report should remain (part D, §6.8).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)
    # Successful run: nothing left to resume, so purge the spec-approval marker. Kept out of
    # cleanup_all_sentinels (which also runs mid-flight) because it must survive an INTERRUPTION;
    # here we are on the success path, so removing it is safe.
    if os.path.exists(SPEC_APPROVED_SENTINEL):
        os.remove(SPEC_APPROVED_SENTINEL)
    # Yolo: same contract for the impact review's approval; 'impact.md' and any
    # 'impact-phase-<id>.md' do stay (audit trail of the arbitrations, committed).
    if os.path.exists(IMPACT_APPROVED_SENTINEL):
        os.remove(IMPACT_APPROVED_SENTINEL)
    print("\n🏁 [CONGRATULATIONS] The Coding factory validated everything (mechanical verdicts + LLM verifier) in a single run!")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    AGENT_CONFIG_FILE=AGENT_CONFIG_FILE,
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    GITIGNORE_BODY=GITIGNORE_BODY,
    IMPACT_DONE_SENTINEL=IMPACT_DONE_SENTINEL,
    IMPACT_FILE=IMPACT_FILE,
    IMPACT_PHASE_PREFIX=IMPACT_PHASE_PREFIX,
    MAX_ATTEMPTS=MAX_ATTEMPTS,
    MAX_VERIFY_RETRIES_ON_TIMEOUT=MAX_VERIFY_RETRIES_ON_TIMEOUT,
    PIPELINE_SKILLS=PIPELINE_SKILLS,
    PLAN_FILE=PLAN_FILE,
    POLL_INTERVAL=POLL_INTERVAL,
    REFACTO_FIX_PHASE_ID=REFACTO_FIX_PHASE_ID,
    REQUIRED_GLOBAL_RULES=REQUIRED_GLOBAL_RULES,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    SPEC_FILE=SPEC_FILE,
    TMP_CODER_FILE=TMP_CODER_FILE,
    TMP_IMPACT_FILE=TMP_IMPACT_FILE,
    TMP_REPAIR_FILE=TMP_REPAIR_FILE,
    TMP_TRIAGE_FILE=TMP_TRIAGE_FILE,
    TMP_VERIFIER_FILE=TMP_VERIFIER_FILE,
    US_HEADING_RE=US_HEADING_RE,
    _GIT=_GIT,
    _PHASE_STATUS_SEEN=_PHASE_STATUS_SEEN,
    _TEST_COUNT=_TEST_COUNT,
    cleanup_pipeline_sentinel=cleanup_pipeline_sentinel,
    parse_skill_frontmatter=parse_skill_frontmatter,
    run_git=run_git,
    wait_for_pipeline_file=wait_for_pipeline_file,
    write_fail_report=write_fail_report,
)


if __name__ == "__main__":
    main()
