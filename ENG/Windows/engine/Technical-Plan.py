#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - PARTIAL "need to blackboard" pipeline (agent harness + tmux)
─────────────────────────────────────────────────────────────────────────────
"TECHNICAL PLAN ONLY" VARIANT: runs steps 1 to 3 of the MAIsterMind pipeline —
PO Agent ('need.md' → 'spec.md' validated by the human), Architect Agent ('spec.md' →
'plan.md'), Blackboard Compiler ('plan.md' → 'blackboard.yaml') — validates the structure
of the produced blackboard, prints the recap, then stops cleanly BEFORE any
production. No scaffold, no code phase, no refactoring.

Why a dedicated entry point:
  - Steps 1 to 3 are the HIGH-LEVERAGE one-shots of the pipeline: this is where a big
    model pays off most, and where the human arbitrates (spec, plan, blackboard). This
    script lets you pay ONLY for that "thinking" part: prepare the technical plan
    today, launch production later (another time, another machine, another
    model — typically a small economical model).
  - SAME FILE CONTRACTS as the full variant: 'spec.md' + '.spec_approved',
    'plan.md', 'blackboard.yaml'. Re-running Safe-Coding.py afterwards resumes these
    deliverables AS IS (steps 1 to 3 skipped) and starts directly at the production y/n.
    This script targets the "universal verdict" variant (Safe-Coding.py): same pipeline
    skills ('plan', 'plan-to-blackboard'), hence the same fields (nature, covers,
    verify_cmd…) in the produced blackboard.

The per-step context window slicing remains the guiding principle: each
agent (PO, Architect, Compiler) runs in a fresh session (/new) and receives
ONLY its instructions and the upstream deliverable — never the history of previous steps.
"""

import os
import re
import sys
import time
import signal
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
    collect_spec_us_ids, load_blackboard, signal_handler,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="techplan", messages={
    "follow": "   👀 Follow the run live in another terminal: tmux attach -t {session}",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
FAIL_REPORT_FILE      = "failReport.md"   # persistent stop report (same contract as the factory)
SKILLS_DIR            = "./.agents/skills"
BLACKBOARD_SKILL_FILE = "./.agents/pipeline/plan-to-blackboard/SKILL.md"
PLAN_SKILL_FILE       = "./.agents/pipeline/plan/SKILL.md"
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"

# Active harness config, as THIS script's messages have always cited it:
# without the leading './' ('.opencode/opencode.json', '.codex/config.toml').
# The prefix is stripped here and not in the runner: the other orchestrators
# cite the './…' form — the migration rewrites no existing message.
AGENT_CONFIG_FILE     = RUNNER.config_file.removeprefix("./")
TMP_PLAN_FILE         = RUNNER.tmp_file("planner")

# Pipeline system skills: never routed to production phases.
PIPELINE_SKILLS       = {"po", "plan", "plan-no-test", "plan-to-blackboard", "refacto"}

# Temporary context routing files
TMP_ARCHITECT_FILE    = RUNNER.tmp_file("architect")
TMP_PO_FILE           = RUNNER.tmp_file("po")

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: the
# only valid choice on all 3 OSes (Windows has no /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# End-of-deliverable sentinels for the pipeline steps (1 to 3): same contract as
# production (the agent creates the .done file AFTER saving the deliverable).
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"
PLAN_DONE_SENTINEL       = ".pipeline_plan.done"
BLACKBOARD_DONE_SENTINEL = ".pipeline_blackboard.done"

# HUMAN approval of the spec, materialized: the mere EXISTENCE of spec.md proves nothing.
# This sentinel SURVIVES the end of this run: it is what Safe-Coding.py will read
# to skip its step 1 when launching production.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session. Prefix DISTINCT from the
# full variants: this script cannot inject a prompt into a production run that would be
# running on the same project.
TMUX_SESSION          = RUNNER.session

POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per step (safety net)
VERIFY_FEEDBACK_LIMIT = 4000           # max size of the excerpts included in the failure report
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net: pipeline deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds)


def fail_pipeline(message: str):
    """Single exit point for pipeline step failures (steps 1 to 3).

    Always kills the tmux session BEFORE exiting: an exit that leaves the agent alive
    lets it finish writing its deliverable AFTER the orchestrator gave up — on relaunch,
    that half-validated file would be mistaken for a valid resume state.
    """
    print(message)
    write_fail_report("Pipeline step failure", message)
    RUNNER.kill()
    sys.exit(1)


# ─── FAILURE REPORT ──────────────────────────────────────────────────────────

def truncate_output(text: str, limit: int = VERIFY_FEEDBACK_LIMIT) -> str:
    """Truncate a long text while keeping the BEGINNING AND the END (the root cause of an
    error usually appears at the beginning, the summary at the end)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return (text[:head]
            + f"\n[... output truncated ({len(text)} characters in total) ...]\n"
            + text[-tail:])


