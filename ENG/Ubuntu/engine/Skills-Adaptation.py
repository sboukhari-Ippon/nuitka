#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - "Your stack" skill adapter (agent harness + tmux)
─────────────────────────────────────────────────────────────────────────────
The shipped coding skills (backend-coding: Java/Spring Boot, frontend-coding:
React/TypeScript, and their testing counterparts) are TEMPLATES. This
orchestrator rewrites them for YOUR stack, through a short questionnaire, then
overwrites the project's originals — each one after YOUR validation, previous
content saved as .bak.

Quality chain, in the factory's spirit (the AI proposes, Python verifies,
the human decides):
  1. QUESTIONNAIRE (Python, free) → persisted adaptation profile
     ('skill_adapt_profile.yaml'): scope, target stacks, conventions,
     line cap (200 by default, 250 or 300 on demand), size of the model that
     WILL CONSUME the skills (standard ≥ 100B, or compact ~27B like Qwen3 27B —
     shorter, more mechanical instructions).
  2. GENERATION (agent, fresh context per skill): rewrite guided by the
     '.agents/pipeline/skill-adapt/SKILL.md' grid — ORDERS, never descriptions;
     mandatory patterns/anti-patterns table; verifiable final checklist.
  3. PYTHON GUARDRAILS (deterministic): line cap, contractual frontmatter
     (name unchanged — it is the blackboard's routing key), ❌/✅ table and
     checklist present. Failure → repair instruction, at most MAX_REPAIRS times.
  4. QUALITY REVIEW (independent agent, fresh context): audit against the
     '.agents/pipeline/skill-adapt-review/SKILL.md' grid, first-line verdict
     ('VERDICT: COMPLIANT' / 'VERDICT: NON-COMPLIANT') parsed by Python — a
     NON-COMPLIANT verdict triggers ONE repair then a re-review; if it
     persists, the human arbitrates with full knowledge.
  5. HUMAN GATE per skill: proposal preview ('skill_adapt-<name>.md' at the
     root, editable before validation), y → .bak backup + overwrite,
     n → proposal kept for inspection, never applied.

Resume from files, as everywhere: existing profile → offered as is;
already-generated proposal → re-presented at the human gate without re-paying
generation or review. Nothing to configure.
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
# The whole tmux layer lives in 'mm_runner.py'. DISTINCT session role
# ('skilladapt'): this orchestrator cannot inject a prompt into a production
# run that would be active on the same project.
RUNNER = resolve_runner(os.getcwd(), role="skilladapt")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
SKILLS_DIR            = "./.agents/skills"
ADAPT_SKILL_FILE      = "./.agents/pipeline/skill-adapt/SKILL.md"
REVIEW_SKILL_FILE     = "./.agents/pipeline/skill-adapt-review/SKILL.md"
PROFILE_FILE          = "skill_adapt_profile.yaml"
REPORT_FILE           = "skill_adapt_report.md"
REVIEW_FILE           = "skill_review.md"
FAIL_REPORT_FILE      = "failReport.md"
PROPOSAL_PREFIX       = "skill_adapt-"

AGENT_CONFIG_FILE     = RUNNER.config_file

# Temporary context-routing files (offloaded prompt)
TMP_ADAPT_FILE        = RUNNER.tmp_file("skilladapt")
TMP_REVIEW_FILE       = RUNNER.tmp_file("skillreview")
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# End-of-pass sentinels (same contract as production: the agent creates the
# .done AFTER saving the deliverable — unambiguous signal).
ADAPT_DONE_SENTINEL   = ".pipeline_skill_adapt.done"
REVIEW_DONE_SENTINEL  = ".pipeline_skill_review.done"

TMUX_SESSION          = RUNNER.session

POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)
STABLE_POLLS_FALLBACK = 15
MAX_REPAIRS           = 2

# Quality controller verdicts, parsed on the report's FIRST line.
VERDICT_LINE_PREFIX   = "VERDICT:"
VERDICT_OK            = "COMPLIANT"
VERDICT_KO            = "NON-COMPLIANT"

