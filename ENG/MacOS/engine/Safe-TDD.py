#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - Code factory with an agent harness + tmux (Full TUI Data Center Version)
─────────────────────────────────────────────────────────────────────────────
"TDD" VARIANT (Test-Driven Development driven by the plan: red → green cycles).

Difference with the "universal verdict" variant Safe-Coding.py (from which this script derives):
  - The plan is no longer split into feature/tests phases but into TDD CYCLES: for each
    behavior, a 'tdd-red' phase (write tests that FAIL, derived from the acceptance
    criteria) immediately followed by its 'tdd-green' phase (implement the MINIMAL
    production code that makes the whole suite pass). This split is decided as early as the
    PLAN (TDD Architect Agent, skill 'plan-tdd') then copied into the blackboard (fields
    'nature' and 'cycle' of each phase, skill 'plan-to-blackboard-tdd').
  - The verdict of a 'tdd-red' phase is INVERTED: the orchestrator runs the universal
    verdict (compilation + full suite) and VALIDATES the phase when it FAILS (exit code
    ≠ 0). Since production code is FROZEN during red (tests-only guard), the tests of the
    previous cycles PROTECTED, and the suite green at the previous phase (scaffold
    included), a suite failure is mechanically attributable to the new tests: it is the
    proof that they are falsifiable — the heart of TDD.
  - The verdict of a 'tdd-green' phase is STANDARD (exit code 0 = full suite green). The
    test files, ALL frozen during green (git guards), are the executable specification:
    it is the test that commands, never the other way around.
  - The third beat of the cycle (refactor) is played AFTER EACH validated green, as in
    Beck: a fresh-context agent polishes the PRODUCTION code laid down by the cycle
    (tests frozen, same guard as in green), the suite is re-verified and any deviation —
    red suite, touched tests, shrinking count, timeout — triggers a mechanical ROLLBACK
    to the green commit (the refactor is opportunistic: it NEVER blocks the run and
    consumes no attempt). Step 5 (global refactoring re-verified, with git rollback on
    a persistent regression) remains as a COMPLEMENT: it alone sees INTER-cycle
    duplication.
  - Brick B (mutation testing) becomes a warn-only SIGNAL on green phases: the only agent
    re-runnable at this stage (the green) is not allowed to harden the tests (frozen) —
    asking it to would lead straight onto the protection guard. Red already proves
    falsifiability at the behavior level; surviving mutants remain a quality signal
    addressed to the HUMAN.

PO → TDD Architect pipeline:
  - Step 1: a PO Agent refines 'need.md' into a business specification 'spec.md' (user
    stories, testable acceptance criteria, out-of-scope, assumptions), VALIDATED by the human.
    Fixing the need costs the least HERE, before paying for plan + blackboard + production.
  - Step 2: a TDD Architect Agent converts 'spec.md' into a plan by CYCLES where each
    phase EXPLICITLY declares its nature ('tdd-red'/'tdd-green') and its cycle number.
  - Step 3: the blackboard conversion becomes a MECHANICAL copy of these decisions
    (zero inference asked of the small model, which only compiles the format). The
    red → green pairing (adjacency, same cycle) is VALIDATED mechanically before
    production: a blackboard that violates it is REFUSED (the inverted verdict applied to
    the wrong phase would falsify the whole run).

Data Center & TUI Strategy (unchanged):
  - The tmux session is initialized DIRECTLY at startup.
  - We directly launch the chosen harness TUI (Cloud / Data Center model).
  - Steps 1 (PO Spec), 2 (Plan) and 3 (Blackboard) are executed directly in the TUI.
  - Production: each phase goes through a Coder Agent, then the orchestrator RUNS the
    phase's verification command itself; the exit code IS the verdict (brick A),
    interpreted according to the nature (failure expected in red, success required in green).
    The coder communicates via a sentinel file ('.phase_<id>.attemptN.done'); the sole
    owner of the blackboard is the Python orchestrator (no concurrent writes).

Accepted residual risks (in addition to those of the parent variant — ghost coder, etc.):
the red verdict proves that the SUITE fails, not that the tests fail "for the right
reason". A FABRICATED red test (always-false assertion, test writing error) passes the red
gate but blocks the cycle's green — since the agents are not allowed to touch the tests, it
is the HUMAN who arbitrates (the green's failure report explicitly points at this lead, cf.
print_failure_message). The quality of the test cases remains carried by the instructions
(acceptance criteria) and by brick B's signal.
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
    apply_blackboard_defaults, build_refacto_fix_prompt, build_skills_dictionary, cleanup_all_sentinels,
    cleanup_sentinels, collect_spec_us_ids, commit_phase, done_sentinel,
    ensure_phase_repo, fail_pipeline, files_changed_since_phase_start, git_head_sha,
    inject_skills_dictionary, is_orchestration_file, load_blackboard, load_skills,
    mutation_tool_available, no_declared_file_touched, parse_test_count, read_touched_files,
    red_suite_damage, resolve_mutation_cmd, resolve_verify_cmd, run_mutation,
    run_verify, run_verify_resilient, save_blackboard, signal_handler,
    test_count_regression, truncate_output, validate_all_skills, wait_for_file_creation,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="tdd")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
FAIL_REPORT_FILE      = "failReport.md"   # persistent stop report (part D, §6.8)
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard-tdd/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan-tdd/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")
AGENT_CONFIG_FILE     = RUNNER.config_file

# Pipeline system skills: never routed to production phases.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-proto", "plan-tdd",
                         "plan-to-blackboard", "plan-to-blackboard-proto",
                         "plan-to-blackboard-tdd", "refacto"}

