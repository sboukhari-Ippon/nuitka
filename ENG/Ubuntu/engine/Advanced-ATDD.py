#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - Code factory with an agent harness + tmux (Full TUI Data Center Version)
─────────────────────────────────────────────────────────────────────────────
"YOLO-ATDD" VARIANT (ATDD + Yolo overlay: impact review, LLM verifier, breakage triage).

Derives from Safe-ATDD.py (full copy, the base is NOT modified), enriched with the
Yolo overlay — the arbitration logic of Guided-Fix.py moved INTO the run:
  - Step 2bis (upstream): an Impact Review Agent crosses 'plan.md' with the project's
    EXISTING code and lists in 'impact.md' the current behaviors the plan is going to
    BREAK; the HUMAN endorses them at a dedicated gate BEFORE production (nobody
    discovers mid-run that the evolution blows the application up).
  - Green path (batch closing): after the mechanical verdict, an independent LLM Verifier
    (fresh context) confronts the code actually produced with the phase's checklist in the
    blackboard — a green suite proves "nothing is broken", not "the batch delivered
    everything". It never stamps DONE: a rejection consumes one coder attempt.
  - Red path (batch closing): a Triage Agent confronts the failing tests with 'impact.md'
    — ENDORSED breakage → the test is deleted by the ORCHESTRATOR (never by an agent,
    accounting adjusted) and the flow continues; UNPLANNED breakage → a Repairer Agent
    fixes the side effect (failing tests FROZEN, the phase's behavior still required);
    a TRUE conflict (the two behaviors exclude each other) → 'impact-phase-<id>.md' is
    arbitrated by the human at a mid-run gate (accepted → mechanical deletion; rejected →
    the historical behavior prevails, the fix is recorded). The REJECTED + failReport.md
    net after MAX_ATTEMPTS is unchanged — it should just become rare.
  - Scope: the 'atdd-test' phases (inverted verdict: a red there is a success) and the
    intermediate 'atdd-impl' steps (compilation alone) are UNCHANGED — the overlay only
    applies to the phases that CLOSE a batch (green verdict expected).

Reminder of the ATDD base (unchanged) — difference with the "TDD" variant Safe-TDD.py:
  - The plan is no longer split into red → green cycles per behavior but into BATCHES per
    USER STORY: for each story, an 'atdd-test' phase (write THE story's ACCEPTANCE test
    suite, derived one for one from its acceptance criteria, black-box against the public
    contract set by the Architect) followed by ONE OR MORE 'atdd-impl' phases (bounded
    implementation steps: one fresh-context agent instance per phase). This split is
    decided as early as the PLAN (ATDD Architect Agent, skill 'plan-atdd') then copied
    into the blackboard (fields 'nature' and 'cycle' = batch number of each phase, skill
    'plan-to-blackboard-atdd').
  - The verdict of an 'atdd-test' phase is INVERTED, like TDD's red: the orchestrator
    runs the universal verdict (compilation + full suite) and VALIDATES the phase when it
    FAILS. Since production code is FROZEN during the phase (git guard), the tests of the
    previous batches PROTECTED, and the suite green at the previous batch's closing, a
    failure is mechanically attributable to the new acceptance tests: the proof that
    they are falsifiable.
  - The verdict of an 'atdd-impl' phase depends on its POSITION in the batch (a
    mechanical position decision, never an LLM inference): an INTERMEDIATE step is
    validated by the COMPILATION ALONE ('build_cmd', production without the tests — the
    batch's acceptance suite is ALLOWED to stay red as long as the batch is not closed);
    the LAST phase of the batch CLOSES it and carries the standard universal verdict
    (full suite green). The test files are FROZEN during ALL the implementation phases
    (git guards): it is the acceptance test that commands.
  - The third beat (refactor) stays MUTUALIZED at the end of the run: step 5 (global
    refactoring re-verified, with git rollback on a persistent regression).
  - Brick B (mutation testing) stays a warn-only SIGNAL, run at the CLOSING of each
    batch and targeted at the WHOLE BATCH's implementation (diff since the end of its
    test phase, '_story_shas' milestone persisted in the blackboard): the acceptance
    suite must bite the story's FINAL implementation. Surviving mutants remain a quality
    signal addressed to the HUMAN (the agents are not allowed to harden the tests,
    frozen).

PO → ATDD Architect pipeline:
  - Step 1: a PO Agent refines 'need.md' into a business specification 'spec.md',
    VALIDATED by the human. In ATDD mode, its acceptance criteria (Given / When / Then)
    are THE contract: each describes a behavior observable from the OUTSIDE of the
    deliverable and will become ONE automated acceptance test as it is.
  - Step 2: an ATDD Architect Agent converts 'spec.md' into a plan by BATCHES where each
    phase EXPLICITLY declares its nature ('atdd-test'/'atdd-impl') and its Batch number,
    sets the PUBLIC CONTRACT targeted by the acceptance tests, and declares the TWO
    verdict commands: the universal verdict AND the production-only compilation.
  - Step 3: the blackboard conversion stays a MECHANICAL copy of these decisions
    (zero inference asked of the small model, which only compiles the format).
    The batch structure (one CONTIGUOUS block per batch: a test phase THEN its
    implementation phases, never a batch without implementation) is VALIDATED
    mechanically before production: a blackboard that violates it is REFUSED.

Data Center & TUI Strategy (unchanged):
  - The tmux session is initialized DIRECTLY at startup.
  - We directly launch the chosen harness TUI (Cloud / Data Center model).
  - Steps 1 (PO Spec), 2 (Plan) and 3 (Blackboard) are executed directly in the TUI.
  - Production: each phase goes through a Coder Agent, then the orchestrator RUNS the
    phase's command itself; the exit code IS the verdict (brick A), interpreted
    according to the nature AND the position in the batch (failure expected on test,
    compilation required on an intermediate step, green suite required at the closing).
    The coder communicates via a sentinel file ('.phase_<id>.attemptN.done'); the sole
    owner of the blackboard is the Python orchestrator (no concurrent writes).

Accepted residual risks (in addition to those of the TDD variant — fabricated test
arbitrated by the human, ghost coder, etc.):
  - an INTERMEDIATE implementation step is judged on the compilation only: it can break
    a behavior of a previous batch without immediate detection — the batch's CLOSING
    (full suite green) mechanically catches it, at the cost of later feedback for the
    closing coder;
  - the "compilation only" promise assumes a 'build_cmd' that does NOT compile the test
    files (mvn -q compile, go build ./..., cargo build…): a build_cmd that compiles them
    would stay red as long as the whole API expected by the acceptance tests does not
    exist, and would block the intermediate steps (lead restated in the failure
    report).
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
    append_arbitration, apply_blackboard_defaults, build_correction_prompt, build_phase_verifier_prompt,
    build_refacto_fix_prompt, build_repair_prompt, build_skills_dictionary, build_triage_prompt,
    collect_spec_us_ids, commit_phase, correction_sentinel, done_sentinel,
    ensure_phase_repo, fail_pipeline, files_changed_since_phase_start, generate_impact_review_tui,
    git_head_sha, impact_phase_file, inject_skills_dictionary, load_blackboard,
    load_skills, lot_closing_ids, mutation_tool_available, no_declared_file_touched,
    parse_test_count, read_repair_outcome, read_touched_files, read_triage,
    read_verdict, repair_sentinel, resolve_build_cmd, resolve_mutation_cmd,
    resolve_verify_cmd, restore_test_files, run_mutation, run_verify,
    run_verify_resilient, save_blackboard, signal_handler, test_count_regression,
    test_phase_damage, triage_sentinel, truncate_output, validate_all_skills,
    verdict_sentinel, wait_for_file_creation,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="yolo-atdd")

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
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard-atdd/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-atdd/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Pipeline system skills: never routed to production phases.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-proto", "plan-tdd", "plan-atdd",
                         "plan-to-blackboard", "plan-to-blackboard-proto",
                         "plan-to-blackboard-tdd", "plan-to-blackboard-atdd", "refacto"}