def write_fail_report(title: str, reason: str, details: str = ""):
    """Write a persistent stop report at the root (same contract as the factory:
    any NON-nominal stop produces one). Best-effort: NEVER raises."""
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — MAIsterMind (technical plan)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "", "### Progress"]
        for label, path in ((f"Specification ('{SPEC_FILE}')", SPEC_FILE),
                            (f"Plan ('{PLAN_FILE}')", PLAN_FILE),
                            (f"Blackboard ('{BLACKBOARD_FILE}')", BLACKBOARD_FILE)):
            mark = "✅" if os.path.exists(path) and os.path.getsize(path) > 0 else "⏳"
            lines.append(f"  - {mark} {label}")
        lines.append("")
        if details.strip():
            lines.append("### Details")
            lines.append(truncate_output(details))
            lines.append("")
        lines.append("### Recommended action")
        lines.append("Fix the cause above (or bump the model up one notch via /model in the "
                     f"TUI or '{AGENT_CONFIG_FILE}'), then re-run: the deliverables already produced "
                     "will be resumed automatically.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── FILE MONITOR SYNCHRONIZATION ─────────────────────────────────────────────

def cleanup_pipeline_sentinel(sentinel: str):
    """Remove a residual pipeline sentinel (previous interrupted run)."""
    try:
        os.remove(sentinel)
    except OSError:
        pass


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
    deliverable. SAFETY NET for an agent that forgets the sentinel: if the deliverable
    exists, is non-empty and has not changed for STABLE_POLLS_FALLBACK consecutive checks,
    it is accepted with a warning (graceful degradation). The optional 'structural_check'
    hardens this fallback ONLY: a stable but structurally incomplete deliverable keeps
    waiting (the agent may pause longer than the stability window)
    until the global timeout.
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


# ─── BLACKBOARD & NEED FILE READING ──────────────────────────────────────────

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


# ─── BLACKBOARD SCHEMA VALIDATION (PRODUCED BY A FALLIBLE SMALL LLM) ───────────
# Faithful copy of the Safe-Coding.py validation: the blackboard produced here is
# INTENDED for its production — validating it now avoids discovering, when launching
# production (later, elsewhere), that it is unusable.

REQUIRED_GLOBAL_RULES = ["target", "styling", "constraints", "accessibility"]


def validate_blackboard_schema(blackboard: dict) -> tuple:
    """Check the structure of the blackboard. Returns (fatal, soft).

    fatal: STRUCTURAL gaps on which production would crash or run empty. soft: gaps
    recovered by apply_blackboard_defaults or purely cosmetic. Writes nothing and fixes
    nothing: the human decides.
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
                "Some phases declare no 'nature' (old blackboard?): their "
                "coder prompt uses the neutral wording instead of the plan-driven one."
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
        # → the check that tests BITE will be inactive in production. Tolerated.
        has_test_phase = any(isinstance(phase, dict)
                             and str(phase.get("nature") or "").strip().lower() == "tests"
                             for phase in phases)
        has_mutation_cmd = bool((blackboard.get("mutation_cmd") or "").strip()) or any(
            isinstance(phase, dict) and (phase.get("mutation_cmd") or "").strip()
            for phase in phases)
        if has_test_phase and not has_mutation_cmd:
            soft.append(
                "No 'mutation_cmd' declared while 'tests' phases exist: brick B "
                "(checking that tests BITE) will be inactive in production. Tolerated; declare it "
                "in the plan for falsifiable tests."
            )
    if not (blackboard.get("verify_cmd") or "").strip():
        fatal.append(
            "Missing global verification command 'verify_cmd': it is the fallback for phases "
            "without their own 'verify_cmd' AND the lock of the scaffold step. Without it, "
            "production will not be able to verify its phases."
        )
    return fatal, soft


def apply_blackboard_defaults(blackboard: dict):
    """Fill IN MEMORY the absent non-critical fields, for displaying the recap
    only: this script NEVER writes the blackboard (the file stays as produced
    by the compiler; the production variant will apply its own defaults)."""
    if not isinstance(blackboard, dict):
        return
    global_rules = blackboard.setdefault("global_rules", {})
    if isinstance(global_rules, dict):
        for key in REQUIRED_GLOBAL_RULES:
            global_rules.setdefault(key, "(unspecified)")
    for phase in blackboard.get("phases", []) or []:
        if isinstance(phase, dict):
            phase.setdefault("skills_required", [])
            phase.setdefault("tasks", [])
            phase.setdefault("covers", [])


# ─── SPEC → PHASES TRACEABILITY ('covers') ────────────────────────────────────

# Heading of a user story in the PO spec (e.g. "### US-1: Balance computation").
US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def check_spec_coverage(blackboard: dict, spec_text: str) -> list:
    """Traceability WARNINGS (non-blocking) spec → phases via 'covers'.

    Two directions: a phase references a US absent from the spec (probable hallucination
    of the compiler); a US of the spec is covered by no phase (a requirement potentially
    FORGOTTEN by the Architect — the most valuable warning). Warn-only: the human eye
    decides.
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
                        f"{', '.join(unknown)} (probable hallucination of the compiler).")
    uncovered = sorted(spec_us - referenced)
    if uncovered:
        warnings.append(f"US of the spec covered by NO phase: {', '.join(uncovered)} "
                        f"(requirement forgotten by the Architect? Check the plan).")
    return warnings


