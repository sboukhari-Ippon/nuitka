#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arbitrated repairer for red-suite halts — companion of Safe-Coding.py.
─────────────────────────────────────────────────────────────────────────────
When a MAIsterMind run halts (phase REJECTED after MAX_ATTEMPTS, run killed mid-phase,
unresolved post-refactoring regression), the human only had two options: manual surgery
(UC4) or "bump the model one notch and relaunch". This script equips the third way,
faithful to the factory's philosophy (the HUMAN arbitrates, PYTHON verifies, the LLM
executes):

  1. DIAGNOSIS: an agent reads the red output, the diff of the faulty phase and the
     spec, then writes a UNIQUELY-named report 'fix_report-<uid>.md' that groups the
     failures by broken business BEHAVIOR (not by file). Unique because MAIsterMind
     purges its 'failReport.md' at startup: the fix report, on the other hand, SURVIVES
     relaunches and serves as the audit trail of the arbitrations (committed, like
     spec/plan/blackboard).
  2. HUMAN TRIAGE: for each broken behavior, the human decides in the console —
     UNWANTED REGRESSION (the tests are right, the code will be fixed) or desired
     EVOLUTION (the code is right, spec THEN tests will be aligned). Detail on demand,
     recap of the action plan, y/n confirmation before paying for any agent.
  3. GUARDED REPAIR: the two modes are the MIRRORS of the production guards —
     regression = production editable / ALL test files frozen (forced git checkout);
     evolution = tests editable / production frozen. A prompt-only interdiction is
     unverifiable; the diff is not. The verdict remains Python's execution of
     'verify_cmd' (exit code 0), never an LLM opinion.
  4. HANDSHAKE: on green, the faulty phase is marked status 'FIXED' (a CLAIM, not a
     verdict) — fix.py NEVER stamps DONE/OK. On relaunch (MANUAL, by you), MAIsterMind
     revalidates by execution then stamps it itself: it remains the sole authority of
     the verdict, and no coder is replayed on an already-complete phase (its anti-ghost
     guard would push the agent into gratuitous changes).

Why EVOLUTION goes through the spec BEFORE the tests: the spec feeds the remaining
phases (per-US slices) and the final refactoring. Adapting the tests without it means
letting a later coder — prompted on the old spec with the interdiction to weaken tests —
reintroduce the old behavior: the factory would fight itself.

NON-negotiable git discipline: fix.py commits the state at halt (wip) THEN each applied
pass. MAIsterMind's guards diff against HEAD: an uncommitted fix would be taken for the
next phase's work — and RESTORED (silently lost) by the tests-only guard of the first
'tests' phase to come.

Dedicated namespace (no residue from another pipeline can be taken for a signal of this
one): tmux session 'oc-fix-<hash>', sentinels '.fix_<slot>.attempt<N>.done', offloaded
prompt named by the harness.
"""

import os
import re
import sys
import time
import signal
import subprocess
import hashlib
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
    load_blackboard, signal_handler,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="fix", new_context_check=False, messages={
    "reuse":     "♻️  Tmux session '{session}' already active. Reusing it.",
    "start":     "🖥️  Starting tmux session '{session}' (repair)...",
    "boot":      "⏳ Waiting for the {tui} TUI to boot ({wait}s)...",
    "ready":     None,
    "follow":    "   👀 Follow the repair live: tmux attach -t {session}",
    "new_reset": "🔄 Resetting the {tui} context (/new)...",
    "kill":      "🛑 Tmux session '{session}' closed.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
PLAN_FILE             = "plan.md"
BLACKBOARD_FILE       = "blackboard.yaml"
REFACTO_REPORT_FILE   = "refactoring_report.md"
FAIL_REPORT_FILE      = "failReport.md"    # written by MAIsterMind; READ here, never purged
FIX_REPORT_PREFIX     = "fix_report-"      # uniquely-named report: the audit trail survives
SKILLS_DIR            = "./.agents/skills"
AGENT_CONFIG_FILE     = RUNNER.config_file
SPEC_APPROVED_SENTINEL = ".spec_approved"

# Offloaded instructions file and tmux prompt buffer (RELATIVE paths: the only valid
# choice on all 3 OSes, cf. Safe-Coding.py).
TMP_FIX_FILE          = RUNNER.tmp_file("fix")
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# DEDICATED tmux session, suffixed by the project: never shared with the main factory
# nor with another project (project B's prompts would land in project A's agent).
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per agent pass (safety net)
VERIFY_TIMEOUT        = resolve_timeout("verify", 300)            # 5 min max for the verification command
MAX_VERIFY_RETRIES_ON_TIMEOUT = 2      # immediate re-verifications on infra timeout
VERIFY_FEEDBACK_LIMIT = 4000           # max size of an output sent back to an agent
DIFF_PROMPT_LIMIT     = 6000           # max size of the culprit diff injected into prompts
STABLE_POLLS_FALLBACK = 15             # sentinel-less safety net for the diagnosis report

# Handshake states with Safe-Coding.py: a repair CLAIM, not a verdict. MAIsterMind
# revalidates by execution on relaunch and stamps DONE/OK itself.
FIXED_STATUS          = "FIXED"
FIXED_VERDICT         = "PENDING_RECHECK"


# ─── DEDICATED SENTINELS (.fix_<slot>.attempt<N>.done) ────────────────────────

def fix_sentinel(slot: str, attempt: int) -> str:
    """Per-pass AND per-attempt sentinel: a late sentinel from a previous attempt can
    never be taken for the signal of the current attempt."""
    return f".fix_{slot}.attempt{attempt}.done"


def cleanup_fix_sentinels(slot: str = None):
    """Purges the fix sentinels (of one slot, or all of them if slot=None)."""
    prefix = f".fix_{slot}." if slot else ".fix_"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def read_declared_files(slot: str, attempt: int) -> list:
    """List of the files declared by the agent in its sentinel (list markers stripped,
    cf. read_touched_files in Safe-Coding.py)."""
    path = fix_sentinel(slot, attempt)
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


def wait_for_file_creation(filepath: str, timeout: int = MAX_PHASE_TIMEOUT) -> bool:
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}'...")
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_init = os.path.getsize(filepath)
            time.sleep(1.5)
            if os.path.getsize(filepath) == size_init:
                return True
    return False


def wait_for_report_file(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> bool:
    """Wait for the diagnosis report: sentinel first, stability safety net as fallback
    (same contract as wait_for_pipeline_file in Safe-Coding.py)."""
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}' (completion signal: '{sentinel}')...")
    stable_streak = 0
    last_size = -1
    structural_warned = False
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        file_ready = os.path.exists(filepath) and os.path.getsize(filepath) > 0
        if file_ready and os.path.exists(sentinel):
            try:
                os.remove(sentinel)
            except OSError:
                pass
            return True
        if file_ready:
            size = os.path.getsize(filepath)
            stable_streak = stable_streak + 1 if size == last_size else 0
            last_size = size
            if stable_streak >= STABLE_POLLS_FALLBACK:
                if structural_check is not None and not structural_check(filepath):
                    if not structural_warned:
                        print(f"   ⏳ '{filepath}' is stable but unstructured: we keep waiting.")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinel '{sentinel}' missing but '{filepath}' is stable: "
                      f"report accepted (safety net).")
                return True
    return False


# ─── BLACKBOARD (ATOMIC read / write, cf. Safe-Coding.py) ─────────────────────

# Last journaled phase statuses (TRANSITION detection by save_blackboard).
_PHASE_STATUS_SEEN = {}


def save_blackboard(data: dict):
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


# ─── GIT GUARDRAILS (BEST-EFFORT — without git, guards inactive, never blocking) ──

_GIT = {"enabled": False}
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]

# Same body as Safe-Coding.py (with '.fix_*'): a project started BEFORE this evolution
# has a .gitignore without this pattern — without the append-only net below, commit_all
# (add -A) would commit the fix sentinels as noise.
GITIGNORE_BODY = f"""# MAIster-Mind orchestration artifacts (ephemeral)
{TMP_PROMPT_BUFFER}
{RUNNER.tmp_glob}
.phase_*
.pipeline_*
.fix_*
.spec_approved
.mm-runs/
__pycache__/
"""


def ensure_orchestration_ignored():
    """Guarantees the orchestration patterns in an existing .gitignore (append-only,
    idempotent, best-effort — cf. Safe-Coding.py). Does not create a .gitignore: that is
    the main factory's job."""
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


