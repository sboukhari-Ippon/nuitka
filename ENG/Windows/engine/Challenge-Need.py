#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - PARTIAL pipeline "challenge the need" (agent harness + tmux)
─────────────────────────────────────────────────────────────────────────────
"CHALLENGE-NEED" VARIANT (AIDD-7): confronts 'need.md' with its own ambiguities
BEFORE paying for a spec. An agent with a fresh context produces 'need_review.md' —
ambiguities, contradictions, grey areas, assumptions, questions to settle — which
the human endorses (y/n) then exploits: they update 'need.md' THEMSELVES and relaunch
the pipeline of their choice.

Why a dedicated entry point:
  - The most UPSTREAM gate is the cheapest of all: a vague need costs a spec,
    a plan and phases; a question settled here costs nothing.
  - NO downstream coupling (v1): no orchestrator reads 'need_review.md' nor
    requires '.need_reviewed'. This script is opt-in, the 15 existing pipelines do
    not change by a single line.

Contracts taken as-is from the existing code (template: Spec.py):
  - file-based resumption: 'need_review.md' present WITHOUT '.need_reviewed' → the
    gate is presented again without re-paying the agent; both present → already done,
    exit 0; deleting 'need_review.md' forces regeneration;
  - verdict: the Python checks can reject (3 attempts max, feedback = exact list
    of shortfalls), the LLM never validates — and every QUOTE of the need must
    exist in 'need.md' (same anti-invented-source guard as Documentation.py);
  - run journal (mm_audit) wired in from the start.