# ─── DYNAMIC SKILLS DICTIONARY ────────────────────────────────────────────────

def parse_skill_frontmatter(skill_path: str) -> tuple:
    """Extract (name, description) from the YAML frontmatter of a SKILL.md."""
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


def build_skills_dictionary() -> str:
    """Dynamically build the catalog of skills assignable to phases.

    Scans ./.agents/skills, reads the frontmatter (name + description) of each SKILL.md
    and excludes the pipeline system skills. The result is injected into the plan
    instructions of the ARCHITECT (step 2): the architect declares each phase's Skill, and
    the blackboard compiler then only COPIES that decision.
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
        lines.append(f'"{keyword}": {desc}')
    return "\n".join(lines)


def inject_skills_dictionary(text: str) -> str:
    """Substitute the REAL catalog of skills into the instructions of a pipeline skill."""
    skills_dictionary = build_skills_dictionary()
    if "{{SKILLS_DICTIONARY}}" in text:
        return text.replace("{{SKILLS_DICTIONARY}}", skills_dictionary)
    return text + f"\n\nDICTIONARY OF ALLOWED SKILLS:\n{skills_dictionary}\n"


def validate_all_skills(blackboard: dict):
    referenced = set()
    for phase in blackboard.get("phases", []) or []:
        if isinstance(phase, dict):
            for skill in phase.get("skills_required", []) or []:
                referenced.add(skill)

    available = set()
    if os.path.isdir(SKILLS_DIR):
        for entry in os.listdir(SKILLS_DIR):
            if os.path.exists(os.path.join(SKILLS_DIR, entry, "SKILL.md")):
                available.add(entry)

    hallucinated = sorted(referenced - available)
    if hallucinated:
        print(f"\n⚠️  Skills referenced in the blackboard but NOT FOUND (probable hallucination of the architect): {', '.join(hallucinated)}")
        usable = sorted(available - PIPELINE_SKILLS)
        print(f"   Skills actually available: {', '.join(usable) or '(none)'}")
        print(f"   → Fix 'blackboard.yaml' before launching production.\n")
    else:
        print(f"✅ All referenced skills exist ({len(referenced)} referenced).\n")


# ─── STEPS 1 TO 3 IN THE TUI (CLOUD) ──────────────────────────────────────────

def generate_spec_from_need_tui():
    print("\n📖 [STEP 1: PO AGENT] Refining the need into a business specification in the Cloud TUI...")

    if not os.path.exists(PO_SKILL_FILE):
        fail_pipeline(f"❌ PO skill missing: '{PO_SKILL_FILE}'")
    with open(PO_SKILL_FILE, "r", encoding="utf-8") as f:
        po_spec = f.read()
    with open(TMP_PO_FILE, "w", encoding="utf-8") as f:
        f.write(po_spec)

    po_prompt = f"""Read the file '{NEED_FILE}' at the root of our project, as well as the Product Owner instructions in the file '{TMP_PO_FILE}'.