def run_git(args: list, timeout: int = 60) -> tuple:
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


def git_head_sha() -> str:
    ok, out = run_git(["rev-parse", "HEAD"])
    return out if ok else ""


def ensure_git_available():
    """fix.py NEVER initializes a repository (that is MAIsterMind's role): it reuses the
    existing one or runs degraded — the mechanical freeze guards and the culprit diff
    require git, the repair itself does not."""
    if shutil.which("git") is None or not os.path.isdir(".git"):
        print("⚠️  git unavailable or repository missing: mechanical file freezing, culprit "
              "diff and repair commits are DISABLED for this session (the interdictions "
              "will only be carried by the prompts).")
        return
    _GIT["enabled"] = True
    ensure_orchestration_ignored()


def commit_all(label: str) -> bool:
    """Commits the whole tree (--allow-empty: a milestone is worth having even without a diff).

    MANDATORY before handing back: MAIsterMind's guards diff against HEAD — an
    uncommitted fix would be taken for the next phase's work, and the tests-only guard
    of a 'tests' phase would RESTORE (lose) the correction silently.
    """
    if not _GIT["enabled"]:
        return False
    ok_add, _ = run_git(["add", "-A"])
    ok_commit = False
    if ok_add:
        ok_commit, _ = run_git(["commit", "-q", "--allow-empty", "-m", label])
    if not ok_commit:
        print(f"⚠️  Git commit failed for \"{label}\" (continuing without this milestone).")
    return ok_commit


def files_changed_since(ref: str) -> set:
    """Tracked files changed since 'ref' (working tree included) + untracked files."""
    if not _GIT["enabled"] or not ref:
        return set()
    changed = set()
    ok_diff, diff_out = run_git(["diff", "--name-only", ref])
    if ok_diff:
        changed.update(line.strip() for line in diff_out.splitlines() if line.strip())
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        changed.update(line.strip() for line in others_out.splitlines() if line.strip())
    return changed


# ─── FILE CLASSIFICATION (same heuristics as the factory) ─────────────────────

def is_test_file(path: str) -> bool:
    """Multi-language heuristic, deliberately BROAD on the test side (cf. Safe-Coding.py)."""
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


_ORCH_BASENAMES = {
    NEED_FILE, SPEC_FILE, PLAN_FILE, BLACKBOARD_FILE, BLACKBOARD_FILE + ".tmp",
    REFACTO_REPORT_FILE, FAIL_REPORT_FILE,
    TMP_FIX_FILE, TMP_PROMPT_BUFFER, SPEC_APPROVED_SENTINEL, ".gitignore",
    os.path.basename(__file__),
}


_ORCHESTRATOR_SCRIPTS = frozenset({
    "Safe-Coding.py", "Coding-Without-Tests.py", "Safe-TDD.py", "Safe-ATDD.py",
    "Design-Prototype.py", "Advanced-Coding.py", "Advanced-TDD.py", "Advanced-ATDD.py",
    "Spec.py", "Technical-Plan.py", "Audit-Design.py", "Audit-A11Y-RGAA.py",
    "Documentation.py", "Guided-Fix.py", "Skills-Adaptation.py",
    "MAIsterMind_App.py", "mm_runner.py",
})


def is_orchestration_file(path: str) -> bool:
    """Factory artifacts (never produced code). Here this includes the WHOLE
    the factory scripts: no repair agent has any legitimate reason to touch them —
    classified as orchestration, they leave the fixer's 'allowed production' scope and
    are therefore FROZEN (restored on sight) like the rest."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _ORCH_BASENAMES:
        return True
    if base in _ORCHESTRATOR_SCRIPTS or (base.startswith("MAIsterMind") and base.endswith(".py")):
        return True
    if base.startswith(".phase_") or base.startswith(".pipeline_") or base.startswith(".fix_"):
        return True
    if base.startswith(FIX_REPORT_PREFIX) and base.endswith(".md"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


# ─── VERIFICATION BY EXECUTION (the exit code IS the verdict) ─────────────────

def truncate_output(text: str, limit: int = VERIFY_FEEDBACK_LIMIT) -> str:
    """Truncates keeping BEGINNING and END (the root cause is often at the beginning)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return (text[:head]
            + f"\n[... output truncated ({len(text)} characters total) ...]\n"
            + text[-tail:])


def resolve_verify_cmd(phase: dict, blackboard: dict) -> str:
    return ((phase or {}).get("verify_cmd") or blackboard.get("verify_cmd") or "").strip()


def run_verify(cmd: str, timeout: int = VERIFY_TIMEOUT) -> tuple:
    """Runs the verification OUTSIDE tmux. (ok, output, timed_out) — cf. Safe-Coding.py."""
    print(f"   🧪 Verification by execution: {cmd}")
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
    """Re-verifies on INFRA timeout (the code did not change) before concluding."""
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
            print(f"   ⏱️  Verification timed out ({VERIFY_TIMEOUT}s) — likely infra incident. "
                  f"Re-verification {i + 1}/{MAX_VERIFY_RETRIES_ON_TIMEOUT}...")
    return False, output, True