# ATDD-mode phase natures, decided by the Architect as early as the plan and copied by the
# blackboard compiler. They drive EVERYTHING: the coder's mission (acceptance tests /
# implementation step), the git guards (production frozen in test, tests frozen in impl)
# and the VERDICT — failure of the universal verdict expected after the test phase;
# compilation required on an intermediate implementation step; full suite green required
# on the phase that CLOSES the batch (a POSITION decision, cf. lot_closing_ids). Any other
# value is an invalid blackboard — validated mechanically before production.
TEST_NATURE           = "atdd-test"
IMPL_NATURE           = "atdd-impl"

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
MUTATION_TIMEOUT      = 300            # CAUTIOUS: bounded budget for mutation testing (brick B). In ATDD mode
                                       # brick B is a warn-only SIGNAL (never a retry): at most one run per
                                       # batch closing, any overrun degrades to a warn
SCAFFOLD_TIMEOUT      = 300            # 5 min: the scaffold is the shortest task of the run — if it
                                       # does not complete, the model's tool calling is almost always
                                       # the culprit, and a fast diagnosis beats a long wait
VERIFY_FEEDBACK_LIMIT = 4000           # max size of the verification feedback sent back to the coder
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net: pipeline deliverable accepted if it
                                       # stayed stable for N consecutive checks (N × POLL_INTERVAL seconds).
                                       # 30s: a slow local model pausing between two writes must not get
                                       # its half-written deliverable accepted (see structural_check too)

# TWO commands structure this script's verification, both declared by the ATDD Architect
# Agent in the plan and copied by the blackboard compiler, never by this script:
#   - the UNIVERSAL VERDICT ('verify_cmd': compilation + full suite), run after the test
#     phase of each batch (it must FAIL: the acceptance tests are red) and after the
#     phase that CLOSES the batch (it must SUCCEED: full suite green);
#   - the COMPILATION ALONE ('build_cmd': production without the tests), the verdict of
#     the INTERMEDIATE implementation steps of a batch (the tree compiles, the acceptance
#     suite is allowed to stay red until the closing).
# What changes between phases is therefore not only the SEMANTICS of the exit code but
# also the COMMAND that is run — decided by the phase's POSITION in its batch.


# ─── PHASE SENTINELS (CODER → ORCHESTRATOR CHANNEL) ────────────

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