You are a Senior Product Owner. By applying the instructions of '{TMP_PO_FILE}' SCRUPULOUSLY, refine the raw need into a business specification and save it DIRECTLY into a new file named '{SPEC_FILE}' at the project root.

Guidelines for the file '{SPEC_FILE}':
- Zero invention: every requirement must derive from the need expressed in '{NEED_FILE}'.
- Each user story carries TESTABLE acceptance criteria (Given / When / Then).
- Any ambiguity in the need becomes an explicit assumption in "Assumptions & Questions".
- The "Out of scope" section is mandatory (anti over-engineering lock).
Do it directly through your file editing tools, without needless chatter in the console.
As the VERY LAST action, after saving '{SPEC_FILE}', create the sentinel file '{SPEC_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
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

    This is where fixing costs the least: a misunderstood requirement rejected at this
    stage avoids paying (and redoing) a plan, a blackboard and production phases.
    The human can edit the spec in another terminal before validating.
    """
    print(f"\n{'='*50}")
    print(f"📋 SPECIFICATION READY: re-read '{SPEC_FILE}' (assumptions and out-of-scope first).")
    print(f"   You can modify it directly in another terminal before validating.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Validate the specification and launch the architecture? (y/n): ")
    mm_audit.event("gate", id="spec", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Cancelled by the user. Refine '{NEED_FILE}', delete '{SPEC_FILE}', then re-run.")
        RUNNER.kill()
        sys.exit(0)
    # The approval is MATERIALIZED (not inferred from the file's existence) and SURVIVES
    # this run: it is what the production variant will read to skip its step 1.
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # frozen copy of the spec AS APPROVED


def generate_plan_from_need_tui():
    print("\n📖 [STEP 2: ARCHITECT AGENT] Generating the implementation plan in the Cloud TUI...")

    if not os.path.exists(PLAN_SKILL_FILE):
        fail_pipeline(f"❌ Planning skill missing: '{PLAN_SKILL_FILE}'")
    with open(PLAN_SKILL_FILE, "r", encoding="utf-8") as f:
        plan_spec = f.read()
    # The REAL catalog of skills goes to the Architect: the routing decision (each phase's
    # Skill field) belongs to the agent that has the whole plan context, then
    # is copied mechanically downstream by the blackboard compiler.
    plan_spec = inject_skills_dictionary(plan_spec)
    with open(TMP_PLAN_FILE, "w", encoding="utf-8") as f:
        f.write(plan_spec)

    print("   📚 Skills detected and proposed to the architect:")
    for line in (build_skills_dictionary().splitlines() or ["(no phase skill detected)"]):
        print(f"      {line}")

    planning_prompt = f"""Read the file '{SPEC_FILE}' at the root of our project (validated business specification), as well as the architecture instructions in the file '{TMP_PLAN_FILE}'.
You are a senior Software Architect. By applying the instructions of '{TMP_PLAN_FILE}' SCRUPULOUSLY, generate a sequential implementation plan in Markdown format and save it DIRECTLY into a new file named '{PLAN_FILE}' at the project root.