"""

import os
import re
import sys
import time
import signal

from mm_runner import resolve_runner, resolve_timeout

# Run journal (black box .mm-runs/, plan-big-last Lot 2): purely additive,
# full no-op if MM_AUDIT=0, NEVER makes a run fail.
import mm_audit

# Shared functions extracted at Lot 4a (plan-big-last): see mm_core.py.
# The configuration (THIS module's constants/objects) is injected at the end
# of the file via mm_core.configure(...) — all names are defined by then.
import mm_core
from mm_core import (
    signal_handler,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer lives in 'mm_runner.py'. DISTINCT session prefix (role
# 'challenge'): this script cannot inject a prompt into another run of the project.
RUNNER = resolve_runner(os.getcwd(), role="challenge")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
REVIEW_FILE           = "need_review.md"
FAIL_REPORT_FILE      = "failReport.md"   # persistent failure report (same contract as the factory)
CHALLENGE_SKILL_FILE  = "./.agents/pipeline/challenge-need/SKILL.md"

AGENT_CONFIG_FILE     = RUNNER.config_file.removeprefix("./")

# Temporary context-routing file (offloaded prompt)
TMP_CHALLENGE_FILE    = RUNNER.tmp_file("challenge")

# Buffer file of the prompt sent to the TUI via tmux.
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# Deliverable end sentinel (the agent creates the .done AFTER saving the review).
REVIEW_DONE_SENTINEL     = ".pipeline_challenge.done"

# HUMAN endorsement of the review, materialized: the EXISTENCE of need_review.md
# proves nothing (a timeout can leave a never-endorsed review behind).
REVIEW_APPROVED_SENTINEL = ".need_reviewed"

TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max for the step (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less net: deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds)

# MANDATORY sections of the review (the grid's locked format): each must be present
# AND non-empty ("None." is valid content — the absence of an issue is a result,
# the absence of a section is a half-written deliverable).
REVIEW_SECTIONS = ["## Ambiguities", "## Contradictions", "## Grey areas",
                   "## Assumptions", "## Questions to settle before the spec"]

# A quote of the need is a passage between double quotes; below this threshold we do
# not check (isolated words, false positives). Anti-invented-source guard: every
# long-enough quote MUST exist in need.md (whitespace normalized).
MIN_QUOTE_CHARS = 12
QUOTE_RE = re.compile(r'"([^"\n]{%d,})"' % MIN_QUOTE_CHARS)


def fail_pipeline(message: str):
    """Single exit point for step failures.

    Always kills the tmux session BEFORE quitting: an exit that leaves the agent alive
    lets it finish writing its deliverable AFTER the orchestrator gave up — at
    relaunch, that half-validated file would be taken for a valid resume state.
    """
    print(message)
    write_fail_report("Failure of the need-challenge step", message)
    RUNNER.kill()
    sys.exit(1)


def write_fail_report(title: str, reason: str):
    """Writes a persistent failure report at the root (same contract as the factory:
    every NON-nominal stop produces one). Best-effort: NEVER raises."""
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — MAIsterMind (need challenge)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "",
                 "### Recommended action",
                 "Fix the cause above (or move the model up a notch via /model in the "
                 f"TUI or '{AGENT_CONFIG_FILE}'), then relaunch."]
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── SYNCHRONIZATION VIA FILE MONITOR ─────────────────────────────────────────

def cleanup_pipeline_sentinel(sentinel: str):
    """Removes a residual pipeline sentinel (previous interrupted run)."""
    try:
        os.remove(sentinel)
    except OSError:
        pass


def review_structural_check(path: str) -> bool:
    """LIGHT structural floor of a review accepted without a sentinel: its mandatory
    sections must be present (a half-written review stops before).
    The STRONG check (non-empty sections, real quotes) is validate_review."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return all(section.lower() in content for section in REVIEW_SECTIONS)
    except OSError:
        return False


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Waits for a pipeline deliverable signaled by a SENTINEL.

    Same contract as production: the agent creates a .done file AFTER saving the
    deliverable. NET for an agent that forgets the sentinel: if the deliverable exists,
    is non-empty and has not moved for STABLE_POLLS_FALLBACK consecutive checks, we
    accept it with a warning (graceful degradation). The optional 'structural_check'
    only hardens THIS net: a stable but structurally incomplete deliverable keeps
    waiting (the agent may pause longer than the stability window)
    until the global timeout.
    """
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}' (end signal: '{sentinel}')...")
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
                              f"we keep waiting (the agent may still be writing).")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinel '{sentinel}' absent but '{filepath}' has been stable for "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s: deliverable accepted (safety net).")
                return True
    return False


def check_need_file():
    if not os.path.exists(NEED_FILE):
        print(f"❌ Critical error: '{NEED_FILE}' is missing.")
        write_fail_report("Missing needs file", f"'{NEED_FILE}' is missing at the project root.")
        sys.exit(1)
    with open(NEED_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        print(f"❌ Critical error: '{NEED_FILE}' is empty.")
        write_fail_report("Empty needs file", f"'{NEED_FILE}' is present but empty.")
        sys.exit(1)
    print("✓ Needs file validation (need.md): OK")


# ─── STRONG STRUCTURAL CHECKS (PYTHON: THE LLM NEVER VALIDATES) ───────────────

def normalize_ws(text: str) -> str:
    """Normalized whitespace (quote comparison tolerates line breaks)."""
    return " ".join(str(text).split()).lower()


def validate_review(path: str, need_text: str) -> list:
    """STRONG check of the review: mandatory sections present and NON-EMPTY, and
    every quote of the need (long-enough passage between double quotes) must
    EXIST in need.md — an invented quote is a rejection with the exact gap,
    phrased to be sent back AS IS to the agent (same philosophy as the
    anti-invented-source guard of Documentation.py). Returns the list of shortfalls."""
    issues = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return [f"'{path}' is unreadable."]

    # 1. Mandatory sections, in the locked format's order, non-empty.
    positions = []
    low = content.lower()
    for section in REVIEW_SECTIONS:
        idx = low.find(section.lower())
        if idx < 0:
            issues.append(f"Missing mandatory section: '{section}'.")
        positions.append(idx)
    if not issues:
        bounds = positions + [len(content)]
        for i, section in enumerate(REVIEW_SECTIONS):
            body = content[bounds[i] + len(section):bounds[i + 1]].strip()
            if not body:
                issues.append(f"Section '{section}' is empty: write its content, or the "
                              f"single line \"None.\" if you found nothing.")

    # 2. Anti-invented-quote guard: every quoted passage must exist in the need.
    need_norm = normalize_ws(need_text)
    for quote in QUOTE_RE.findall(content):
        if normalize_ws(quote) not in need_norm:
            issues.append(f"The quote \"{quote[:80]}\" does not exist in '{NEED_FILE}': "
                          f"quote the need WORD FOR WORD (exact copy), or rephrase without "
                          f"quotes if it is your interpretation.")
    return issues


# ─── SINGLE STEP: CHALLENGER AGENT IN THE TUI ─────────────────────────────────

def build_challenge_prompt(feedback: str) -> str:
    with open(CHALLENGE_SKILL_FILE, "r", encoding="utf-8") as f:
        grid = f.read()
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are a Need challenger: you confront a raw need with its ambiguities BEFORE
a specification is paid for. You modify NO project file: you write ONLY your
review '{REVIEW_FILE}', then your end sentinel.

--- GRID (instructions and LOCKED output format) ---
{grid}

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY DELIVERABLE ---
Read '{NEED_FILE}' at the root, then write your review to '{REVIEW_FILE}' (project
root) STRICTLY following the grid's format.
Do it directly via your file-editing tools, with no needless console chatter.

--- MANDATORY FINAL INSTRUCTION ---
As your very LAST action, after saving '{REVIEW_FILE}', create the sentinel file
'{REVIEW_DONE_SENTINEL}' at the root (content: the single word done): it is the
end signal for the orchestrator.
"""
    with open(TMP_CHALLENGE_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)
    return (f"Read the instructions file '{TMP_CHALLENGE_FILE}' at the project root and "
            f"perform the requested need review.")