# Line caps offered by the questionnaire (choice 1 = default).
LINE_CAPS             = {"1": 200, "2": 250, "3": 300}
MODEL_TARGETS         = {"1": "standard", "2": "compact"}

# Calibration directives injected into the generation prompt, per size.
MODEL_DIRECTIVES = {
    "standard": "models ≥ 100B: expert-level concision allowed, standard technical vocabulary without defining it.",
    "compact":  "small local model (~27B, e.g. Qwen3 27B): sentences of 20 words max, zero implicit knowledge, "
                "every rule mechanically applicable, define every acronym, a single minimal template per layer.",
}

# Domain shown per skill (context given to the generator and the controller).
SKILL_DOMAINS = {
    "backend-coding":   "backend production code",
    "backend-testing":  "backend tests",
    "frontend-coding":  "frontend production code",
    "frontend-testing": "frontend tests",
}


def fail_pipeline(message: str):
    """Single exit point for failures. Always kills the tmux session BEFORE
    exiting: an agent left alive would finish writing its proposal AFTER the
    abandon, and the resume-from-files would take that half-written file for
    a valid proposal."""
    print(message)
    write_fail_report("Skill adaptation failure", message)
    RUNNER.kill()
    sys.exit(1)


def write_fail_report(title: str, reason: str):
    """Persistent stop report at the root (same contract as the factory).
    Best-effort: NEVER raises."""
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — MAIsterMind (skill adaptation)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "",
                 "### Recommended action",
                 "Fix the cause above (or move the model one notch up via /model in the "
                 f"TUI or '{AGENT_CONFIG_FILE}'), then relaunch: already accepted "
                 "proposals are resumed as they are."]
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


# ─── SYNCHRONIZATION VIA FILE MONITORING ──────────────────────────────────────

def cleanup_pipeline_sentinel(sentinel: str):
    try:
        os.remove(sentinel)
    except OSError:
        pass