# TDD-mode phase natures, decided by the Architect as early as the plan and copied by the
# blackboard compiler. They drive EVERYTHING: the coder's mission (tests first / minimal
# implementation), the git guards (production frozen in red, tests frozen in green) and the
# SEMANTICS of the verdict (failure expected in red, success required in green). Any other
# value is an invalid blackboard — validated mechanically before production.
RED_NATURE            = "tdd-red"
GREEN_NATURE          = "tdd-green"

# Temporary context routing files
TMP_CODER_FILE        = RUNNER.tmp_file("task")
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
REFACTO_FIX_PHASE_ID  = -1             # dedicated sentinel id for post-refacto regression fixes (≠ phases ≥1, ≠ scaffold 0)
CYCLE_REFACTO_PHASE_ID = -2            # dedicated sentinel id for the per-cycle refactor (Beck's 3rd beat); the cycle number serves as attempt number — a late sentinel from a previous cycle cannot be mistaken for the current cycle's
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per phase (safety net)
VERIFY_TIMEOUT        = resolve_timeout("verify", 300)            # 5 min max for running the verification command
MAX_VERIFY_RETRIES_ON_TIMEOUT = 2      # immediate re-verifications on an infra timeout (the code did not change)
MAX_PHASE_VERIFY_TIMEOUTS     = 3      # persistent timeouts tolerated on a phase before aborting ("broken infra")
MUTATION_TIMEOUT      = 300            # CAUTIOUS: bounded budget for mutation testing (brick B). In TDD mode
                                       # brick B is a warn-only SIGNAL (never a retry): at most one run per
                                       # green phase, any overrun degrades to a warn
SCAFFOLD_TIMEOUT      = 300            # 5 min: the scaffold is the shortest task of the run — if it
                                       # does not complete, the model's tool calling is almost always
                                       # the culprit, and a fast diagnosis beats a long wait
VERIFY_FEEDBACK_LIMIT = 4000           # max size of the verification feedback sent back to the coder
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net: pipeline deliverable accepted if it
                                       # stayed stable for N consecutive checks (N × POLL_INTERVAL seconds).
                                       # 30s: a slow local model pausing between two writes must not get
                                       # its half-written deliverable accepted (see structural_check too)

# ONE single verification command in this script: the UNIVERSAL VERDICT (compilation +
# full suite), declared by the TDD Architect Agent and copied by the blackboard compiler,
# never by this script. What changes between phases is NOT the command but the SEMANTICS
# of its exit code: a 'tdd-red' phase is validated when the command FAILS (the new tests
# must be red), a 'tdd-green' phase when it SUCCEEDS (full suite green). The TDD plan never
# declares a per-phase command.