Guidelines for the file '{PLAN_FILE}':
- The plan MUST start with the "Stack & Verification" block (with the UNIVERSAL VERDICT verification command: compilation + full suite) and EACH phase MUST declare its Nature (feature/tests) and its "Covers" field (US-x): the following pipeline steps copy these decisions without inferring them.
- Split the specification into BOUNDED micro-phases (1 to 5 tasks, at most 5 files created/modified, at most 3 files to read per phase); the indicative range of 3 to 12 phases always yields to these size bounds. Do not add any phase for a requirement absent from '{SPEC_FILE}'.
- YAGNI principle: plan ONLY what the specification asks for; its "Out of scope" section is a prohibition.
- Precise unit checklists, clear stack.
Do it directly through your file editing tools, without needless chatter in the console.
As the VERY LAST action, after saving '{PLAN_FILE}', create the sentinel file '{PLAN_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
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
        fail_pipeline(f"❌ Blackboard compiler skill missing: '{BLACKBOARD_SKILL_FILE}'")

    print("\n📖 [STEP 3: BLACKBOARD COMPILER] Generating the YAML Blackboard in the Cloud TUI...")

    # The compiler COPIES the plan's decisions (including each phase's Skill): the
    # skills dictionary goes to the Architect (step 2), not here. The Python net
    # validate_all_skills still catches hallucinated keywords downstream.
    with open(BLACKBOARD_SKILL_FILE, "r", encoding="utf-8") as f:
        compiler_spec = f.read()
    with open(TMP_ARCHITECT_FILE, "w", encoding="utf-8") as f:
        f.write(compiler_spec)

    prompt = f"""You are a Blackboard Compiler: you COPY the plan's decisions, you make none of your own. Read the plan that was just generated in '{PLAN_FILE}' as well as the structure instructions in the file '{TMP_ARCHITECT_FILE}'.
Generate the file '{BLACKBOARD_FILE}' at the root of our project, scrupulously respecting the requested YAML format.

Write the clean YAML directly into the file '{BLACKBOARD_FILE}', without wrapping it in markdown tags like ```yaml.
As the VERY LAST action, after saving '{BLACKBOARD_FILE}', create the sentinel file '{BLACKBOARD_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
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
        fail_pipeline(f"❌ [STEP 3] Timeout or failure creating '{BLACKBOARD_FILE}'.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "tech-plan", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan approval sentinel (spec.md deleted since) must never validate a FUTURE
    # spec: we purge it first of all.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # A residual failReport.md from a previous run must not be mistaken for the one of the
    # current run: we purge it at startup.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # 🚀 STEP ZERO: Immediate boot of the harness Data Center in Tmux
    RUNNER.start()

    # Step 1: PO refinement via the TUI (need.md → spec.md), validated by the HUMAN.
    # Three resume states, as in the full variant: no spec → generation
    # + confirmation; spec WITHOUT approval sentinel (interrupted run) → human
    # revalidation; spec + sentinel → step skipped.
    if not os.path.exists(SPEC_FILE):
        generate_spec_from_need_tui()
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
    else:
        print(f"🔄 Existing '{BLACKBOARD_FILE}' found. Loading...")
        try:
            blackboard = load_blackboard()
        except Exception as err:
            print(f"❌ '{BLACKBOARD_FILE}' present but unreadable (invalid or corrupt YAML): {err}")
            print(f"   → Fix or delete '{BLACKBOARD_FILE}', then re-run "
                  f"(it will be regenerated from '{PLAN_FILE}').")
            write_fail_report(
                "Unreadable blackboard",
                f"'{BLACKBOARD_FILE}' is present but unreadable (invalid or corrupt YAML): {err}. "
                f"Fix or delete this file then re-run.")
            RUNNER.kill()
            sys.exit(1)

    # What follows is PURE LOCAL VALIDATION (no agent): the session can be closed
    # right now, the human reads the recap at leisure.
    RUNNER.kill()

    # "need" context for traceability: the validated spec (source of truth).
    need_is_spec = os.path.exists(SPEC_FILE)
    need_context_file = SPEC_FILE if need_is_spec else NEED_FILE
    with open(need_context_file, "r", encoding="utf-8") as f:
        user_need = f.read()

    # Guardrail: the blackboard is produced by a fallible small LLM. We validate it
    # NOW — discovering when launching production (later, elsewhere) that it
    # is structurally unusable would waste the whole point of this partial
    # pipeline. This script FIXES nothing and never writes the blackboard: it reports.
    fatal, soft = validate_blackboard_schema(blackboard)
    if soft:
        print("\nℹ️  Non-critical fields absent (filled automatically in production):")
        for problem in soft:
            print(f"   - {problem}")
    if fatal:
        print("\n❌ The blackboard has STRUCTURAL anomalies:")
        for problem in fatal:
            print(f"   - {problem}")
        print(f"   → Fix '{BLACKBOARD_FILE}' (or edit '{PLAN_FILE}', delete "
              f"'{BLACKBOARD_FILE}' and re-run this script) BEFORE launching production.")
        write_fail_report(
            "Structurally invalid blackboard",
            "The produced blackboard has STRUCTURAL anomalies that would make production fail or "
            "go wrong.",
            details="\n".join(f"- {p}" for p in fatal))
        sys.exit(1)
    apply_blackboard_defaults(blackboard)

    # NON-blocking traceability warnings spec → phases ('covers').
    if need_is_spec:
        coverage_warnings = check_spec_coverage(blackboard, user_need)
        if coverage_warnings:
            print("\n⚠️  Traceability spec → phases:")
            for warning in coverage_warnings:
                print(f"   - {warning}")

    validate_all_skills(blackboard)

    print(f"{'='*50}")
    print(f"📋 BLACKBOARD READY — Recap:")
    print(f"   Project: {blackboard.get('project', '(untitled)')}")
    print(f"   Stack (global_rules.target): {blackboard['global_rules']['target']}")
    print(f"   Universal verdict (verify_cmd): {blackboard.get('verify_cmd') or '⚠️  ABSENT'}")
    print(f"   Phases: {len(blackboard['phases'])}")
    for p in blackboard['phases']:
        skills = ', '.join(p.get('skills_required', []))
        covers = ', '.join(p.get('covers', []))
        own_cmd = (p.get('verify_cmd') or '').strip()
        extra = f" — specific verify: {own_cmd}" if own_cmd else ""
        print(f"   Phase {p['id']}: {p['name']} [{skills}] "
              f"({len(p.get('tasks', []))} task(s); covers: {covers or '?'}){extra}")
    print(f"{'='*50}")

    # Clean shutdown: the DELIVERABLES ('spec.md' + '.spec_approved', 'plan.md',
    # 'blackboard.yaml') survive — that is the whole point of this script; the temporary
    # files and any .done sentinels written late (deliverable accepted by the stability
    # net) are purged.
    for tmp_f in [TMP_PO_FILE, TMP_PLAN_FILE, TMP_ARCHITECT_FILE, TMP_PROMPT_BUFFER,
                  SPEC_DONE_SENTINEL, PLAN_DONE_SENTINEL, BLACKBOARD_DONE_SENTINEL]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)

    print(f"""
🏁 Technical pipeline complete: from need to blackboard, without production.
   📦 Deliverables: '{SPEC_FILE}' (approved), '{PLAN_FILE}', '{BLACKBOARD_FILE}'
   ➡️  To launch production: python3 Safe-Coding.py
      Resume by files: spec, plan and blackboard are resumed AS IS, and
      production starts directly after your blackboard y/n. This is the right moment to
      switch to a more economical model (/model or '{AGENT_CONFIG_FILE}'):
      big model to think (this run), small model to produce (the next one).
   ♻️  To tweak before production: small tweak → edit '{BLACKBOARD_FILE}';
      overhaul of the slicing → edit '{PLAN_FILE}', delete '{BLACKBOARD_FILE}', re-run
      this script (existing spec and plan will be resumed as is).""")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    RUNNER=RUNNER,
    US_HEADING_RE=US_HEADING_RE,
)


if __name__ == "__main__":
    main()
