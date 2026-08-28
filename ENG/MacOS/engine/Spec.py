#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - PARTIAL pipeline "from need to spec" (agent harness + tmux)
─────────────────────────────────────────────────────────────────────────────
"SPEC ONLY" VARIANT: runs ONLY step 1 of the MAIsterMind pipeline —
the PO Agent refines 'need.md' into a business specification 'spec.md' (user stories,
testable acceptance criteria, out-of-scope, assumptions), VALIDATED by the human — then
stops cleanly.

Why a dedicated entry point:
  - It is the CHEAPEST human gate of the whole pipeline: fixing a misunderstood
    requirement here avoids paying for (and redoing) a plan, a blackboard and production
    phases. This script lets you pay ONLY for this step (asynchronous review,
    workshop with the business, large model reserved for the refinement…).
  - Same FILE CONTRACTS as the full variants ('spec.md' + approval sentinel
    '.spec_approved'): any orchestrator relaunched afterwards —
    Technical-Plan.py (up to the blackboard), Coding.py, Coding-Without-Tests.py
    or Design-Prototype.py — finds the approved spec and skips step 1 (resume by
    files, no configuration).

Per-step context window slicing remains the guiding principle: the PO Agent
runs in a fresh session, receives ONLY the need and its instructions, and the run
stops before accumulating anything else.
"""

import os
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
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="spec")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
FAIL_REPORT_FILE      = "failReport.md"   # persistent stop report (same contract as the factory)
PO_SKILL_FILE         = "./.agents/pipeline/po/SKILL.md"

# Active harness config, as THIS script's messages have always cited it:
# without the leading './' ('.opencode/opencode.json', '.codex/config.toml').
# The prefix is stripped here and not in the runner: the other orchestrators
# cite the './…' form — the migration rewrites no existing message.
AGENT_CONFIG_FILE     = RUNNER.config_file.removeprefix("./")

# Temporary context routing file (offloaded prompt)
TMP_PO_FILE           = RUNNER.tmp_file("po")

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: the
# only valid choice on all 3 OSes (Windows has no /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# End-of-deliverable sentinel (same contract as production: the agent creates the .done
# AFTER saving the deliverable — an unambiguous signal, robust to writing pauses).
SPEC_DONE_SENTINEL       = ".pipeline_spec.done"

# HUMAN approval of the spec, materialized: the mere EXISTENCE of spec.md proves
# nothing (a timeout can leave a never-validated spec behind). It is THIS
# sentinel that the full orchestrators will read to skip their step 1.
SPEC_APPROVED_SENTINEL   = ".spec_approved"

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session. Prefix DISTINCT from the
# full variants: this script cannot inject a prompt into a production run that would
# be running on the same project.
TMUX_SESSION          = RUNNER.session

POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max for the step (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net: deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds)


def fail_pipeline(message: str):
    """Single exit point for step failures.

    Always kills the tmux session BEFORE exiting: an exit that leaves the agent alive
    lets it finish writing its deliverable AFTER the orchestrator gave up — on
    relaunch, that half-validated file would be mistaken for a valid resume state
    (this is how a never-approved spec would become the source of truth).
    """
    print(message)
    write_fail_report("Specification step failure", message)
    RUNNER.kill()
    sys.exit(1)


def write_fail_report(title: str, reason: str):
    """Write a persistent stop report at the root (same contract as the factory:
    every NON-nominal stop produces one). Best-effort: NEVER raises."""
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — MAIsterMind (spec only)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "",
                 "### Recommended action",
                 "Fix the cause above (or bump the model one notch via /model in the "
                 f"TUI or '{AGENT_CONFIG_FILE}'), then re-run."]
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── FILE MONITOR SYNCHRONIZATION ─────────────────────────────────

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


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                           structural_check=None) -> bool:
    """Wait for a pipeline deliverable signaled by a SENTINEL.

    Same contract as production: the agent creates a .done file AFTER saving the
    deliverable. SAFETY NET for an agent that forgets the sentinel: if the deliverable
    exists, is non-empty and has not changed for STABLE_POLLS_FALLBACK consecutive
    checks, it is accepted with a warning (graceful degradation). The optional
    'structural_check' hardens this fallback ONLY: a stable but structurally incomplete
    deliverable keeps waiting (the agent may pause longer than the stability window)
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


# ─── STEP 1: PO AGENT IN THE TUI (CLOUD) ───────────────────────────────────

