#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_core — the SHARED functions of the orchestrators (plan-big-last, Lot 4a)
─────────────────────────────────────────────────────────────────────────────
Embedded module (NEVER an entry point: excluded from build.yml's Nuitka loop,
like mm_runner and mm_audit). Every function in this file was duplicated
identically — AST AND STRINGS — across several orchestrators: the extraction is a
COPY, generated and verified by tools/migrate_mm_core.py, never a rewrite.
A logic fix now happens HERE, once (× 2 languages), instead of
N files × 6 variants.

Configuration contract: the functions reference the orchestrator's constants and
objects (RUNNER, BLACKBOARD_FILE, _GIT…). Each orchestrator calls ONCE, at the end
of its module (all its names are then defined, nothing has run yet):

    mm_core.configure(RUNNER=RUNNER, BLACKBOARD_FILE=BLACKBOARD_FILE, ...)

One process = one orchestrator: this module-level state cannot conflict.
MUTABLE objects (_GIT, _TEST_COUNT…) are SHARED by reference:
both sides see the same mutations, exactly as before the extraction.
"""

import os
import re
import sys
import time
import subprocess
import shlex
import shutil
import yaml

from mm_runner import resolve_timeout

import mm_audit

# Canonical constants used in DEFAULT arguments (bound at def time) —
# same values as in every orchestrator, computed at the same moment (import).
MAX_PHASE_TIMEOUT = resolve_timeout("phase", 600)
VERIFY_TIMEOUT = resolve_timeout("verify", 300)
VERIFY_FEEDBACK_LIMIT = 4000
MUTATION_TIMEOUT = 300

# Names injected by configure() — placeholders overwritten by the orchestrator at load time:
AGENT_CONFIG_FILE = None
BLACKBOARD_FILE = None
CYCLE_REFACTO_PHASE_ID = None
GITIGNORE_BODY = None
IMPACT_DONE_SENTINEL = None
IMPACT_FILE = None
IMPACT_PHASE_PREFIX = None
IMPL_NATURE = None
MAX_ATTEMPTS = None
MAX_VERIFY_RETRIES_ON_TIMEOUT = None
PIPELINE_SKILLS = None
PLAN_FILE = None
POLL_INTERVAL = None
REFACTO_DONE_SENTINEL = None
REFACTO_FIX_PHASE_ID = None
REFACTO_REPORT_FILE = None
REQUIRED_GLOBAL_RULES = None
RUNNER = None
SCAFFOLD_TIMEOUT = None
SKILLS_DIR = None
SPEC_FILE = None
TMP_ARCHITECT_FILE = None
TMP_CODER_FILE = None
TMP_IMPACT_FILE = None
TMP_PLAN_FILE = None
TMP_PO_FILE = None
TMP_PROMPT_BUFFER = None
TMP_REFACTO_FILE = None
TMP_REPAIR_FILE = None
TMP_TRIAGE_FILE = None
TMP_VERIFIER_FILE = None
TMUX_SESSION = None
UI_EXTENSIONS = None
US_HEADING_RE = None
_GIT = None
_ORCH_BASENAMES = None
_PHASE_STATUS_SEEN = None
_TEST_COUNT = None
cleanup_pipeline_sentinel = None
parse_skill_frontmatter = None
run_git = None
wait_for_pipeline_file = None
write_fail_report = None


def configure(**names):
    """Injects the orchestrator's constants and objects (called ONCE per
    orchestrator, at the end of its module). Deliberately blunt: the extraction is
    an identical copy, the functions read the same NAMES as before."""
    globals().update(names)


def append_arbitration(phase_id, accepted: bool):
    """Record the human decision in the arbitration report (audit trail, best-effort)."""
    try:
        with open(impact_phase_file(phase_id), "a", encoding="utf-8") as f:
            f.write("\n## Human decision\n")
            f.write("ACCEPTED: the impact is endorsed, the tests concerned are deleted by the orchestrator.\n"
                    if accepted else
                    "REJECTED: the historical behavior prevails, the phase is fixed while preserving it.\n")
    except OSError:
        pass

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
            # Architect decisions carried from the plan. 'nature' and 'cycle' are
            # MANDATORY in TDD mode (validated as fatal BEFORE this call): the defaults here
            # are only anti-KeyError nets, never a degraded mode.
            phase.setdefault("nature", "")
            phase.setdefault("cycle", "")
            phase.setdefault("context", "")
            phase.setdefault("files_to_read", [])

def build_coder_prompt(phase: dict, blackboard: dict, user_need: str,
                       skills_context: str, critic_feedback: str, attempt: int) -> str:
    verify_cmd = resolve_verify_cmd(phase, blackboard)

    # 'nature' is the Architect's decision, copied by the compiler: it drives the test
    # policy line. Absent or unknown → neutral wording (old blackboards stay valid).
    nature = str(phase.get("nature") or "").strip().lower()
    if nature == "feature":
        nature_line = ("This phase's nature is 'feature': you create or modify NO test — "
                       "another phase of the plan is dedicated to tests.")
    elif nature == "tests":
        nature_line = ("This phase's nature is 'tests': your mission is precisely to write "
                       "the tests requested by this checklist.")
    else:
        nature_line = ("The plan drives the nature of your work: write or modify tests ONLY "
                       "if a task of this phase explicitly asks for it.")

    # Production-code editing policy, driven by nature (tests-only guard §6.6): in a 'tests'
    # phase production is FROZEN (anti-cheat + foundation of brick B); otherwise the coder may
    # fix a production bug revealed by the suite. The orchestrator mechanically enforces this
    # policy (git restore in a tests phase).
    if nature == "tests":
        prod_edit_policy = ("In a 'tests' phase, you only modify test files: production code is "
                            "FROZEN. If a test you write reveals a real bug in the production "
                            "code, do NOT fix it — let the verification fail, a human will "
                            "arbitrate (the orchestrator restores by default any production file "
                            "you would modify).")
    else:
        prod_edit_policy = ("You MAY modify existing production code if needed to make the "
                            "verification pass (the suite may reveal a bug in an earlier feature "
                            "to fix).")

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
You implement ONLY the tasks of THIS phase and stop as soon as they are done.
Do NOT do work planned for other phases: another phase of the plan may be dedicated to
tests or to another feature. YAGNI principle: nothing that is not explicitly requested
by this phase's checklist.

--- AUTOMATIC VERIFICATION OF THIS PHASE ---
{nature_line}
{prod_edit_policy}
You NEVER delete NOR weaken an existing test to make the verification pass: if an
existing test turns red, the code is what must be fixed.
If you write tests, they must be EXECUTABLE and FAST: NO Testcontainers, NO Docker, and
no network or database I/O.
Before writing tests, FIRST read the source files you are testing to learn their real
signatures, and test the EXPECTED BEHAVIOR (never an always-true assertion).
The orchestrator automatically runs this phase's verification command
« {verify_cmd} » (universal verdict: compilation + full suite): it MUST succeed
(exit code 0), otherwise the phase is rejected. This is your ONLY success criterion.

{context_block}{files_block}--- NEED (spec slice covered by this phase) ---
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

def build_correction_prompt(phase: dict, blackboard: dict, failure_output: str,
                            phase_cmd: str, attempt: int) -> str:
    """Instructions for the Corrector, after a human REJECTION of an unplanned impact: the
    old behavior prevails, the phase adapts to it, and the arbitration is recorded (decision 5)."""
    full_context = f"""--- SYSTEM RULES ---