def parse_test_count(output: str):
    """Best-effort count of passed tests (same patterns as Safe-Coding.py)."""
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


def record_test_count(output: str, blackboard: dict):
    """Refreshes the 'last_test_count' floor after the final green: an evolution can
    LEGITIMATELY shrink the suite (test now pointless) — without this refresh, the
    non-decreasing guard would wrongly reject the next phase of the resumed run."""
    new_count = parse_test_count(output)
    if new_count is None:
        return
    old_count = blackboard.get("last_test_count")
    if isinstance(old_count, int) and new_count != old_count:
        print(f"   ℹ️  Test floor refreshed: {old_count} → {new_count} passing.")
    blackboard["last_test_count"] = new_count
    save_blackboard(blackboard)


# ─── SPEC SLICING PER US (fixer's context window) ─────────────────────────────

US_HEADING_RE = re.compile(r"^###\s+(US-\d+)\b", re.IGNORECASE)


def collect_spec_us_ids(spec_text: str) -> set:
    ids = set()
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            ids.add(match.group(1).upper())
    return ids


def extract_spec_slice(spec_text: str, covers: list) -> str:
    """Spec slice limited to the covered US (+ common trunk) — graceful degradation:
    without a usable 'covers', the WHOLE spec (cf. Safe-Coding.py)."""
    wanted = {c.strip().upper() for c in (covers or []) if isinstance(c, str) and c.strip()}
    if not wanted:
        return spec_text
    spec_us_ids = collect_spec_us_ids(spec_text)
    if not spec_us_ids or not (wanted & spec_us_ids):
        return spec_text
    kept = []
    current_us = None
    for line in spec_text.splitlines():
        match = US_HEADING_RE.match(line.strip())
        if match:
            current_us = match.group(1).upper()
        elif current_us is not None and line.startswith("## "):
            current_us = None
        if current_us is None or current_us in wanted:
            kept.append(line)
    return "\n".join(kept)