def generate_spec_from_need_tui():
    print("\n📖 [STEP 1: PO AGENT] Refining the need into a business specification in the Cloud TUI...")

    if not os.path.exists(PO_SKILL_FILE):
        fail_pipeline(f"❌ PO skill missing: '{PO_SKILL_FILE}'")
    with open(PO_SKILL_FILE, "r", encoding="utf-8") as f:
        po_spec = f.read()
    with open(TMP_PO_FILE, "w", encoding="utf-8") as f:
        f.write(po_spec)

    po_prompt = f"""Read the file '{NEED_FILE}' at the root of our project, along with the Product Owner instructions in the file '{TMP_PO_FILE}'.
You are a Senior Product Owner. Applying the instructions of '{TMP_PO_FILE}' SCRUPULOUSLY, refine the raw need into a business specification and save it DIRECTLY in a new file named '{SPEC_FILE}' at the project root.

Directives for the file '{SPEC_FILE}':
- Zero invention: every requirement must derive from the need expressed in '{NEED_FILE}'.
- Every user story carries TESTABLE acceptance criteria (Given / When / Then).
- Any ambiguity in the need becomes an explicit assumption in "Assumptions & Questions".
- The "Out of scope" section is mandatory (anti over-engineering lock).
Do it directly via your file-editing tools, without needless chatter in the console.
As your VERY LAST action, after saving '{SPEC_FILE}', create the sentinel file '{SPEC_DONE_SENTINEL}' at the root (content: the single word done): it is the completion signal for the orchestrator.
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
    stage avoids paying for (and redoing) a plan, a blackboard and production phases.
    The human can edit the spec in another terminal before validating.
    """
    print(f"\n{'='*50}")
    print(f"📋 SPECIFICATION READY: re-read '{SPEC_FILE}' (assumptions and out-of-scope first).")
    print(f"   You can edit it directly in another terminal before validating.")
    print(f"{'='*50}")
    confirm = input("\n▶️  Validate the specification? (y/n): ")
    mm_audit.event("gate", id="spec", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Cancelled by the user. Refine '{NEED_FILE}', delete '{SPEC_FILE}', then re-run.")
        RUNNER.kill()
        sys.exit(0)
    # The approval is MATERIALIZED (not inferred from the file's existence): it is this
    # sentinel that the full orchestrators will read to skip their own step 1.
    # It must therefore SURVIVE the end of this run (never purged here).
    with open(SPEC_APPROVED_SENTINEL, "w", encoding="utf-8") as f:
        f.write("approved\n")
    mm_audit.snapshot(SPEC_FILE)   # frozen copy of the spec AS APPROVED


def print_handover():
    """Recall how to chain: resume by files does all the work."""
    print(f"""
{'─'*50}
➡️  Possible next steps (resume by files: the approved spec is reused
   as is, the PO step will NOT be replayed):
   - python3 Technical-Plan.py   → stop at the blackboard (technical plan only)
   - python3 Coding.py             → run the whole pipeline down to the code
   Tip: this is the right moment to switch models (/model in the TUI or
   '{AGENT_CONFIG_FILE}') — large model to think, small model to produce.
{'─'*50}""")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "spec", RUNNER.name,
                   model=RUNNER.configured_model())
    check_need_file()

    # An orphan approval sentinel (spec.md deleted since) must never validate a FUTURE
    # spec: we purge it first of all.
    if os.path.exists(SPEC_APPROVED_SENTINEL) and not os.path.exists(SPEC_FILE):
        os.remove(SPEC_APPROVED_SENTINEL)

    # A residual failReport.md from a previous run must not be mistaken for the current
    # run's: we purge it at startup.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # Step already DONE (spec present AND approved): nothing to do, not even a TUI
    # boot — this script is idempotent, like the resume of the full variants.
    if os.path.exists(SPEC_FILE) and os.path.exists(SPEC_APPROVED_SENTINEL):
        print(f"✓ '{SPEC_FILE}' already exists and has been approved by the human: nothing to do.")
        print(f"   → To regenerate a spec from '{NEED_FILE}': delete '{SPEC_FILE}' then re-run.")
        print_handover()
        return

    # Three resume states, like in the full variants: no spec →
    # generation + confirmation; spec WITHOUT the approval sentinel (interrupted run:
    # timeout, Ctrl-C during the y/n) → we ask the human again instead of trusting a
    # file that may never have been validated.
    if not os.path.exists(SPEC_FILE):
        RUNNER.start()
        generate_spec_from_need_tui()
        confirm_spec_with_human()
    else:
        print(f"🔄 Existing '{SPEC_FILE}' found but NEVER approved (interrupted run?).")
        confirm_spec_with_human()

    # Clean shutdown: the '.spec_approved' sentinel SURVIVES deliberately (it is the
    # resume signal of the downstream orchestrators); the temporary files and any
    # .done sentinel written late (deliverable accepted by the stability net) are
    # purged.
    for tmp_f in [TMP_PO_FILE, TMP_PROMPT_BUFFER, SPEC_DONE_SENTINEL]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    RUNNER.kill()

    print(f"\n🏁 Specification '{SPEC_FILE}' validated and approved.")
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