Stack: {blackboard['global_rules']['target']}
Prohibitions: {blackboard['global_rules']['constraints']}

--- CONTEXT ---
The human REJECTED the impact described in '{impact_phase_file(phase['id'])}': the HISTORICAL behavior (the one of the failing tests) prevails and must be preserved.

--- OUTPUT OF THE FAILING VERIFICATION ---
{truncate_output(failure_output)}

--- YOUR MISSION ---
1. Fix the PRODUCTION code so that the command “{phase_cmd}” succeeds (exit code 0): the existing tests are preserved and must pass again. You modify, delete or disable NO test file (frozen, checked by git diff).
2. Preserve as much as possible of the work of phase {phase['id']} “{phase['name']}” as long as it does not contradict the historical behavior; remove or adjust whatever contradicts it.
3. Record your arbitration: append at the END of '{impact_phase_file(phase['id'])}' an '## Applied fix' section explaining what was wrong and what you did to set it right.
As your very LAST action, create the sentinel file '{correction_sentinel(phase['id'], attempt)}' at the root (content: the single word done).
"""
    with open(TMP_REPAIR_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the instruction file '{TMP_REPAIR_FILE}' at the project root and follow its instructions scrupulously."

def build_mutation_targets(phase: dict) -> list:
    """Files to mutate for a 'tests' phase = its 'files_to_read' filtered on existence.

    'files_to_read' (Required input) lists the PRODUCTION sources the tester reads, i.e. what
    it is supposed to test: that is the natural targeting (no new field). We drop missing paths
    (a listed-but-never-created file cannot be mutated) and test files themselves (we mutate
    production, not tests).
    """
    out = []
    for p in (phase.get("files_to_read") or []):
        clean = str(p).strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if clean and os.path.exists(clean) and not is_test_file(clean):
            out.append(clean)
    return out

def build_phase_verifier_prompt(phase: dict, blackboard: dict, user_need: str,
                                touched_files: list, attempt: int) -> str:
    """Instructions for the phase LLM Verifier (decision 2 of the Yolo plan).

    A green suite proves “nothing is broken”, not “the phase did all of its work”: an
    independent fresh-context agent confronts the code actually produced with EACH task of
    the phase in the blackboard. It can only REJECT (hand back to the coder, one attempt
    consumed): the DONE stamp remains the orchestrator's act, after THIS verdict AND the
    mechanical verdict."""
    files_block = "\n".join(f"- {p}" for p in touched_files) if touched_files \
        else "(no file declared — explore the project with your tools to find the coder's work)"

    full_context = f"""You are a strict and independent Senior QA Verifier Agent. The test suite is ALREADY green: your mission is NOT to re-run the tests, but to verify that Phase '{phase['name']}' has ACTUALLY delivered everything its checklist asks for.

--- GLOBAL RULES TO ENFORCE ---
Architecture: {blackboard['global_rules']['target']}
Prohibitions: {blackboard['global_rules']['constraints']}

--- NEED COVERED BY THE PHASE ---
{user_need}

--- PHASE CHECKLIST TO VERIFY ({BLACKBOARD_FILE}) ---
{chr(10).join([f'- {t}' for t in phase['tasks']])}

--- FILES MODIFIED BY THE CODER ---
{files_block}

--- MANDATORY VERIFICATION METHOD ---
1. Open and ACTUALLY READ the content of each file above with your reading tools. Do not rely on any summary.
2. Confront the real code against EACH checklist task AND EACH global rule.
3. Only validate what you have actually observed in the code. You modify NO file of the project.

--- VERDICT ---
Write your conclusion in the sentinel file '{verdict_sentinel(phase['id'], attempt)}' at the project root:
  - If every task is actually implemented and compliant: the FIRST line contains EXACTLY the word "OK" (nothing else).
  - Otherwise: the FIRST line contains EXACTLY the word "REJECTED", then the following lines precisely list the missing or non-compliant tasks.
You NEVER touch the file {BLACKBOARD_FILE}: the orchestrator updates it from your verdict.
"""
    with open(TMP_VERIFIER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the audit file '{TMP_VERIFIER_FILE}' at the project root. Follow its instructions to verify Phase {phase['id']}."

def build_refacto_fix_prompt(blackboard: dict, user_need: str, failure_output: str,
                             verify_cmd: str, attempt: int) -> str:
    """Instructions to FIX a regression revealed by the global suite after the refacto.

    Same channel as the coder (file-deported prompt + sentinel). The agent fixes the faulty
    PRODUCTION code, without undoing the refacto nor weakening/deleting tests.
    """
    full_context = f"""--- SYSTEM RULES ---
Stack: {blackboard['global_rules']['target']}
Prohibitions: {blackboard['global_rules']['constraints']}

--- CONTEXT ---
The project was produced phase by phase then polished (final refactoring). Re-running the FULL
verification SUITE surfaced a REGRESSION: the refactoring likely broke a feature validated earlier.

--- INITIAL NEED (reference) ---
{user_need}

--- OUTPUT OF THE FAILING VERIFICATION ---
{failure_output}

--- YOUR MISSION ---
Fix ONLY the regression above so that the command « {verify_cmd} » succeeds (exit code 0). Do
NOT undo the refactoring improvements without necessity; do NOT delete or weaken tests to make
the suite pass: fix the faulty production code. This is your ONLY success criterion.