def load_skills(skills_list: list) -> str:
    content = ""
    for skill in skills_list or []:
        skill_path = os.path.join(SKILLS_DIR, skill, "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                content += f"--- SKILL: {skill.upper()} ---\n{f.read()}\n\n"
    return content


# ─── REPAIR REPORT (uid, parsable format, appended sections) ──────────────────

def make_report_path() -> str:
    """UNIQUE timestamped name + short hash: sortable at a glance, never overwritten by
    a later session (the history of the arbitrations is the value of the file)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    salt = hashlib.sha1(f"{os.getcwd()}{time.time()}".encode("utf-8")).hexdigest()[:4]
    return f"{FIX_REPORT_PREFIX}{stamp}-{salt}.md"


# Heading of a group in the diagnosis report. STRICT format imposed on the prompt:
# it is what makes the console triage possible (one group = one human decision).
FIX_GROUP_RE = re.compile(r"^##\s*Broken behavior\s+(\d+)\s*:\s*(.+?)\s*$",
                          re.MULTILINE | re.IGNORECASE)


def fix_report_structural_check(path: str) -> bool:
    """Structural floor of the sentinel-less safety net: at least one group heading."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return bool(FIX_GROUP_RE.search(f.read()))
    except OSError:
        return False


def parse_fix_report(text: str) -> list:
    """Splits the report into groups [{num, title, body}]. Graceful degradation: an
    unstructured report (agent that ignored the format) becomes ONE global group — the
    human then decides in one block instead of per behavior, but decides."""
    matches = list(FIX_GROUP_RE.finditer(text))
    if not matches:
        return [{"num": 1,
                 "title": "All failures (report not structured by the AI)",
                 "body": text.strip()}]
    groups = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        groups.append({"num": int(m.group(1)),
                       "title": m.group(2).strip(),
                       "body": text[m.end():end].strip()})
    return groups


def extract_ai_reading(body: str) -> str:
    """The group's \"AI reading\" line (shown at triage), else an empty string."""
    match = re.search(r"\*\*AI reading\s*:?\*\*\s*:?\s*(.+)", body)
    return match.group(1).strip() if match else ""


def append_report(report_path: str, text: str):
    """Appends at the end of the report (human arbitration, outcome). Best-effort: never raises."""
    try:
        with open(report_path, "a", encoding="utf-8") as f:
            f.write("\n" + text.rstrip() + "\n")
    except OSError:
        pass


# ─── FAULTY PHASE DETECTION ───────────────────────────────────────────────────

def find_broken_phase(blackboard: dict):
    """First phase left failing by the run: REJECTED (nominal halt after MAX_ATTEMPTS),
    IN_PROGRESS (run killed mid-phase) or FIXED (previous repair to re-play). TODO/PENDING
    phases never started do not count."""
    for phase in blackboard.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        status = str(phase.get("status") or "").upper()
        verdict = str(phase.get("verdict") or "").upper()
        if status == "DONE" and verdict == "OK":
            continue
        if verdict == "REJECTED" or status in ("IN_PROGRESS", FIXED_STATUS):
            return phase
    return None


def mark_phase_fixed(phase: dict, blackboard: dict):
    """Sets the repair CLAIM (never DONE/OK: MAIsterMind revalidates)."""
    phase["status"] = FIXED_STATUS
    phase["verdict"] = FIXED_VERDICT
    phase["critic_feedback"] = ""
    save_blackboard(blackboard)


# ─── MECHANICAL GUARDS OF THE AGENT PASSES ────────────────────────────────────

def enforce_allowed_files(allowed_pred) -> list:
    """Restores (git checkout) any TRACKED file changed since HEAD outside the pass's
    allowed scope. HEAD is reliable because fix.py commits the state at halt THEN each
    applied pass: the diff only contains the current pass's work. Stricter than the
    production guards (blackboard, spec, plan, scripts: anything not explicitly allowed
    is frozen): during a pass, fix.py rewrites NOTHING itself, so no exception is
    needed. Limit inherited from the family: UNTRACKED files created by the agent
    escape the freeze."""
    if not _GIT["enabled"]:
        return []
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    if not ok_diff:
        return []
    forbidden = sorted(f.strip() for f in diff_out.splitlines()
                       if f.strip() and not allowed_pred(f.strip()))
    if forbidden:
        run_git(["checkout", "--"] + forbidden)
    return forbidden


def nothing_declared_touched(declared: list, since_ts: float, allowed_pred) -> bool:
    """Anti \"ghost agent\" guard of a pass: True if NO declared file actually changed.
    Signals: git diff since HEAD filtered to the pass's allowed scope (the committed
    work of previous passes does not count), then mtime since the start of the pass
    (fallback without git)."""
    changed_allowed = {f for f in files_changed_since("HEAD") if allowed_pred(f)}
    for path in declared:
        clean = path.strip().strip("'\"`")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean:
            continue
        if clean in changed_allowed:
            return False
        try:
            if os.path.exists(clean) and os.path.getmtime(clean) >= since_ts:
                return False
        except OSError:
            continue
    return True


def run_agent_pass(slot: str, build_context, allowed_pred, forbidden_label: str,
                   allow_noop: bool = False) -> bool:
    """A guarded agent pass: offloaded prompt → sentinel → mechanical freeze →
    anti-ghost, with MAX_ATTEMPTS attempts. build_context(attempt, feedback) provides
    the full instructions. allow_noop: the agent may legitimately conclude \"nothing to
    change\" (sentinel containing the single word NO_CHANGE) — reserved for the spec
    pass, where the spec may ALREADY describe the endorsed behavior."""
    pass_started = time.time()
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cleanup_fix_sentinels(slot)
        print(f"\n🚀 [PASS {slot.upper()} — attempt {attempt}/{MAX_ATTEMPTS}] Launching the agent...")
        with open(TMP_FIX_FILE, "w", encoding="utf-8") as f:
            f.write(build_context(attempt, feedback))
        RUNNER.new_context()
        mm_audit.event("agent_task")
        RUNNER.send_task(f"Read the instructions file '{TMP_FIX_FILE}' at the project root "
                         f"and follow its instructions scrupulously.")
        if not wait_for_file_creation(fix_sentinel(slot, attempt)):
            print(f"⏱️  The agent did not signal completion (sentinel missing). New attempt.")
            feedback = ("Your previous attempt produced no completion signal: you MUST finish "
                        "by creating the requested sentinel file.")
            continue
        declared = read_declared_files(slot, attempt)
        if allow_noop and [d.strip().upper() for d in declared] == ["NO_CHANGE"]:
            print(f"   ℹ️  Pass {slot}: the agent declares that no change is needed.")
            cleanup_fix_sentinels(slot)
            return True
        # Mechanical freeze BEFORE the anti-ghost: restore the forbidden first, judge
        # what remains next (a 100% out-of-scope attempt becomes a freeze rejection,
        # with the most useful feedback).
        forbidden = enforce_allowed_files(allowed_pred)
        if forbidden:
            feedback = (f"You changed files that are FORBIDDEN for this step: "
                        f"{', '.join(forbidden)}. They were restored on sight. "
                        f"{forbidden_label}")
            print(f"🔒 [REJECTED] Out-of-scope files restored: {', '.join(forbidden)}.")
            continue
        if nothing_declared_touched(declared, pass_started, allowed_pred):
            feedback = (f"Your sentinel declares {len(declared)} file(s), but NONE was "
                        f"actually created or changed during this pass. CONCRETELY do "
                        f"the requested work, and only then recreate the sentinel with "
                        f"the real list of touched files.")
            print(f"👻 [REJECTED] Sentinel written but no declared file was touched.")
            continue
        cleanup_fix_sentinels(slot)
        return True
    cleanup_fix_sentinels(slot)
    return False


# ─── PASS SCOPE PREDICATES ────────────────────────────────────────────────────
# Regression = hardened mirror of the 'protected_test_files' guard (production editable,
# ALL tests frozen). Evolution = mirror of the tests-only guard (tests editable,
# production frozen).

def allowed_for_code_pass(path: str) -> bool:
    return not is_test_file(path) and not is_orchestration_file(path)


def allowed_for_tests_pass(path: str) -> bool:
    return is_test_file(path) and not is_orchestration_file(path)


def allowed_for_spec_pass(path: str) -> bool:
    clean = str(path).strip().replace("\\", "/")
    if clean.startswith("./"):
        clean = clean[2:]
    return clean == SPEC_FILE


def allowed_nothing(path: str) -> bool:
    return False


# ─── AGENT PROMPTS ────────────────────────────────────────────────────────────

def group_block(group: dict, limit: int = 2500) -> str:
    return truncate_output(f"[Behavior {group['num']}] {group['title']}\n{group['body']}", limit)


def build_diag_context(report_path: str, sentinel: str, phase: dict, verify_cmd: str,
                       verify_output: str, culprit_diff: str, fail_report_text: str) -> str:
    if phase:
        phase_block = (f"Halted phase: Phase {phase.get('id', '?')} \"{phase.get('name', '(unnamed)')}\" "
                       f"(nature: {phase.get('nature') or 'not declared'})\n"
                       f"Phase checklist:\n"
                       + "\n".join(f"- {t}" for t in (phase.get("tasks") or []))
                       + f"\nLast recorded feedback:\n{truncate_output(str(phase.get('critic_feedback') or '(empty)'), 1500)}")
    else:
        phase_block = ("No failing phase in the blackboard: the halt probably comes from "
                       "the final refactoring or a manual change to the code.")
    fail_block = f"\n--- FACTORY HALT REPORT ({FAIL_REPORT_FILE}, truncated) ---\n{truncate_output(fail_report_text, 1500)}\n" if fail_report_text else ""
    return f"""--- BEHAVIORAL CONTRACT ---
You are a senior Diagnosis Engineer. The MAIsterMind factory run halted: the
verification suite is RED. You FIX nothing and you change NO file of the project: your
ONLY production is the requested report (any other changed file will be restored on
sight).

--- WHAT HAPPENED ---
{phase_block}
Verification command (universal verdict): "{verify_cmd}"
{fail_block}
--- OUTPUT OF THE FAILING VERIFICATION (truncated) ---
{verify_output}

--- CHANGES INTRODUCED BY THE HALTED PHASE (diff, truncated) ---
{culprit_diff or "(diff unavailable: git disabled or no committed change)"}

--- YOUR MISSION ---
1. Read '{SPEC_FILE}' (source of truth of the EXPECTED behavior) and, if needed, the
   failing test files to understand what they actually verify.
2. Group the failures by broken business BEHAVIOR (never by file): one group = one
   decision the human can take in one block. Aim for FEW groups (1 to 5).
3. Write the report '{report_path}' in the STRICT format below. The orchestrator PARSES
   it (exact headings) and the human will decide, group by group: unwanted regression
   (the code will be fixed) or desired evolution (spec and tests will be aligned).
   The real question asked to the human is: "is the spec's criterion still right?" —
   write each group so they can answer it.

--- STRICT FORMAT OF THE REPORT '{report_path}' ---
# Repair report — Guided-Fix
(2 to 4 lines of context: halted phase, verification command, volume of failures.)

## Broken behavior 1: <short title, meaningful to a business human>
- **Red tests:** <names of the tests and files concerned>
- **Expected (spec):** <US and acceptance criterion concerned, quoted from {SPEC_FILE}; write "out of spec" if not found>
- **Observed:** <what the code currently does, according to the runner output>
- **Suspect change:** <file(s) and change of the halted phase that seem to be the cause>
- **AI reading:** <"likely regression" or "likely evolution"> — <justification in 1 or 2 sentences>

## Broken behavior 2: <...>
(as many sections as distinct behaviors, numbered 1, 2, 3, ...)

--- MANDATORY COMPLETION INSTRUCTION ---
You NEVER touch the {BLACKBOARD_FILE} file. As your VERY LAST action, after saving
'{report_path}', create the sentinel file '{sentinel}' at the root (content: the single
word done): it is the completion signal for the orchestrator.
"""


def build_spec_context(sentinel: str, evolution_groups: list, feedback: str) -> str:
    groups_text = "\n\n".join(group_block(g) for g in evolution_groups)
    return f"""--- BEHAVIORAL CONTRACT ---
You are a senior Product Owner. The human just ENDORSED a behavior evolution: the
current code is right, the specification is the one lagging behind it.

--- EVOLUTIONS ENDORSED BY THE HUMAN ---
{groups_text}

--- YOUR MISSION ---
Update '{SPEC_FILE}' so that the acceptance criteria concerned describe the behavior
that is NOW desired (the one observed in the code):
- Change ONLY the user stories and criteria touched by the evolutions above.
- Rewrite nothing else: zero rewording of the rest, zero new requirement (YAGNI).
- You change NO file other than '{SPEC_FILE}' (any other changed file will be restored
  on sight).
- Special case: if '{SPEC_FILE}' ALREADY correctly describes the endorsed behavior
  (only the tests had drifted), do not touch it and write the single word NO_CHANGE in
  the completion sentinel.

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback or "First attempt — no previous feedback."}

--- MANDATORY COMPLETION INSTRUCTION ---
You NEVER touch the {BLACKBOARD_FILE} file. As your VERY LAST action, create the
sentinel file '{sentinel}' at the root, containing the list of changed files
(one path per line — normally the single line {SPEC_FILE}, or NO_CHANGE).
"""


def build_tests_context(sentinel: str, evolution_groups: list, verify_cmd: str, feedback: str) -> str:
    groups_text = "\n\n".join(group_block(g) for g in evolution_groups)
    return f"""--- BEHAVIORAL CONTRACT ---
You are a senior Test Engineer. The human ENDORSED an evolution: the current code is
right, and the tests listed below still verify the OLD behavior. The specification
'{SPEC_FILE}' has just been aligned: it is the new source of truth.

--- ENDORSED EVOLUTIONS (tests to align) ---
{groups_text}

--- CURRENT OUTPUT OF THE VERIFICATION (truncated) ---
{feedback or "(see the groups above)"}

--- YOUR MISSION ---
1. Read '{SPEC_FILE}' (updated) then the test files concerned.
2. Adapt these tests to the behavior that is NOW desired, with the same rigor as
   before: test the REAL behavior (never an always-true assertion), cover the bounds.
3. INTERDICTIONS: you change ONLY test files (any changed production file will be
   restored on sight); you do not weaken the suite (no test emptied, disabled or
   deleted — EXCEPT a test made strictly pointless by the evolution); no
   Testcontainers, Docker, network I/O or database.
4. Your ONLY success criterion: the command "{verify_cmd}" must succeed
   (exit code 0). The orchestrator will run it itself.

--- MANDATORY COMPLETION INSTRUCTION ---
You NEVER touch the {BLACKBOARD_FILE} file. As your VERY LAST action, create the
sentinel file '{sentinel}' at the root, containing the list of changed files
(one path per line).
"""


def build_code_context(sentinel: str, regression_groups: list, phase: dict, blackboard: dict,
                       spec_slice: str, culprit_diff: str, verify_cmd: str, feedback: str) -> str:
    groups_text = "\n\n".join(group_block(g) for g in regression_groups)
    rules = blackboard.get("global_rules") or {}
    skills_context = load_skills((phase or {}).get("skills_required"))
    if phase:
        phase_block = (f"Phase {phase.get('id', '?')} \"{phase.get('name', '(unnamed)')}\". Its checklist "
                       f"(do what is missing, BETTER than the halted attempt):\n"
                       + "\n".join(f"- {t}" for t in (phase.get("tasks") or [])))
    else:
        phase_block = "(no identified phase: post-refactoring regression or manual change)"
    return f"""--- SYSTEM RULES ---
Stack: {rules.get('target', '(not specified)')}
Interdictions: {rules.get('constraints', '(not specified)')}

{skills_context}--- BEHAVIORAL CONTRACT ---
You are a senior Fixer Engineer. The human CONFIRMED an UNWANTED regression: the
behavior expected by the spec is broken, and the TESTS are the ones that are right —
so the PRODUCTION code is what must be fixed.

--- REGRESSIONS CONFIRMED BY THE HUMAN ---
{groups_text}

--- PHASE AT THE ORIGIN OF THE HALT ---
{phase_block}

--- SPEC EXCERPT COVERED BY THIS PHASE ---
{spec_slice}

--- CHANGES INTRODUCED BY THE PHASE (diff, truncated) ---
{culprit_diff or "(diff unavailable)"}

--- CURRENT OUTPUT OF THE VERIFICATION (truncated) ---
{feedback}

--- YOUR MISSION ---
Fix the PRODUCTION code so that the command "{verify_cmd}" succeeds (exit code 0). It
is your ONLY success criterion; the orchestrator will run it itself.
INTERDICTIONS: you change NO test file (they are right; any changed test file will be
restored on sight); you NEVER work around a test — you fix the behavior it verifies.

--- MANDATORY COMPLETION INSTRUCTION ---
You NEVER touch the {BLACKBOARD_FILE} file. As your VERY LAST action, create the
sentinel file '{sentinel}' at the root, containing the list of changed files
(one path per line).
"""


# ─── HUMAN TRIAGE (the heart of the UX: one decision per behavior) ────────────

def print_group_detail(group: dict):
    print(f"\n{'─' * 62}")
    print(f"## Broken behavior {group['num']}: {group['title']}")
    print(group["body"])
    print(f"{'─' * 62}")


def triage_groups(groups: list, report_path: str):
    """Console triage loop. Returns {group_index: 'r'|'e'} or None (abandon).

    Intended ergonomics: everything happens HERE (the detail shows up on demand via
    'o', no need to open the report elsewhere), the AI reading is a displayed OPINION —
    never a pre-filled decision (anchoring bias) —, and nothing is launched before the
    confirmed recap ('n' redoes the triage instead of abandoning everything).
    """
    total = len(groups)
    while True:
        decisions = {}
        print(f"\n{'=' * 62}")
        print(f"🔍 TRIAGE — {total} broken behavior(s) to arbitrate.")
        print(f"   Full detail in '{report_path}' (editable in another terminal);")
        print(f"   you can also display it here with 'o'. The question, every time:")
        print(f"   \"is the spec's criterion still right?\"")
        print(f"{'=' * 62}")
        for i, group in enumerate(groups, 1):
            print(f"\n[{i}/{total}] {group['title']}")
            reading = extract_ai_reading(group["body"])
            if reading:
                print(f"      🤖 AI reading: {reading}")
            print("      [r] UNWANTED regression → the code will be fixed (tests frozen)")
            print("      [e] desired evolution   → spec then tests aligned (production frozen)")
            print("      [o] display the full detail of this behavior")
            while True:
                answer = input("   → Your decision (r/e/o): ").strip().lower()
                mm_audit.event("gate", id="fix-triage", gate_kind="choice", answer=answer)
                if answer == "o":
                    print_group_detail(group)
                    continue
                if answer in ("r", "e"):
                    decisions[i] = answer
                    break
                print("   ↳ Answer with r (regression), e (evolution) or o (detail).")
        labels = {"r": "🔧 REGRESSION", "e": "📈 EVOLUTION "}
        print(f"\n{'=' * 62}")
        print("📋 ARBITRATION TO CONFIRM")
        for i, group in enumerate(groups, 1):
            print(f"   {i}. {labels[decisions[i]]} — {group['title']}")
        print("Action plan:")
        step = 1
        if any(d == "e" for d in decisions.values()):
            print(f"   {step}) Update of '{SPEC_FILE}' (evolutions), validated by you (y/n).")
            step += 1
            print(f"   {step}) Adaptation of the evolutions' tests (production code FROZEN).")
            step += 1
        if any(d == "r" for d in decisions.values()):
            print(f"   {step}) Fix of the regressions' code (test files FROZEN).")
            step += 1
        print(f"   {step}) Full verification run by Python; on green, FIXED marker")
        print(f"      set — you will relaunch Safe-Coding.py yourself for the revalidation.")
        print(f"{'=' * 62}")
        answer = input("\n▶️  Confirm this arbitration and launch the repair? "
                       "(y = yes / n = redo the triage / q = abandon): ").strip().lower()
        mm_audit.event("gate", id="fix-confirm", gate_kind="choice", answer=answer)
        if answer == "y":
            return decisions
        if answer == "q":
            return None
        print("\n🔁 Restarting the triage from the beginning.")


def write_arbitration_section(report_path: str, groups: list, decisions: dict):
    lines = ["## Human arbitration", f"_(timestamped {time.strftime('%Y-%m-%d %H:%M')})_", ""]
    for i, group in enumerate(groups, 1):
        if decisions[i] == "r":
            lines.append(f"- Behavior {i} \"{group['title']}\": **UNWANTED REGRESSION** "
                         f"→ code fix (tests frozen).")
        else:
            lines.append(f"- Behavior {i} \"{group['title']}\": **DESIRED EVOLUTION** "
                         f"→ spec and tests aligned (production frozen).")
    append_report(report_path, "\n".join(lines))


# ─── END-OF-RUN BOOKKEEPING (handshake + guards of the resumed run) ───────────

def update_protected_test_files(blackboard: dict, pre_wip_sha: str, phase: dict):
    """If the repaired phase is a 'tests' phase, its deliverables (phase work + repair,
    i.e. everything that changed since the halt) join the protected files — bookkeeping
    that MAIsterMind maintains on the nominal path and that the revalidation cannot
    reconstruct (it does not have the pre-repair sha)."""
    if not phase or str(phase.get("nature") or "").strip().lower() != "tests":
        return
    if not _GIT["enabled"] or not pre_wip_sha:
        return
    protected = set(blackboard.get("protected_test_files") or [])
    added = {f for f in files_changed_since(pre_wip_sha)
             if is_test_file(f) and not is_orchestration_file(f)}
    if added:
        protected.update(added)
        blackboard["protected_test_files"] = sorted(protected)
        save_blackboard(blackboard)
        print(f"   🛡️  {len(added)} test file(s) added to the protected files.")


def print_failure_message(report_path: str, last_output: str):
    model = RUNNER.configured_model()
    print(f"""
{'=' * 62}
❌ The repair did not converge after {MAX_ATTEMPTS} attempt(s): the suite remains RED.

   Last output (truncated):
   {truncate_output(last_output, 1200)}

💡 The current model ({model}) is stuck on this repair. Leads:
   - Bump the model one notch (/model in the TUI or '{AGENT_CONFIG_FILE}')
     then relaunch Guided-Fix.py: new diagnosis, new triage.
   - Or fix by hand, then relaunch Guided-Fix.py: it will observe the
     green and offer to set the FIXED marker without paying for an agent.
   The arbitrations of this session are kept in '{report_path}'.
{'=' * 62}
""")


# ─── MAIN ORCHESTRATION ───────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "fix", RUNNER.name,
                   model=RUNNER.configured_model())
    print(f"{'=' * 62}\n🩺 Guided-Fix — arbitrated repair of a red-suite halt\n{'=' * 62}")

    # ── Prerequisite: a MAIsterMind run took place (blackboard = resume state). ──
    if not os.path.exists(BLACKBOARD_FILE):
        print(f"❌ '{BLACKBOARD_FILE}' not found: nothing to repair here. Launch "
              f"Safe-Coding.py first (this script heals production halts, not the pipeline).")
        sys.exit(1)
    try:
        blackboard = load_blackboard()
    except Exception as err:
        print(f"❌ '{BLACKBOARD_FILE}' unreadable (invalid or corrupted YAML): {err}")
        print(f"   → Fix or delete '{BLACKBOARD_FILE}' then relaunch Safe-Coding.py.")
        sys.exit(1)
    if not isinstance(blackboard, dict) or not isinstance(blackboard.get("phases"), list):
        print(f"❌ '{BLACKBOARD_FILE}' has no usable 'phases' block: nothing to repair.")
        sys.exit(1)

    broken_phase = find_broken_phase(blackboard)
    verify_cmd = resolve_verify_cmd(broken_phase or {}, blackboard)
    if not verify_cmd:
        print(f"❌ No verification command ('verify_cmd' on the phase or global) in "
              f"'{BLACKBOARD_FILE}': without an executable verdict, no repair can be proven.")
        sys.exit(1)

    ensure_git_available()
    cleanup_fix_sentinels()  # residues of an interrupted fix session

    if broken_phase:
        print(f"🎯 Failing phase detected: Phase {broken_phase.get('id', '?')} "
              f"\"{broken_phase.get('name', '(unnamed)')}\" "
              f"[{broken_phase.get('status', '?')}/{broken_phase.get('verdict', '?')}]")
    else:
        print("ℹ️  No failing phase in the blackboard: if the suite is red, the halt "
              "comes from the final refactoring or a manual change.")

    # ── Entry verdict: Python executes, nobody assumes. ──
    ok, initial_output, timed_out = run_verify_resilient(verify_cmd)
    if timed_out:
        print(f"""
🛑 [INFRA TIMEOUT] The verification "{verify_cmd}" times out repeatedly: an
   INFRASTRUCTURE incident (machine, network, frozen process), not a state of the code.
   There is no regression/evolution arbitration to render: repair the environment, then
   relaunch.
""")
        sys.exit(1)
    if ok:
        if broken_phase:
            print(f"\n✅ The suite ALREADY passes green: the code was probably repaired by hand "
                  f"(cf. UC4). Phase {broken_phase.get('id', '?')} can be marked '{FIXED_STATUS}': "
                  f"MAIsterMind will revalidate it by execution on relaunch, without re-paying a coder.")
            answer = input(f"\n▶️  Mark phase {broken_phase.get('id', '?')} '{FIXED_STATUS}' "
                           f"and let you relaunch Safe-Coding.py? (y/n): ").strip().lower()
            mm_audit.event("gate", id="fix-mark-fixed", gate_kind="yn", answer=answer)
            if answer == "y":
                mark_phase_fixed(broken_phase, blackboard)
                record_test_count(initial_output, blackboard)
                update_protected_test_files(blackboard, git_head_sha(), broken_phase)
                commit_all(f"fix(phase {broken_phase.get('id', '?')}): green state observed (manual repair)")
                print(f"\n🏁 Marker set. Relaunch 'python3 Safe-Coding.py' to revalidate and continue.")
            else:
                print("⏹️  Nothing was changed.")
            sys.exit(0)
        print("\n✅ The verification suite passes: nothing to repair. Relaunch Safe-Coding.py "
              "if you want to continue or replay the final polish.")
        sys.exit(0)

    # ── Red suite confirmed: freeze the state BEFORE any intervention. ──
    # pre_wip_sha = last commit of the run (green phases are committed): the diff
    # pre_wip → wip is therefore EXACTLY the faulty phase's work — a gift for the
    # diagnosis. The wip commit then makes HEAD reliable for the mechanical freezes.
    pre_wip_sha = git_head_sha()
    commit_all("wip(fix): state of the run at halt (before repair)")
    culprit_diff = ""
    if _GIT["enabled"] and pre_wip_sha:
        ok_diff, diff_out = run_git(["diff", pre_wip_sha, "HEAD"])
        if ok_diff:
            culprit_diff = truncate_output(diff_out, DIFF_PROMPT_LIMIT)

    fail_report_text = ""
    if os.path.exists(FAIL_REPORT_FILE):
        try:
            with open(FAIL_REPORT_FILE, "r", encoding="utf-8") as f:
                fail_report_text = f.read()
        except OSError:
            pass

    # ── Diagnosis by agent → uniquely-named report. ──
    RUNNER.start()
    report_path = make_report_path()
    diag_sentinel = fix_sentinel("diag", 1)
    cleanup_fix_sentinels("diag")
    print(f"\n📖 [DIAGNOSIS] AI analysis of the failures → '{report_path}'...")
    with open(TMP_FIX_FILE, "w", encoding="utf-8") as f:
        f.write(build_diag_context(report_path, diag_sentinel, broken_phase, verify_cmd,
                                   initial_output, culprit_diff, fail_report_text))
    mm_audit.event("agent_task")
    RUNNER.send_task(f"Read the instructions file '{TMP_FIX_FILE}' at the project root "
                     f"and follow its instructions scrupulously.")
    if not wait_for_report_file(report_path, diag_sentinel,
                                structural_check=fix_report_structural_check):
        print(f"❌ [DIAGNOSIS] Timeout: '{report_path}' not produced. Suspect the model's tool "
              f"calling (attach yourself: tmux attach -t {TMUX_SESSION}), then relaunch.")
        RUNNER.kill()
        sys.exit(1)
    stray = enforce_allowed_files(allowed_nothing)
    if stray:
        print(f"   ⚠️  The diagnosis had changed files ({', '.join(stray)}): restored "
              f"(it is read-only, only its report matters).")
    with open(report_path, "r", encoding="utf-8") as f:
        groups = parse_fix_report(f.read())
    print(f"✅ [DIAGNOSIS] {len(groups)} broken behavior(s) described in '{report_path}'.")

    # ── Human triage (the arbitration is THE added value of this script). ──
    decisions = triage_groups(groups, report_path)
    if decisions is None:
        append_report(report_path, "## Human arbitration\n- Abandoned by the user (no action).")
        print("⏹️  Abandoned: nothing was changed (the state at halt remains committed as wip).")
        RUNNER.kill()
        sys.exit(0)
    write_arbitration_section(report_path, groups, decisions)
    evolution_groups = [g for i, g in enumerate(groups, 1) if decisions[i] == "e"]
    regression_groups = [g for i, g in enumerate(groups, 1) if decisions[i] == "r"]

    # ── EVOLUTIONS FIRST: the spec becomes true again, then the tests follow it. ──
    # Order imposed by the factory's architecture: spec → tests → code. Fixing the code
    # against still-wrong tests would make the repair oscillate.
    if evolution_groups:
        print(f"\n📈 [EVOLUTION 1/2] Aligning '{SPEC_FILE}' with the endorsed behavior...")
        if not run_agent_pass(
                "spec",
                lambda attempt, feedback: build_spec_context(fix_sentinel("spec", attempt),
                                                             evolution_groups, feedback),
                allowed_for_spec_pass,
                f"This step changes ONLY '{SPEC_FILE}'.",
                allow_noop=True):
            print(f"❌ The spec update did not succeed after {MAX_ATTEMPTS} attempts.")
            commit_all(f"wip(fix): repair not completed (see {report_path})")
            RUNNER.kill()
            sys.exit(1)
        # HUMAN validation of the spec diff: same contract as confirm_spec_with_human —
        # the human can edit spec.md in another terminal BEFORE answering.
        spec_changed = False
        if _GIT["enabled"]:
            ok_diff, spec_diff = run_git(["diff", "HEAD", "--", SPEC_FILE])
            spec_changed = bool(ok_diff and spec_diff.strip())
            if spec_changed:
                print(f"\n{'─' * 62}\n📋 PROPOSED SPEC DIFF:\n{truncate_output(spec_diff, 3000)}\n{'─' * 62}")
        if not spec_changed:
            print(f"\nℹ️  '{SPEC_FILE}' unchanged (or diff unavailable without git): reread it "
                  f"in another terminal if you want to check.")
        print(f"   You can edit '{SPEC_FILE}' directly in another terminal before validating.")
        answer = input("\n▶️  Validate this updated spec and adapt the tests? (y/n): ").strip().lower()
        mm_audit.event("gate", id="fix-spec-update", gate_kind="yn", answer=answer)
        if answer != "y":
            if _GIT["enabled"]:
                run_git(["checkout", "--", SPEC_FILE])
                print(f"↩️  '{SPEC_FILE}' restored.")
            print("⏹️  Cancelled by the user. Relaunch Guided-Fix.py to redo the triage.")
            RUNNER.kill()
            sys.exit(0)
        commit_all("fix: spec aligned with the endorsed evolution")

        print(f"\n📈 [EVOLUTION 2/2] Adapting the tests to the endorsed behavior...")
        if not run_agent_pass(
                "tests",
                lambda attempt, feedback: build_tests_context(fix_sentinel("tests", attempt),
                                                              evolution_groups, verify_cmd,
                                                              feedback or initial_output),
                allowed_for_tests_pass,
                "This step changes ONLY test files: the production code is FROZEN."):
            print(f"❌ The tests adaptation did not succeed after {MAX_ATTEMPTS} attempts.")
            commit_all(f"wip(fix): repair not completed (see {report_path})")
            RUNNER.kill()
            sys.exit(1)
        commit_all("fix: tests aligned with the endorsed evolution")

    # ── REGRESSIONS: the code gets fixed, the tests are authoritative. ──
    spec_text = ""
    if os.path.exists(SPEC_FILE):
        with open(SPEC_FILE, "r", encoding="utf-8") as f:
            spec_text = f.read()
    spec_slice = extract_spec_slice(spec_text, (broken_phase or {}).get("covers")) if spec_text \
        else "(spec not found: rely on the red tests)"

    def run_code_pass(feedback: str) -> bool:
        return run_agent_pass(
            "code",
            lambda attempt, fb: build_code_context(fix_sentinel("code", attempt),
                                                   regression_groups, broken_phase, blackboard,
                                                   spec_slice, culprit_diff, verify_cmd,
                                                   fb or feedback),
            allowed_for_code_pass,
            "This step changes ONLY the production code: test files are FROZEN.")

    def run_tests_retry_pass(feedback: str) -> bool:
        return run_agent_pass(
            "tests",
            lambda attempt, fb: build_tests_context(fix_sentinel("tests", attempt),
                                                    evolution_groups, verify_cmd, fb or feedback),
            allowed_for_tests_pass,
            "This step changes ONLY test files: the production code is FROZEN.")

    if regression_groups:
        print(f"\n🔧 [REGRESSION] Fixing the production code...")
        if not run_code_pass(initial_output):
            print(f"❌ The code fix did not succeed after {MAX_ATTEMPTS} attempts.")
            commit_all(f"wip(fix): repair not completed (see {report_path})")
            RUNNER.kill()
            sys.exit(1)
        commit_all("fix: regression fixed in the production code")

    # ── VERDICT LOOP: Python executes, the appropriate agent retries on red. ──
    # In the mixed case, the retries go to the FIXER (production): the tests, now
    # arbitrated, are authoritative. Assumed limit: if the residual red comes from a
    # badly-adapted evolution test, the loop will not converge — the failure documents it.
    success = False
    last_output = initial_output
    for round_idx in range(1, MAX_ATTEMPTS + 1):
        ok, last_output, timed_out = run_verify_resilient(verify_cmd)
        if timed_out:
            print(f"🛑 [INFRA TIMEOUT] The verification times out repeatedly: an infrastructure "
                  f"incident, not a verdict on the repair. The current state is "
                  f"committed; repair the environment then relaunch Guided-Fix.py.")
            commit_all(f"wip(fix): repair interrupted by an infra timeout (see {report_path})")
            RUNNER.kill()
            sys.exit(1)
        if ok:
            success = True
            break
        print(f"⚠️  [RED {round_idx}/{MAX_ATTEMPTS}] The suite does not pass yet. "
              f"Output forwarded to the agent.")
        if round_idx == MAX_ATTEMPTS:
            break
        retried = run_code_pass(last_output) if regression_groups else run_tests_retry_pass(last_output)
        if not retried:
            break
        commit_all(f"wip(fix): correction pass {round_idx + 1}")

    if not success:
        append_report(report_path, f"## Outcome\n- **FAILURE**: the suite remains RED after "
                                   f"{MAX_ATTEMPTS} repair attempt(s).\n"
                                   f"- Last output (truncated):\n\n```\n"
                                   f"{truncate_output(last_output, 2000)}\n```\n"
                                   f"- Leads: bump the model one notch then relaunch "
                                   f"Guided-Fix.py (new triage), or fix by hand "
                                   f"then relaunch (the observed green will set the {FIXED_STATUS} marker).")
        commit_all(f"wip(fix): repair not completed (see {report_path})")
        print_failure_message(report_path, last_output)
        RUNNER.kill()
        sys.exit(1)

    # ── SUCCESS: bookkeeping, handshake, commit, HUMAN handoff. ──
    record_test_count(last_output, blackboard)
    update_protected_test_files(blackboard, pre_wip_sha, broken_phase)
    result_lines = ["## Outcome", "- Full suite **GREEN** after repair."]
    if broken_phase:
        mark_phase_fixed(broken_phase, blackboard)
        result_lines.append(f"- Phase {broken_phase.get('id', '?')} "
                            f"\"{broken_phase.get('name', '(unnamed)')}\" marked **{FIXED_STATUS}**: "
                            f"MAIsterMind will revalidate it by execution on relaunch.")
    result_lines.append("- Next step: relaunch `python3 Safe-Coding.py` (MANUAL relaunch: "
                        "the resume is interactive anyway).")
    append_report(report_path, "\n".join(result_lines))
    summary = []
    if regression_groups:
        summary.append(f"{len(regression_groups)} regression(s) fixed")
    if evolution_groups:
        summary.append(f"{len(evolution_groups)} evolution(s) endorsed")
    commit_all(f"fix(phase {broken_phase.get('id', '?') if broken_phase else '-'}): "
               + ", ".join(summary))

    for tmp_f in [TMP_FIX_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_fix_sentinels()
    RUNNER.kill()
    print(f"""
{'=' * 62}
🏁 Repair complete: the full suite is GREEN ({', '.join(summary)}).
   📄 Audit trail: '{report_path}' (committed with the repair).""")
    if broken_phase:
        print(f"""   🔁 Phase {broken_phase.get('id', '?')} \"{broken_phase.get('name', '(unnamed)')}\" marked {FIXED_STATUS}:
      relaunch 'python3 Safe-Coding.py' — it revalidates by execution (without
      re-paying a coder) then continues the run at the next phase.""")
    else:
        print("""   🔁 No phase to mark (halt outside production): relaunch 'python3 Safe-Coding.py'
      if you want to replay the final polish, or ship as is.""")
    print(f"{'=' * 62}")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    BLACKBOARD_FILE=BLACKBOARD_FILE,
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