# ─── PHASE SENTINELS (CODER → ORCHESTRATOR CHANNEL) ────────────

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

    Called ONLY on GREEN suites (scaffold, green phases, post-refacto): the last green
    state is the reference of the non-decreasing guards — a red phase, whose suite fails by
    construction, never records a count. A green that passes WITHOUT strictly increasing
    the count only gets a console warning: a weak signal, deliberately not a verdict
    (re-organizations happen).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if expect_growth and isinstance(old_count, int) and new_count <= old_count:
        print(f"⚠️  Green went green without increasing the suite ({old_count} → {new_count} "
              f"passing): are the tests added by this cycle's red actually "
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
    when in doubt we classify as test, so as NOT to stall a legitimate red phase on a false
    "modified production file" (the production freeze only restores what is NOT a test).
    Accepted trade-off in green (test freeze): a helper off-convention may be classified as
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
# factory sabotages its own state, even its own script, and no red phase ever converges
# (cause of a systematic rejection when these artifacts are git-tracked, e.g. a human repo
# whose .gitignore did not cover them). Deliberately BROAD: when in doubt we protect (at worst
# we miss a false "touched code" on an orchestration file, never on real produced code).
_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE, FAIL_REPORT_FILE,
    TMP_PLAN_FILE, TMP_CODER_FILE, TMP_REFACTO_FILE, TMP_ARCHITECT_FILE, TMP_PO_FILE,
    TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


def build_mutation_targets(phase: dict, phase_start_sha: str = "") -> list:
    """PRODUCTION files to mutate after a green 'tdd-green' phase.

    Natural TDD targeting: what the green just wrote or modified (git diff since the phase
    start, working tree still uncommitted at this stage), filtered on existing production
    files — that is exactly the code the cycle's tests must bite. Fallback without git or
    without an exploitable diff: the phase's 'files_to_read' filtered on existence, like the
    parent variant (better than a silently inactive brick B — in green they mostly list the
    cycle's tests, hence the is_test_file filter).
    """
    out = sorted(
        f for f in files_changed_since_phase_start(phase_start_sha)
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
        # ── TDD CYCLE PAIRING (structural) ── : the whole verdict rests on the nature
        # ('tdd-red' → the suite must fail, 'tdd-green' → it must pass) and on the
        # red → green pairing (same cycle, adjacent, red first). A blackboard that violates
        # them can only produce a WRONG run (inverted verdict applied to the wrong phase, or
        # a run ending on a red suite): FATAL, never tolerated.
        bad_nature = sorted({str(phase.get("nature") or "(absent)").strip() or "(absent)"
                             for phase in phases if isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower()
                             not in (RED_NATURE, GREEN_NATURE)})
        if bad_nature:
            fatal.append(
                f"phases[].nature outside {{{RED_NATURE}, {GREEN_NATURE}}}: {', '.join(bad_nature)}. "
                f"In TDD mode the nature drives the VERDICT (failure expected in red, success required "
                f"in green): every phase must declare one of the two."
            )
        missing_cycle = sorted(str(phase.get("id", "?")) for phase in phases
                               if isinstance(phase, dict)
                               and not str(phase.get("cycle") or "").strip())
        if missing_cycle:
            fatal.append(
                f"phases[].cycle missing (phases {', '.join(missing_cycle)}): the "
                f"red → green pairing is checked by this number, copied from the plan by the compiler."
            )
        if not bad_nature and not missing_cycle:
            if len(phases) % 2 != 0:
                fatal.append(
                    f"ODD number of phases ({len(phases)}): a TDD cycle = exactly one "
                    f"'{RED_NATURE}' phase immediately followed by its '{GREEN_NATURE}' phase. "
                    f"A red without a green would end the run on a red suite."
                )
            else:
                for i in range(0, len(phases), 2):
                    red, green = phases[i], phases[i + 1]
                    if not (isinstance(red, dict) and isinstance(green, dict)):
                        continue  # non-mapping phase: already reported as fatal above
                    red_nat = str(red.get("nature") or "").strip().lower()
                    green_nat = str(green.get("nature") or "").strip().lower()
                    if red_nat != RED_NATURE or green_nat != GREEN_NATURE:
                        fatal.append(
                            f"Phases {red.get('id', '?')}/{green.get('id', '?')}: invalid cycle "
                            f"order (expected '{RED_NATURE}' THEN '{GREEN_NATURE}', found "
                            f"'{red_nat}' then '{green_nat}'). Each red immediately precedes "
                            f"its green, with no phase in between."
                        )
                    elif str(red.get("cycle")) != str(green.get("cycle")):
                        fatal.append(
                            f"Phases {red.get('id', '?')} (cycle {red.get('cycle')}) and "
                            f"{green.get('id', '?')} (cycle {green.get('cycle')}): the two "
                            f"phases of a cycle must carry the SAME cycle number."
                        )
                    else:
                        if (isinstance(red.get("covers"), list) and isinstance(green.get("covers"), list)
                                and red.get("covers") and green.get("covers")
                                and [str(c).strip().upper() for c in red["covers"]]
                                != [str(c).strip().upper() for c in green["covers"]]):
                            soft.append(
                                f"Cycle {red.get('cycle')}: \"Covers\" differs between the red and "
                                f"the green ({red.get('covers')} vs {green.get('covers')}) — the "
                                f"two phases of a cycle normally cover the same user story."
                            )
                        if isinstance(red.get("covers"), list) and len(red.get("covers")) > 1:
                            soft.append(
                                f"Cycle {red.get('cycle')}: the red phase covers several user "
                                f"stories ({', '.join(str(c) for c in red['covers'])}) — prefer "
                                f"one cycle per US (tester context window, tighter mutated scope). "
                                f"Tolerated, informational."
                            )
        with_own_cmd = sorted(str(phase.get("id", "?")) for phase in phases
                              if isinstance(phase, dict) and (phase.get("verify_cmd") or "").strip())
        if with_own_cmd:
            soft.append(
                f"Phases with their own 'verify_cmd' ({', '.join(with_own_cmd)}): TDD mode does "
                f"not expect any (universal verdict everywhere). It will still be applied, with "
                f"the semantics of the nature (failure expected in red): check that it is intended."
            )
        # Brick B (informational): without 'mutation_cmd', the signal "does the suite still bite
        # the FINAL implementation?" (beyond the initial red proven by each red phase) will be
        # inactive on green phases. Tolerated (brick B is optional).
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if not has_mutation_cmd:
            soft.append(
                "No 'mutation_cmd' declared: brick B (warn-only signal \"do the tests bite the "
                "final implementation?\") will be inactive on green phases. Tolerated; declare it "
                "in the plan for falsifiable end-to-end tests."
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
    print("\n📖 [STEP 2: TDD ARCHITECT AGENT] Generating the TDD-cycle plan in Cloud TUI...")

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
- The plan MUST start with the "Stack & Verification" block (with the UNIVERSAL VERDICT verification command: compilation + full suite) and EVERY phase MUST declare its Nature (tdd-red/tdd-green), its Cycle and its "Covers" field (US-x): the next pipeline steps copy these decisions without inferring them.
- Break the specification down into TDD CYCLES: for each behavior, a 'tdd-red' micro-phase (tests derived from the acceptance criteria, which must FAIL against the current code) IMMEDIATELY followed by its 'tdd-green' micro-phase (MINIMAL implementation that makes the whole suite pass), carrying the same Cycle number.
- Each micro-phase stays BOUNDED (1 to 5 tasks, at most 5 files created/modified, at most 3 files to read); split a CYCLE that is too big into two cycles, never a single phase. Do not add any cycle for a requirement absent from '{SPEC_FILE}'.
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
    verify_cmd = resolve_verify_cmd(phase, blackboard)

    # 'nature' is the TDD Architect's decision, copied by the compiler and validated
    # mechanically before production (only RED_NATURE or GREEN_NATURE here): it drives
    # the mission, the editing policy AND the verdict's semantics.
    nature = str(phase.get("nature") or "").strip().lower()
    cycle = phase.get("cycle", "?")
    if nature == RED_NATURE:
        nature_line = (f"This phase is the RED of TDD cycle {cycle}: you write ONLY "
                       "tests, BEFORE any implementation. Derive each test case from an acceptance "
                       "criterion of the need below; name and place the files according to the "
                       "runner's conventions so that they are actually DISCOVERED and executed. "
                       "Your tests must FAIL against the current code because the behavior "
                       "does not exist yet — never through a fabricated failure (always-false "
                       "assertion, deliberate fail(), test writing error): a fabricated test "
                       "would block the implementation phase that follows, which is not allowed to "
                       "fix it.")
    else:
        nature_line = (f"This phase is the GREEN of TDD cycle {cycle}: the tests written by the "
                       "red phase of this cycle fail and DESCRIBE the expected behavior — they "
                       "are your executable specification. Read them first, then implement the "
                       "MINIMAL production code that makes the WHOLE suite pass (strict YAGNI: "
                       "nothing beyond what the tests and the checklist require). You write and "
                       "modify NO test.")

    # Editing policy, driven by the nature (mechanical git guards): in red the production
    # code is FROZEN (the red must come from the tests, not from sabotaging production); in
    # green it is the TEST files that are FROZEN (it is the test that commands, never the
    # other way around). The orchestrator enforces these policies by git restore.
    if nature == RED_NATURE:
        prod_edit_policy = ("In a red phase, you only create and modify test files: the "
                            "production code is FROZEN (the orchestrator restores by default any "
                            "production file you would modify). You also do not touch the tests "
                            "of previous cycles (protected, restored by default): you ADD the "
                            "tests of THIS cycle.")
    else:
        prod_edit_policy = ("In a green phase, you create and modify NO test file "
                            "(the orchestrator restores or deletes by default any test edit "
                            "and rejects the attempt). You MAY modify the existing production "
                            "code if needed to make the suite pass (it may reveal a bug from an "
                            "earlier cycle to fix).")

    # Quality instructions and verdict, by nature: in red the verdict is INVERTED (the suite
    # must fail BECAUSE OF the new tests), in green it is standard (green suite).
    if nature == RED_NATURE:
        test_rules = ("Your tests must be EXECUTABLE and FAST: NO "
                      "Testcontainers, NO Docker and no network or database I/O.\n"
                      "Before writing your tests, READ the acceptance criteria of the need "
                      "slice below and, if you hook into existing code, the real signatures "
                      "of the listed files: each test expresses a precise EXPECTED BEHAVIOR "
                      "(never an always-true assertion, never an always-false assertion).")
        verdict_block = (f"The orchestrator automatically runs the verification command "
                         f"« {verify_cmd} » (universal verdict: compilation + full suite): "
                         f"it MUST FAIL (exit code ≠ 0) BECAUSE OF your new tests — "
                         f"this is the mechanical proof that they are falsifiable. If the suite "
                         f"stays green, the phase is REJECTED (your tests already pass or are not "
                         f"discovered by the runner). The pre-existing tests, for their part, "
                         f"must KEEP passing. This is your ONLY success criterion.")
    else:
        test_rules = ("You NEVER delete NOR weaken a test to make the "
                      "verification pass: if a test is red, it is the production code that "
                      "must be written or fixed.")
        verdict_block = (f"The orchestrator automatically runs the verification command "
                         f"« {verify_cmd} » (universal verdict: compilation + full suite): "
                         f"it MUST succeed (exit code 0), otherwise the phase is rejected. "
                         f"This is your ONLY success criterion.")

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
You are an ultra-specialized Coder Agent for Phase {phase['id']} ONLY (TDD cycle {cycle}).
You implement ONLY the tasks of THIS phase and stop as soon as they are done.
Do NOT do work planned for other phases: the implementation belongs to the cycle's green
phase, the other behaviors to the following cycles. YAGNI principle: nothing that is not
explicitly requested by this phase's checklist.

--- AUTOMATIC VERIFICATION OF THIS PHASE ---
{nature_line}
{prod_edit_policy}
{test_rules}
{verdict_block}

{context_block}{files_block}--- NEED (spec slice covered by this phase) ---
{user_need}

--- PHASE {phase['id']} GOAL ({'RED' if nature == RED_NATURE else 'GREEN'}, cycle {cycle}): {phase['name']} ---
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
    # TDD-SPECIFIC diagnosis leads: a red that never fails and a green that never converges
    # do not have the same probable causes — nor the same human remedy (since the agents are
    # not allowed to touch the tests, a faulty red test is fixed by hand).
    if nature == RED_NATURE:
        tdd_hint = ("   RED phase: if the suite stays green, the model does not write tests that\n"
                    "   express the MISSING behavior (or places them outside the runner conventions).\n")
    elif nature == GREEN_NATURE:
        tdd_hint = (f"   GREEN phase: if the blockage comes from a test of cycle {phase.get('cycle', '?')} "
                    f"being itself faulty\n   (wrong assertion, fabricated failure in red), fix that test "
                    f"YOURSELF — the agents\n   are not allowed to (tests frozen) — then relaunch.\n")
    else:
        tdd_hint = ""
    print(f"""
{'='*60}
❌ Phase {phase['id']} "{phase['name']}" (cycle {phase.get('cycle', '?')}) did not converge after {MAX_ATTEMPTS} attempts.

   Last blocking point raised by the verification:
   "{critic_feedback}"

{tdd_hint}💡 The current model ({model}) is stuck on this specific step.
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
        lines = ["# Failure report — Safe-TDD", "", f"## {title}", "", "### Cause", reason.strip(), ""]
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
    """Guarantee an executable project AND a GREEN suite before the first cycle.

    Hard prerequisite of brick A, and DOUBLY of the inverted verdict: the suite must be
    green (even at a single health test) before each red phase so that a failure after it
    is attributable to the new tests. If the global verification command does not pass
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

    # RESUME: as soon as a phase is validated, the scaffold belongs to the past. TDD
    # specificity that makes this short-circuit MANDATORY (and not just an economy): if the
    # run was interrupted after a validated red, the suite is RED BY CONSTRUCTION until the
    # cycle's green — the "does the command pass?" check below would wrongly conclude a broken
    # chain, launch a scaffold agent on an already-advanced project, then abort the run on a
    # perfectly nominal state.
    if any(isinstance(p, dict) and p.get("status") == "DONE" and p.get("verdict") == "OK"
           for p in blackboard.get("phases", []) or []):
        print("↩️  Resuming mid-production: scaffold step skipped (a phase is already "
              "validated; after a red, the suite is red by construction until its "
              "green).")
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


# ─── PER-CYCLE REFACTOR (BECK'S 3rd BEAT) ─────────────────────────────────────

def execute_cycle_refactoring(blackboard: dict, phase: dict, verify_cmd: str, cycle,
                              phase_start_sha: str):
    """REFACTOR — 3rd beat of Beck's cycle (red → green → refactor), played after EACH
    validated green.

    The green has just been committed: the full suite is GREEN and that commit is the
    rollback point. A fresh-context agent polishes the PRODUCTION code laid down by the
    cycle (duplication, names, structure) WITHOUT changing any behavior; tests stay
    FROZEN (same git-diff guard as in green). The verdict is mechanical: the suite must
    STAY green (verify_cmd, exit code 0) without losing tests. Any deviation — touched
    tests, red suite, shrinking count, timeout — triggers the ROLLBACK to the green
    commit: unlike the final polish (step 5), NO fix loop here. The cycle refactor is
    OPPORTUNISTIC: it never blocks the run, consumes no attempt, and a rollback leaves
    exactly the all-phases-green state. The rollback is not a mere convenience: the
    NEXT red's inverted verdict assumes a green suite at start — a refactor state not
    proven green would break its attribution.
    Without git, no rollback is possible: step skipped (degraded mode assumed, like the
    other guards).
    """
    if not _GIT["enabled"]:
        print(f"ℹ️  Cycle {cycle} refactor skipped: without git, no rollback is possible.")
        return
    green_sha = git_head_sha()
    if not green_sha:
        return

    # Scope: the PRODUCTION files laid down/modified by the green phase (this cycle's
    # red only writes tests). Commit-to-commit diff: all the green work has just been
    # committed. Nothing to polish → nothing to pay.
    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"]) if phase_start_sha else (False, "")
    scope = sorted(f for f in (diff_out.splitlines() if ok_diff else [])
                   if f.strip() and not is_test_file(f.strip())
                   and not is_orchestration_file(f.strip()) and os.path.exists(f.strip()))
    if not scope:
        print(f"ℹ️  Cycle {cycle} refactor skipped: no production file to polish.")
        return

    print(f"\n🧹 REFACTOR — 3rd beat of cycle {cycle} (opportunistic, re-verified, rollback to the green commit)...")
    sentinel = done_sentinel(CYCLE_REFACTO_PHASE_ID, cycle)
    cleanup_sentinels(CYCLE_REFACTO_PHASE_ID)
    refacto_skills = load_skills(["refacto"])
    scope_block = "\n".join(f"   - {f}" for f in scope)

    full_context = f"""You are an Expert Craftsman, a TDD practitioner. Cycle {cycle} has just been closed:
the full suite is GREEN. Play the THIRD BEAT of Beck's cycle (red → green → REFACTOR):
improve the design of the production code this cycle just laid down, WITHOUT changing
any behavior.

--- SPECIALIZED SKILLS ---
{refacto_skills}
--- GLOBAL CONSTRAINTS ---
Stack: {blackboard['global_rules']['target']}
Styling: {blackboard['global_rules']['styling']}
Prohibitions: {blackboard['global_rules']['constraints']}
Accessibility: {blackboard['global_rules']['accessibility']}

--- SCOPE (production code laid down by this cycle) ---
{scope_block}
   Focus on these files; you may adjust ANOTHER PRODUCTION file only if a duplication
   directly ties it in. Everything else is OUT OF SCOPE.

--- ABSOLUTE RULES ---
1. Behavior STRICTLY unchanged: the command « {verify_cmd} » must stay green.
2. You touch NO test file (frozen: any modification is detected by git diff and
   reverted) nor the file {BLACKBOARD_FILE}.
3. Refactor ONLY when useful (duplication to absorb, misleading name, convoluted
   structure). If nothing is worth it, CHANGE NOTHING: a cosmetic refactor costs
   more than it pays.

--- MANDATORY END OF TASK ---
As your VERY LAST action, create the sentinel file '{sentinel}' at the root: the list
of modified files (one per line), or the single word NO_CHANGE if you modified nothing.
"""
    with open(TMP_REFACTO_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    RUNNER.new_context()
    mm_audit.event("agent_task", prompt_bytes=len(f"Read the instruction file '{TMP_REFACTO_FILE}' at the project root and execute the cycle refactor."))
    RUNNER.send_task(f"Read the instruction file '{TMP_REFACTO_FILE}' at the project root and execute the cycle refactor.")

    if not wait_for_file_creation(sentinel):
        print(f"⏱️  The cycle {cycle} refactor did not signal its end: the tree is verified as is (rollback at the slightest doubt).")
    cleanup_sentinels(CYCLE_REFACTO_PHASE_ID)

    # The git diff is the only truth (the sentinel is just an end signal): touched
    # tests are restored/deleted as in green, then the production remainder decides
    # what happens next.
    touched = sorted(f for f in files_changed_since_phase_start(green_sha)
                     if f.strip() and not is_orchestration_file(f.strip()))
    touched_tests = [f for f in touched if is_test_file(f)]
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
        print(f"🛡️  Cycle {cycle} refactor: test files touched ({', '.join(touched_tests)}) — restored/deleted (tests frozen, the refactor only changes production).")
    touched_prod = [f for f in touched if not is_test_file(f)]
    if not touched_prod:
        print(f"✓ Cycle {cycle} refactor: no change retained (NO_CHANGE) — the green commit stands.")
        return

    is_ok, output, timed_out = run_verify_resilient(verify_cmd)
    count_regression = test_count_regression(output, blackboard) if is_ok else None
    if is_ok and not count_regression:
        record_test_count(output, blackboard)
        commit_phase(f"cycle {cycle} refactor: {phase['name']}")
        print(f"🧹 [SUCCESS] Cycle {cycle} refactor re-verified: the suite stays green ({len(touched_prod)} production file(s) polished).")
        return

    # ── MECHANICAL ROLLBACK TO THE GREEN COMMIT ──: red suite, shrinking count or
    # timeout (a timeout proves no green, and only a PROVEN green state may precede the
    # next red). Tracked files come back via reset --hard; files CREATED by the refactor
    # (untracked) are deleted — the equivalent of restoration for a file that did not
    # exist at the green commit.
    ok_tracked, tracked_out = run_git(["ls-files", "--"] + touched_prod)
    tracked = set(tracked_out.splitlines()) if ok_tracked else set()
    run_git(["reset", "--hard", green_sha])
    for f in touched_prod:
        if f not in tracked:
            try:
                os.remove(f)
            except OSError:
                pass
    if timed_out:
        reason = "re-verification timed out (an unproven green is not enough)"
    elif count_regression:
        reason = "the passing-test count shrank"
    else:
        reason = "the suite does not stay green"
    print(f"↩️  Cycle {cycle} refactor CANCELED ({reason}): back to green commit {green_sha[:8]} — the run continues, nothing is lost.")


# ─── MAIN PRODUCTION LOOP ─────────────────────────────────────────────────────

def run_production_phases(blackboard: dict, user_need: str, need_is_spec: bool = False):
    total = len(blackboard["phases"])

    for phase in blackboard["phases"]:
        if phase.get("status") == "DONE" and phase.get("verdict") == "OK":
            print(f"⏭️  Phase {phase['id']}/{total} already validated: {phase['name']}")
            continue

        # TDD Architect's decisions (copied from the plan, validated before production):
        # the nature drives the git guards AND the verdict's semantics; the cycle links the
        # red phase to its green phase.
        nature = str(phase.get("nature") or "").strip().lower()
        cycle = phase.get("cycle", "?")
        icon = "🔴 red" if nature == RED_NATURE else "🟢 green"
        print(f"\n{'='*50}\n🛠️  PHASE {phase['id']}/{total} [cycle {cycle} — {icon}] : {phase['name']}\n{'='*50}")

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

            # ── PROTECTION OF PREVIOUS CYCLES' TESTS (mechanical guard, best-effort) ──:
            # during a red phase, the VALIDATED tests of earlier cycles are out of
            # bounds — the red must come from the tests ADDED by THIS cycle, otherwise
            # the mechanical attribution of the failure collapses (and a red could "prepare"
            # an easy green by weakening the existing code). The prompt-only prohibition
            # is unverifiable; this diff is not. Known false positive (a legitimately
            # shared test helper to extend): the feedback names the files, the human
            # arbitrates (cf. protected_test_files in the blackboard).
            if nature == RED_NATURE and _GIT["enabled"]:
                protected = set(blackboard.get("protected_test_files") or [])
                if protected:
                    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                    touched_protected = sorted(set(diff_out.splitlines()) & protected) if ok_diff else []
                    if touched_protected:
                        run_git(["checkout", "--"] + touched_protected)
                        critic_feedback = (
                            f"You modified PROTECTED tests of previous cycles during your red "
                            f"phase: {', '.join(touched_protected)}. They have been restored. A red "
                            f"phase ADDS the tests of ITS cycle; the already-green tests are "
                            f"untouchable."
                        )
                        phase["critic_feedback"] = critic_feedback
                        save_blackboard(blackboard)
                        print(f"🛡️  [REJECTED] Attempt {attempts}: protected tests modified "
                              f"({', '.join(touched_protected)}) — restored.")
                        RUNNER.new_context()
                        continue

            # ── PRODUCTION FREEZE IN RED (mechanical guard, best-effort) ──: a red phase
            # only modifies test files; the production code is FROZEN. Any touched production
            # file is restored (git checkout) and the attempt rejected. Placed BEFORE the
            # verification: we catch the cheat whatever the color of the suite, and avoid a
            # wasted verify on a state we will reject. It is THIS freeze that makes the
            # inverted verdict reliable: the suite was green before the phase and production
            # did not move, so a failure can only come from the new tests. Settled caveat: a
            # red never "fixes" production quietly, even to make its test writable — the
            # implementation belongs to the green.
            if nature == RED_NATURE and _GIT["enabled"]:
                ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
                # Excludes the orchestrator's own files (prompts, blackboard, sentinels,
                # .pyc, its own script…), which it rewrites every phase: counting them as
                # "modified production code" would reject EVERY red attempt and, worse, their
                # restoration (git checkout below) would sabotage the orchestrator's state —
                # even its script. Cf. is_orchestration_file.
                touched_prod = sorted(f for f in diff_out.splitlines()
                                      if f.strip() and not is_test_file(f.strip())
                                      and not is_orchestration_file(f.strip())) if ok_diff else []
                if touched_prod:
                    run_git(["checkout", "--"] + touched_prod)
                    critic_feedback = (
                        f"In a red phase, you only touch test files. You modified production "
                        f"code: {', '.join(touched_prod)}. These files have been "
                        f"restored. Write only the cycle's tests: the implementation "
                        f"belongs to the green phase that follows."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJECTED] Attempt {attempts}: production code modified in a red "
                          f"phase ({', '.join(touched_prod)}) — restored.")
                    RUNNER.new_context()
                    continue

            # ── TEST FREEZE IN GREEN (mirror of the production freeze, best-effort) ──: in
            # green, NO test file is created nor modified — even a NEW test (thus unprotected)
            # is rejected: writing tests is the exclusive role of red phases, and a green that
            # "completes" or adapts the suite blurs everything (anti-cheat: the green cannot
            # make the suite pass by tweaking the cycle's tests). Tracked files → restored;
            # new files → deleted (the equivalent of restoration for a file that did not
            # exist). The feedback names everything, the human arbitrates borderline helpers.
            if nature == GREEN_NATURE and _GIT["enabled"]:
                touched_tests = sorted(
                    f for f in files_changed_since_phase_start(phase_start_sha)
                    if f.strip() and is_test_file(f.strip()) and not is_orchestration_file(f.strip()))
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
                        f"In a green phase, you write and modify NO test; you touched: "
                        f"{', '.join(touched_tests)}. Everything has been restored or deleted — "
                        f"writing the tests is the exclusive role of red phases. Implement the "
                        f"MINIMAL PRODUCTION code that makes the suite pass as it is."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🔒 [REJECTED] Attempt {attempts}: test files created/modified in a "
                          f"green phase ({', '.join(touched_tests)}) — restored/deleted.")
                    RUNNER.new_context()
                    continue

            print(f"  → Coder finished ({len(touched_files)} declared file(s)). Verification by EXECUTION...")

            # ── BRICK A: the verdict IS the exit code. ──
            # Python runs the command itself; no LLM judges functional completeness.
            # An objective signal that neither the coder nor a verifier can hallucinate. The
            # SEMANTICS of the exit code depends on the nature: failure expected in red,
            # success required in green. A TIMEOUT is NOT a verdict (neither red nor green):
            # dedicated branch below.
            is_ok, output, verify_timed_out = run_verify_resilient(verify_cmd)

            if verify_timed_out:
                # Infra timeout, not a verdict: we do NOT consume the attempt (otherwise a
                # few machine slowdowns would exhaust the coder's MAX_ATTEMPTS). We replay the
                # same attempt after reset, with an anti-loop guard if the infra is durably
                # broken. Holds for BOTH natures: a timeout proves a legitimate red (red) no
                # more than a green suite (green).
                verify_timeouts += 1
                if verify_timeouts >= MAX_PHASE_VERIFY_TIMEOUTS:
                    critic_feedback = (
                        f"Verification « {verify_cmd} » timed out (timeout {VERIFY_TIMEOUT}s) "
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

            if nature == RED_NATURE:
                # ── INVERTED RED VERDICT ──: the phase is validated when the suite FAILS.
                # The production code is frozen (guard above), the tests of previous cycles
                # protected, and the suite was green at the previous phase: a failure is
                # therefore mechanically attributable to the NEW tests — proof of
                # falsifiability, the heart of TDD. A GREEN suite means the tests express
                # nothing new (or are not discovered by the runner): rejection.
                if is_ok:
                    critic_feedback = (
                        "The verification suite PASSES while your red phase must make it "
                        "FAIL. Probable causes: your tests already pass (they do not test the "
                        "NEW behavior requested by the checklist), they are not discovered by "
                        "the runner (name or location outside conventions), or their assertions "
                        "are hollow. Write tests that express the EXPECTED behavior of the "
                        "acceptance criteria and that fail against the CURRENT code."
                    )
                    phase["critic_feedback"] = critic_feedback
                    save_blackboard(blackboard)
                    print(f"🟢 [REJECTED] Attempt {attempts}: the suite stays GREEN — the new "
                          f"tests do not fail (red not reached).")
                    RUNNER.new_context()
                    continue
                damage = red_suite_damage(output, blackboard)
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
                print(f"🔴 [SUCCESS] Phase {phase['id']}: the suite fails as expected — the tests "
                      f"of cycle {cycle} are falsifiable (red reached).")
                # A red phase's commit deliberately captures a suite-red state: it is the
                # journal of the TDD cycle, and the HEAD landmark against which the green will
                # measure its diff.
                commit_phase(f"cycle {cycle} red: {phase['name']}")
                # The tests written in red immediately become PROTECTED: neither this
                # cycle's green nor any later phase may adapt them to the code. Accepted
                # best-effort: if the commit above failed, the diff is empty and the protection
                # is simply missed (same trade-off as the parent variant).
                if _GIT["enabled"] and phase_start_sha:
                    ok_diff, diff_out = run_git(["diff", "--name-only", phase_start_sha, "HEAD"])
                    if ok_diff:
                        protected = set(blackboard.get("protected_test_files") or [])
                        # Filters: neither the orchestration artifacts committed during the
                        # phase (blackboard, prompts… — if protected, they would stall every
                        # following phase), nor the non-tests (the production freeze only sees
                        # TRACKED files: a production stub created then committed in red must
                        # not enter protected_test_files, whose semantics is "validated tests").
                        protected.update(line.strip() for line in diff_out.splitlines()
                                         if line.strip() and is_test_file(line.strip())
                                         and not is_orchestration_file(line.strip()))
                        blackboard["protected_test_files"] = sorted(protected)
                        save_blackboard(blackboard)
                continue  # next phase: the cycle's green

            # ── STANDARD GREEN VERDICT ──: exit code 0 = full suite green.
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

                # ── BRICK B: does the suite BITE the FINAL implementation? (signal) ──:
                # the red already proved that the tests fail WITHOUT the implementation; the
                # mutation checks that they still turn red when the DELIVERED implementation is
                # altered. WARN-ONLY in TDD mode, never a verdict nor a retry: the only agent
                # re-runnable here is the green, which precisely is NOT allowed to harden the
                # tests (frozen) — sending it the surviving mutants would lead it straight onto
                # the freeze guard. Surviving mutants are a quality signal for the HUMAN.
                # Graceful degradation everywhere (tool absent / timeout → warn).
                mcmd = resolve_mutation_cmd(phase, blackboard)
                targets = build_mutation_targets(phase, phase_start_sha)
                if not mcmd:
                    print("ℹ️  Brick B inactive (no 'mutation_cmd' declared).")
                elif "{targets}" in mcmd and not targets:
                    print("⚠️  Brick B: no mutable target (no production file visible "
                          "for this phase) — skipped.")
                elif not mutation_tool_available(mcmd):
                    print("⚠️  Brick B: mutation tool not found — skipped (graceful degradation).")
                else:
                    run_cmd = mcmd.replace("{targets}", " ".join(shlex.quote(t) for t in targets)) if "{targets}" in mcmd else mcmd
                    print("🧬 Brick B: the suite passes — checking that it BITES the final "
                          "implementation (targeted mutation, warn-only signal)...")
                    mut_started = time.time()
                    ok_mut, mout, mut_timed_out = run_mutation(run_cmd)
                    print(f"   ⏱️  Brick B: mutation finished in {time.time() - mut_started:.0f}s.")
                    if mut_timed_out:
                        print(f"⏱️  Brick B: mutation timed out ({MUTATION_TIMEOUT}s) — ignored, "
                              f"phase validated on the universal verdict (graceful degradation, "
                              f"run never lengthened without bound).")
                    elif not ok_mut:
                        print("⚠️  Brick B: mutants SURVIVE the cycle's tests — quality "
                              "signal (harden the tests YOURSELF: the agents are not allowed "
                              "to touch them). The phase stays validated on the universal verdict:\n"
                              + truncate_output(mout, 1200))
                    else:
                        print("🧬 Brick B: the suite BITES (mutants killed). Green truly validated.")

                record_test_count(output, blackboard, expect_growth=True)
                success = True
                phase["status"]  = "DONE"
                phase["verdict"] = "OK"
                phase["critic_feedback"] = ""
                save_blackboard(blackboard)
                cleanup_sentinels(phase["id"])
                print(f"✅ [SUCCESS] Phase {phase['id']}: the full suite passes — cycle {cycle} "
                      f"closed (red → green).")
                green_committed = commit_phase(f"cycle {cycle} green: {phase['name']}")
                # ── PER-CYCLE REFACTOR (Beck's 3rd beat) ──: played only if the green
                # commit succeeded — without this milestone no reliable rollback exists,
                # and a non-revertible refactor would break the red attribution of the
                # next cycle (red assumes a green suite at start).
                if green_committed:
                    execute_cycle_refactoring(blackboard, phase, verify_cmd, cycle,
                                              phase_start_sha)
                else:
                    print(f"ℹ️  Cycle {cycle} refactor skipped: the green commit failed, "
                          f"no reliable rollback point.")
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
            # TDD lead specific to the green: the agents are not allowed to touch the
            # tests — if the cycle's red test is ITSELF faulty (wrong assertion, fabricated
            # failure), no green will ever converge. Only the human can fix it.
            tdd_hint = ""
            if nature == GREEN_NATURE:
                tdd_hint = (f"\nTDD lead: a test written by the red phase of cycle {cycle} may be "
                            f"itself faulty (wrong assertion, fabricated failure). The agents are "
                            f"not allowed to fix it (tests frozen): inspect the cycle's tests, "
                            f"fix them YOURSELF if needed, then relaunch.")
            write_fail_report(
                f"Phase {phase['id']} \"{phase['name']}\" (cycle {cycle}) did not converge after "
                f"{MAX_ATTEMPTS} attempts",
                f"Last blocking point raised by the verification:\n{critic_feedback}{tdd_hint}",
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
    print(f"\n{'='*50}\n🛡️  STEP 5: END-OF-RUN GLOBAL REFACTOR (INTER-CYCLE COMPLEMENT, RE-VERIFIED)\n{'='*50}")
    print("   ℹ️  Each cycle already had its refactor (Beck's 3rd beat): this pass only targets")
    print("      what no single cycle could see alone — INTER-cycle duplication.")

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
   this project is produced in TDD, the tests are its executable specification and are
   authoritative — if a test turns red, the production code is what must be fixed.
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
    mm_audit.start(os.getcwd(), "tdd", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan approval sentinel (spec.md deleted since) must never validate a FUTURE
    # spec: purge it before anything else.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

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
        print(f"   Phases: {len(blackboard['phases'])} "
              f"({len(blackboard['phases']) // 2} TDD cycle(s) red → green)")
        for p in blackboard['phases']:
            skills = ', '.join(p.get('skills_required', []))
            covers = ', '.join(p.get('covers', []))
            own_cmd = (p.get('verify_cmd') or '').strip()
            extra = f" — specific verify: {own_cmd}" if own_cmd else ""
            icon = "🔴 red " if str(p.get('nature') or '').strip().lower() == RED_NATURE else "🟢 green"
            print(f"   Phase {p['id']} [cycle {p.get('cycle', '?')} {icon}]: {p['name']} [{skills}] "
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

    print(f"\n🚀 Starting TDD production (red → green cycles): {blackboard.get('project', '')}")

    # Step 4: Production loop (TDD cycles: red = the suite must fail,
    # green = the suite must pass, cycle refactor re-verified with rollback)
    run_production_phases(blackboard, user_need, need_is_spec)

    # Step 5: Complementary global refactor re-verified (inter-cycle duplication; each cycle already played its 3rd beat)
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
    print("\n🏁 [CONGRATULATIONS] The TDD factory closed all its cycles (red → green → refactor) in a single run!")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    GITIGNORE_BODY=GITIGNORE_BODY,
    MAX_VERIFY_RETRIES_ON_TIMEOUT=MAX_VERIFY_RETRIES_ON_TIMEOUT,
    PIPELINE_SKILLS=PIPELINE_SKILLS,
    POLL_INTERVAL=POLL_INTERVAL,
    REFACTO_FIX_PHASE_ID=REFACTO_FIX_PHASE_ID,
    REQUIRED_GLOBAL_RULES=REQUIRED_GLOBAL_RULES,
    RUNNER=RUNNER,
    SKILLS_DIR=SKILLS_DIR,
    TMP_CODER_FILE=TMP_CODER_FILE,
    US_HEADING_RE=US_HEADING_RE,
    _GIT=_GIT,
    _ORCH_BASENAMES=_ORCH_BASENAMES,
    _PHASE_STATUS_SEEN=_PHASE_STATUS_SEEN,
    _TEST_COUNT=_TEST_COUNT,
    parse_skill_frontmatter=parse_skill_frontmatter,
    run_git=run_git,
    write_fail_report=write_fail_report,
)


if __name__ == "__main__":
    main()