def wait_for_pipeline_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT) -> bool:
    """Waits for a deliverable signaled by SENTINEL, with the factory's
    standard stability net (non-empty deliverable, still for N checks)."""
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}' (end signal: '{sentinel}')...")
    stable_streak = 0
    last_size = -1
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
                print(f"   ⚠️  Sentinel '{sentinel}' missing but '{filepath}' has been stable for "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s: deliverable accepted (safety net).")
                return True
    return False


# ─── ADAPTATION PROFILE (PYTHON QUESTIONNAIRE, FREE) ──────────────────────────

def ask_scope() -> str:
    """Scope question. The prompt is LITERAL at the input() call site:
    that is the contract of check_gate_labels.py (AST read) and the manifest."""
    print("\n   [1] Backend + Frontend")
    print("   [2] Backend only")
    print("   [3] Frontend only")
    while True:
        answer = input("   → Scope to adapt (1/2/3): ").strip()
        mm_audit.event("gate", id="scope", gate_kind="choice", answer=answer)
        if answer in ("1", "2", "3"):
            return answer
        print("   ↳ Answer with 1, 2 or 3.")


def ask_backend_stack() -> str:
    while True:
        answer = input("   → Target backend stack (e.g. Kotlin + Spring Boot): ").strip()
        mm_audit.event("gate", id="stack-back", gate_kind="text", answer=answer)
        if answer:
            return answer
        print("   ↳ Answer required: name the language and the framework.")


def ask_frontend_stack() -> str:
    while True:
        answer = input("   → Target frontend stack (e.g. Vue 3 + Vite): ").strip()
        mm_audit.event("gate", id="stack-front", gate_kind="text", answer=answer)
        if answer:
            return answer
        print("   ↳ Answer required: name the framework and the tooling.")


def ask_conventions() -> str:
    answer = input("   → Specific conventions to enforce (- if none): ").strip()
    mm_audit.event("gate", id="conventions", gate_kind="text", answer=answer)
    return answer if answer else "-"


def ask_line_cap() -> str:
    print("\n   [1] 200 lines per skill (recommended default)")
    print("   [2] 250 lines per skill")
    print("   [3] 300 lines per skill")
    while True:
        answer = input("   → Line cap per skill (1/2/3): ").strip()
        mm_audit.event("gate", id="line-cap", gate_kind="choice", answer=answer)
        if answer in LINE_CAPS:
            return answer
        print("   ↳ Answer with 1, 2 or 3.")


def ask_model_target() -> str:
    print("\n   [1] Standard: models ≥ 100B (cloud or large local)")
    print("   [2] Compact: small local model ~27B (e.g. Qwen3 27B) — shorter, more mechanical instructions")
    while True:
        answer = input("   → Target model size (1/2): ").strip()
        mm_audit.event("gate", id="model-target", gate_kind="choice", answer=answer)
        if answer in MODEL_TARGETS:
            return answer
        print("   ↳ Answer with 1 or 2.")


def run_questionnaire() -> dict:
    """The question flow that builds the profile. Every prompt is an EXACT
    label from the orchestrators.json manifest: the app detects them as gates
    (numbered choices, free text, y/n) — never reword them without it."""
    print(f"\n{'=' * 62}")
    print("🧬 ADAPTATION PROFILE — a few questions, zero paid agent.")
    print("   The shipped skills are templates (Java/Spring, React/TS):")
    print("   describe your stack, the factory rewrites them for it.")
    print(f"{'=' * 62}")

    scope = ask_scope()

    testing = input("\n▶️  Also adapt the associated testing skills? (y/n): ").strip().lower()
    mm_audit.event("gate", id="testing", gate_kind="yn", answer=testing)

    backend_stack = "-"
    frontend_stack = "-"
    if scope in ("1", "2"):
        backend_stack = ask_backend_stack()
    if scope in ("1", "3"):
        frontend_stack = ask_frontend_stack()

    conventions = ask_conventions()

    cap = ask_line_cap()

    model = ask_model_target()

    return {
        "scope": scope,
        "include_testing": "y" if testing == "y" else "n",
        "backend_stack": backend_stack,
        "frontend_stack": frontend_stack,
        "conventions": conventions,
        "line_cap": str(LINE_CAPS[cap]),
        "model_target": MODEL_TARGETS[model],
    }


PROFILE_KEYS = ["scope", "include_testing", "backend_stack", "frontend_stack",
                "conventions", "line_cap", "model_target"]


def write_profile(profile: dict):
    """Profile persisted as flat YAML (key: value): readable, hand-editable,
    parsed here without any dependency."""
    lines = ["# Skill adaptation profile — generated by Skills-Adaptation.py",
             "# Hand-editable: relaunch the orchestrator and answer y to reuse it."]
    for key in PROFILE_KEYS:
        lines.append(f"{key}: {profile[key]}")
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_profile() -> dict:
    """Re-reads the persisted profile. Returns {} if missing or incomplete (the
    questionnaire will be replayed: never a guessed profile)."""
    if not os.path.exists(PROFILE_FILE):
        return {}
    profile = {}
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                profile[key.strip()] = value.strip()
    except OSError:
        return {}
    if any(key not in profile or profile[key] == "" for key in PROFILE_KEYS):
        return {}
    if profile["scope"] not in ("1", "2", "3") or profile["model_target"] not in MODEL_TARGETS.values():
        return {}
    if profile["line_cap"] not in {str(cap) for cap in LINE_CAPS.values()}:
        return {}
    return profile


def profile_summary(profile: dict) -> str:
    """PROFILE block injected as is into the generation and review prompts."""
    scope_labels = {"1": "backend + frontend", "2": "backend only", "3": "frontend only"}
    return (f"- Scope: {scope_labels[profile['scope']]}"
            f" (testing skills included: {profile['include_testing']})\n"
            f"- Target backend stack: {profile['backend_stack']}\n"
            f"- Target frontend stack: {profile['frontend_stack']}\n"
            f"- Enforced conventions: {profile['conventions']}\n"
            f"- STRICT cap: {profile['line_cap']} lines in total (frontmatter included)\n"
            f"- Target model: {MODEL_DIRECTIVES[profile['model_target']]}")


def target_skills(profile: dict) -> list:
    """Skills to adapt, in order (code first, tests next)."""
    targets = []
    if profile["scope"] in ("1", "2"):
        targets.append("backend-coding")
    if profile["scope"] in ("1", "3"):
        targets.append("frontend-coding")
    if profile["include_testing"] == "y":
        if profile["scope"] in ("1", "2"):
            targets.append("backend-testing")
        if profile["scope"] in ("1", "3"):
            targets.append("frontend-testing")
    return targets


def stack_for(name: str, profile: dict) -> str:
    return profile["backend_stack"] if name.startswith("backend") else profile["frontend_stack"]


# ─── PYTHON GUARDRAILS (DETERMINISTIC, FREE) ──────────────────────────────────

def parse_frontmatter(content: str) -> dict:
    """name + description from a SKILL.md YAML frontmatter, without dependency."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() in ("name", "description"):
            meta[key.strip()] = value.strip()
    return meta


def check_proposal(path: str, expected_name: str, line_cap: int) -> list:
    """Structural checks of a proposal. Every failure becomes a repair
    instruction sent to the agent — never an opinion, always a measured fact."""
    failures = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return [f"the file '{path}' is unreadable or missing"]
    total_lines = len(content.splitlines())
    if total_lines > line_cap:
        failures.append(f"the file is {total_lines} lines long, the STRICT cap is "
                        f"{line_cap} (cut in the templates, never in the rules)")
    meta = parse_frontmatter(content)
    if meta.get("name") != expected_name:
        failures.append(f"the frontmatter must keep exactly 'name: {expected_name}' "
                        f"(phase routing key)")
    if not meta.get("description"):
        failures.append("the frontmatter must carry a one-line 'description:' that "
                        "names the target stack")
    if "| ❌" not in content or "| ✅" not in content:
        failures.append("the patterns/anti-patterns table (columns '❌ FORBIDDEN' / "
                        "'✅ CORRECT') is mandatory")
    if "- [ ]" not in content:
        failures.append("the final checklist ('- [ ]' boxes) is mandatory")
    return failures


def parse_verdict(path: str) -> tuple:
    """Verdict (first line) + findings of the quality controller's report.
    Returns (verdict|None, findings) — None if the format is not respected."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return None, ""
    lines = content.splitlines()
    if not lines:
        return None, ""
    first = lines[0].strip()
    issues = "\n".join(lines[1:]).strip()
    if first == f"{VERDICT_LINE_PREFIX} {VERDICT_KO}":
        return VERDICT_KO, issues
    if first == f"{VERDICT_LINE_PREFIX} {VERDICT_OK}":
        return VERDICT_OK, issues
    return None, issues


# ─── AGENT PASSES (GENERATION, REPAIR, REVIEW) ────────────────────────────────

_RUNNER_STARTED = {"done": False}


def ensure_runner_started():
    """Lazy TUI boot: a resume where every proposal already exists does not
    even pay an agent startup."""
    if not _RUNNER_STARTED["done"]:
        RUNNER.start()
        _RUNNER_STARTED["done"] = True


def route_grid(grid_path: str, tmp_path: str):
    """Copies a pipeline grid to its routing file (offloaded prompt)."""
    if not os.path.exists(grid_path):
        fail_pipeline(f"❌ Missing pipeline grid: '{grid_path}' (re-equip the project from the app).")
    with open(grid_path, "r", encoding="utf-8") as f:
        grid = f.read()
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(grid)


def generate_proposal(name: str, profile: dict, proposal: str):
    """GENERATION pass: skill rewrite for the target stack, in a fresh
    context, guided by the skill-adapt grid."""
    print(f"\n🧬 [GENERATION] Adapting skill '{name}' for: {stack_for(name, profile)}")
    ensure_runner_started()
    RUNNER.new_context()
    route_grid(ADAPT_SKILL_FILE, TMP_ADAPT_FILE)
    skill_path = f"{SKILLS_DIR}/{name}/SKILL.md"

    prompt = f"""Read the adaptation instructions from the file '{TMP_ADAPT_FILE}', then the current skill '{skill_path}'.
You are a Skill Adapter. Applying the instructions of '{TMP_ADAPT_FILE}' SCRUPULOUSLY, rewrite this skill ({SKILL_DOMAINS[name]}) for the target stack of the PROFILE below, and save the result DIRECTLY into a new file '{proposal}' at the project root. The original skill '{skill_path}' stays INTACT.

ADAPTATION PROFILE:
{profile_summary(profile)}

NON-NEGOTIABLE reminders:
- Keep 'name: {name}' as is in the frontmatter; rewrite 'description' naming the target stack.
- ORDERS in the imperative, never descriptions.
- ❌/✅ table (anti-patterns → patterns) of at least 6 rows, specific to the target stack.
- Checkable final checklist, cap of {profile['line_cap']} lines in total.
Do it directly through your file-editing tools, without useless chatter in the console.
As the very LAST action, after saving '{proposal}', create the sentinel file '{ADAPT_DONE_SENTINEL}' at the root (content: the single word done): it is the end signal for the orchestrator.
"""
    cleanup_pipeline_sentinel(ADAPT_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)
    if not wait_for_pipeline_file(proposal, ADAPT_DONE_SENTINEL):
        fail_pipeline(f"❌ [GENERATION] Timeout or failure creating '{proposal}'.")


def repair_proposal(name: str, profile: dict, proposal: str, failures: str):
    """REPAIR pass: findings (Python guardrails or quality review) sent back
    to the agent, in the SAME context (the file is the truth)."""
    print(f"   🔧 [REPAIR] Findings sent back to the agent about '{proposal}'.")
    prompt = f"""Your proposal '{proposal}' is REJECTED as it stands. Findings to fix, one by one:
{failures}

Fix '{proposal}' DIRECTLY through your editing tools (the original skill stays intact), still respecting the instructions of '{TMP_ADAPT_FILE}' and the cap of {profile['line_cap']} lines in total.
As the very LAST action, recreate the sentinel file '{ADAPT_DONE_SENTINEL}' at the root (content: the single word done).
"""
    cleanup_pipeline_sentinel(ADAPT_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)
    if not wait_for_pipeline_file(proposal, ADAPT_DONE_SENTINEL):
        fail_pipeline(f"❌ [REPAIR] Timeout or failure updating '{proposal}'.")


def review_proposal(name: str, profile: dict, proposal: str) -> tuple:
    """REVIEW pass: independent quality controller, fresh context, verdict
    parsed by Python. Returns (verdict|None, findings)."""
    print(f"   🔎 [QUALITY REVIEW] Independent audit of '{proposal}'...")
    ensure_runner_started()
    RUNNER.new_context()
    route_grid(REVIEW_SKILL_FILE, TMP_REVIEW_FILE)

    prompt = f"""Read the quality-control grid from the file '{TMP_REVIEW_FILE}', then the proposed skill '{proposal}' and the original skill '{SKILLS_DIR}/{name}/SKILL.md'.
You are a skill Quality Controller, independent from the author. Applying the grid of '{TMP_REVIEW_FILE}', audit the proposal against the EXPECTED PROFILE below and write your report DIRECTLY into '{REVIEW_FILE}' at the project root. Read-only on everything else.

EXPECTED PROFILE:
{profile_summary(profile)}

STRICT format of the '{REVIEW_FILE}' report:
- First line, EXACTLY: 'VERDICT: COMPLIANT' or 'VERDICT: NON-COMPLIANT'.
- Then one finding per line: '- [BLOCKING] …' or '- [MINOR] …' (without findings: '- [MINOR] Nothing to report').
As the very LAST action, after saving '{REVIEW_FILE}', create the sentinel file '{REVIEW_DONE_SENTINEL}' at the root (content: the single word done).
"""
    try:
        os.remove(REVIEW_FILE)
    except OSError:
        pass
    cleanup_pipeline_sentinel(REVIEW_DONE_SENTINEL)
    mm_audit.event("agent_task", prompt_bytes=len(prompt))
    RUNNER.send_task(prompt)
    if not wait_for_pipeline_file(REVIEW_FILE, REVIEW_DONE_SENTINEL):
        fail_pipeline(f"❌ [QUALITY REVIEW] Timeout or failure producing '{REVIEW_FILE}'.")
    return parse_verdict(REVIEW_FILE)


# ─── QUALITY CHAIN PER SKILL ──────────────────────────────────────────────────

def build_proposal(name: str, profile: dict, proposal: str):
    """Generation + Python guardrails + quality review, with bounded repairs.
    On exit, the proposal is structurally valid; a quality verdict still
    NON-COMPLIANT is SHOWN, never hidden: the human decides at the gate."""
    generate_proposal(name, profile, proposal)

    line_cap = int(profile["line_cap"])
    repairs = 0
    failures = check_proposal(proposal, name, line_cap)
    while failures and repairs < MAX_REPAIRS:
        repairs += 1
        print(f"   ⚠️  Python guardrails: {len(failures)} finding(s) (repair {repairs}/{MAX_REPAIRS}).")
        repair_proposal(name, profile, proposal, "\n".join(f"- {failure}" for failure in failures))
        failures = check_proposal(proposal, name, line_cap)
    if failures:
        details = "\n".join(f"   - {failure}" for failure in failures)
        fail_pipeline(f"❌ '{proposal}' is still invalid after {MAX_REPAIRS} repair(s):\n{details}")
    print("   ✓ Python guardrails: structure, frontmatter and line cap respected.")

    verdict, issues = review_proposal(name, profile, proposal)
    if verdict != VERDICT_OK:
        shown = issues if issues else "(report without usable findings)"
        print(f"   ⚠️  Quality review: {VERDICT_KO if verdict == VERDICT_KO else 'unreadable verdict'} — one repair then re-review.\n{shown}")
        repair_proposal(name, profile, proposal,
                        issues if issues else "- the review report is empty: go through every rule of the adaptation grid one by one")
        failures = check_proposal(proposal, name, line_cap)
        if failures:
            details = "\n".join(f"   - {failure}" for failure in failures)
            fail_pipeline(f"❌ The post-review repair broke the structure of '{proposal}':\n{details}")
        verdict, issues = review_proposal(name, profile, proposal)
    if verdict == VERDICT_OK:
        print("   ✅ Quality review: COMPLIANT.")
    else:
        print(f"   ⚠️  Quality review still reserved after repair — findings shown, your call:\n{issues}")


def confirm_overwrite(name: str, proposal: str) -> bool:
    """Human gate per skill: the preview is the proposal at the root,
    editable in the app or another terminal before validating."""
    print(f"\n{'=' * 50}")
    print(f"📋 PROPOSAL READY: reread '{proposal}' (it will replace "
          f"'{SKILLS_DIR}/{name}/SKILL.md', previous content saved as .bak).")
    print(f"   You can edit it directly before validating: the file is the source of truth.")
    print(f"{'=' * 50}")
    answer = input(f"\n▶️  Overwrite the skill '{name}' with the adapted version? (y/n): ").strip().lower()
    mm_audit.event("gate", id="overwrite", gate_kind="yn", answer=answer)
    return answer == "y"


def apply_proposal(name: str, proposal: str) -> str:
    """.bak backup then overwrite. The .bak is the rollback trail
    (on top of git when the project has one)."""
    skill_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    backup_path = skill_path + ".bak"
    with open(skill_path, "r", encoding="utf-8") as f:
        original = f.read()
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(original)
    with open(proposal, "r", encoding="utf-8") as f:
        adapted = f.read()
    with open(skill_path, "w", encoding="utf-8") as f:
        f.write(adapted)
    os.remove(proposal)
    print(f"   ✅ '{skill_path}' overwritten (previous content: '{backup_path}').")
    return backup_path


def write_report(profile: dict, rows: list):
    """Final deliverable: what was adapted, refused, and where the rollbacks are."""
    scope_labels = {"1": "backend + frontend", "2": "backend only", "3": "frontend only"}
    lines = ["# Skill adaptation report — MAIsterMind", "",
             "## Applied profile",
             f"- Scope: {scope_labels[profile['scope']]} "
             f"(testing included: {profile['include_testing']})",
             f"- Backend stack: {profile['backend_stack']} · Frontend stack: {profile['frontend_stack']}",
             f"- Conventions: {profile['conventions']}",
             f"- Cap: {profile['line_cap']} lines · Target model: {profile['model_target']}",
             "", "## Processed skills", ""]
    lines.extend(rows)
    lines.extend(["", "Rollback: restore the matching '.bak' (or 'git checkout' of the skill).",
                  f"Reusable profile: '{PROFILE_FILE}' (relaunch the orchestrator and answer y)."])
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n🧾 Report written to '{REPORT_FILE}'.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "skill-adapt", RUNNER.name,
                   model=RUNNER.configured_model())
    # A leftover failReport.md from a previous run must not be taken for the
    # current run's one: purged at startup.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # ── Profile: reused on explicit agreement, otherwise questionnaire. ──
    profile = read_profile()
    if profile:
        print(f"🔄 Existing adaptation profile found ('{PROFILE_FILE}'):")
        for line in profile_summary(profile).splitlines():
            print(f"   {line}")
        answer = input("\n▶️  Reuse the existing adaptation profile? (y/n): ").strip().lower()
        mm_audit.event("gate", id="reuse-profile", gate_kind="yn", answer=answer)
        if answer != "y":
            profile = {}
    if not profile:
        profile = run_questionnaire()
        write_profile(profile)
        print(f"   ✓ Profile saved to '{PROFILE_FILE}' (hand-editable).")

    # ── Scope: every targeted skill must exist (equipped project). ──
    targets = target_skills(profile)
    missing = [name for name in targets
               if not os.path.exists(os.path.join(SKILLS_DIR, name, "SKILL.md"))]
    if missing:
        print(f"❌ Skill(s) missing from the project: {', '.join(missing)}.")
        write_fail_report("Project not equipped for this scope",
                          f"Missing skills under '{SKILLS_DIR}': {', '.join(missing)}. "
                          "Equip the project from the app (or narrow the scope), then relaunch.")
        sys.exit(1)
    print(f"\n🎯 Scope: {len(targets)} skill(s) → {', '.join(targets)}")

    # ── Quality chain + human gate, skill by skill. ──
    rows = []
    for name in targets:
        proposal = f"{PROPOSAL_PREFIX}{name}.md"
        if os.path.exists(proposal) and os.path.getsize(proposal) > 0:
            # Resume from files: the proposal is the state; re-arbitrated
            # without re-paying generation or review (edit it if needed, it is the truth).
            print(f"\n🔄 Existing proposal found for '{name}' ('{proposal}'): resumed without re-generation.")
        else:
            build_proposal(name, profile, proposal)
        if confirm_overwrite(name, proposal):
            backup_path = apply_proposal(name, proposal)
            rows.append(f"- **{name}**: OVERWRITTEN (stack: {stack_for(name, profile)}; rollback: '{backup_path}').")
        else:
            rows.append(f"- **{name}**: REFUSED — proposal kept in '{proposal}', the original is intact.")
            print(f"   ⏭️  '{name}' left intact; '{proposal}' kept for inspection.")

    write_report(profile, rows)

    # Clean close: routing files, tmux buffer, late sentinels and the transient
    # review report are purged; profile and .bak files SURVIVE.
    for tmp_f in [TMP_ADAPT_FILE, TMP_REVIEW_FILE, TMP_PROMPT_BUFFER,
                  ADAPT_DONE_SENTINEL, REVIEW_DONE_SENTINEL, REVIEW_FILE]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    RUNNER.kill()

    applied = sum(1 for row in rows if "OVERWRITTEN" in row)
    print(f"\n🏁 Adaptation finished: {applied}/{len(targets)} skill(s) overwritten. "
          f"The next production runs will use these adapted skills as they are.")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