--- MANDATORY END INSTRUCTION ---
You NEVER touch the file {BLACKBOARD_FILE}. As your very last action, create the sentinel file
'{done_sentinel(REFACTO_FIX_PHASE_ID, attempt)}' at the root, containing the list of modified
files (one path per line).
"""
    with open(TMP_CODER_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)
    return f"Read the task file '{TMP_CODER_FILE}' at the project root and fix the described regression."

def build_repair_prompt(phase: dict, blackboard: dict, failure_output: str,
                        phase_cmd: str, attempt: int) -> str:
    """Instructions for the Repairer: absorb an UNPLANNED side effect without touching the
    tests (frozen, checked by git diff) nor sacrificing the phase's behavior — the mirror of
    Guided-Fix's 'regression' mode, moved into the run."""
    full_context = f"""--- SYSTEM RULES ---
Stack: {blackboard['global_rules']['target']}
Prohibitions: {blackboard['global_rules']['constraints']}

--- CONTEXT ---
During phase {phase['id']} “{phase['name']}”, the verification suite revealed EXISTING tests that fail, and this breakage is NOT covered by the validated impact review ('{IMPACT_FILE}'): it is an unplanned SIDE EFFECT of the phase's work.

--- OUTPUT OF THE FAILING VERIFICATION ---
{truncate_output(failure_output)}

--- YOUR MISSION ---
Make the two coexist: the command “{phase_cmd}” must succeed (exit code 0) WITHOUT sacrificing the behavior the phase has just implemented (checklist: {'; '.join(phase['tasks'])}).
ABSOLUTE RULES:
1. You modify, delete or disable NO test file: they are FROZEN (any modification will be detected by git diff and reverted). Fix the PRODUCTION code.
2. You do not undo the phase's work: the behavior its checklist asks for must stay implemented.
3. EXCEPTION CASE — a true inconsistency: if you find that the old tested behavior and the new required behavior are LOGICALLY INCOMPATIBLE (this is not a code bug: the two cannot coexist), write NO shaky fix. Create the file '{impact_phase_file(phase['id'])}' at the root describing: the old behavior (and its tests, real paths), the new behavior required by the phase, and why they exclude each other. Then write in the sentinel below the word CONFLICT on the first line, followed by one 'TEST: <path>' line per test file concerned.
As your very LAST action, create the sentinel file '{repair_sentinel(phase['id'], attempt)}' at the root: the single word DONE if you repaired, or the CONFLICT block described above.
"""
    with open(TMP_REPAIR_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the instruction file '{TMP_REPAIR_FILE}' at the project root and follow its instructions scrupulously."

def build_skills_dictionary() -> str:
    """Build the catalog of phase-assignable skills dynamically.

    Scans ./.agents/skills, reads each SKILL.md frontmatter (name + description)
    and excludes the pipeline system skills. The result is injected into the
    ARCHITECT's plan instructions (step 2): the architect declares each phase's
    Skill, and the blackboard compiler then only COPIES that decision. The tool
    adapts to whatever skills are present, with no hard-coded catalog.
    """
    lines = []
    if not os.path.isdir(SKILLS_DIR):
        return ""
    for entry in sorted(os.listdir(SKILLS_DIR)):
        if entry in PIPELINE_SKILLS:
            continue
        skill_path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.exists(skill_path):
            continue
        name, description = parse_skill_frontmatter(skill_path)
        keyword = name or entry
        desc = description or "(no description provided)"
        lines.append(f'"{keyword}" : {desc}')
    return "\n".join(lines)

def build_triage_prompt(phase: dict, failure_output: str, attempt: int) -> str:
    """Instructions for the Triage Agent: is each failing test file covered by the VALIDATED
    impact review? Read-only, verdict line by line, doubt = UNPLANNED (the repairer is the
    safe path; a wrongful deletion cannot be taken back)."""
    impact_content = read_impact_review() or "(impact review absent: treat every breakage as UNPLANNED)"

    full_context = f"""You are a mechanical and cautious Triage Agent. During phase {phase['id']} “{phase['name']}”, the verification suite fails. Your mission: determine, for EACH failing test file, whether its breakage was PLANNED by the impact review validated by the human, or UNPLANNED.

--- IMPACT REVIEW VALIDATED BY THE HUMAN ({IMPACT_FILE}) ---
{impact_content}

--- OUTPUT OF THE FAILING VERIFICATION ---
{truncate_output(failure_output)}

--- MANDATORY METHOD ---
1. Identify the failing test files (REAL paths from the root: check they exist with your reading tools).
2. A file is PREVU only if ALL of its failing tests match an impact from the “Existing behaviors that are going to break” section above. A file mixing planned and unplanned failures is IMPREVU. When in doubt: IMPREVU (a repairer agent will take over, it is the safe path — a wrongful deletion is irreversible).
3. You modify NO file of the project: your only deliverable is the verdict below.

--- VERDICT ---
Write your conclusion in the sentinel file '{triage_sentinel(phase['id'], attempt)}' at the root: ONE line per failing test file, in the EXACT format:
PREVU: <path of the test file>
or
IMPREVU: <path of the test file or summary of the failure>
No other line.
"""
    with open(TMP_TRIAGE_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return f"Read the instruction file '{TMP_TRIAGE_FILE}' at the project root and follow its instructions scrupulously."

def cleanup_all_sentinels():
    """Final cleanup of all residual sentinels (phases AND pipeline)."""
    for name in os.listdir("."):
        if (name.startswith(".phase_") or name.startswith(".pipeline_")) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass

def cleanup_sentinels(phase_id: int):
    """Remove all sentinels (every attempt) of a phase."""
    prefix = f".phase_{phase_id}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass

def collect_spec_us_ids(spec_text: str) -> set:
    """Ids of the user stories (US-n) declared in the spec."""
    ids = set()
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            ids.add(match.group(1).upper())
    return ids

def commit_phase(label: str) -> bool:
    """Commit the whole working tree (best-effort; failure → warn and keep going).

    --allow-empty: a green phase that changed nothing still gets its landmark commit,
    so per-phase shas stay reliable for diffs and rollback.
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

def correction_sentinel(phase_id: int, attempt: int) -> str:
    """End of the Corrector's pass (after a human rejection of an unplanned impact)."""
    return f".phase_{phase_id}.attempt{attempt}.correction.done"

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

def done_sentinel(phase_id: int, attempt: int) -> str:
    """File written by the Coder at the very end of a phase (signal 'I'm done').

    The attempt number is part of the name: a sentinel written late by the agent
    of a previous attempt cannot be mistaken for the current attempt's signal
    (no false positive on phase completion).
    """
    return f".phase_{phase_id}.attempt{attempt}.done"

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

def ensure_orchestration_ignored():
    """On a pre-existing HUMAN git repo, ensures the orchestrator's EPHEMERAL artifacts are
    listed in .gitignore (append-only, idempotent, best-effort).

    Without this, MAIster-Mind rewrites them every phase and, if they end up git-tracked, its
    'git diff'-based guards would take them for modified code. is_orchestration_file already
    protects the in-memory guards; this additionally avoids dirtying the repo and its diffs.
    NEVER touches already-tracked files (no 'git rm', left to the human) nor audit
    deliverables (blackboard/spec/plan, deliberately committed).
    """
    if not os.path.exists(".gitignore"):
        return
    wanted = [ln for ln in GITIGNORE_BODY.splitlines() if ln.strip() and not ln.startswith("#")]
    try:
        with open(".gitignore", "r", encoding="utf-8") as f:
            present = {ln.strip() for ln in f.read().splitlines()}
    except OSError:
        return
    missing = [p for p in wanted if p not in present]
    if not missing:
        return
    try:
        with open(".gitignore", "a", encoding="utf-8") as f:
            f.write("\n# MAIster-Mind orchestration artifacts (added automatically)\n")
            f.write("\n".join(missing) + "\n")
        print(f"✓ {len(missing)} orchestration pattern(s) added to the existing .gitignore.")
    except OSError:
        pass

def ensure_phase_repo():
    """Per-phase git safety net, set up BEFORE the scaffold (best-effort).

    If the project is already a git repo (human-managed), it is reused AS IS. Otherwise
    'git init' + a minimal .gitignore (ephemeral orchestration files only) + a baseline
    commit. Without git: warn once and run in degraded mode without guards.
    """
    if shutil.which("git") is None:
        print("⚠️  git not found: per-phase commits, test-file protection and refacto "
              "rollback are disabled for this run.")
        return
    if os.path.isdir(".git"):
        _GIT["enabled"] = True
        ensure_orchestration_ignored()
        print("✓ Existing git repo reused (per-phase commits enabled).")
        return
    ok, _ = run_git(["init", "-q"])
    if not ok:
        print("⚠️  'git init' failed: per-phase commits, test-file protection and refacto "
              "rollback are disabled for this run.")
        return
    if not os.path.exists(".gitignore"):
        with open(".gitignore", "w", encoding="utf-8") as f:
            f.write(GITIGNORE_BODY)
    _GIT["enabled"] = True
    commit_phase("baseline: factory start")

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

def fail_pipeline(message: str):
    """Single exit point for pipeline step failures (steps 1 to 3).

    Always kills the tmux session BEFORE exiting: an exit that leaves the agent alive
    lets it finish writing its deliverable AFTER the orchestrator gave up — on relaunch,
    that half-validated file would be mistaken for a valid resume state (this is how a
    never-approved spec used to become the source of truth). RUNNER.kill() is a no-op when
    no session exists, so this helper is safe everywhere.
    """
    print(message)
    write_fail_report("Pipeline step failure", message)
    RUNNER.kill()
    sys.exit(1)

def files_changed_since_phase_start(start_sha: str) -> set:
    """Set of files modified/created since the phase started (the robust signal of the
    anti-ghost guard, PHASE scale). Empty without git or without a sha → the caller falls
    back to the mtime check.

    No intermediate commit is made during a phase: the work lives in the working tree. So we
    compare the tree to the phase-start sha ('git diff <sha>', tracked files) and add the
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

def generate_impact_review_tui():
    """Impact Review Agent: crosses the plan with the EXISTING code and materializes in
    'impact.md' the current behaviors the evolution is going to break. Placed AFTER the plan
    (it needs it) and BEFORE the blackboard: the human arbitrates the breakages at the moment
    when fixing costs the least, and the validated list then drives the triage of the red
    path in production (a breakage endorsed here will never block the run again)."""
    print("\n🔎 [STEP 2BIS: IMPACT REVIEW AGENT] Analyzing the plan's impact on the existing code...")

    impact_prompt = f"""You are an independent and cautious Impact Review Agent. The implementation plan '{PLAN_FILE}' is going to evolve this project: your mission is to identify the EXISTING BEHAVIORS this plan is going to BREAK, so that the human validates them BEFORE production (nobody must discover mid-run that the evolution blows the application up).

MANDATORY method:
1. Read '{PLAN_FILE}' (the plan) and '{SPEC_FILE}' (the validated specification).
2. Explore the project's EXISTING code and its test suite with your reading tools (if the project is empty, simply state it).
3. For each current behavior the plan is going to modify or remove, describe PRECISELY: the behavior observable today, the test files that carry it (real, verified paths), and the part of the plan that breaks it.

Write the result in '{IMPACT_FILE}' at the root, with EXACTLY this structure:
# Impact review
## Existing behaviors that are going to break
(one '### IMPACT-<n> — <short title>' block per behavior, containing three lines: 'Current behavior: ...', 'Carrying tests: <real paths>', 'Cause in the plan: ...'. If there is NO impact at all — empty project or purely additive plan — write instead the single line: 'No impact: <short justification>.')
## Deletion log
(leave this section EMPTY: it is reserved for the orchestrator.)

Zero invention: only list what you have actually observed in the real code; when in doubt, mention the impact (the human will decide). You modify no other file.
Do it directly via your file editing tools, without unnecessary chatter in the console.
As your very LAST action, after saving '{IMPACT_FILE}', create the sentinel file '{IMPACT_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
"""
    with open(TMP_IMPACT_FILE, "w", encoding="utf-8") as f:
        f.write(impact_prompt)
    cleanup_pipeline_sentinel(IMPACT_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(f"Read the instruction file '{TMP_IMPACT_FILE}' at the project root and follow its instructions scrupulously."))
    RUNNER.send_task(f"Read the instruction file '{TMP_IMPACT_FILE}' at the project root and follow its instructions scrupulously.")

    if wait_for_pipeline_file(IMPACT_FILE, IMPACT_DONE_SENTINEL):
        print(f"✅ [STEP 2BIS] Impact review '{IMPACT_FILE}' created successfully!")
    else:
        fail_pipeline(f"❌ [STEP 2BIS] Timeout or failed to create '{IMPACT_FILE}'.")

def git_head_sha() -> str:
    """Current HEAD sha, or empty string without git/commits."""
    ok, out = run_git(["rev-parse", "HEAD"])
    return out if ok else ""

def impact_phase_file(phase_id) -> str:
    """Mid-run arbitration report of an UNPLANNED impact (impact-phase-<id>.md, committed)."""
    return f"{IMPACT_PHASE_PREFIX}{phase_id}.md"

def inject_skills_dictionary(text: str) -> str:
    """Substitute the REAL skills catalog into a pipeline skill's instructions.

    The dictionary goes to the ARCHITECT (step 2), who declares each phase's Skill
    in the plan; the blackboard compiler then only COPIES that decision. Routing is
    thus decided by the agent with the most context, never by the weakest link.
    """
    skills_dictionary = build_skills_dictionary()
    if "{{SKILLS_DICTIONARY}}" in text:
        return text.replace("{{SKILLS_DICTIONARY}}", skills_dictionary)
    return text + f"\n\nAUTHORIZED SKILLS DICTIONARY:\n{skills_dictionary}\n"

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
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Python caches, virtual environment and tooling directories: never produced code.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False

def is_test_file(path: str) -> bool:
    """Best-effort naming heuristic: does 'path' look like a test file?

    Multi-language and agnostic (tests/__tests__/spec directories, conventions test_*.py,
    *_test.go, *.test.ts, *.spec.js, *Test.java/*Spec.kt). Deliberately WIDE on the test side:
    when in doubt we classify as test, so as NOT to stall a legitimate tests phase on a false
    "modified production file" (the tests-only guard only restores what is NOT a test). False
    positive possible (a helper off-convention): the feedback names the files, the human
    arbitrates, exactly like protected_test_files.
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
    """Is 'name' (bare file name) an interface source to audit?

    Deliberately pragmatic: known UI extensions, MINUS the tooling that shares these
    extensions without being interface — minified bundles (unreadable, generated),
    TypeScript declarations, configuration files (vite/webpack/tailwind…),
    Storybook stories (demo, not product), dotfiles.
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

def load_blackboard() -> dict:
    with open(BLACKBOARD_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_skills(skills_list: list) -> str:
    content = ""
    for skill in skills_list:
        skill_path = os.path.join(SKILLS_DIR, skill, "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                content += f"--- SKILL: {skill.upper()} ---\n{f.read()}\n\n"
        else:
            print(f"   ⚠️  Missing skill: '{skill}' (expected path: {skill_path})")
    return content

def lot_closing_ids(phases: list) -> set:
    """Ids of the phases that CLOSE their ATDD batch (last phase of their batch's block).

    The batch structure (one contiguous block per batch: a test phase then its
    implementation phases, never a batch without implementation) is validated mechanically
    before production: the last phase of a block is therefore always an 'atdd-impl' phase.
    It is that phase — and it alone — that carries the universal verdict (full suite
    green); the intermediate implementation steps are validated by the compilation alone
    (build_cmd). A POSITION decision, computed here by the orchestrator: never declared
    by an LLM, therefore never hallucinable.
    """
    closing = set()
    for i, phase in enumerate(phases or []):
        if not isinstance(phase, dict):
            continue
        if str(phase.get("nature") or "").strip().lower() != IMPL_NATURE:
            continue
        nxt = phases[i + 1] if i + 1 < len(phases) else None
        if not isinstance(nxt, dict) or str(nxt.get("cycle")) != str(phase.get("cycle")):
            closing.add(phase.get("id"))
    return closing

def mutation_tool_available(cmd: str) -> bool:
    """Best-effort probe: does the mutation command's main executable respond? (§6.5)

    "Tool absent" must NEVER be mistaken for "surviving mutants": without this probe, a stack
    lacking the tool would fail a run for nothing. We extract the first useful executable
    (skipping leading VAR=val assignments) and test its presence (shutil.which / file). WHEN IN
    DOUBT, we consider the tool present (best-effort): the probe must never, on its own, disable
    a declared brick B.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return True  # unparsable command: do not block brick B on a doubt
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1  # skip leading environment assignments (e.g. 'CI=1 mvn ...')
    if i >= len(tokens):
        return True
    exe = tokens[i]
    # 'npx <tool>' / 'npm exec <tool>': these launchers download on demand, so we consider them
    # available as soon as the launcher itself is present.
    if exe in ("npx", "npm", "pnpm", "yarn", "bunx"):
        return shutil.which(exe) is not None
    if "/" in exe or "\\" in exe:
        return os.path.exists(exe) or shutil.which(os.path.basename(exe)) is not None
    if shutil.which(exe) is not None:
        return True
    # Local JS binary without prefix (e.g. 'stryker' installed in node_modules/.bin).
    local_bin = os.path.join("node_modules", ".bin", exe)
    return os.path.exists(local_bin) or os.path.exists(local_bin + ".cmd")

def no_declared_file_touched(files: list, since_ts: float, changed_since_phase: set = None) -> bool:
    """True if NO declared file actually changed SINCE THE PHASE STARTED.

    Signature of the "ghost coder": sentinel written without real work. The full-suite
    verdict CANNOT catch this case (nothing changed → everything stays green): this cheap,
    agnostic check takes care of it. Reference = the PHASE, not the attempt: a file produced
    in one attempt and re-declared unchanged in the next is still recognized as real work
    (deliberately LENIENT — ONE actually-touched file IN THE PHASE is enough to pass). A
    per-attempt reference wrongly reclassified as "ghost" a file written in an earlier
    attempt. Two signals: 'changed_since_phase' (git diff since the phase start, robust and
    primary — insensitive to the truncated mtimes of DrvFs/WSL2) then, as a fallback without
    git, the mtime since the phase start ('since_ts').
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

def parse_test_count(output: str):
    """Best-effort count of PASSED tests in a runner's output; None when no known
    pattern matches (unknown runner, garbled output).

    Recognized: Maven "Tests run: N" (last summary line wins), vitest/jest
    "Tests: N passed", pytest/cargo "N passed", go test -v "--- PASS:" lines.
    """
    if not output:
        return None
    maven = re.findall(r"Tests run:\s*(\d+)", output)
    if maven:
        return int(maven[-1])
    vitest = re.findall(r"Tests:?\s+(\d+)\s+passed", output)
    if vitest:
        return int(vitest[-1])
    generic = re.findall(r"(\d+)\s+passed", output)
    if generic:
        return int(generic[-1])
    go_passes = len(re.findall(r"^--- PASS:", output, re.MULTILINE))
    if go_passes:
        return go_passes
    return None

def print_failure_message(phase: dict, blackboard: dict, critic_feedback: str):
    model = RUNNER.configured_model()
    done_count = sum(1 for p in blackboard["phases"]
                     if p.get("status") == "DONE" and p.get("verdict") == "OK")
    print(f"""
{'='*60}
❌ Phase {phase['id']} "{phase['name']}" did not converge after {MAX_ATTEMPTS} attempts.

   Last blocking point raised by the verification:
   "{critic_feedback}"

💡 The current model ({model}) is stuck on this specific step.
   Most effective: relaunch after bringing in a model one notch above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the {done_count} already-validated phase(s) will be resumed
   automatically, you don't start from scratch. See you soon! 🚀
{'='*60}
""")

def read_impact_review() -> str:
    """Content of the validated impact review (empty if absent: all-UNPLANNED triage)."""
    if not os.path.exists(IMPACT_FILE):
        return ""
    with open(IMPACT_FILE, "r", encoding="utf-8") as f:
        return f.read()

def read_repair_outcome(phase_id: int, attempt: int) -> tuple:
    """Read the Repairer's sentinel. Returns (is_conflict: bool, conflict_tests: list).

    DONE (or any non-CONFLICT content) = repair claimed, the verdict remains the
    re-verification by execution. CONFLICT = a true inconsistency declared: the
    'TEST: <path>' lines list the test files concerned (for the mechanical deletion if the
    human endorses the impact).
    """
    path = repair_sentinel(phase_id, attempt)
    if not os.path.exists(path):
        return False, []
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines or not lines[0].upper().startswith("CONFLICT"):
        return False, []
    tests = []
    for line in lines[1:]:
        m = re.match(r"^TEST\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            tests.append(m.group(1).strip())
    return True, tests

def read_touched_files(phase_id: int, attempt: int) -> list:
    """Read the list of files declared by the Coder in its .done sentinel.

    Small models often format the list as bullets ('- src/foo.ts', '* a.py', '1. b.go'):
    leading list markers are stripped, otherwise every line would fail the
    os.path.exists check downstream (false "ghost coder" with misleading feedback).
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

def read_triage(phase_id: int, attempt: int) -> tuple:
    """Parse the triage sentinel. Returns (prevu: list, imprevu: list).

    Any line that does not match the 'PREVU: ...' / 'IMPREVU: ...' format is ignored: a
    garbled triage degrades towards the repairer (the safe path), never towards a deletion.
    """
    path = triage_sentinel(phase_id, attempt)
    prevu, imprevu = [], []
    if not os.path.exists(path):
        return prevu, imprevu
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^(PREVU|IMPREVU)\s*:\s*(.+)$", line.strip(), re.IGNORECASE)
            if not m:
                continue
            (prevu if m.group(1).upper() == "PREVU" else imprevu).append(m.group(2).strip())
    return prevu, imprevu

def read_verdict(phase_id: int, attempt: int) -> tuple:
    """Read the Verifier's verdict. Returns (is_ok: bool, feedback: str).

    Tolerant parsing (taken from Coding-Without-Tests): leading blank lines and markdown
    fences are skipped, then the first word of the first meaningful line is read. 'OK',
    'OK.', 'OK, compliant'... validate; everything else (including 'REJECTED') rejects.
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

def record_test_count(output: str, blackboard: dict, expect_growth: bool = False):
    """Persist the last parsable passed-test count in the blackboard (survives resumes).

    A 'tests' phase that goes green WITHOUT strictly increasing the count only gets a
    console warning: a weak signal, deliberately not a verdict (re-organizations happen).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if expect_growth and isinstance(old_count, int) and new_count <= old_count:
        print(f"⚠️  'tests' phase went green without increasing the suite "
              f"({old_count} → {new_count} passed): weak or duplicate tests?")
    blackboard["last_test_count"] = new_count
    save_blackboard(blackboard)

def red_suite_damage(output: str, blackboard: dict):
    """Feedback message when a 'tdd-red' phase DAMAGED the existing suite, else None.

    After a legitimate red, the suite fails because of the NEW tests: the pre-existing
    tests, for their part, must keep passing (the production code is frozen and the tests of
    previous cycles are protected). If the count of PASSING tests decreased compared to the
    last recorded green state, the phase broke existing code (fixture or shared state,
    editing a test outside protection like the scaffold health test): it is a red for the
    WRONG reason, rejected. Non-parsable output → guard inactive — this is the NORMAL case of
    a red that breaks compilation (API not created yet): no count is emitted, and that red is
    legitimate.
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return None
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count < old_count:
        return (f"Your red phase broke EXISTING tests: {old_count} passing before, "
                f"{new_count} now. A legitimate red ADDS tests that fail, without "
                f"touching the already-green tests: restore what you broke (modified existing "
                f"test, fixture or shared state…) and make the suite fail ONLY through "
                f"this cycle's new tests.")
    return None

def repair_sentinel(phase_id: int, attempt: int) -> str:
    """End of the Repairer's pass (DONE, or CONFLICT + TEST: lines)."""
    return f".phase_{phase_id}.attempt{attempt}.repair.done"

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

def resolve_build_cmd(phase: dict, blackboard: dict) -> str:
    """COMPILATION-ONLY command of a phase: the phase's 'build_cmd', else the global one.

    It is the verdict of the INTERMEDIATE implementation steps of a batch: the tree must
    COMPILE, the batch's acceptance suite is allowed to stay red until the closing.
    Contract carried by the plan (and validated by the human at the y/n): this command
    compiles the PRODUCTION ONLY, never the test files — otherwise it would stay red as
    long as the whole API expected by the acceptance tests does not exist, and no
    intermediate step would pass. Returns an empty string if nothing is defined (fatal in
    validation as soon as a batch has several implementation phases).
    """
    return (phase.get("build_cmd") or blackboard.get("build_cmd") or "").strip()

def resolve_mutation_cmd(phase: dict, blackboard: dict) -> str:
    """Mutation testing command: the phase's 'mutation_cmd', else the global one.

    Optional and non-blocking (same path as verify_cmd / build_cmd). Empty → brick B
    inactive. May contain the '{targets}' placeholder, substituted by the files to mutate.
    """
    return (phase.get("mutation_cmd") or blackboard.get("mutation_cmd") or "").strip()

def resolve_verify_cmd(phase: dict, blackboard: dict) -> str:
    """Verification command for a phase: the phase's 'verify_cmd', else the global one.

    UNIVERSAL verdict: by default, every phase is validated by the global 'verify_cmd'
    (compilation + full suite). The phase field only exists as an EXCEPTION declared by
    the Architect in the plan. Returns an empty string if nothing is defined (handled
    upstream).
    """
    return (phase.get("verify_cmd") or blackboard.get("verify_cmd") or "").strip()

def restore_test_files(paths: list):
    """Restore test files touched in violation of the freeze (tracked: git checkout;
    new ones: deletion — the equivalent of restoring a file that did not exist)."""
    if not _GIT["enabled"] or not paths:
        return
    ok_tracked, tracked_out = run_git(["ls-files", "--"] + paths)
    tracked = set(tracked_out.splitlines()) if ok_tracked else set()
    to_restore = sorted(p for p in paths if p in tracked)
    if to_restore:
        run_git(["checkout", "--"] + to_restore)
    for p in paths:
        if p not in tracked:
            try:
                os.remove(p)
            except OSError:
                pass

def run_mutation(cmd: str, timeout: int = MUTATION_TIMEOUT) -> tuple:
    """Run the mutation testing command OUTSIDE tmux. Returns (ok, output, timed_out).

    Modeled on run_verify: shell=True, PATH prefixed with node_modules/.bin (local JS binaries),
    truncated output. ok = exit code 0 — the tool ITSELF encodes its surviving-mutant tolerance
    threshold; Python never parses the result to DECIDE, only for feedback. timed_out
    distinguishes a budget overrun (cost/infra incident, degraded to a warn) from a genuine
    "the suite does not bite enough".
    """
    print(f"   🧬 Mutation testing: {cmd}")
    env = os.environ.copy()
    local_bin = os.path.abspath(os.path.join("node_modules", ".bin"))
    if os.path.isdir(local_bin):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        return False, f"TIMEOUT after {timeout}s during mutation testing.\n{truncate_output(partial)}", True
    except Exception as exc:
        return False, f"Unable to run the mutation command: {exc}", False
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, truncate_output(output), False

def run_verify(cmd: str, timeout: int = VERIFY_TIMEOUT) -> tuple:
    """Run the verification command OUTSIDE tmux and return (ok, output, timed_out).

    ok = (exit code 0). output = truncated stdout+stderr, useful as retry feedback for a small
    model. timed_out distinguishes a TIME LIMIT EXCEEDED (infra incident: slow machine/network,
    hung process) from a genuine "red" on the code — which lets us NOT charge a coder attempt
    to a mere timeout (see run_verify_resilient). The Python orchestrator stays the SOLE judge
    of the verdict: we never delegate interpreting the result to an LLM. The command comes from
    the human-validated blackboard (y/n).
    """
    print(f"   🧪 Verification by execution: {cmd}")
    # JS/TS tools (tsc, vitest, vite…) are often installed LOCALLY in node_modules/.bin
    # and absent from the global PATH: under shell=True, /bin/sh fails to find them
    # ("tsc: not found"). So we prefix PATH with node_modules/.bin when it exists, so
    # that bare binaries as well as npx invocations work. Harmless outside the Node
    # ecosystem (the folder is simply absent).
    env = os.environ.copy()
    local_bin = os.path.abspath(os.path.join("node_modules", ".bin"))
    if os.path.isdir(local_bin):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        return False, f"TIMEOUT after {timeout}s while running the verification.\n{truncate_output(partial)}", True
    except Exception as exc:
        return False, f"Unable to run the verification command: {exc}", False
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, truncate_output(output), False

def run_verify_resilient(cmd: str) -> tuple:
    """Verification resilient to INFRA timeouts. Returns (ok, output, persistently_timed_out).

    Exceeding VERIFY_TIMEOUT is almost never a "red" verdict on the code: it is an environment
    incident (slow machine/network, hung process). Counting it as a failure would consume one
    of the coder's MAX_ATTEMPTS (open point in proposition.md). Since the code has not changed
    between two runs, we RE-RUN the command (without relaunching the coder) up to
    MAX_VERIFY_RETRIES_ON_TIMEOUT times to obtain a firm verdict. If every attempt times out,
    we surface timed_out=True (the caller will not consume the attempt).
    """
    output = ""
    for i in range(MAX_VERIFY_RETRIES_ON_TIMEOUT + 1):
        verify_started = time.time()
        ok, output, timed_out = run_verify(cmd)
        if not timed_out:
            mm_audit.event("verdict", cmd=cmd, exit=0 if ok else 1,
                           duration_s=round(time.time() - verify_started, 1),
                           output_bytes=len(output or ""))
            return ok, output, False
        if i < MAX_VERIFY_RETRIES_ON_TIMEOUT:
            print(f"   ⏱️  Verification timed out ({VERIFY_TIMEOUT}s) — likely an infra incident, "
                  f"not a code failure. Re-verification {i + 1}/{MAX_VERIFY_RETRIES_ON_TIMEOUT}...")
    return False, output, True

def save_blackboard(data: dict):
    """Write the blackboard ATOMICALLY (temp file + os.replace).

    The blackboard is the ONLY resume state (which phases are DONE/OK). A kill exactly during a
    classic 'w' dump (which truncates then rewrites in place) would leave a half-written YAML →
    resume impossible, whole run lost. So we write to a temp file, force the flush to disk, then
    rename atomically: the final file is ALWAYS either the old complete version or the new one,
    never a partial state. os.replace is atomic on the same filesystem (POSIX and Windows).
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

def signal_handler(sig, frame):
    print("\n⚠️  Interruption detected. Cleaning up...")
    RUNNER.kill()
    sys.exit(1)

def test_count_regression(output: str, blackboard: dict):
    """Feedback message when the green suite LOST tests vs the recorded count, else None.

    A weakened suite passes its own verdict trivially: the non-decreasing count is the
    cheap mechanical floor. Non-parsable output → guard inactive (warned once per run).
    """
    new_count = parse_test_count(output)
    if new_count is None:
        if not _TEST_COUNT["warned"]:
            print("ℹ️  Test count not parsable from the runner output: the non-decreasing "
                  "guard is inactive for this run (unknown runner).")
            _TEST_COUNT["warned"] = True
        return None
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count < old_count:
        mm_audit.event("guard", name="regression_compte_tests", action="rejet",
                       avant=old_count, apres=new_count)
        return (f"The verification suite LOST tests: {old_count} passing before, "
                f"{new_count} now. Deleting, disabling or weakening tests is forbidden: "
                f"restore the missing tests and make them pass by fixing the code.")
    return None

def test_phase_damage(output: str, blackboard: dict):
    """Feedback message when an 'atdd-test' phase DAMAGED the existing suite, else None.

    After a legitimate test phase, the suite fails because of the NEW acceptance tests:
    the pre-existing tests, for their part, must keep passing (the production code is frozen
    and the tests of previous batches are protected). If the count of PASSING tests decreased
    compared to the last recorded green state, the phase broke existing code (fixture
    or shared state, editing a test outside protection like the scaffold health test):
    it is a red for the WRONG reason, rejected. Non-parsable output → guard inactive —
    this is the NORMAL case of a test phase that breaks compilation (API not created yet):
    no count is emitted, and that red is legitimate.
    """
    new_count = parse_test_count(output)
    if new_count is None:
        return None
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count < old_count:
        return (f"Your acceptance test phase broke EXISTING tests: {old_count} "
                f"passing before, {new_count} now. A legitimate test phase ADDS "
                f"tests that fail, without touching the already-green tests: restore what you "
                f"broke (modified existing test, fixture or shared state…) and make the "
                f"suite fail ONLY through this batch's new tests.")
    return None

def triage_sentinel(phase_id: int, attempt: int) -> str:
    """Verdict of the Triage Agent (one PREVU:/IMPREVU: line per failing test file)."""
    return f".phase_{phase_id}.attempt{attempt}.triage"

def truncate_output(text: str, limit: int = VERIFY_FEEDBACK_LIMIT) -> str:
    """Truncate a verification output while keeping the BEGINNING AND the END.

    The previous behavior (keep the last N characters) often lost the essential part:
    on most tools (compilers, pytest, Maven…), the FIRST error — the root cause —
    appears at the beginning of the output, while the end is just a count summary. So
    we keep half beginning / half end, with an explicit marker so the coder knows a
    segment is missing.
    """
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return (text[:head]
            + f"\n[... output truncated ({len(text)} characters in total) ...]\n"
            + text[-tail:])

def validate_all_skills(blackboard: dict):
    referenced = set()
    for phase in blackboard["phases"]:
        for skill in phase.get("skills_required", []):
            referenced.add(skill)

    available = set()
    if os.path.isdir(SKILLS_DIR):
        for entry in os.listdir(SKILLS_DIR):
            if os.path.exists(os.path.join(SKILLS_DIR, entry, "SKILL.md")):
                available.add(entry)

    hallucinated = sorted(referenced - available)
    if hallucinated:
        print(f"\n⚠️  Skills referenced in the blackboard but NOT FOUND (likely architect hallucination): {', '.join(hallucinated)}")
        usable = sorted(available - PIPELINE_SKILLS)
        print(f"   Actually available skills: {', '.join(usable) or '(none)'}")
        print("   → Affected phases will run without these skills. Fix 'blackboard.yaml' if needed before continuing.\n")
    else:
        print(f"✅ All referenced skills exist ({len(referenced)} referenced).\n")

    if not os.path.exists(os.path.join(SKILLS_DIR, "refacto", "SKILL.md")):
        print("⚠️  Skill 'refacto' not found: the final polish step will be degraded.\n")

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
                f"Duplicated phases[].id ({', '.join(duplicated)}): the '.phase_N.attemptM.done' "
                f"sentinels would be SHARED between two phases (false completion signals)."
            )
        elif ids and ids != [str(i) for i in range(1, len(ids) + 1)]:
            soft.append(
                f"phases[].id is not a contiguous 1..N sequence ({', '.join(ids)}): tolerated, "
                f"but check that the compiler did not skip or renumber a phase."
            )
        bad_nature = sorted({str(phase.get("nature")) for phase in phases
                             if isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip()
                             and str(phase.get("nature")).strip().lower() not in ("feature", "tests")})
        if bad_nature:
            soft.append(
                f"phases[].nature outside {{feature, tests}}: {', '.join(bad_nature)} "
                f"(those coder prompts will fall back to the neutral wording)."
            )
        if any(isinstance(phase, dict) and not str(phase.get("nature") or "").strip()
               for phase in phases):
            soft.append(
                "Some phases declare no 'nature' (old blackboard?): their coder prompt "
                "uses the neutral wording instead of the plan-driven one."
            )
        # Part A (informational): a 'tests' phase should cover AT MOST one user story
        # (tester context window, tighter mutated scope). Tolerated, never blocking.
        multi_cover_tests = sorted(
            str(phase.get("id")) for phase in phases
            if isinstance(phase, dict)
            and str(phase.get("nature") or "").strip().lower() == "tests"
            and isinstance(phase.get("covers"), list) and len(phase.get("covers")) > 1)
        if multi_cover_tests:
            soft.append(
                f"'tests' phases covering several user stories ({', '.join(multi_cover_tests)}): "
                f"prefer one tests phase per US (tester context window, mutated scope). "
                f"Tolerated, informational."
            )
        # Brick B (informational): no 'mutation_cmd' while 'tests' phases exist
        # → the check that tests BITE will be inactive. Tolerated (brick B is optional).
        has_test_phase = any(isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower() == "tests"
                             for phase in phases)
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if has_test_phase and not has_mutation_cmd:
            soft.append(
                "No 'mutation_cmd' declared while 'tests' phases exist: brick B (checking that "
                "tests BITE) will be inactive. Tolerated; declare it in the plan for falsifiable "
                "tests."
            )
    if not (blackboard.get("verify_cmd") or "").strip():
        fatal.append(
            "Missing global verification command 'verify_cmd': it is the fallback for phases "
            "without their own 'verify_cmd' AND the lock of the scaffold step. Without it, the "
            "scaffold is skipped and a phase with no dedicated command cannot be verified."
        )
    return fatal, soft

def verdict_sentinel(phase_id: int, attempt: int) -> str:
    """Verdict of the phase LLM Verifier (first line: OK or REJECTED)."""
    return f".phase_{phase_id}.attempt{attempt}.verdict"

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

def wait_for_file_creation(filepath: str, timeout: int = MAX_PHASE_TIMEOUT) -> bool:
    """Wait for a file to be created and stabilized by the agent in the TUI."""
    start = time.time()
    print(f"   ⏳ Waiting for generation of '{filepath}'...")
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_init = os.path.getsize(filepath)
            time.sleep(1.5)  # Safety to ensure writing is closed
            if os.path.getsize(filepath) == size_init:
                return True
    return False