def generate_review_tui(need_text: str):
    """Review production loop: 3 attempts max, the EXACT shortfalls from the Python
    checks become the next attempt's feedback."""
    print("\n🔍 [SINGLE STEP: CHALLENGER] Critical review of the need in the Cloud TUI...")

    if not os.path.exists(CHALLENGE_SKILL_FILE):
        fail_pipeline(f"❌ Missing challenge grid: '{CHALLENGE_SKILL_FILE}'")

    feedback = "First pass — no previous feedback."
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cleanup_pipeline_sentinel(REVIEW_DONE_SENTINEL)
        print(f"\n🚀 [ATTEMPT {attempt}/{MAX_ATTEMPTS}] Launching the Need challenger...")
        prompt = build_challenge_prompt(feedback)
        mm_audit.event("agent_task", prompt_bytes=len(prompt), attempt=attempt)
        RUNNER.send_task(prompt)

        if not wait_for_pipeline_file(REVIEW_FILE, REVIEW_DONE_SENTINEL,
                                      structural_check=review_structural_check):
            feedback = (f"On the previous pass, no deliverable was received ('{REVIEW_FILE}' "
                        f"absent, empty or never signaled). First write the complete review, "
                        f"THEN the sentinel, in that order.")
            print(f"⏱️  The challenger did not signal the end of its pass. Retrying.")
            RUNNER.new_context()
            continue

        issues = validate_review(REVIEW_FILE, need_text)
        if not issues:
            print(f"✅ Review '{REVIEW_FILE}' produced and compliant with the mechanical checks.")
            return
        feedback = ("Your review does not pass the mechanical checks:\n"
                    + "\n".join(f"- {issue}" for issue in issues)
                    + "\nRewrite the file entirely in the grid's format.")
        try:
            os.remove(REVIEW_FILE)
        except OSError:
            pass
        print(f"⚠️  [REJECT] Attempt {attempt}: review off-contract "
              f"({len(issues)} shortfall(s): {' ; '.join(issues[:2])}…).")
        RUNNER.new_context()

    fail_pipeline(f"❌ Need review not completed after {MAX_ATTEMPTS} attempts.")


def confirm_review_with_human():
    """Human endorsement of the review: endorsing changes NOTHING in the project — it
    is a human input. The user reads, updates 'need.md' themselves, and relaunches
    the pipeline of their choice."""
    print(f"\n{'='*50}")
    print(f"🔍 NEED REVIEW READY: reread '{REVIEW_FILE}' ([BLOCKING] questions first).")
    print(f"   It changes nothing: update '{NEED_FILE}' YOURSELF, then relaunch the "
          f"pipeline of your choice.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Endorse this need review? (y/n): ")
    mm_audit.event("gate", id="review", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Clean stop. '{REVIEW_FILE}' is kept: delete it to replay the "
              f"review, or relaunch to present this gate again.")
        RUNNER.kill()
        sys.exit(0)
    # The endorsement is MATERIALIZED: at resume, a review without this sentinel
    # goes through the y/n again instead of being taken as endorsed.
    with open(REVIEW_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("reviewed\n")
    mm_audit.snapshot(REVIEW_FILE)   # frozen copy of the review AS ENDORSED


def print_handover():
    """Reminds how to move on: the review is a human input, not a state."""
    print(f"""
{'─'*50}
➡️  Exploit the review: settle the [BLOCKING] questions, update '{NEED_FILE}',
   then relaunch the pipeline of your choice (Spec.py, Technical-Plan.py, Safe-Coding.py…).
   NO pipeline reads '{REVIEW_FILE}' nor requires '.need_reviewed': zero coupling,
   zero slowdown — this review is only worth what YOU take from it.
{'─'*50}""")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "challenge-need", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan endorsement sentinel (review deleted since) must never endorse a
    # FUTURE review: we purge it before anything else.
    if os.path.exists(REVIEW_APPROVED_SENTINEL) and not os.path.exists(REVIEW_FILE):
        os.remove(REVIEW_APPROVED_SENTINEL)

    # A residual failReport.md from a previous run must not be taken for the current
    # run's: we purge it at startup.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # Step already DONE (review present AND endorsed): nothing to do, not even a TUI
    # boot — this script is idempotent, like the other partial pipelines.
    if os.path.exists(REVIEW_FILE) and os.path.exists(REVIEW_APPROVED_SENTINEL):
        print(f"✓ '{REVIEW_FILE}' already exists and has been endorsed: nothing to do.")
        print(f"   → To replay the review from '{NEED_FILE}': delete '{REVIEW_FILE}' then relaunch.")
        print_handover()
        return

    # File-based resumption: a never-endorsed review (run interrupted during the y/n)
    # presents the gate again WITHOUT re-paying the agent.
    if not os.path.exists(REVIEW_FILE):
        with open(NEED_FILE, "r", encoding="utf-8") as f:
            need_text = f.read()
        RUNNER.start()
        generate_review_tui(need_text)
        confirm_review_with_human()
    else:
        print(f"🔄 Existing '{REVIEW_FILE}' found but NEVER endorsed (interrupted run?).")
        confirm_review_with_human()

    # Clean shutdown: '.need_reviewed' deliberately SURVIVES (trace of the arbitration);
    # temporary files and a possible late .done sentinel are purged.
    for tmp_f in [TMP_CHALLENGE_FILE, TMP_PROMPT_BUFFER, REVIEW_DONE_SENTINEL]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    RUNNER.kill()

    print(f"\n🏁 Need review '{REVIEW_FILE}' endorsed.")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")
    print_handover()


mm_core.configure(
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