def record_test_count(output: str, blackboard: dict, expect_growth: bool = False):
    """Persist the last parsable passed-test count in the blackboard (survives resumes).

    Called ONLY on GREEN suites (scaffold, batch closings, post-refacto): the last green
    state is the reference of the non-decreasing guards — a test phase, whose suite fails by
    construction, never records a count. A batch closed WITHOUT strictly increasing
    the count only gets a console warning: a weak signal, deliberately not a verdict
    (re-organizations happen).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if expect_growth and isinstance(old_count, int) and new_count <= old_count:
        print(f"⚠️  Batch closed green without increasing the suite ({old_count} → {new_count} "
              f"passing): are the acceptance tests added by this batch's test phase actually "
              f"discovered by the runner?")
    blackboard["last_test_count"] = new_count
    save_blackboard(blackboard)


# ─── VERIFICATION EXECUTION (BRICK A: EXECUTION = VERDICT) ────────────────────

# ─── BRICK B: TARGETED MUTATION TESTING (DOES THE SUITE BITE?) ────────────────
# Extension of brick A: the universal verdict proves "nothing is broken"; brick B proves
# "the suite turns RED when the code is wrong" (falsifiable tests). End-to-end mechanical —
# the mutation tool's exit code IS the verdict, no LLM judges. Driven by the Architect via an
# OPTIONAL 'mutation_cmd' field; absent → brick inactive (run identical to today). Graceful
# degradation everywhere (tool absent / timeout → warn, never a block).

def is_test_file(path: str) -> bool:
    """Best-effort naming heuristic: does 'path' look like a test file?

    Multi-language and agnostic (tests/__tests__/spec directories, conventions test_*.py,
    *_test.go, *.test.ts, *.spec.js, *Test.java/*Spec.kt). Deliberately WIDE on the test side:
    when in doubt we classify as test, so as NOT to stall a legitimate test phase on a false
    "modified production file" (the production freeze only restores what is NOT a test).
    Accepted trade-off in implementation (test freeze): a helper off-convention may be classified as
    production, a production file named like a test may be classified as test — the feedback
    names the files, the human arbitrates, exactly like protected_test_files.
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


# Files belonging to the ORCHESTRATOR itself (never code produced by the coder): buffer
# prompts, pipeline deliverables, blackboard, sentinels, Python caches, venv, agent configs
# and the MAIster-Mind script. They are rewritten every phase; NO 'git diff'-based guard must
# count them as "modified production code" nor restore them (git checkout) — otherwise the
# factory sabotages its own state, even its own script, and no test phase ever converges
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
    # Ephemeral sentinels and buffers, wherever they sit in the tree.
    if base.startswith(".phase_") or base.startswith(".pipeline_"):
        return True
    # Yolo: mid-run arbitration reports (impact-phase-<id>.md), written by the
    # orchestrator's agents — never produced code.
    if base.startswith(IMPACT_PHASE_PREFIX) and base.endswith(".md"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Python caches, virtual environment and tooling directories: never produced code.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def build_mutation_targets(phase: dict, since_sha: str = "") -> list:
    """PRODUCTION files to mutate after the CLOSING of a batch (green suite).

    Natural ATDD targeting: the WHOLE BATCH's implementation (git diff since the end of
    the batch's test phase — '_story_shas' milestone persisted in the blackboard —,
    working tree included), filtered on existing production files: that is exactly the
    code the story's acceptance suite must bite. The caller falls back to the start of
    the closing phase when the milestone is missing (old blackboard, git arrived along
    the way). Fallback without git or without an exploitable diff: the phase's
    'files_to_read' filtered on existence (better than a silently inactive brick B —
    they mostly list the batch's tests, hence the is_test_file filter).
    """
    out = sorted(
        f for f in files_changed_since_phase_start(since_sha)
        if os.path.exists(f) and not is_test_file(f) and not is_orchestration_file(f)
    )
    if out:
        return out
    for p in (phase.get("files_to_read") or []):
        clean = str(p).strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if clean and os.path.exists(clean) and not is_test_file(clean):
            out.append(clean)
    return out


# ─── BLACKBOARD SCHEMA VALIDATION (PRODUCED BY A FALLIBLE SMALL LLM) ───────────

REQUIRED_GLOBAL_RULES = ["target", "styling", "constraints", "accessibility"]


def validate_blackboard_schema(blackboard: dict) -> tuple:
    """Check the structure of the blackboard. Returns (fatal, soft).

    The blackboard comes out of a fallible small LLM; two classes of problems:
      - fatal: STRUCTURAL gaps on which production would crash (direct access `blackboard[...]`
        / `phase[...]`) or run empty (checklist with no tasks, no global 'verify_cmd' → scaffold
        skipped and fallback absent). The orchestrator MUST stop on these: paying for plan +
        architecture then launching a run doomed to fail is pointless.
      - soft: gaps recovered by apply_blackboard_defaults (global_rules and its keys, filled
        with "(unspecified)") or purely cosmetic ('project', display only): reported, not blocking.
    Writes nothing and fixes nothing: the orchestrator (and the human at the y/n) decides.
    """
    fatal, soft = [], []
    if not isinstance(blackboard, dict):
        return ["The blackboard is not a valid YAML mapping."], []
    if not blackboard.get("project"):
        soft.append("Missing 'project' field (display title only).")
    global_rules = blackboard.get("global_rules")
    if not isinstance(global_rules, dict):
        soft.append("Missing or invalid 'global_rules' block (will be filled with \"(unspecified)\").")
    else:
        for key in REQUIRED_GLOBAL_RULES:
            if key not in global_rules:
                soft.append(f"Missing 'global_rules.{key}' key (will be filled with \"(unspecified)\").")
    phases = blackboard.get("phases")
    if not isinstance(phases, list) or not phases:
        fatal.append("Missing or empty 'phases' block: nothing to produce.")
    else:
        for idx, phase in enumerate(phases):
            if not isinstance(phase, dict):
                fatal.append(f"phases[{idx}] is not a mapping.")
                continue
            if "id" not in phase:
                fatal.append(f"phases[{idx}].id missing (accessed directly in production).")
            if not phase.get("name"):
                fatal.append(f"phases[{idx}].name missing.")
            if not isinstance(phase.get("tasks"), list) or not phase.get("tasks"):
                fatal.append(f"phases[{idx}].tasks missing or empty: checklist with no content.")
        ids = [str(phase.get("id")) for phase in phases if isinstance(phase, dict) and "id" in phase]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            fatal.append(
                f"Duplicated phases[].id ({', '.join(duplicated)}): the "
                f"'.phase_N.attemptM.done' sentinels would be SHARED between two phases (false completion signals)."
            )
        elif ids and ids != [str(i) for i in range(1, len(ids) + 1)]:
            soft.append(
                f"phases[].id is not a contiguous 1..N sequence ({', '.join(ids)}): tolerated, "
                f"but check that the compiler did not skip or renumber a phase."
            )
        # ── ATDD BATCH STRUCTURE (structural) ── : the whole verdict rests on the
        # nature ('atdd-test' → the suite must fail; intermediate 'atdd-impl' → the
        # compilation must pass; last 'atdd-impl' of the batch → full suite green) and
        # on the batch structure (one CONTIGUOUS block per batch: a test phase THEN its
        # implementation phases) — the POSITION in the block decides which phase closes
        # the batch (cf. lot_closing_ids). A blackboard that violates them can only
        # produce a WRONG run (inverted verdict applied to the wrong phase, or a run
        # ending on a red suite): FATAL, never tolerated.
        bad_nature = sorted({str(phase.get("nature") or "(absent)").strip() or "(absent)"
                             for phase in phases if isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower()
                             not in (TEST_NATURE, IMPL_NATURE)})
        if bad_nature:
            fatal.append(
                f"phases[].nature outside {{{TEST_NATURE}, {IMPL_NATURE}}}: {', '.join(bad_nature)}. "
                f"In ATDD mode the nature drives the VERDICT (failure expected after the test phase, "
                f"compilation then green suite on the implementation phases): every phase "
                f"must declare one of the two."
            )
        missing_cycle = sorted(str(phase.get("id", "?")) for phase in phases
                               if isinstance(phase, dict)
                               and not str(phase.get("cycle") or "").strip())
        if missing_cycle:
            fatal.append(
                f"phases[].cycle missing (phases {', '.join(missing_cycle)}): the batch "
                f"structure (test phase → implementation phases) is checked by this batch "
                f"number, copied from the plan by the compiler."
            )
        if not bad_nature and not missing_cycle:
            # Split into contiguous blocks by batch number. A fragmented batch (non-
            # contiguous phases) is FATAL: the phase that closes a batch is recognized by
            # its POSITION (last of the block) — an interleaved phase would move the
            # universal verdict onto the wrong phase.
            blocks = []
            for phase in phases:
                if not isinstance(phase, dict):
                    continue  # non-mapping phase: already reported as fatal above
                cycle_id = str(phase.get("cycle"))
                if not blocks or blocks[-1][0] != cycle_id:
                    blocks.append((cycle_id, []))
                blocks[-1][1].append(phase)
            seen_cycles = set()
            multi_impl = False
            for cycle_id, block in blocks:
                if cycle_id in seen_cycles:
                    fatal.append(
                        f"Batch {cycle_id} FRAGMENTED: its phases are not contiguous in the "
                        f"blackboard. A batch = its '{TEST_NATURE}' phase immediately followed "
                        f"by ALL its '{IMPL_NATURE}' phases, with no phase in between."
                    )
                    continue
                seen_cycles.add(cycle_id)
                natures = [str(ph.get("nature") or "").strip().lower() for ph in block]
                if natures[0] != TEST_NATURE:
                    fatal.append(
                        f"Batch {cycle_id}: the first phase of the batch is '{natures[0]}' — a "
                        f"batch always OPENS with its '{TEST_NATURE}' phase (the user story's "
                        f"acceptance tests, written BEFORE the implementation)."
                    )
                if natures.count(TEST_NATURE) > 1:
                    fatal.append(
                        f"Batch {cycle_id}: {natures.count(TEST_NATURE)} "
                        f"'{TEST_NATURE}' phases — a batch carries only ONE (the story's whole "
                        f"acceptance suite), followed by its '{IMPL_NATURE}' phases."
                    )
                impl_count = natures.count(IMPL_NATURE)
                if impl_count == 0:
                    fatal.append(
                        f"Batch {cycle_id}: no '{IMPL_NATURE}' phase — a batch without "
                        f"implementation would end the run on a red suite."
                    )
                multi_impl = multi_impl or impl_count > 1
                covers_lists = [[str(c).strip().upper() for c in ph.get("covers")]
                                for ph in block
                                if isinstance(ph.get("covers"), list) and ph.get("covers")]
                if covers_lists and any(c != covers_lists[0] for c in covers_lists[1:]):
                    soft.append(
                        f"Batch {cycle_id}: \"Covers\" differs between the batch's phases — "
                        f"all the phases of a batch normally cover the same user story."
                    )
                if covers_lists and len(covers_lists[0]) > 1:
                    soft.append(
                        f"Batch {cycle_id}: the batch covers several user stories "
                        f"({', '.join(covers_lists[0])}) — prefer one batch per US (tighter "
                        f"acceptance suite and implementation scope). Tolerated, "
                        f"informational."
                    )
            # 'build_cmd' is the VERDICT of the intermediate implementation steps: as soon
            # as a batch has several of them, its absence makes those phases unverifiable.
            if multi_impl and not (blackboard.get("build_cmd") or "").strip():
                fatal.append(
                    "Missing compilation command 'build_cmd' while at least one batch has "
                    "several implementation phases: it is what VALIDATES the intermediate "
                    "steps (the tree compiles, the acceptance suite is allowed to stay "
                    "red). Without it, those phases are unverifiable."
                )
            elif not (blackboard.get("build_cmd") or "").strip():
                soft.append(
                    "No 'build_cmd' declared: tolerated (each batch has a single "
                    "implementation phase, all carry the universal verdict), but declare it "
                    "in the plan before re-splitting a batch into several steps."
                )
        with_own_cmd = sorted(str(phase.get("id", "?")) for phase in phases
                              if isinstance(phase, dict) and (phase.get("verify_cmd") or "").strip())
        if with_own_cmd:
            soft.append(
                f"Phases with their own 'verify_cmd' ({', '.join(with_own_cmd)}): ATDD mode "
                f"does not expect any. On a test phase or a phase that closes its batch, it "
                f"replaces the universal verdict (with the semantics of the position); on an "
                f"intermediate implementation step it is IGNORED (verdict = "
                f"compilation). Check that it is intended."
            )
        # Brick B (informational): without 'mutation_cmd', the signal "does the acceptance
        # suite bite the batch's FINAL implementation?" (beyond the initial red proven by
        # each test phase) will be inactive at batch closings. Tolerated (optional brick).
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if not has_mutation_cmd:
            soft.append(
                "No 'mutation_cmd' declared: brick B (warn-only signal \"do the acceptance "
                "tests bite the batch's final implementation?\") will be inactive at "
                "batch closings. Tolerated; declare it in the plan for falsifiable "
                "end-to-end tests."
            )
    if not (blackboard.get("verify_cmd") or "").strip():
        fatal.append(
            "Missing global verification command 'verify_cmd': it is the fallback for phases "
            "without their own 'verify_cmd' AND the lock of the scaffold step. Without it, the "
            "scaffold is skipped and a phase with no dedicated command cannot be verified."
        )
    return fatal, soft


# ─── PER-PHASE SPEC SLICING (CONTEXT WINDOW) ──────────────────────────────────
# The old "regression coverage" heuristic (guessing on a free string whether the last
# tests phase ran the full suite) is REMOVED: the universal verdict (every phase =
# compilation + full suite) makes the coverage structural.

# Heading of a user story in the PO spec (e.g. "### US-1: Balance computation").
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Slice of the spec limited to the phase's covered US (+ everything outside US sections).

    The coder prompt used to embed the WHOLE spec at every phase: on a large spec, every
    phase paid the full context cost. We only keep here the '### US-n' sections listed in
    'covers' (field copied from the plan by the blackboard compiler), plus everything that
    is not a US section (business goal, constraints, out-of-scope, assumptions). Small-model
    prudence: if 'covers' is empty, if the spec does not follow the US format, or if no
    covered US is found in it, return the WHOLE spec (graceful degradation — never starve
    the coder of context out of zeal).
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

    Two directions:
      - a phase references a US absent from the spec: probable hallucination of the
        blackboard compiler (same family as hallucinated skills);
      - a US of the spec is covered by no phase: requirement potentially FORGOTTEN by
        the Architect — this is the most precious warning.
    Warn-only: 'covers' is optional and the spec may not follow the US format;
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
- ATDD MODE: every acceptance criterion describes a behavior observable FROM THE OUTSIDE of the deliverable (input provided → result observable through its interface: return value, console output, HTTP response, display). Each criterion will become ONE automated acceptance test, as it is and without rewriting: a criterion that cannot be verified black-box is forbidden.
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
    print("\n📖 [STEP 2: ATDD ARCHITECT AGENT] Generating the ATDD-batch plan in Cloud TUI...")

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
- The plan MUST start with the "Stack & Verification" block (with the UNIVERSAL VERDICT verification command — compilation + full suite — AND the PRODUCTION-ONLY compilation command, the verdict of the intermediate implementation steps) and EVERY phase MUST declare its Nature (atdd-test/atdd-impl), its Batch and its "Covers" field (US-x): the next pipeline steps copy these decisions without inferring them.
- Break the specification down into ATDD BATCHES: for each user story, an 'atdd-test' phase (THE story's acceptance test suite, derived one for one from its acceptance criteria, written BLACK-BOX against the public contract YOU set in the plan, and which must FAIL against the current code) followed by ONE OR MORE 'atdd-impl' phases carrying the same Batch number; only the LAST phase of the batch must turn the full suite green again, the intermediate steps leave a tree that COMPILES.
- Each phase stays BOUNDED (1 to 5 tasks, at most 5 files created/modified, at most 3 files to read): one phase = one fresh-context agent instance. Add implementation phases to the batch rather than fattening one. Do not add any batch for a requirement absent from '{SPEC_FILE}'.
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

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str, skills_context: str,
                       critic_feedback: str, attempt: int, closes_lot: bool) -> str:
    verify_cmd = resolve_verify_cmd(phase, blackboard)
    build_cmd = resolve_build_cmd(phase, blackboard)

    # 'nature' is the ATDD Architect's decision, copied by the compiler and validated
    # mechanically before production (only TEST_NATURE or IMPL_NATURE here); the
    # POSITION in the batch ('closes_lot', computed by the orchestrator) distinguishes an
    # intermediate implementation step from the phase that closes the batch. Nature and
    # position drive the mission, the editing policy AND the verdict's command and semantics.
    nature = str(phase.get("nature") or "").strip().lower()
    cycle = phase.get("cycle", "?")
    if nature == TEST_NATURE:
        nature_line = (f"This phase OPENS ATDD batch {cycle}: you write THE ACCEPTANCE TEST "
                       "SUITE of the covered user story, BEFORE any implementation. "
                       "Derive each test case from an acceptance criterion (Given / "
                       "When / Then) of the need below — one criterion = at least one test — "
                       "BLACK-BOX: your tests go ONLY through the public contract described "
                       "by the checklist (signatures, endpoints, CLI…), never through the "
                       "internal details of an implementation that does not exist yet. Name and "
                       "place the files according to the runner's conventions so that they are "
                       "actually DISCOVERED and executed. Your tests must FAIL against the "
                       "current code because the behavior does not exist yet — never through "
                       "a fabricated failure (always-false assertion, deliberate fail(), test "
                       "writing error): a fabricated test would block the implementation "
                       "phases that follow, which are not allowed to fix it.")
    elif closes_lot:
        nature_line = (f"This phase CLOSES ATDD batch {cycle}: the batch's acceptance tests "
                       "describe the expected behavior of the user story — they are your "
                       "executable specification — and the batch's previous implementation "
                       "steps have already laid down their share. Read the tests first, then "
                       "implement the MINIMAL production code that is missing to make the WHOLE "
                       "suite pass (strict YAGNI: nothing beyond what the tests and the "
                       "checklist require). You write and modify NO test.")
    else:
        nature_line = (f"This phase is an IMPLEMENTATION STEP of ATDD batch {cycle}: the "
                       "batch's acceptance tests still fail and describe the expected FINAL "
                       "behavior — they are your executable specification, but it is NOT "
                       "your job to make them all pass: carry out ONLY the tasks of your "
                       "checklist (the rest of the batch belongs to the following phases) and "
                       "leave a tree that COMPILES. You write and modify NO test.")

    # Editing policy, driven by the nature (mechanical git guards): in a test phase the
    # production code is FROZEN (the red must come from the tests, not from sabotaging
    # production); in implementation it is the TEST files that are FROZEN — intermediate
    # steps INCLUDED (it is the acceptance test that commands, never the other way around).
    # The orchestrator enforces these policies by git restore.
    if nature == TEST_NATURE:
        prod_edit_policy = ("In an acceptance test phase, you only create and modify "
                            "test files: the production code is FROZEN (the orchestrator "
                            "restores by default any production file you would modify). "
                            "You also do not touch the tests of previous batches (protected, "
                            "restored by default): you ADD the tests of THIS batch.")
    else:
        prod_edit_policy = ("In an implementation phase, you create and modify NO test file "
                            "(the orchestrator restores or deletes by default any test edit "
                            "and rejects the attempt). You MAY modify the existing production "
                            "code if needed (a previous step of the batch "
                            "or an earlier batch may have left a bug to fix).")

    # Quality instructions and verdict, by nature and position: in a test phase the verdict
    # is INVERTED (the suite must fail BECAUSE OF the new acceptance tests); on an
    # intermediate step it bears on the COMPILATION alone; at the closing it is standard
    # (full suite green).
    if nature == TEST_NATURE:
        test_rules = ("Your tests must be EXECUTABLE and FAST: NO "
                      "Testcontainers, NO Docker and no network or database I/O.\n"
                      "Before writing your tests, READ the acceptance criteria of the need "
                      "slice below and the public contract described by the checklist: each "
                      "test expresses a precise expected BEHAVIOR, observable from the OUTSIDE "
                      "(never an always-true assertion, never an always-false assertion).")
        verdict_block = (f"The orchestrator automatically runs the verification command "
                         f"« {verify_cmd} » (universal verdict: compilation + full suite): "
                         f"it MUST FAIL (exit code ≠ 0) BECAUSE OF your new tests — "
                         f"this is the mechanical proof that they are falsifiable. If the suite "
                         f"stays green, the phase is REJECTED (your tests already pass or are not "
                         f"discovered by the runner). The pre-existing tests, for their part, "
                         f"must KEEP passing. This is your ONLY success criterion.")
    elif closes_lot:
        test_rules = ("You NEVER delete NOR weaken a test to make the "
                      "verification pass: if a test is red, it is the production code that "
                      "must be written or fixed.")
        verdict_block = (f"The orchestrator automatically runs the verification command "
                         f"« {verify_cmd} » (universal verdict: compilation + full suite): "
                         f"it MUST succeed (exit code 0), otherwise the phase is rejected. "
                         f"This is your ONLY success criterion.")
    else:
        test_rules = ("You NEVER delete NOR weaken a test: the batch's acceptance tests "
                      "are ALLOWED to stay red at this stage (the batch's last phase "
                      "will close them) — do not touch them for all that.")
        verdict_block = (f"The orchestrator automatically runs the compilation command "
                         f"« {build_cmd} » (production only): it MUST succeed (exit "
                         f"code 0), otherwise the phase is rejected. The test suite, for its "
                         f"part, is NOT run on this phase: the batch's acceptance tests "
                         f"may stay red. This is your ONLY success criterion.")

    role_label = ("ACCEPTANCE TESTS" if nature == TEST_NATURE
                  else "IMPLEMENTATION — BATCH CLOSING" if closes_lot
                  else "IMPLEMENTATION — INTERMEDIATE STEP")

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
You are an ultra-specialized Coder Agent for Phase {phase['id']} ONLY (ATDD batch {cycle}).
You implement ONLY the tasks of THIS phase and stop as soon as they are done.
Do NOT do work planned for other phases: each implementation step of the batch has
its own checklist, the other user stories belong to the following batches. YAGNI
principle: nothing that is not explicitly requested by this phase's checklist.

--- AUTOMATIC VERIFICATION OF THIS PHASE ---
{nature_line}
{prod_edit_policy}
{test_rules}
{verdict_block}

{context_block}{files_block}--- NEED (spec slice covered by this phase) ---
{user_need}

--- PHASE {phase['id']} GOAL ({role_label}, batch {cycle}): {phase['name']} ---
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


# ─── FAILURE MESSAGE ──────────────────────────────────────────────────────────


def print_failure_message(phase: dict, blackboard: dict, critic_feedback: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    nature = str(phase.get("nature") or "").strip().lower()
    closes_lot = phase.get("id") in lot_closing_ids(blackboard.get("phases") or [])
    # ATDD-SPECIFIC diagnosis leads: a test phase that never fails, an intermediate step
    # that never compiles and a closing that never converges do not have the same probable
    # causes — nor the same human remedy (since the agents are not allowed to touch the
    # tests, a faulty acceptance test is fixed by hand).
    if nature == TEST_NATURE:
        atdd_hint = ("   TEST phase: if the suite stays green, the model does not write "
                     "acceptance tests that\n   express the MISSING behavior (or places them outside "
                     "the runner conventions).\n")
    elif nature == IMPL_NATURE and closes_lot:
        atdd_hint = (f"   CLOSING phase: if the blockage comes from an acceptance test of batch "
                     f"{phase.get('cycle', '?')} being itself\n   faulty (wrong assertion, fabricated "
                     f"failure), fix that test YOURSELF — the agents\n   are not allowed to "
                     f"(tests frozen) — then relaunch.\n")
    elif nature == IMPL_NATURE:
        atdd_hint = ("   Implementation step: the verdict is the COMPILATION (build_cmd). If "
                     "it does not\n   converge, check that it compiles the production ONLY — a "
                     "command that\n   also compiles the tests stays red as long as the expected API "
                     "does not exist.\n")
    else:
        atdd_hint = ""
    print(f"""
{'='*60}
❌ Phase {phase['id']} "{phase['name']}" (batch {phase.get('cycle', '?')}) did not converge after {MAX_ATTEMPTS} attempts.

   Last blocking point raised by the verification:
   "{critic_feedback}"

{atdd_hint}💡 The current model ({model}) is stuck on this specific step.
   Most effective: relaunch after bringing in a model one notch above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the {done_count} already-validated phase(s) will be resumed
   automatically, you don't start from scratch. See you soon! 🚀
{'='*60}
""")


def write_fail_report(title: str, reason: str, blackboard: dict = None, details: str = ""):
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
        lines = ["# Failure report — Advanced-ATDD", "", f"## {title}", "", "### Cause", reason.strip(), ""]
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
        lines.append("Fix the cause above (or bring in a model one notch higher via /model or "
                     f"'{AGENT_CONFIG_FILE}'), then relaunch: the already-validated phases will "
                     "be resumed automatically.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── SCAFFOLD STEP (EXECUTABLE SKELETON + HEALTH TEST) ────────────────────────

def ensure_executable_scaffold(blackboard: dict, user_need: str):
    """Guarantee an executable project AND a GREEN suite before the first batch.

    Hard prerequisite of brick A, and DOUBLY of the inverted verdict: the suite must be
    green (even at a single health test) before each test phase so that a failure after it
    is attributable to the new acceptance tests. If the global verification command does not pass
    (missing toolchain/scaffold), a dedicated agent creates the minimal skeleton (build file
    + directory tree + a trivial health test), then we re-test. Early, readable failure
    rather than N red phases unrelated to their own logic.

    Idempotent: if verification already passes (resume after crash, or pre-bootstrapped
    project), the step is skipped without invoking any agent.
    """
    verify_cmd = (blackboard.get("verify_cmd") or "").strip()
    if not verify_cmd:
        print("⚠️  No global verification command: scaffold step skipped "
              "(execution-based verification will be inoperative).")
        return

    # RESUME: as soon as a phase is validated, the scaffold belongs to the past. ATDD
    # specificity that makes this short-circuit MANDATORY (and not just an economy): if the
    # run was interrupted in the MIDDLE of a batch (test phase validated, batch not closed
    # yet), the suite is RED BY CONSTRUCTION until the batch's closing — the "does the
    # command pass?" check below would wrongly conclude a broken chain, launch a scaffold
    # agent on an already-advanced project, then abort the run on a perfectly nominal
    # state.
    if any(isinstance(p, dict) and p.get("status") == "DONE" and p.get("verdict") == "OK"
           for p in blackboard.get("phases", []) or []):
        print("↩️  Resuming mid-production: scaffold step skipped (a phase is already "
              "validated; in the middle of a batch, the suite is red by construction until "
              "its closing).")
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

    # Position in the batch, computed ONCE by the orchestrator: the LAST phase of each
    # batch carries the universal verdict (full suite green), the intermediate
    # implementation steps are validated by the compilation alone.
    closing_ids = lot_closing_ids(blackboard["phases"])

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} already validated: {phase['name']}")
            continue

        # ATDD Architect's decisions (copied from the plan, validated before production):
        # the nature drives the git guards AND the verdict; the cycle (batch number) links
        # the test phase to its implementation phases, and the POSITION in the batch
        # decides the verdict command.
        nature = str(phase.get("nature") or "").strip().lower()
        cycle = phase.get("cycle", "?")
        closes_lot = phase.get("id") in closing_ids
        if nature == TEST_NATURE:
            icon = "🧪 acceptance tests"
        elif closes_lot:
            icon = "🏁 implementation (closing)"
        else:
            icon = "🔧 implementation"
        print(f"\n{'='*50}\n🛠️  PHASE {phase['id']}/{total} [batch {cycle} — {icon}] : {phase['name']}\n{'='*50}")

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

        # Defensive net: the schema validation already made a multi-step batch without
        # 'build_cmd' FATAL — but the blackboard may have been hand-edited since.
        build_cmd = resolve_build_cmd(phase, blackboard)
        if nature == IMPL_NATURE and not closes_lot and not build_cmd:
            print(f"❌ Phase {phase['id']}: intermediate implementation step without a "
                  f"compilation command ('build_cmd' on the phase or global). Fix '{BLACKBOARD_FILE}' "
                  f"then relaunch.")
            write_fail_report(
                f"Phase {phase['id']} \"{phase['name']}\" has no compilation command",
                f"This intermediate implementation step has no 'build_cmd' (on the phase or "
                f"global): it cannot be verified. Fix '{BLACKBOARD_FILE}' then relaunch.",
                blackboard)
            RUNNER.kill()
            sys.exit(1)

        attempts = 0
        verify_timeouts = 0
        success  = False
        critic_feedback = "First draft — no previous criticism."
        # Landmark for the per-phase diff (3c): empty without git.
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

            coder_prompt = build_coder_prompt(phase, blackboard, phase_need, skills_context,
                                              critic_feedback, attempts, closes_lot)
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

            # ── PROTECTION OF PREVIOUS BATCHES' TESTS (mechanical guard, best-effort) ──:
            # during a test phase, the VALIDATED tests of earlier batches are out of
            # bounds — the red must come from the tests ADDED by THIS batch, otherwise
            # the mechanical attribution of the failure collapses (and a test phase could
            # "prepare" an easy closing by weakening the existing code). The prompt-only
            # prohibition is unverifiable; this diff is not. Known false positive (a
            # legitimately shared test helper to extend): the feedback names the files,
            # the human arbitrates (cf. protected_test_files in the blackboard).
            if nature == TEST_NATURE and _GIT["enabled"]:
                protected = set(blackboard.get("protected_test_files") or [])
                if protected:
                    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                    touched_protected = sorted(set(diff_out.splitlines()) & protected) if ok_diff else []
                    if touched_protected:
                        run_git(["checkout", "--"] + touched_protected)
                        critic_feedback = (
                            f"You modified PROTECTED tests of previous batches during your "
                            f"acceptance test phase: {', '.join(touched_protected)}. They have been "
                            f"restored. A test phase ADDS the tests of ITS batch; the tests "
                            f"already green are untouchable."
                        )
                        phase["critic_feedback"] = critic_feedback
                        save_blackboard(blackboard)
                        print(f"🛡️  [REJECTED] Attempt {attempts}: protected tests modified "
                              f"({', '.join(touched_protected)}) — restored.")
                        RUNNER.new_context()
                        continue

            # ── PRODUCTION FREEZE IN A TEST PHASE (mechanical guard, best-effort) ──: a
            # test phase only modifies test files; the production code is FROZEN. Any
            # touched production file is restored (git checkout) and the attempt rejected.
            # Placed BEFORE the verification: we catch the cheat whatever the color of the
            # suite, and avoid a wasted verify on a state we will reject. It is THIS freeze
            # that makes the inverted verdict reliable: the suite was green at the previous
            # batch's closing and production did not move, so a failure can only come from
            # the new acceptance tests. Settled caveat: a test phase never "fixes"
            # production quietly, even to make its test writable — the implementation
            # belongs to the batch's following phases.
            if nature == TEST_NATURE and _GIT["enabled"]:
                ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                # Excludes the orchestrator's own files (prompts, blackboard, sentinels,
                # .pyc, its own script…), which it rewrites every phase: counting them as
                # "modified production code" would reject EVERY attempt of the test phase
                # and, worse, their restoration (git checkout below) would sabotage the
                # orchestrator's state — even its script. Cf. is_orchestration_file.
                touched_prod = sorted(f for f in diff_out.splitlines()
                                      if f.strip() and not is_test_file(f.strip())
                                      and not is_orchestration_file(f.strip())) if ok_diff else []
                if touched_prod:
                    run_git(["checkout", "--"] + touched_prod)
                    critic_feedback = (
                        f"In an acceptance test phase, you only touch test files. "
                        f"You modified production code: {', '.join(touched_prod)}. These "
                        f"files have been restored. Write only the batch's tests: "
                        f"the implementation belongs to the batch's following phases."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJECTED] Attempt {attempts}: production code modified in a "
                          f"test phase ({', '.join(touched_prod)}) — restored.")
                    RUNNER.new_context()
                    continue

            # ── TEST FREEZE IN IMPLEMENTATION (mirror of the production freeze, best-effort) ──:
            # in implementation — intermediate steps INCLUDED —, NO test file is created nor
            # modified; even a NEW test (thus unprotected) is rejected: writing tests is the
            # exclusive role of the batch's test phase, and an implementation that
            # "completes" or adapts the suite blurs everything (anti-cheat: the closing
            # cannot make the suite pass by tweaking the batch's tests). Tracked files →
            # restored; new files → deleted (the equivalent of restoration for a file that
            # did not exist). The feedback names everything, the human arbitrates borderline
            # helpers.
            if nature == IMPL_NATURE and _GIT["enabled"]:
                # Yolo: the tests deleted by the ORCHESTRATOR (endorsed breakage) do differ
                # from the phase start but are the work of no agent — restoring them here
                # would cancel a human arbitration.
                yolo_deleted = set(blackboard.get("_yolo_deleted_tests") or [])
                touched_tests = sorted(
                    f for f in files_changed_since_phase_start(phase_start_sha)
                    if f.strip() and is_test_file(f.strip()) and not is_orchestration_file(f.strip())
                    and f.strip() not in yolo_deleted)
                if touched_tests:
                    ok_tracked, tracked_out = run_git(["ls-files", "--"] + touched_tests)
                    tracked = set(tracked_out.splitlines()) if ok_tracked else set()
                    to_restore = sorted(f for f in touched_tests if f in tracked)
                    if to_restore:
                        run_git(["checkout", "--"] + to_restore)
                    for f in touched_tests:
                        if f not in tracked:
                            try:
                                os.remove(f)
                            except OSError:
                                pass
                    critic_feedback = (
                        f"In an implementation phase, you write and modify NO test; you "
                        f"touched: {', '.join(touched_tests)}. Everything has been restored or deleted — "
                        f"writing the tests is the exclusive role of the batch's test phase. "
                        f"Implement the PRODUCTION code your checklist requests, with the "
                        f"suite as it is."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJECTED] Attempt {attempts}: test files created/modified in an "
                          f"implementation phase ({', '.join(touched_tests)}) — "
                          f"restored/deleted.")
                    RUNNER.new_context()
                    continue

            print(f"  → Coder finished ({len(touched_files)} declared file(s)). Verification by EXECUTION...")

            # ── BRICK A: the verdict IS the exit code. ──
            # Python runs the command itself; no LLM judges functional completeness.
            # An objective signal that neither the coder nor a verifier can hallucinate. The
            # COMMAND and the semantics of its exit code depend on the nature AND on the
            # position in the batch: universal verdict that must FAIL after the test phase,
            # compilation alone that must succeed on an intermediate step, universal verdict
            # that must succeed at the batch's closing. A TIMEOUT is NOT a verdict (neither
            # red nor green): dedicated branch below.
            phase_cmd = verify_cmd if (nature == TEST_NATURE or closes_lot) else build_cmd
            is_ok, output, verify_timed_out = run_verify_resilient(phase_cmd)

            if verify_timed_out:
                # Infra timeout, not a verdict: we do NOT consume the attempt (otherwise a
                # few machine slowdowns would exhaust the coder's MAX_ATTEMPTS). We replay the
                # same attempt after reset, with an anti-loop guard if the infra is durably
                # broken. Holds for ALL positions: a timeout proves a legitimate red (test
                # phase) no more than a compilation or a green suite (implementation).
                verify_timeouts += 1
                if verify_timeouts >= MAX_PHASE_VERIFY_TIMEOUTS:
                    critic_feedback = (
                        f"Verification « {phase_cmd} » timed out (timeout {VERIFY_TIMEOUT}s) "
                        f"repeatedly ({verify_timeouts}x): an INFRASTRUCTURE incident, not "
                        f"a code failure. Check the machine or the command, then relaunch."
                    )
                    print(f"🛑 [INFRA TIMEOUT] Giving up phase {phase['id']} after {verify_timeouts} "
                          f"persistent timeouts (not {MAX_ATTEMPTS} code failures).")
                    break
                attempts -= 1  # attempt not consumed: it was not a code verdict
                print(f"⏱️  [INFRA TIMEOUT] Inconclusive verification (time limit exceeded). Attempt NOT "
                      f"consumed ({verify_timeouts}/{MAX_PHASE_VERIFY_TIMEOUTS}) — relaunch after reset.")
                RUNNER.new_context()
                continue

            if nature == TEST_NATURE:
                # ── INVERTED VERDICT OF THE TEST PHASE ──: the phase is validated when the
                # suite FAILS. The production code is frozen (guard above), the tests of
                # previous batches protected, and the suite was green at the previous
                # batch's closing: a failure is therefore mechanically attributable to the
                # NEW acceptance tests — proof of falsifiability, the heart of ATDD. A
                # GREEN suite means the tests express nothing new (or are not discovered
                # by the runner): rejection.
                if is_ok:
                    critic_feedback = (
                        "The verification suite PASSES while your acceptance test phase "
                        "must make it FAIL. Probable causes: your tests already pass (they "
                        "do not test the NEW behavior requested by the checklist), they are "
                        "not discovered by the runner (name or location outside "
                        "conventions), or their assertions are hollow. "
                        "Write tests that express the EXPECTED behavior of the acceptance "
                        "criteria and that fail against the CURRENT code."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🟢 [REJECTED] Attempt {attempts}: the suite stays GREEN — the new "
                          f"acceptance tests do not fail (red not reached).")
                    RUNNER.new_context()
                    continue
                damage = test_phase_damage(output, blackboard)
                if damage:
                    critic_feedback = damage
                    phase["critic_feedback"] = damage
                    save_blackboard(blackboard)
                    print(f"🛡️  [REJECTED] Attempt {attempts}: suite red but EXISTING tests "
                          f"were broken (the red must come from the new tests only).")
                    RUNNER.new_context()
                    continue
                # Red reached. We do NOT record a test count (suite red: the last GREEN
                # state remains the reference of the non-decreasing guards).
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"🧪 [SUCCESS] Phase {phase['id']}: the suite fails as expected — the "
                      f"acceptance tests of batch {cycle} are falsifiable (red reached).")
                # A test phase's commit deliberately captures a suite-red state: it is
                # the journal of the ATDD batch, and the HEAD landmark against which the
                # implementation phases will measure their diff.
                commit_phase(f"batch {cycle} acceptance tests (red): {phase['name']}")
                # The acceptance tests written here immediately become PROTECTED: neither
                # this batch's implementation phases nor any later phase may adapt them to
                # the code. Accepted best-effort: if the commit above failed, the diff is
                # empty and the protection is simply missed (same trade-off as the parent
                # variant).
                if _GIT["enabled"] and phase_start_sha:
                    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"])
                    if ok_diff:
                        protected = set(blackboard.get("protected_test_files") or [])
                        # Filters: neither the orchestration artifacts committed during the
                        # phase (blackboard, prompts… — if protected, they would stall every
                        # following phase), nor the non-tests (the production freeze only sees
                        # TRACKED files: a production stub created then committed during the
                        # test phase must not enter protected_test_files, whose semantics is
                        # "validated tests").
                        protected.update(line.strip() for line in diff_out.splitlines()
                                         if line.strip() and is_test_file(line.strip())
                                         and not is_orchestration_file(line.strip()))
                        blackboard["protected_test_files"] = sorted(protected)
                        save_blackboard(blackboard)
                # BATCH milestone for the closing's brick B: everything that differs from
                # this sha at the closing is the WHOLE batch's implementation — the natural
                # target of the mutation. Persisted in the blackboard (a resume must find it).
                if _GIT["enabled"]:
                    story_shas = blackboard.setdefault("_story_shas", {})
                    story_shas[str(cycle)] = git_head_sha()
                    save_blackboard(blackboard)
                continue  # next phase: the batch's first implementation step

            if not closes_lot:
                # ── VERDICT OF AN INTERMEDIATE IMPLEMENTATION STEP (compilation alone) ──:
                # in the middle of a batch, the acceptance suite is red BY CONSTRUCTION (the
                # complete behavior does not exist yet): requiring a green suite here would
                # be absurd, requiring nothing would let everything through. The minimal
                # mechanical contract of a step is therefore: the tree COMPILES (build_cmd,
                # exit code 0). Accepted residual risk: a step can break a behavior of a
                # previous batch without immediate detection — the batch's closing (FULL
                # suite green) mechanically catches it.
                if is_ok:
                    success = True
                    phase["status"]  = "DONE"
                    phase["verdict"] = "OK"
                    phase["critic_feedback"] = ""
                    save_blackboard(blackboard)
                    cleanup_sentinels(phase["id"])
                    print(f"🔧 [SUCCESS] Phase {phase['id']}: the tree compiles — implementation "
                          f"step of batch {cycle} validated (the acceptance suite may "
                          f"stay red until the batch's closing).")
                    commit_phase(f"batch {cycle} implementation step: {phase['name']}")
                else:
                    critic_feedback = output
                    phase["critic_feedback"] = output
                    save_blackboard(blackboard)
                    print(f"⚠️  [REJECTED] Attempt {attempts}: the compilation fails. Output "
                          f"relayed to the coder:\n{output}")
                    RUNNER.new_context()
                continue  # success: next phase of the batch; failure: next attempt

            # ── BATCH CLOSING VERDICT ──: exit code 0 = full suite green.
            # ── YOLO · RED PATH ── : before handing a red back to the coder, the impact
            # triage tries to resolve it — breakage endorsed by the human → mechanical
            # deletion of the test and carry on; unplanned side effect → repairer (tests
            # frozen); a true conflict → human arbitration (impact-phase-<id>.md). If it
            # turns the suite green, the closing carries on below (LLM verifier included);
            # otherwise the freshest red output becomes the coder's feedback again.
            if not is_ok:
                is_ok, output = attempt_impact_resolution(
                    phase, blackboard, phase_need, output, attempts, phase_cmd, phase_start_sha)
            if is_ok:
                # ── NON-DECREASING TEST COUNT (mechanical guard, best-effort) ──:
                # a green suite that LOST tests is a weakened suite, not a success.
                count_regression = test_count_regression(output, blackboard)
                if count_regression:
                    critic_feedback = count_regression
                    phase["critic_feedback"] = count_regression
                    save_blackboard(blackboard)
                    print(f"🛡️  [REJECTED] Attempt {attempts}: suite green but the passing-test "
                          f"count DECREASED.")
                    RUNNER.new_context()
                    continue

                # ── YOLO · PHASE LLM VERIFIER (green path) ── : a green suite proves
                # "nothing is broken", not "the batch delivered everything". An independent
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
                print("🧐 LLM verifier: the batch delivered its whole checklist (compliant).")

                # ── BRICK B: does the suite BITE the batch's FINAL implementation? (signal) ──:
                # the test phase already proved that the acceptance suite fails WITHOUT the
                # implementation; the mutation checks that it still turns red when the
                # batch's DELIVERED implementation is altered. WARN-ONLY, never a verdict nor
                # a retry: the only agent re-runnable here is the implementation coder, which
                # precisely is NOT allowed to harden the tests (frozen) — sending it the
                # surviving mutants would lead it straight onto the freeze guard. Surviving
                # mutants are a quality signal for the HUMAN. Graceful degradation everywhere
                # (tool absent / timeout → warn). Target: the WHOLE BATCH ('_story_shas'
                # milestone laid at the end of the test phase), not the closing phase alone.
                mcmd = resolve_mutation_cmd(phase, blackboard)
                lot_sha = str((blackboard.get("_story_shas") or {}).get(str(cycle)) or "") or phase_start_sha
                targets = build_mutation_targets(phase, lot_sha)
                if not mcmd:
                    print("ℹ️  Brick B inactive (no 'mutation_cmd' declared).")
                elif "{targets}" in mcmd and not targets:
                    print("⚠️  Brick B: no mutable target (no production file visible "
                          "for this batch) — skipped.")
                elif not mutation_tool_available(mcmd):
                    print("⚠️  Brick B: mutation tool not found — skipped (graceful degradation).")
                else:
                    run_cmd = mcmd.replace("{targets}", " ".join(shlex.quote(t) for t in targets)) if "{targets}" in mcmd else mcmd
                    print("🧬 Brick B: the suite passes — checking that it BITES the batch's "
                          "final implementation (targeted mutation, warn-only signal)...")
                    mut_started = time.time()
                    ok_mut, mout, mut_timed_out = run_mutation(run_cmd)
                    print(f"   ⏱️  Brick B: mutation finished in {time.time() - mut_started:.0f}s.")
                    if mut_timed_out:
                        print(f"⏱️  Brick B: mutation timed out ({MUTATION_TIMEOUT}s) — ignored, "
                              f"phase validated on the universal verdict (graceful degradation, "
                              f"run never lengthened without bound).")
                    elif not ok_mut:
                        print("⚠️  Brick B: mutants SURVIVE the batch's acceptance tests — "
                              "quality signal (harden the tests YOURSELF: the agents are not "
                              "allowed to touch them). The phase stays validated on the universal verdict:\n"
                              + truncate_output(mout, 1200))
                    else:
                        print("🧬 Brick B: the suite BITES (mutants killed). Closing truly validated.")

                record_test_count(output, blackboard, expect_growth=True)
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCESS] Phase {phase['id']}: the full suite passes — batch {cycle} "
                      f"closed (acceptance tests → implementation).")
                commit_phase(f"batch {cycle} closing: {phase['name']}")
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
            # ATDD-specific leads: the agents are not allowed to touch the tests — if an
            # acceptance test of the batch is ITSELF faulty (wrong assertion, fabricated
            # failure), no closing will ever converge; and an intermediate step that never
            # compiles often points at a 'build_cmd' that ALSO compiles the tests.
            atdd_hint = ""
            if nature == IMPL_NATURE and closes_lot:
                atdd_hint = (f"\nATDD lead: an acceptance test written by the test phase of batch "
                             f"{cycle} may be itself faulty (wrong assertion, fabricated "
                             f"failure). The agents are not allowed to fix it (tests "
                             f"frozen): inspect the batch's tests, fix them YOURSELF if needed, "
                             f"then relaunch.")
            elif nature == IMPL_NATURE:
                atdd_hint = (f"\nATDD lead: check that « {build_cmd} » compiles the "
                             f"PRODUCTION ONLY — a command that also compiles the test files "
                             f"stays red as long as the whole API expected by the batch's "
                             f"acceptance tests does not exist, and no intermediate step can "
                             f"converge. Fix it in '{BLACKBOARD_FILE}' if that is the case.")
            write_fail_report(
                f"Phase {phase['id']} \"{phase['name']}\" (batch {cycle}) did not converge after "
                f"{MAX_ATTEMPTS} attempts",
                f"Last blocking point raised by the verification:\n{critic_feedback}{atdd_hint}",
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
    print(f"\n{'='*50}\n🛡️  STEP 5: FINAL ATDD REFACTOR (GLOBAL, RE-VERIFIED)\n{'='*50}")

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
4. You NEVER delete NOR weaken an existing test to make the suite pass:
   this project is produced in ATDD, its acceptance tests are its executable specification
   and are authoritative — if a test turns red, the production code is what must be fixed.
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
            print(f"↩️  Refacto rolled back (return to {pre_refacto_sha[:8]}): the delivered code is the "
                  f"all-phases-green state. « {REFACTO_REPORT_FILE} » (untracked) survives for inspection.")
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
    mm_audit.start(os.getcwd(), "yolo-atdd", RUNNER.name,
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
        print(f"   Production-only compilation (build_cmd): "
              f"{blackboard.get('build_cmd') or "(missing — required as soon as a batch has several implementation phases)"}")
        lot_count = len({str(p.get('cycle')) for p in blackboard['phases'] if isinstance(p, dict)})
        closing_ids = lot_closing_ids(blackboard['phases'])
        print(f"   Phases: {len(blackboard['phases'])} "
              f"({lot_count} ATDD batch(es): acceptance tests → implementation)")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers', []))
            own_cmd = (p.get('verify_cmd') or '').strip()
            extra = f" — specific verify: {own_cmd}" if own_cmd else ""
            nat = str(p.get('nature') or '').strip().lower()
            icon = ("🧪 test" if nat == TEST_NATURE
                    else "🏁 impl·closing" if p.get('id') in closing_ids else "🔧 impl")
            print(f"   Phase {p['id']} [batch {p.get('cycle', '?')} {icon}]: {p['name']} [{skills}] "
                  f"({len(p.get('tasks', []))} task(s); covers: {covers or '?'}){extra}")
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
    # already-advanced HEAD, and the refacto would then miss earlier phases' files.
    if _GIT["enabled"] and not blackboard.get("_run_baseline_sha"):
        blackboard["_run_baseline_sha"] = git_head_sha()
        save_blackboard(blackboard)

    # Step 0: executable skeleton (hard prerequisite of execution-based verification).
    ensure_executable_scaffold(blackboard, user_need)

    print(f"\n🚀 Starting ATDD production (batches: acceptance tests → implementation "
          f"by steps): {blackboard.get('project', '')}")

    # Step 4: Production loop (ATDD batches: the test phase must make the suite fail,
    # each intermediate step must compile, the closing turns the suite green again)
    run_production_phases(blackboard, user_need, need_is_spec)

    # Step 5: Global refactor re-verified (ATDD's third beat, mutualized at end of run)
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
    print("\n🏁 [CONGRATULATIONS] The Advanced-ATDD factory closed all its batches (acceptance tests → implementation → refactor) in a single run!")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    GITIGNORE_BODY=GITIGNORE_BODY,
    IMPACT_DONE_SENTINEL=IMPACT_DONE_SENTINEL,
    IMPACT_FILE=IMPACT_FILE,
    IMPACT_PHASE_PREFIX=IMPACT_PHASE_PREFIX,
    IMPL_NATURE=IMPL_NATURE,
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
