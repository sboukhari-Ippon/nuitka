#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - DESIGN AUDIT factory with an agent harness + tmux (Nielsen rubric)
─────────────────────────────────────────────────────────────────────────────
"AUDIT" VARIANT: it writes NO code — it evaluates an EXISTING web interface
(prototype from Design-Prototype.py, front-end produced by Safe-Coding.py, or any
other web project) against Nielsen's 10 usability heuristics, and delivers a
consolidated report 'design_audit_report.md' (severities 0 to 4, locations,
actionable recommendations).

This is the direct application of the MAIsterMind logic — slice the context window
per phase to make small or medium models reliable over the long run — to an AUDIT
task: asking for "all 10 heuristics at once" saturates the context and produces
superficial findings; here EACH heuristic is a dedicated phase, run in a fresh
session (/new), which receives ONLY its slice of the rubric (common trunk + ITS section)
and writes ONLY its findings file. A final synthesis phase consolidates the ten
files into a single report sorted by severity.

Pipeline:
  - Step 0: scope discovery (the project's UI files) by PYTHON — deterministic,
    zero LLM — then human confirmation (y/n) BEFORE paying for 11 agent turns.
  - Step 1: 10 audit phases, one per heuristic. Each auditor writes its findings
    in 'audit_nielsen/Hxx_<slug>.md' then signals its completion via a sentinel. No
    executable verdict (an audit has neither build nor test): liveness net (3 attempts) +
    STRUCTURAL floor on the findings file (mandatory sections), like the proto.
  - Step 2: synthesis. An agent consolidates the 10 findings files into
    'design_audit_report.md' — it re-audits nothing, it copies and orders (same family
    of contract as the blackboard compiler: zero inference asked of the small model).

Resume by files, like the other variants: a findings file present and structurally
valid skips its phase; the synthesis is ALWAYS replayed at the end of the run
(it must reflect the up-to-date findings). To redo a full audit: delete
'audit_nielsen/' and re-run.

READ-ONLY guard (best-effort, if the project is already a git repo): an audit does not
modify the audited project. Any tracked file modified by an auditor is restored
(git checkout) and reported; any file created outside the audit deliverables is reported
(never deleted: decision left to the human). Without git, the ban stays carried
by the prompts (graceful degradation, as everywhere else in the factory).
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil

from mm_runner import resolve_runner, resolve_timeout

# Run journal (black box .mm-runs/, plan-big-last Lot 2): purely additive,
# full no-op if MM_AUDIT=0, NEVER makes a run fail.
import mm_audit

# Shared functions extracted at Lot 4a (plan-big-last): see mm_core.py.
# The configuration (THIS module's constants/objects) is injected at the end
# of the file via mm_core.configure(...) — all names are defined by then.
import mm_core
from mm_core import (
    is_ui_file, signal_handler,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="audit", messages={
    "follow": "   👀 Follow the audit live in another terminal: tmux attach -t {session}",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
AUDIT_DIR             = "audit_nielsen"          # intermediate findings (one file per heuristic)
AUDIT_REPORT_FILE     = "design_audit_report.md" # final consolidated deliverable
FAIL_REPORT_FILE      = "failReport.md"          # persistent stop report (same contract as the factory)
AUDIT_SKILL_FILE      = "./.agents/pipeline/audit-nielsen/SKILL.md"
AGENT_CONFIG_FILE     = RUNNER.config_file

# Temporary context routing file (offloaded prompt, named by the harness)
TMP_AUDIT_FILE        = RUNNER.tmp_file("audit")

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: the
# only valid choice on all 3 OSes (Windows has no /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session. Prefix DISTINCT from the
# other variants (oc-factory / oc-proto): an audit can coexist on the machine
# with a production run on ANOTHER project without any risk of session collision.
TMUX_SESSION          = RUNNER.session

MAX_ATTEMPTS          = 3              # Attempts per pass (liveness net + structural floor)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per audit pass (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less net: deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds)

# Beyond this size, the list of scope files is truncated in the prompt
# (the auditor's context window): the remaining files are counted, not listed.
MAX_SCOPE_FILES_IN_PROMPT = 150

# ─── NIELSEN'S 10 HEURISTICS (id, file slug, title) ───────────
# FIXED and deterministic list: the audit needs neither a PO, nor an Architect, nor a
# blackboard — the split into phases is known in advance, Python drives it alone.
# The titles must match the '### H<n>' sections of the rubric
# (AUDIT_SKILL_FILE): the rubric carries the content, here we only carry the plan.
NIELSEN_HEURISTICS = [
    (1,  "visibilite-etat-systeme",   "Visibility of system status"),
    (2,  "correspondance-monde-reel", "Match between the system and the real world"),
    (3,  "controle-liberte",          "User control and freedom"),
    (4,  "coherence-standards",       "Consistency and standards"),
    (5,  "prevention-erreurs",        "Error prevention"),
    (6,  "reconnaissance-rappel",     "Recognition rather than recall"),
    (7,  "flexibilite-efficacite",    "Flexibility and efficiency of use"),
    (8,  "esthetique-minimalisme",    "Aesthetic and minimalist design"),
    (9,  "recuperation-erreurs",      "Help users recognize, diagnose, and recover from errors"),
    (10, "aide-documentation",        "Help and documentation"),
]


# ─── SENTINELS (AUDITOR → ORCHESTRATOR CHANNEL) ─────────────────────────────
# The '.audit_' prefix is DISTINCT from the '.phase_' / '.pipeline_' of the other variants: a
# residue from an old production run cannot be taken for an audit signal,
# and vice versa.

def audit_sentinel(slot: str, attempt: int) -> str:
    """File written by the auditor at the very end of a pass (signal 'I'm done').

    'slot' identifies the pass ('h1'…'h10', 'synthese'). The attempt number is included
    in the name: a sentinel written late by the agent of a previous attempt
    cannot be taken for the current attempt's signal.
    """
    return f".audit_{slot}.attempt{attempt}.done"


def cleanup_slot_sentinels(slot: str):
    """Remove all sentinels (every attempt) of a pass."""
    prefix = f".audit_{slot}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_audit_sentinels():
    """Final cleanup of all residual audit sentinels."""
    for name in os.listdir("."):
        if name.startswith(".audit_") and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


# ─── FILE MONITOR SYNCHRONIZATION ─────────────────────────────────────────────

def wait_for_deliverable(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> bool:
    """Wait for an audit deliverable signaled by a SENTINEL (same contract as the pipeline
    of the other variants: the agent creates the .done file AFTER saving the deliverable).

    SAFETY NET for an agent that forgets the sentinel: if the deliverable exists, is non-empty
    and has not changed for STABLE_POLLS_FALLBACK consecutive checks, it is accepted with a
    warning (graceful degradation). The optional 'structural_check' hardens ONLY
    this net: a stable but structurally incomplete deliverable keeps waiting
    (the agent may still be writing) until the global timeout.
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
                        print(f"   ⏳ '{filepath}' is stable but structurally incomplete: "
                              f"still waiting (the agent may still be writing).")
                        structural_warned = True
                    continue
                print(f"   ⚠️  Sentinel '{sentinel}' missing but '{filepath}' has been stable for "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s: deliverable accepted (safety net).")
                return True
    return False


def findings_structural_check(path: str) -> bool:
    """Minimal structural floor of a findings file: its mandatory sections
    '## Findings' and '## Summary' must be present (a half-written file — or
    off-format chatter — stops before them)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return "## findings" in content and "## summary" in content
    except OSError:
        return False


def report_structural_check(path: str) -> bool:
    """Minimal structural floor of the final report: its mandatory
    "Executive summary" section must be present."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "executive summary" in f.read().lower()
    except OSError:
        return False


def findings_path(h_id: int, slug: str) -> str:
    """Path of a heuristic's findings file (zero-padded for eyeball sorting)."""
    return f"{AUDIT_DIR}/H{h_id:02d}_{slug}.md"


def findings_ok(path: str) -> bool:
    """Is a findings file usable (present, non-empty, structurally valid)?
    Used for resume (skipped phase), progress display and the failure report."""
    return os.path.exists(path) and os.path.getsize(path) > 0 and findings_structural_check(path)


# ─── AUDIT RUBRIC: LOADING AND SLICING PER HEURISTIC ───────────────────
# Core of the MAIsterMind logic applied to the audit: each pass receives only the
# COMMON TRUNK of the rubric (role, iron rules, severity scale, output format)
# plus ITS '### H<n>' section — never the other 9. Same family as extract_spec_slice
# in the production variants.

# Header of a heuristic section in the rubric (e.g. "### H4: Consistency and standards").
H_HEADING_RE = re.compile(r"^###\s+H(\d+)\b")


def load_audit_grid() -> str:
    """Load the audit rubric (SKILL.md). Its absence is an IMMEDIATE failure: without
    a rubric, the auditors would improvise — exactly what the factory forbids."""
    if not os.path.exists(AUDIT_SKILL_FILE):
        return ""
    with open(AUDIT_SKILL_FILE, "r", encoding="utf-8") as f:
        return f.read()


def collect_grid_h_ids(grid_text: str) -> set:
    """Ids (int) of the '### H<n>' sections present in the rubric."""
    ids = set()
    for line in grid_text.splitlines():
        match = H_HEADING_RE.match(line.strip())
        if match:
            ids.add(int(match.group(1)))
    return ids


def extract_heuristic_slice(grid_text: str, h_id: int) -> str:
    """Slice of the rubric limited to the common trunk + THE pass's heuristic.

    Small-model prudence: if the rubric does not follow the H-section format, or if the
    requested heuristic is not in it (hand-edited rubric), return the WHOLE rubric
    (graceful degradation — never starve the auditor of its definition out of slicing zeal).
    """
    grid_ids = collect_grid_h_ids(grid_text)
    if not grid_ids or h_id not in grid_ids:
        return grid_text
    kept = []
    current_h = None  # id of the current H section, None = common trunk
    for line in grid_text.splitlines():
        match = H_HEADING_RE.match(line.strip())
        if match:
            current_h = int(match.group(1))
        elif current_h is not None and line.startswith("## "):
            current_h = None  # end of the heuristics zone: back to the common trunk
        if current_h is None or current_h == h_id:
            kept.append(line)
    return "\n".join(kept)


# ─── SCOPE DISCOVERY (PYTHON, DETERMINISTIC, ZERO LLM) ─────────────────
# The scope is established by the orchestrator, never by an agent: a stable,
# reproducible list, shown to the human BEFORE paying for a single LLM turn.

UI_EXTENSIONS = {".html", ".htm", ".css", ".scss", ".sass", ".less",
                 ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                 ".vue", ".svelte", ".astro", ".ejs", ".hbs", ".njk", ".twig"}

# Directories excluded by NAME; any hidden directory ('.git', '.agents', '.opencode'/'.codex',
# '.venv', '.next'…) is excluded by default by the walk's startswith('.') filter.
EXCLUDED_DIR_NAMES = {"node_modules", "dist", "build", "out", "coverage", "target",
                      "vendor", "__pycache__", AUDIT_DIR}


def is_test_file(path: str) -> bool:
    """Best-effort naming heuristic: does 'path' look like a test file?

    Tests are not an interface exposed to the user: they fall outside the audit scope
    (noise). Same multi-language conventions as the production variants.
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


def discover_ui_scope() -> list:
    """Sorted list (relative paths, '/' separator) of the UI files to audit."""
    scope = []
    for root, dirs, files in os.walk(".", topdown=True):
        dirs[:] = sorted(d for d in dirs
                         if d not in EXCLUDED_DIR_NAMES and not d.startswith("."))
        for name in files:
            if not is_ui_file(name):
                continue
            rel = os.path.normpath(os.path.join(root, name)).replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            if is_test_file(rel):
                continue
            scope.append(rel)
    return sorted(scope)


def business_context_file() -> str:
    """Available business-context file ('spec.md' preferred, else 'need.md'),
    or empty string. The audit does NOT need it to run: it is an optional plus."""
    for candidate in (SPEC_FILE, NEED_FILE):
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


def business_context_hint() -> str:
    """OPTIONAL pointer to the business context: we never inline the spec into the
    audit prompt (context window), we only indicate where to find it."""
    context = business_context_file()
    if context:
        return (f"The file '{context}' (business context) exists at the root: consult it "
                f"ONLY if a user flow is incomprehensible without it (save your context).")
    return "(no business-context file detected: audit the interface as it presents itself)"


# ─── READ-ONLY GUARD (GIT, BEST-EFFORT) ───────────────────────────────────────
# "Python verifies what is verifiable": the ban on modifying the audited project
# is carried by the prompts (unverifiable alone) AND by this mechanical diff when a git
# repo pre-exists. Unlike the production variants, we NEVER do a
# 'git init' nor a commit: an audit must leave NO trace in the audited project
# beyond its deliverables ('audit_nielsen/', report).

_GIT = {"enabled": False, "baseline_untracked": set(), "baseline_dirty": set()}

# Identity passed per command: the factory must not depend on the machine's git config.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]


def run_git(args: list, timeout: int = 60) -> tuple:
    """Run a git command. Returns (ok, stdout stripped). Never raises."""
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


# Deliverables and artifacts of the AUDIT itself: the only files the auditor is
# allowed to produce — never restored nor reported by the read-only guard. Unlike
# production, NO '.gitignore' here: the audit never writes that file; an
# auditor touching it must be restored like any other project file.
_AUDIT_BASENAMES = {AUDIT_REPORT_FILE, FAIL_REPORT_FILE, TMP_AUDIT_FILE,
                    TMP_PROMPT_BUFFER, os.path.basename(__file__)}


def is_audit_artifact(path: str) -> bool:
    """Is 'path' an audit deliverable/artifact (and not a file of the audited project)?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _AUDIT_BASENAMES:
        return True
    if segments[0] == AUDIT_DIR:
        return True
    # Ephemeral sentinels and buffers, wherever they sit in the tree.
    if base.startswith(".audit_") and base.endswith(".done"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Python caches, virtual environment and tooling directories: outside the audited project.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def init_readonly_guard():
    """Enable the read-only guard if (and only if) the project is ALREADY a git repo.

    TWO baselines are captured now, BEFORE the first agent:
      - the pre-existing untracked files: without this baseline, the user's untracked
        files would be reported on every pass as "created by the auditor"
        (permanent false positive);
      - the ALREADY-MODIFIED tracked files (dirty worktree): without this baseline, the
        'git checkout' restore would DESTROY uncommitted human work predating the
        audit — unacceptable. These files leave the guard for the whole run
        (accepted trade-off: an auditor edit to an already-dirty file is not
        restored; never destroying human work outweighs the guard).
    """
    if shutil.which("git") is None or not os.path.isdir(".git"):
        print("ℹ️  No pre-existing git repo: the mechanical read-only guard is inactive "
              "(the ban on modifying the project stays carried by the prompts).")
        return
    _GIT["enabled"] = True
    ok, out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok:
        _GIT["baseline_untracked"] = {line.strip() for line in out.splitlines() if line.strip()}
    ok_dirty, dirty_out = run_git(["diff", "--name-only", "HEAD"])
    if ok_dirty:
        _GIT["baseline_dirty"] = {line.strip() for line in dirty_out.splitlines() if line.strip()}
    print("✓ Git repo detected: read-only guard active (any tracked file modified by "
          "an auditor will be restored).")
    dirty_project = sorted(f for f in _GIT["baseline_dirty"] if not is_audit_artifact(f))
    if dirty_project:
        print(f"   ⚠️  {len(dirty_project)} file(s) already modified BEFORE the audit (work in "
              f"progress?): they are excluded from the guard (never restored automatically) — "
              f"{', '.join(dirty_project[:10])}{'…' if len(dirty_project) > 10 else ''}")


def enforce_readonly(label: str):
    """Restore the TRACKED files modified during a pass and report the files created
    outside the audit deliverables (best-effort, after EACH pass).

    Automatic restore for modifications (an audit does not fix); mere REPORTING for
    creations (we never delete a file we did not create: decision left to the human,
    as for protected_test_files in the factory).
    """
    if not _GIT["enabled"]:
        return
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    # 'baseline_dirty' excluded from the restore: a file already modified BEFORE the audit
    # carries uncommitted human work — restoring it would DESTROY it (cf. init).
    touched = sorted(f for f in diff_out.splitlines()
                     if f.strip() and not is_audit_artifact(f.strip())
                     and f.strip() not in _GIT["baseline_dirty"]) if ok_diff else []
    if touched:
        run_git(["checkout", "--"] + touched)
        print(f"🛡️  [{label}] AUDIT = READ-ONLY: {len(touched)} project file(s) "
              f"modified by the auditor — restored: {', '.join(touched)}")
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        strays = sorted(
            f for f in ({line.strip() for line in others_out.splitlines() if line.strip()}
                        - _GIT["baseline_untracked"])
            if not is_audit_artifact(f))
        if strays:
            print(f"⚠️  [{label}] File(s) created outside the audit deliverables (not deleted, "
                  f"to inspect): {', '.join(strays)}")


# ─── FAILURE REPORT & FAILURE MESSAGE ────────────────────────────────────────


def audited_count() -> int:
    """Number of heuristics whose findings file is already usable."""
    return sum(1 for (h_id, slug, _t) in NIELSEN_HEURISTICS if findings_ok(findings_path(h_id, slug)))


def write_fail_report(title: str, reason: str, details: str = ""):
    """Write a persistent stop report at the root (same contract as the factory:
    any NON-nominal stop produces one). Best-effort: NEVER raises."""
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — MAIsterMind (design audit)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "", "### Progress",
                 f"- Heuristics audited: {audited_count()}/{len(NIELSEN_HEURISTICS)}"]
        for h_id, slug, h_title in NIELSEN_HEURISTICS:
            mark = "✅" if findings_ok(findings_path(h_id, slug)) else "⏳"
            lines.append(f"  - {mark} H{h_id}: {h_title}")
        lines.append("")
        if details.strip():
            lines.append("### Details")
            lines.append(details.strip()[:4000])
            lines.append("")
        lines.append("### Recommended action")
        lines.append("Fix the cause above (or bump the model up one tier via /model or "
                     f"'{AGENT_CONFIG_FILE}'), then re-run: the already-audited heuristics "
                     "will be resumed automatically.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


def fail_audit(message: str, details: str = ""):
    """Single exit point for failures. Always kills the tmux session BEFORE exiting:
    an exit that leaves the agent alive lets it finish writing its deliverable AFTER
    the orchestrator gave up (misleading resume state on relaunch)."""
    print(message)
    write_fail_report("Audit pass failure", message, details)
    RUNNER.kill()
    sys.exit(1)


def print_pass_failure(label: str, reason: str):
    model = RUNNER.configured_model()
    print(f"""
{'='*60}
❌ The pass "{label}" did not succeed after {MAX_ATTEMPTS} attempts.

   Cause: {reason}

💡 The current model ({model}) stalls on this pass (often a tool-calling
   problem: the findings file or the sentinel are never created, or the
   requested format is not respected).
   Most effective: re-run after bringing in a model one tier above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the {audited_count()} already-audited heuristic(s) will be resumed
   automatically, you are not starting from scratch. See you shortly! 🚀
{'='*60}
""")


# ─── OFFLOADED PROMPTS BY FILE ─────────────────────────────────────────────

def build_scope_block(scope_files: list) -> str:
    """'scope' block of the prompts: bounded list (the auditor's context window)."""
    listed = scope_files[:MAX_SCOPE_FILES_IN_PROMPT]
    block = "\n".join(f"- {f}" for f in listed)
    overflow = len(scope_files) - len(listed)
    if overflow > 0:
        block += (f"\n(+ {overflow} other file(s) not listed: focus on the "
                  f"main screens and flows above.)")
    return block


def build_auditor_prompt(h_id: int, title: str, skill_slice: str, scope_files: list,
                         findings_file: str, feedback: str, attempt: int) -> str:
    sentinel = audit_sentinel(f"h{h_id}", attempt)
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are an ultra-specialized UX Auditor Agent, assigned to A SINGLE Nielsen heuristic:
H{h_id} "{title}". This is pass {h_id}/10 of a sliced heuristic evaluation.
AUDIT = READ-ONLY: you do not modify, fix, or create ANY project file.
You write ONLY two files: your findings file, then your completion sentinel.
Ignore any problem belonging to ANOTHER heuristic than yours: a dedicated pass
handles it (reporting it here would create duplicates in the report).

--- AUDIT RUBRIC (common trunk + YOUR heuristic) ---
{skill_slice}

--- SCOPE TO AUDIT ({len(scope_files)} UI file(s), discovered by the orchestrator) ---
{build_scope_block(scope_files)}
Proceed screen by screen (the .html files first, then the styles and scripts they reference);
do not load the whole scope at once.

--- BUSINESS CONTEXT (optional) ---
{business_context_hint()}

--- ORCHESTRATOR FEEDBACK TO ADDRESS (if any) ---
{feedback}

--- MANDATORY DELIVERABLE ---
Write your findings in '{findings_file}' (create the '{AUDIT_DIR}/' folder if needed),
STRICTLY following the rubric format above: sections '## Findings' and
'## Summary' mandatory; explicit "No findings." if the heuristic is respected.
Do it directly via your file-editing tools, without needless chatter in the console.

--- MANDATORY COMPLETION INSTRUCTION ---
As your very LAST action, after saving '{findings_file}', create the sentinel
file '{sentinel}' at the root (content: the single word done): it is the completion signal
for the orchestrator. Only create it when the findings file is TRULY finished.
"""
    with open(TMP_AUDIT_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_AUDIT_FILE}' at the project root and perform "
            f"audit pass H{h_id}.")


def build_synthesis_prompt(attempt: int) -> str:
    sentinel = audit_sentinel("synthese", attempt)
    findings_list = "\n".join(f"- {findings_path(h_id, slug)} (H{h_id}: {title})"
                              for h_id, slug, title in NIELSEN_HEURISTICS)
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are a Lead Product Designer tasked with CONSOLIDATING a Nielsen heuristic evaluation
carried out in ten independent passes. You re-audit NOTHING and you do NOT re-read the
project code: you synthesize the existing findings, that's all. ZERO invention: the report
contains ONLY findings present in the files listed below — you may rephrase
for readability, never add, remove or requalify a severity.
You modify no project file; you write ONLY the final report, then your sentinel.

--- FINDINGS FILES TO CONSOLIDATE (one per heuristic, read them ALL) ---
{findings_list}

--- REPORT TO PRODUCE: '{AUDIT_REPORT_FILE}' ---
MANDATORY structure:

# Design audit — Nielsen rubric

## 1. Executive summary
[3 to 6 sentences: general state of the interface, total count of findings by severity
(rely on the '## Summary' lines of the findings files), the 2 or 3 priority
work items.]

## 2. View by heuristic
[Markdown table: Heuristic | Title | Findings count | Max severity.]

## 3. Major and blocking issues
[All severity-4 findings, then severity-3 — each with title, source heuristic,
location, user impact and recommendation, taken from the findings files.]

## 4. Quick wins
[The severity-1 or 2 findings whose recommendation is cheap to apply.]

## 5. Detail by heuristic
[For each heuristic H1 → H10: its findings taken as is, or "No findings.".]

--- MANDATORY COMPLETION INSTRUCTION ---
As your very LAST action, after saving '{AUDIT_REPORT_FILE}', create the sentinel
file '{sentinel}' at the root (content: the single word done): it is the completion signal
for the orchestrator.
"""
    with open(TMP_AUDIT_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_AUDIT_FILE}' at the project root and consolidate "
            f"the audit into a final report.")


# ─── AUDIT LOOP (ONE PASS PER HEURISTIC) ───────────────────────────────

def run_audit_passes(grid_text: str, scope_files: list):
    total = len(NIELSEN_HEURISTICS)

    for h_id, slug, title in NIELSEN_HEURISTICS:
        findings_file = findings_path(h_id, slug)

        # Resume by files: a usable findings file skips its pass.
        if findings_ok(findings_file):
            print(f"⏭️  Pass H{h_id}/{total} already audited ('{findings_file}'): skipped.")
            continue
        if os.path.exists(findings_file):
            # Half-written residue of an interrupted run: start cleanly.
            try:
                os.remove(findings_file)
                print(f"🧹 Residual '{findings_file}' (incomplete) removed: the pass is replayed.")
            except OSError:
                pass

        print(f"\n{'='*50}\n🔎 PASS H{h_id}/{total}: {title}\n{'='*50}")

        # Context window: the auditor receives only the common trunk of the rubric
        # plus ITS section — never the other 9 heuristics.
        skill_slice = extract_heuristic_slice(grid_text, h_id)
        if len(skill_slice) < len(grid_text):
            print(f"   ✂️  Rubric sliced for the pass: {len(skill_slice)}/{len(grid_text)} characters.")

        attempts = 0
        success = False
        feedback = "First pass — no previous feedback."

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1

            # Catch a LATE deliverable: the agent of the previous attempt may have
            # finished writing AFTER the orchestrator's timeout. If its file has
            # become usable in the meantime, take it as is rather than paying
            # an agent turn to redo everything.
            if attempts > 1 and findings_ok(findings_file):
                print(f"   ♻️  '{findings_file}' finally arrived (late deliverable): accepted.")
                success = True
                break

            cleanup_slot_sentinels(f"h{h_id}")
            print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Pass H{h_id} — launching the UX Auditor...")

            prompt = build_auditor_prompt(h_id, title, skill_slice, scope_files,
                                          findings_file, feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(prompt))
            RUNNER.send_task(prompt)

            got_deliverable = wait_for_deliverable(findings_file,
                                                   audit_sentinel(f"h{h_id}", attempts),
                                                   structural_check=findings_structural_check)
            # Read-only guard after EACH attempt (successful or not): an auditor that
            # "fixed" code along the way is restored immediately.
            enforce_readonly(f"H{h_id}")

            if not got_deliverable:
                feedback = ("On the previous pass, no deliverable was received (findings "
                            "file absent, empty or never signaled). First write the complete "
                            "findings file, THEN the sentinel, in that order.")
                print(f"⏱️  The auditor did not signal the end of pass H{h_id}. Retrying.")
                RUNNER.new_context()
                continue

            # Structural floor AFTER the fact, even when the sentinel arrived: the
            # sentinel path of wait_for_deliverable does not check the structure, and an
            # off-format file could not be consolidated by the synthesis.
            if not findings_structural_check(findings_file):
                feedback = (f"Your file '{findings_file}' does not follow the requested format: "
                            f"the sections '## Findings' (with findings in the rubric "
                            f"format, or the single line \"No findings.\") and '## Summary' are "
                            f"MANDATORY. Rewrite it entirely in the correct format.")
                try:
                    os.remove(findings_file)
                except OSError:
                    pass
                print(f"⚠️  [REJECTED] Attempt {attempts}: findings file off-format "
                      f"(mandatory sections missing).")
                RUNNER.new_context()
                continue

            success = True

        if not success:
            reason = feedback
            cleanup_all_audit_sentinels()
            print_pass_failure(f"H{h_id}: {title}", reason)
            fail_audit(f"❌ Pass H{h_id} did not succeed after {MAX_ATTEMPTS} attempts.", details=reason)

        print(f"✅ Pass H{h_id} completed: findings in '{findings_file}'.")
        cleanup_slot_sentinels(f"h{h_id}")
        RUNNER.new_context()


# ─── FINAL SYNTHESIS ──────────────────────────────────────────────────────────

def run_synthesis():
    print(f"\n{'='*50}\n🧾 SYNTHESIS: CONSOLIDATING THE 10 PASSES INTO ONE REPORT\n{'='*50}")

    # The synthesis is ALWAYS replayed (it must reflect the up-to-date findings): a
    # residual report — from this run or a previous one — is purged so the wait
    # below observes only THIS pass's report.
    if os.path.exists(AUDIT_REPORT_FILE):
        try:
            os.remove(AUDIT_REPORT_FILE)
            print(f"   🧹 Residual '{AUDIT_REPORT_FILE}' removed (the synthesis is regenerated).")
        except OSError:
            pass

    attempts = 0
    success = False
    while not success and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Catch a LATE deliverable (same logic as the audit passes): a
        # report that became valid after the previous attempt's timeout is accepted.
        if attempts > 1 and os.path.exists(AUDIT_REPORT_FILE) \
                and os.path.getsize(AUDIT_REPORT_FILE) > 0 \
                and report_structural_check(AUDIT_REPORT_FILE):
            print(f"   ♻️  '{AUDIT_REPORT_FILE}' finally arrived (late deliverable): accepted.")
            success = True
            break

        cleanup_slot_sentinels("synthese")
        print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Launching the synthesis agent...")

        prompt = build_synthesis_prompt(attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable = wait_for_deliverable(AUDIT_REPORT_FILE,
                                               audit_sentinel("synthese", attempts),
                                               structural_check=report_structural_check)
        enforce_readonly("Synthesis")

        if not got_deliverable or not report_structural_check(AUDIT_REPORT_FILE):
            if os.path.exists(AUDIT_REPORT_FILE) and not report_structural_check(AUDIT_REPORT_FILE):
                try:
                    os.remove(AUDIT_REPORT_FILE)
                except OSError:
                    pass
            print("⏱️  Synthesis absent or off-format. Retrying.")
            RUNNER.new_context()
            continue
        success = True

    if not success:
        # Failure of the CONSOLIDATION only: the raw findings stay usable as
        # is — we say so explicitly so the run does not look lost.
        cleanup_all_audit_sentinels()
        print_pass_failure("Synthesis", "the consolidated report was never produced in the correct format.")
        fail_audit(f"❌ Synthesis did not succeed after {MAX_ATTEMPTS} attempts. The raw findings "
                   f"stay usable in '{AUDIT_DIR}/'.")

    print(f"✅ Consolidated audit report: '{AUDIT_REPORT_FILE}'.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "audit-design", RUNNER.name,
                   model=RUNNER.configured_model())
    # A residual failReport.md from a previous run must not be taken for the current
    # run's: we purge it at startup (same contract as the factory).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # The rubric is the reference for the WHOLE audit: its absence is an immediate failure
    # (without it, the auditors would improvise — exactly what the factory forbids).
    grid_text = load_audit_grid()
    if not grid_text.strip():
        print(f"❌ Audit rubric missing or empty: '{AUDIT_SKILL_FILE}'.")
        write_fail_report("Audit rubric missing",
                          f"'{AUDIT_SKILL_FILE}' is not found or empty: impossible to audit without a reference.")
        sys.exit(1)
    grid_ids = collect_grid_h_ids(grid_text)
    missing = [str(h_id) for h_id, _s, _t in NIELSEN_HEURISTICS if h_id not in grid_ids]
    if missing:
        # Warn-only: the slicing falls back to the whole rubric for these passes
        # (graceful degradation of extract_heuristic_slice).
        print(f"⚠️  Missing sections in the rubric: H{', H'.join(missing)} — these passes "
              f"will receive the WHOLE rubric instead of their slice.")

    # Step 0: scope discovered by PYTHON (deterministic), shown to the human BEFORE
    # paying for a single agent turn.
    scope_files = discover_ui_scope()
    if not scope_files:
        print("❌ No interface file found in this directory (extensions searched: "
              + ", ".join(sorted(UI_EXTENSIONS)) + ").")
        print("   → Run the audit from the root of the project that contains the interface to evaluate.")
        write_fail_report("Empty audit scope",
                          "No interface file detected in the current directory.")
        sys.exit(1)

    already = audited_count()
    preview = scope_files[:20]

    print(f"\n{'='*50}")
    print(f"🔎 NIELSEN AUDIT — Discovered scope:")
    print(f"   Directory: {os.getcwd()}")
    print(f"   {len(scope_files)} UI file(s) to audit. Preview:")
    for f in preview:
        print(f"      - {f}")
    if len(scope_files) > len(preview):
        print(f"      … and {len(scope_files) - len(preview)} more.")
    context = business_context_file()
    if context:
        print(f"   Business context: '{context}' detected (pointed to the auditors as optional reading).")
    else:
        print(f"   Business context: none ('{SPEC_FILE}'/'{NEED_FILE}' absent) — the interface is "
              f"audited as it presents itself.")
    if already:
        print(f"   Resume: {already}/{len(NIELSEN_HEURISTICS)} heuristic(s) already audited "
              f"(findings present in '{AUDIT_DIR}/').")
    print(f"   Flow: {len(NIELSEN_HEURISTICS)} audit passes (one per heuristic, context "
          f"reset between each) + 1 synthesis → '{AUDIT_REPORT_FILE}'.")
    print(f"{'='*50}")

    confirm = input("\n▶️  Run the audit on this scope? (y/n): ")
    mm_audit.event("gate", id="scope", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print("⏹️  Cancelled by the user.")
        sys.exit(0)

    # Read-only guard: baseline captured BEFORE the first agent.
    init_readonly_guard()

    # 🚀 Boot the harness Data Center in tmux
    RUNNER.start()

    # Step 1: the 10 audit passes (a fresh session per heuristic).
    run_audit_passes(grid_text, scope_files)

    # Step 2: consolidation into the final report.
    run_synthesis()

    # Final pass of the read-only guard: covers the window between the last enforce
    # of a pass and the end of the run (notably the synthesis's "late report accepted"
    # path, which exits without enforce).
    enforce_readonly("final")

    # Cleanup of temporary files and sentinels, then a clean shutdown.
    for tmp_f in [TMP_AUDIT_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_audit_sentinels()
    RUNNER.kill()
    # Successful run: no failure report must remain.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    print(f"""
🏁 [CONGRATULATIONS] Nielsen audit completed!
   📄 Consolidated report: '{AUDIT_REPORT_FILE}'
   🗂️  Detailed findings per heuristic: '{AUDIT_DIR}/'
   ♻️  To redo a FULL audit (after fixes, e.g.): delete '{AUDIT_DIR}/'
      then re-run — a kept findings file skips its pass (resume by files).""")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    journal_dir = mm_audit.run_dir()
    mm_audit.end("success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")


mm_core.configure(
    RUNNER=RUNNER,
    UI_EXTENSIONS=UI_EXTENSIONS,
)


if __name__ == "__main__":
    main()
