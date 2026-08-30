#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - BEHAVIORAL DOCUMENTATION factory with an agent harness + tmux
─────────────────────────────────────────────────────────────────────────────
« DOCUMENTATION » VARIANT: it writes NO code — it documents ALL the behavior of an
EXISTING project (features and possible acceptance tests, covered by existing tests or
proposed), and delivers a consolidated 'documentation.md' file at the ROOT of the
documented project, pleasant for a human to read.

This is the direct application of the MAIsterMind logic — slicing the context window by
phase to make small or medium models reliable over time — to a DOCUMENTATION job: asking
« document the whole project at once » saturates the context and produces shallow docs;
here each FUNCTIONAL ZONE is a dedicated phase, run in a fresh session (/new), which
receives ONLY its slice (rubric + ITS zone + ITS files) and writes ONLY its zone file.
The final assembly is MECHANICAL (Python): zero loss, zero paraphrase, whatever the volume.

Pipeline:
  - Step 0: SCOPE — code and test files discovered by PYTHON (deterministic, zero LLM),
    displayed then confirmed by the human (y/n) BEFORE paying for a single agent.
  - Step 1: MAPPING — unlike the audit (10 fixed heuristics), the split into phases is not
    known in advance: a mapper agent ASSIGNS the scope files to named and ordered
    functional zones ('doc_map.yaml', the equivalent of the blackboard). Double validation:
    Python schema (total coverage, « Miscellaneous » zone added mechanically when needed)
    then human (y/n, YAML editable before validating).
  - Step 2: DOCUMENTATION — N passes, one per zone, a fresh session each time. Each
    documenter writes 'doc_zones/Zxx_<slug>.md' (features + Covered/Proposed acceptance
    tests) then signals its end with a sentinel. No executable verdict, but three
    mechanical CONTENT GUARDS (Python, zero LLM) on top of the liveness net (3 attempts)
    and the STRUCTURAL floor: every path cited between backticks must EXIST (a
    hallucinated source = rejection with the exact discrepancy), every « Covered by »
    must rest on a real TEST file (otherwise the AT is « Proposed »), and the Summary
    counters must equal the REAL count of the file (features and ATs recounted by
    Python). A completeness signal (warn-only) lists the zone files never cited as a
    source. We deliberately gave up an LLM verifier per zone: the OBJECTIVE
    discrepancies are all catchable mechanically, and one more LLM opinion would be one
    more hallucination risk.
  - Step 3: OVERVIEW — an agent writes the reading lead-in (product, flows, guide) from the
    intents and summaries alone (never the whole zones). Non-blocking: after 3 failures, a
    mechanical Python fallback takes over.
  - Step 4: ASSEMBLY — deterministic Python: concatenation in the doc_map order, shifted
    headings, table of contents, zone map and coverage appendix generated mechanically →
    'documentation.md' at the root (atomic write).

File-based resume, like the other variants: a valid 'doc_map.yaml' skips the mapping; a
usable zone file skips its pass; the overview and the assembly are ALWAYS replayed.
FRESHNESS: a skipped zone whose files were modified AFTER its documentation was written
is flagged « stale » (mtime, best-effort, warn-only) — on the scope screen as well as at
skip time. To re-document a zone: delete its file in 'doc_zones/' and relaunch. To redo
everything: delete 'doc_zones/' and 'doc_map.yaml'.

READ-ONLY guard (best-effort, if the project is already a git repo): documenting does not
modify the documented project. Any tracked file modified by an agent is restored
(git checkout) and reported; any file created outside the deliverables is reported (never
deleted). Without git, the ban stays carried by the prompts (graceful degradation).

Protection of a manual doc: the generated 'documentation.md' carries an invisible HTML
marker; a pre-existing 'documentation.md' WITHOUT this marker (hand-written) is announced
explicitly on the confirmation screen BEFORE the y/n — we never silently destroy human work.
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil
import fnmatch
import unicodedata

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
    expand_dir_entry, residual_deliverable_warning, select_carto_sample,
    signal_handler, wait_should_continue,
)

# ─── AGENT HARNESS ────────────────────────────────────────────────────────────
# The whole tmux layer (TUI start-up, prompt pasting, fresh context, screen capture,
# kill) lives in 'mm_runner.py': one class per harness (OpenCode, Codex), chosen here
# at start-up from the project equipment or MM_AGENT_HARNESS. The rest of this script
# knows nothing about it — sentinels, gates, verdicts and prompts stay agnostic.
RUNNER = resolve_runner(os.getcwd(), role="doc", messages={
    "reuse":  None,
    "follow": "   👀 Follow the documentation live in another terminal: tmux attach -t {session}",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
DOC_DIR               = "doc_zones"                # intermediate findings (one file per zone)
DOC_MAP_FILE          = "doc_map.yaml"             # zone map (the equivalent of the blackboard)
DOC_FILE              = "documentation.md"         # final consolidated deliverable, at the ROOT
OVERVIEW_FILE         = f"{DOC_DIR}/_overview.md"  # reading lead-in (overview)
FAIL_REPORT_FILE      = "failReport.md"            # persistent stop report (same contract as the factory)
DOC_MAP_SKILL_FILE    = "./.agents/pipeline/doc-map/SKILL.md"
DOC_ZONE_SKILL_FILE   = "./.agents/pipeline/doc-zone/SKILL.md"
AGENT_CONFIG_FILE     = RUNNER.config_file

# Temporary context routing file (offloaded prompt, named by the harness)
TMP_DOC_FILE          = RUNNER.tmp_file("doc")

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: the
# only valid choice on all 3 OSes (Windows has no /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session. Prefix DISTINCT from the
# other variants (oc-factory / oc-proto / oc-audit): a documentation can coexist with
# a production or an audit on ANOTHER project without collision.
TMUX_SESSION          = RUNNER.session

# Invisible HTML marker of the generated deliverable: it is what distinguishes a factory
# doc (overwritable) from a hand-written doc (announced before the y/n, decision D6).
DOC_MARKER            = "<!-- generated by Documentation -->"
DOC_MARKER_LEGACY     = "<!-- generated by MAIsterMind_documentation -->"

MAX_ATTEMPTS          = 3              # Attempts per pass (liveness net + structural floor)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per pass (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less net: deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds)

# Context window bounds (same families as the audit's MAX_SCOPE_FILES_IN_PROMPT):
MAX_ZONE_FILES_IN_PROMPT  = 150   # beyond this, a zone's file list is truncated in the prompt
MAX_SCOPE_FILES_IN_CARTO  = 400   # beyond this, the scope surplus is summarized by directory
                                  # (the unlisted ones will land in the « Miscellaneous » zone via coverage,
                                  # unless the mapper assigns them PER DIRECTORY: map entry
                                  # ending with '/')
DIVERS_RETRY_THRESHOLD    = 100   # beyond N files in « Miscellaneous », the map is REPLAYED
                                  # (as long as attempts remain): a residual that contains
                                  # the bulk of the project is not a mapping
SOFT_MAX_FILES_PER_ZONE   = 25    # warn (non-blocking) beyond this: the zone pass may saturate
                                  # (harmonized with the doc-map rubric's « 25 files max per
                                  # zone » bound: the mapper is supposed to sub-split before)


# ─── SENTINELS (AGENT → ORCHESTRATOR CHANNEL) ─────────────────────────────────
# '.doc_' prefix DISTINCT from the '.phase_' / '.pipeline_' / '.audit_' of the other
# variants: a residue from an old run of another pipeline cannot be mistaken for a
# documentation signal, and vice versa.

def doc_sentinel(slot: str, attempt: int) -> str:
    """File written by the agent at the very end of a pass (signal 'I'm done').

    'slot' identifies the pass ('map', 'z1'…'zN', 'overview'). The attempt number is
    part of the name: a sentinel written late by the agent of a previous attempt cannot
    be mistaken for the current attempt's signal.
    """
    return f".doc_{slot}.attempt{attempt}.done"


def cleanup_slot_sentinels(slot: str):
    """Remove all sentinels (every attempt) of a pass."""
    prefix = f".doc_{slot}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_doc_sentinels():
    """Final cleanup of all residual documentation sentinels."""
    for name in os.listdir("."):
        if name.startswith(".doc_") and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


# ─── FILE MONITOR SYNCHRONIZATION ─────────────────────────────────────────────

def wait_for_deliverable(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> bool:
    """Wait for a deliverable signaled by a SENTINEL (same contract as the other variants'
    pipeline: the agent creates the .done AFTER saving the deliverable).

    SAFETY NET for an agent that forgets the sentinel: if the deliverable exists, is
    non-empty and has not changed for STABLE_POLLS_FALLBACK consecutive checks, it is
    accepted with a warning (graceful degradation). The optional 'structural_check'
    hardens this net ONLY: a stable but structurally incomplete deliverable keeps waiting
    (the agent may still be writing) until the global timeout.
    """
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}' (completion signal: '{sentinel}')...")
    stable_streak = 0
    last_size = -1
    structural_warned = False
    activity = {}   # wait_should_continue state: extension if the agent is still working,
                    # immediate stop if it is frozen on a permission request
    while wait_should_continue(start, timeout, activity):
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


# ─── STRUCTURAL FLOORS (SAME FAMILY AS THE AUDIT) ─────────────────────────────

def zone_structural_check(path: str) -> bool:
    """Minimal structural floor for a zone file: its mandatory sections '## Features' and
    '## Summary' must be present (a half-written file — or off-format chatter — stops
    before them)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return "## features" in content and "## summary" in content
    except OSError:
        return False


def map_structural_check(path: str) -> bool:
    """Minimal structural floor for the map: parsable YAML AND non-empty zones.
    Used as structural_check for wait_for_deliverable during the mapping."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return isinstance(data, dict) and isinstance(data.get("zones"), list) and bool(data["zones"])
    except (OSError, yaml.YAMLError):
        return False


def overview_structural_check(path: str) -> bool:
    """Minimal structural floor for the overview: it begins with its heading."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip().lower().startswith("## overview")
    except OSError:
        return False


def zone_ok(path: str) -> bool:
    """Is a zone file usable (present, non-empty, structurally valid)?
    Used for resume (skipped pass), progress display and the failure report."""
    return os.path.exists(path) and os.path.getsize(path) > 0 and zone_structural_check(path)


def slugify(name: str) -> str:
    """File slug derived by PYTHON (never by the model — one error source fewer):
    lowercase, transliterated accents, kebab-case."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "zone"


def zone_path(zone: dict) -> str:
    """Zone file path (zero-padded for at-a-glance sorting). The model NEVER provides this
    path: it is computed here from the map's id and name."""
    try:
        zid = int(zone.get("id"))
    except (TypeError, ValueError):
        zid = 0
    return f"{DOC_DIR}/Z{zid:02d}_{slugify(str(zone.get('name') or 'zone'))}.md"


# ─── ZONE CONTENT GUARDS (MECHANICAL, ZERO LLM) ───────────────────────────────
# The structural floor proves the file has the right SHAPE; these guards prove that
# everything VERIFIABLE in it does not lie: a cited path exists on disk, a « Covered »
# status rests on a real test file, the Summary counters equal the real count of the
# content. The rest (fidelity of the described behaviors, falsifiability of the ATs) is
# EDITORIAL: no mechanical verdict possible — and we deliberately gave up an LLM verifier
# here (one more opinion = one more hallucination risk): the human reads, with numbers
# and sources guaranteed exact.

CITED_TOKEN_RE       = re.compile(r"`([^`\n]+)`")
CITED_LINE_SUFFIX_RE = re.compile(r":L?\d+(?:-\d+)?$")
AT_STATUS_RE         = re.compile(r"^\s*-\s*\*\*AT\d+\s*[—–-]\s*(Covered|Proposed)", re.IGNORECASE)
COVERED_AT_RE        = re.compile(r"\*\*AT\d+\s*[—–-]\s*Covered by\s+`([^`\n]+)`", re.IGNORECASE)


def clean_cited(token: str) -> str:
    """Normalize a cited path (backslashes, ':line' suffix, './') to the scope format —
    the format the disk can confirm or deny."""
    return norm_rel(CITED_LINE_SUFFIX_RE.sub("", str(token).strip().replace("\\", "/")))


# Characters that make a token a PATTERN (glob, placeholder, arrow) rather than a path.
PATTERN_CHARS = "<>*?{}$|→"


def looks_like_path(token: str) -> bool:
    """Is a backticked token a FILE CITATION (and not a code identifier)? Deliberately
    strict: '/' or a known code extension required — `canActivate`, `npm test` or
    `--flag` never trigger the guard (better to miss an exotic citation than to reject
    a zone over an identifier)."""
    t = str(token).strip()
    if not t or " " in t or "(" in t or t.startswith("-"):
        return False
    if any(ch in t for ch in PATTERN_CHARS):
        # Glob, placeholder or arrow: a PATTERN, not a file citation
        # (`docs/*.md`, `epic/<KEY>`, `tick_*_agent_<TICKET>.json`, `epic/<KEY> → main`).
        return False
    t = clean_cited(t)
    if "/" in t:
        return True
    return os.path.splitext(t)[1].lower() in CODE_EXTENSIONS


def is_project_rooted(cited: str) -> bool:
    """A cited path with '/' is a project SOURCE only if its first segment is a real
    entry of the project root (`scripts/…`, `src/…`, `.claude/…`). Otherwise it is a
    runtime path or a git reference (`epic/<KEY>`, `origin/main`, `docs/` created by a
    script, `/report`): the documentation may talk about it, the invented-source guard
    is not concerned. Observed on 28/08: 8 false positives out of 11 in a zone of
    orchestration scripts, three attempts burnt."""
    if "/" not in cited:
        return True
    first = cited.split("/", 1)[0]
    return bool(first) and os.path.exists(first)


def suggest_zone_file(cited: str, zone_files) -> str | None:
    """A bare basename (`dispatch_plan.sh`) matching ONE single file of the zone: the
    exact path to send back to the documenter, so it gets fixed on the first try."""
    if "/" in cited or not zone_files:
        return None
    matches = sorted({norm_rel(f) for f in zone_files
                      if os.path.basename(norm_rel(f)) == cited})
    return matches[0] if len(matches) == 1 else None


def cited_paths(content: str) -> set:
    """Paths cited between backticks in a zone file (':line' suffix removed, fences
    ignored): the raw material of the source and completeness guards."""
    out = set()
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        for token in CITED_TOKEN_RE.findall(line):
            if looks_like_path(token):
                out.add(clean_cited(token))
    return out


def covered_test_citations(content: str) -> list:
    """Raw citations of the « AT<i> — Covered by `…` » lines (fences ignored)."""
    out = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if not in_fence:
            out.extend(COVERED_AT_RE.findall(line))
    return out


def count_zone_content(content: str) -> dict:
    """MECHANICAL counters of a zone file (features, ATs, covered, proposed), fences
    ignored: the truth the declared Summary must equal — and the one the assembly
    displays, whatever the Summary declares."""
    features = ats = covered = 0
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        if FEATURE_HEADING_RE.match(line.strip()):
            features += 1
            continue
        match = AT_STATUS_RE.match(line)
        if match:
            ats += 1
            if match.group(1).lower().startswith("covered"):
                covered += 1
    return {"features": features, "ats": ats, "covered": covered, "proposed": ats - covered}


def zone_content_issues(path: str, test_scope: set, zone_files=None) -> list:
    """VERIFIABLE discrepancies of a zone file (empty list = compliant). Each discrepancy
    is worded to be sent back AS IS to the documenter (exact feedback, never vague)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return [f"'{path}' is unreadable."]
    issues = []
    covered_raw = covered_test_citations(content)
    covered_norm = {clean_cited(raw) for raw in covered_raw}
    # 1. Hallucinated sources: every cited path must exist (the « Covered » citations
    #    are handled separately, with a more specific message).
    for cited in sorted(cited_paths(content) - covered_norm):
        if os.path.exists(cited) or not is_project_rooted(cited):
            continue
        exact = suggest_zone_file(cited, zone_files)
        if exact:
            issues.append(f"The cited path `{cited}` is a bare file name: cite the exact "
                          f"path from the project root, `{exact}`.")
        else:
            issues.append(f"The cited path `{cited}` does not exist in the project: cite "
                          f"ONLY files you actually read (exact path copied from your "
                          f"zone), or remove this source.")
    # 2. « Covered » without a real test: the deliverable's most precious status never
    #    rests on the model's word.
    for raw in covered_raw:
        p = clean_cited(raw)
        if not os.path.exists(p):
            issues.append(f"AT « Covered by `{raw}` »: this file does not exist — switch "
                          f"the AT to « Proposed » or cite the REAL test file that verifies "
                          f"this scenario.")
        elif p not in test_scope and not is_test_file(p):
            issues.append(f"AT « Covered by `{raw}` »: this path is not a TEST file of the "
                          f"project — « Covered » cites an existing test; otherwise the AT "
                          f"is « Proposed ».")
    # 3. Summary ≠ real content: the counters are recounted by Python — the exact
    #    discrepancy (with the right numbers) is sent back to the model.
    declared = parse_zone_bilan(content)
    mech = count_zone_content(content)
    if any(declared[key] is None for key in mech):
        issues.append(f"The '## Summary' does not respect the locked format. Write EXACTLY "
                      f"these two lines (real count of your file): "
                      f"« - Features : {mech['features']} » then « - Acceptance tests : "
                      f"{mech['ats']} (covered : {mech['covered']}, proposed : "
                      f"{mech['proposed']}) ».")
    elif any(declared[key] != mech[key] for key in mech):
        issues.append(f"The '## Summary' counters do not match the real content of the "
                      f"file ({mech['features']} feature(s), {mech['ats']} AT(s) of which "
                      f"{mech['covered']} covered and {mech['proposed']} proposed): "
                      f"reconcile the Summary and the content.")
    return issues


def warn_uncited_zone_files(deliverable: str, zone: dict):
    """COMPLETENESS signal (warn-only, never blocking): the zone's code files never cited
    as a source. Not all of them carry a feature — but a blind spot must be SEEN, not
    guessed."""
    try:
        with open(deliverable, "r", encoding="utf-8") as f:
            cited = cited_paths(f.read())
    except OSError:
        return
    files = [str(f) for f in (zone.get("files") or [])]
    uncited = [f for f in files if norm_rel(f) not in cited]
    if uncited:
        shown = ", ".join(uncited[:8]) + ("…" if len(uncited) > 8 else "")
        print(f"   ℹ️  Completeness: {len(uncited)}/{len(files)} zone file(s) never cited "
              f"as a source ({shown}) — possible blind spot, to check while reading.")


def stale_zone_sources(zone: dict, deliverable: str) -> list:
    """Zone files modified AFTER its documentation was written: the zone doc is probably
    STALE. Best-effort mtime (DrvFs/WSL2 sometimes truncates): a signal for the human,
    never a verdict — they decide whether to replay the pass."""
    try:
        doc_mtime = os.path.getmtime(deliverable)
    except OSError:
        return []
    stale = []
    for entry in list(zone.get("files") or []) + list(zone.get("tests") or []):
        p = norm_rel(entry)
        try:
            if os.path.exists(p) and os.path.getmtime(p) > doc_mtime:
                stale.append(p)
        except OSError:
            continue
    return sorted(stale)


# ─── GRIDS: LOADING ───────────────────────────────────────────────────────────
# Unlike the Nielsen grid (common trunk + sections to slice), the two grids of this
# pipeline are sent WHOLE: the context « slice » comes from the doc_map (ITS zone, ITS
# files), not from the grid.

def load_grid(path: str) -> str:
    """Load a grid (SKILL.md). Its absence is an IMMEDIATE failure: without a grid, the
    agents would improvise — exactly what the factory forbids."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ─── SCOPE DISCOVERY (PYTHON, DETERMINISTIC, ZERO LLM) ────────────────────────
# The scope is established by the orchestrator, never by an agent: a stable, reproducible
# list, shown to the human BEFORE paying for a single LLM turn.

# The audit's UI extensions + back/scripts extensions: documentation covers ALL the
# project's behavior, not only its interface. EDITABLE constant: add here the extensions
# specific to your stack if the displayed scope misses any.
UI_EXTENSIONS   = {".html", ".htm", ".css", ".scss", ".sass", ".less",
                   ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                   ".vue", ".svelte", ".astro", ".ejs", ".hbs", ".njk", ".twig"}
CODE_EXTENSIONS = UI_EXTENSIONS | {".py", ".java", ".kt", ".kts", ".go", ".rb", ".php",
                                   ".cs", ".rs", ".c", ".h", ".cpp", ".hpp", ".swift",
                                   ".scala", ".sql", ".sh", ".ps1", ".bat"}

# Directories excluded by NAME; any hidden directory ('.git', '.agents', '.opencode'/'.codex',
# '.venv', '.next'…) is excluded outright by the walk's startswith('.') filter.
EXCLUDED_DIR_NAMES = {"node_modules", "dist", "build", "out", "coverage", "target",
                      "vendor", "__pycache__", DOC_DIR}

# The factory does not document itself when dropped into a target project: the
# orchestrators are excluded from the scope. (Their .md/.yaml are already out of scope:
# non-code extensions.)
ORCHESTRATION_BASENAME_PATTERN = "MAIsterMind*.py"
ORCHESTRATOR_SCRIPTS = frozenset({
    "Coding.py", "Coding-Without-Tests.py", "Test-First.py", "Acceptance-First.py",
    "Design-Prototype.py",
    "Spec.py", "Audit-Design.py", "Pre-Audit-A11Y-RGAA.py",
    "Documentation.py", "Guided-Fix.py", "Skills-Adaptation.py", "mm_runner.py",
})


def is_test_file(path: str) -> bool:
    """Best-effort naming heuristic: does 'path' look like a test file?

    Unlike the audit (tests out of scope), tests are HERE a source of behavioral truth:
    they are routed to a separate bucket to allow distinguishing a « Covered » acceptance
    test from a « Proposed » one.
    Same multi-language conventions as the production variants.
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


def is_code_file(name: str) -> bool:
    """Is 'name' (bare file name) a source of behavior to document?

    Deliberately pragmatic: known code extensions, MINUS the tooling that shares these
    extensions without carrying product behavior — minified bundles (unreadable,
    generated), TypeScript declarations, configuration files (vite/webpack/tailwind…),
    Storybook stories (demo, not product), dotfiles, and the MAIsterMind orchestrators
    themselves.
    """
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext not in CODE_EXTENSIONS:
        return False
    if low.startswith("."):
        return False
    if low.endswith(".d.ts") or ".min." in low or ".config." in low or ".stories." in low:
        return False
    if name in ORCHESTRATOR_SCRIPTS or fnmatch.fnmatch(name, ORCHESTRATION_BASENAME_PATTERN):
        return False
    return True


def discover_code_scope() -> tuple:
    """Sorted lists (relative paths, '/' separator) of the scope files:
    (code_files, test_files). Same walk as the audit, with tests routed to the second
    bucket instead of being excluded."""
    code_files, test_files = [], []
    for root, dirs, files in os.walk(".", topdown=True):
        dirs[:] = sorted(d for d in dirs
                         if d not in EXCLUDED_DIR_NAMES and not d.startswith("."))
        for name in files:
            if not is_code_file(name):
                continue
            rel = os.path.normpath(os.path.join(root, name)).replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            if is_test_file(rel):
                test_files.append(rel)
            else:
                code_files.append(rel)
    return sorted(code_files), sorted(test_files)


def business_context_file() -> str:
    """Available business context file ('spec.md' preferred, else 'need.md'), or empty
    string. The documentation does NOT need it to run: it is an optional bonus."""
    for candidate in (SPEC_FILE, NEED_FILE):
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


def business_context_hint() -> str:
    """OPTIONAL pointer to the business context: we never inline the spec in the prompts
    (context window), we only indicate where to find it."""
    context = business_context_file()
    if context:
        return (f"The file '{context}' (business context) exists at the root: consult it "
                f"ONLY if a flow is incomprehensible to you without it (save your context).")
    return "(no business context file detected: document the behavior as the code shows it)"


# ─── READ-ONLY GUARD (GIT, BEST-EFFORT) ───────────────────────────────────────
# « Python verifies what is verifiable »: the ban on modifying the documented project is
# carried by the prompts (unverifiable alone) AND by this mechanical diff when a git repo
# pre-exists. Like the audit: NEVER a 'git init' nor a commit — documenting must leave NO
# trace in the project other than the deliverables.

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


# Deliverables and artifacts of the DOCUMENTATION itself: the only files the agents are
# allowed to produce — never restored nor reported by the read-only guard.
_DOC_BASENAMES = {DOC_FILE, DOC_MAP_FILE, FAIL_REPORT_FILE, TMP_DOC_FILE,
                  TMP_PROMPT_BUFFER, f"{DOC_FILE}.tmp", f"{DOC_MAP_FILE}.tmp",
                  os.path.basename(__file__)}


def is_doc_artifact(path: str) -> bool:
    """Is 'path' a documentation deliverable/artifact (and not a project file)?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _DOC_BASENAMES:
        return True
    if segments[0] == DOC_DIR:
        return True
    # Ephemeral sentinels and buffers, wherever they sit in the tree.
    if base.startswith(".doc_") and base.endswith(".done"):
        return True
    if base.startswith(RUNNER.tmp_dot_prefix) and base.endswith(".md"):
        return True
    # Python caches, virtual environment and tooling directories: outside the documented project.
    if "__pycache__" in segments or base.endswith(".pyc"):
        return True
    if segments[0] in (".venv", RUNNER.equip_dir, ".agents"):
        return True
    return False


def init_readonly_guard():
    """Enable the read-only guard if (and only if) the project is ALREADY a git repo.

    TWO baselines are captured now, BEFORE the first agent:
      - the pre-existing untracked files: without this baseline, the user's untracked
        files would be reported at every pass as « created by the agent » (permanent
        false positive);
      - the tracked files ALREADY MODIFIED (dirty worktree): without this baseline, the
        'git checkout' restoration would DESTROY uncommitted human work predating the
        run — unacceptable. These files leave the guard for the whole run (accepted
        trade-off: never destroying human work outweighs the guard).
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
    print("✓ git repo detected: read-only guard active (any tracked file modified by "
          "an agent will be restored).")
    dirty_project = sorted(f for f in _GIT["baseline_dirty"] if not is_doc_artifact(f))
    if dirty_project:
        print(f"   ⚠️  {len(dirty_project)} file(s) already modified BEFORE the run (work in "
              f"progress?): they are excluded from the guard (never restored outright) — "
              f"{', '.join(dirty_project[:10])}{'…' if len(dirty_project) > 10 else ''}")


def enforce_readonly(label: str):
    """Restore the TRACKED files modified during a pass and report the files created
    outside the deliverables (best-effort, after EACH pass).

    Outright restoration for modifications (documenting does not fix); mere REPORTING for
    creations (we never delete a file we did not create: decision left to the human).
    """
    if not _GIT["enabled"]:
        return
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    # 'baseline_dirty' excluded from restoration: a file already modified BEFORE the run
    # carries uncommitted human work — restoring it would DESTROY it (cf. init).
    touched = sorted(f for f in diff_out.splitlines()
                     if f.strip() and not is_doc_artifact(f.strip())
                     and f.strip() not in _GIT["baseline_dirty"]) if ok_diff else []
    if touched:
        run_git(["checkout", "--"] + touched)
        print(f"🛡️  [{label}] DOCUMENTATION = READ-ONLY: {len(touched)} project file(s) "
              f"modified by the agent — restored: {', '.join(touched)}")
    ok_others, others_out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok_others:
        strays = sorted(
            f for f in ({line.strip() for line in others_out.splitlines() if line.strip()}
                        - _GIT["baseline_untracked"])
            if not is_doc_artifact(f))
        if strays:
            print(f"⚠️  [{label}] File(s) created outside the documentation deliverables (not deleted, "
                  f"to inspect): {', '.join(strays)}")


# ─── FAILURE REPORT & FAILURE MESSAGE ─────────────────────────────────────────

# Current map (set as soon as it is validated): the failure reports index progress on the
# zones — when the map does not exist yet, they say so.
_DOC_MAP_STATE = {"map": None}


def documented_count(doc_map: dict) -> int:
    """Number of zones whose file is already usable."""
    if not isinstance(doc_map, dict) or not isinstance(doc_map.get("zones"), list):
        return 0
    return sum(1 for zone in doc_map["zones"]
               if isinstance(zone, dict) and zone_ok(zone_path(zone)))


def write_fail_report(title: str, reason: str, details: str = ""):
    """Write a persistent stop report at the root (same contract as the factory: any
    NON-nominal stop produces one). Best-effort: NEVER raises."""
    # Chokepoint of non-nominal stops: the run journal closes here (every
    # caller exits with sys.exit(1) right after). Idempotent: end() after end() is a no-op.
    mm_audit.end("failed")
    try:
        lines = ["# Failure report — MAIsterMind (documentation)", "",
                 f"## {title}", "", "### Cause", reason.strip(), "", "### Progress"]
        doc_map = _DOC_MAP_STATE["map"]
        if isinstance(doc_map, dict) and isinstance(doc_map.get("zones"), list) and doc_map["zones"]:
            lines.append(f"- Documented zones: {documented_count(doc_map)}/{len(doc_map['zones'])}")
            for zone in doc_map["zones"]:
                if not isinstance(zone, dict):
                    continue
                mark = "✅" if zone_ok(zone_path(zone)) else "⏳"
                lines.append(f"  - {mark} Z{zone.get('id')} : {zone.get('name')}")
        else:
            lines.append("- Mapping: not established ('doc_map.yaml' missing or invalid).")
        lines.append("")
        if details.strip():
            lines.append("### Details")
            lines.append(details.strip()[:4000])
            lines.append("")
        lines.append("### Recommended action")
        lines.append("Fix the cause above (or bump the model up one notch via /model or "
                     f"'{AGENT_CONFIG_FILE}'), then relaunch: the map and the already "
                     "documented zones will be resumed automatically.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


def fail_doc(message: str, details: str = "", title: str = "Documentation pass failure"):
    """Single exit point for failures. Always kills the tmux session BEFORE exiting: an
    exit that leaves the agent alive lets it finish writing its deliverable AFTER the
    orchestrator gave up (misleading resume state on relaunch)."""
    print(message)
    write_fail_report(title, message, details)
    RUNNER.kill()
    sys.exit(1)


def print_pass_failure(label: str, reason: str):
    model = RUNNER.configured_model()
    done = documented_count(_DOC_MAP_STATE["map"]) if _DOC_MAP_STATE["map"] else 0
    print(f"""
{'='*60}
❌ The pass « {label} » did not complete after {MAX_ATTEMPTS} attempts.

   Cause: {reason}

💡 The current model ({model}) stalls on this pass (often a tool-calling problem:
   the deliverable or the sentinel is never created, or the requested format is
   not respected).
   The most effective move: relaunch after bringing in a model one notch above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the validated map and the {done} already documented zone(s) will be
   resumed automatically, you do not start from scratch. See you soon! 🚀
{'='*60}
""")


# ─── STEP S1: MAPPING — SCHEMA VALIDATION (PYTHON) ────────────────────────────

def norm_rel(path) -> str:
    """Normalize a path supplied by the model to the scope format
    (relative, '/' separator, no './')."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def validate_and_normalize_doc_map(doc_map, code_files: list, test_files: list) -> tuple:
    """Check and normalize the map. Returns (fatal, soft) and MUTATES doc_map in place.

    The map comes out of a fallible small LLM; two classes of problems (same family as
    validate_blackboard_schema in the factory):
      - fatal: STRUCTURAL gaps (not a mapping, zones absent, zone without id/name,
        duplicated ids — shared sentinels —, zone where NO listed file exists).
        The orchestrator MUST stop or replay the pass on them.
      - soft: gaps recovered MECHANICALLY here (invented paths removed, assignment
        duplicates deduplicated — first zone wins —, coverage completed by a
        « Miscellaneous » zone, intent/project filled in): reported to the human, never
        blocking.
    The model proposes, the code checks, the human arbitrates (at the y/n that follows).
    """
    fatal, soft = [], []
    if not isinstance(doc_map, dict):
        return ["The map is not a valid YAML mapping."], []
    zones = doc_map.get("zones")
    if not isinstance(zones, list) or not zones:
        return ["Missing or empty 'zones' block: nothing to document."], []

    if not str(doc_map.get("project") or "").strip():
        doc_map["project"] = os.path.basename(os.getcwd()) or "Project"
        soft.append(f"Missing 'project' field: filled with « {doc_map['project']} » (display only).")

    scope = set(code_files) | set(test_files)
    seen_paths = {}   # path -> id of the first zone that assigns it
    seen_ids = set()

    for idx, zone in enumerate(zones):
        if not isinstance(zone, dict):
            fatal.append(f"zones[{idx}] is not a mapping.")
            continue
        try:
            zone["id"] = int(zone.get("id"))
        except (TypeError, ValueError):
            fatal.append(f"zones[{idx}].id missing or not an integer.")
            continue
        if zone["id"] in seen_ids:
            fatal.append(f"Duplicated zones[].id ({zone['id']}): the sentinels "
                         f"'.doc_z{zone['id']}.attemptM.done' would be SHARED between two zones.")
        seen_ids.add(zone["id"])
        if not str(zone.get("name") or "").strip():
            fatal.append(f"zones[{idx}].name missing.")
            continue
        zone["name"] = str(zone["name"]).strip()
        if not str(zone.get("intent") or "").strip():
            zone["intent"] = "(unspecified)"
            soft.append(f"Zone Z{zone['id']} « {zone['name']} »: 'intent' missing (filled in).")

        removed, kept = [], {"files": [], "tests": []}
        declared = 0
        for bucket in ("files", "tests"):
            entries = zone.get(bucket) or []
            if not isinstance(entries, list):
                entries = []
            for entry in entries:
                declared += 1
                p = norm_rel(entry)
                # DIRECTORY entry (path ending with '/'): every scope file it contains, not
                # yet assigned — code AND tests, each in its bucket. This is what lets a
                # monorepo be mapped without copying thousands of paths (and without the
                # surplus mechanically falling into « Miscellaneous »).
                expanded_code = expand_dir_entry(p, code_files, seen_paths)
                expanded_tests = expand_dir_entry(p, test_files, seen_paths)
                if expanded_code or expanded_tests:
                    for f in expanded_code:
                        seen_paths[f] = zone["id"]
                        kept["files"].append(f)
                    for f in expanded_tests:
                        seen_paths[f] = zone["id"]
                        kept["tests"].append(f)
                    continue
                if p not in scope:
                    removed.append(p)
                    continue
                if p in seen_paths:
                    soft.append(f"'{p}' assigned to several zones: kept in zone "
                                f"Z{seen_paths[p]} (first assignment), removed from Z{zone['id']}.")
                    continue
                seen_paths[p] = zone["id"]
                kept[bucket].append(p)
        zone["files"], zone["tests"] = kept["files"], kept["tests"]
        if removed:
            shown = ", ".join(removed[:10]) + ("…" if len(removed) > 10 else "")
            soft.append(f"Zone Z{zone['id']} « {zone['name']} »: {len(removed)} out-of-scope "
                        f"path(s) removed mechanically ({shown}).")
        if declared and not (zone["files"] or zone["tests"]):
            fatal.append(f"Zone Z{zone['id']} « {zone['name']} »: NONE of the listed files "
                         f"exists in the scope (invented paths?).")
        elif not declared:
            # An empty « Miscellaneous » is not a fault: the prompt asks NOT to copy the
            # surplus into it (coverage fills it) — rejecting it contradicted the instruction.
            if slugify(zone["name"]) == "miscellaneous":
                soft.append(f"Zone Z{zone['id']} « {zone['name']} » declared empty: completed "
                            f"by the coverage check (or removed if nothing remains).")
            else:
                fatal.append(f"Zone Z{zone['id']} « {zone['name']} »: no file assigned.")
        if len(zone["files"]) + len(zone["tests"]) > SOFT_MAX_FILES_PER_ZONE:
            soft.append(f"Zone Z{zone['id']} « {zone['name']} »: "
                        f"{len(zone['files']) + len(zone['tests'])} files "
                        f"(> {SOFT_MAX_FILES_PER_ZONE}) — the pass may saturate its window; "
                        f"re-split the map before validating if possible.")

    if fatal:
        return fatal, soft

    ids = sorted(seen_ids)
    if ids != list(range(1, len(ids) + 1)):
        soft.append(f"zones[].id is not a contiguous 1..N sequence "
                    f"({', '.join(str(i) for i in ids)}): tolerated, the YAML order prevails.")

    # TOTAL COVERAGE (symmetric of the factory's check_spec_coverage): every scope file
    # absent from the map is added MECHANICALLY to a « Miscellaneous » zone (created when
    # needed) — the documentation leaves no silent blind spot.
    missing_code = [f for f in code_files if f not in seen_paths]
    missing_tests = [f for f in test_files if f not in seen_paths]
    if missing_code or missing_tests:
        divers = next((z for z in zones if isinstance(z, dict)
                       and slugify(str(z.get("name") or "")) == "miscellaneous"), None)
        if divers is None:
            divers = {"id": max(seen_ids) + 1, "name": "Miscellaneous",
                      "intent": "Technical and cross-cutting residual "
                                "(completed mechanically by the coverage check).",
                      "files": [], "tests": []}
            zones.append(divers)
        divers["files"] = list(divers.get("files") or []) + missing_code
        divers["tests"] = list(divers.get("tests") or []) + missing_tests
        soft.append(f"Coverage: {len(missing_code) + len(missing_tests)} scope file(s) "
                    f"absent from the map — added mechanically to the « Miscellaneous » "
                    f"zone (Z{divers['id']}).")

    # A « Miscellaneous » declared empty and still empty after coverage has no reason to
    # exist any more (a documentation pass on zero files would make no sense).
    zones[:] = [z for z in zones
                if not (isinstance(z, dict) and slugify(str(z.get("name") or "")) == "miscellaneous"
                        and not (z.get("files") or z.get("tests")))]

    return fatal, soft


def divers_size(doc_map: dict) -> int:
    """Number of files (code + tests) placed in the « Miscellaneous » zone — 0 if absent."""
    for zone in doc_map.get("zones") or []:
        if isinstance(zone, dict) and slugify(str(zone.get("name") or "")) == "miscellaneous":
            return len(zone.get("files") or []) + len(zone.get("tests") or [])
    return 0


def divers_files(doc_map: dict) -> list:
    """Files (code + tests) of the « Miscellaneous » zone — [] if absent."""
    for zone in doc_map.get("zones") or []:
        if isinstance(zone, dict) and slugify(str(zone.get("name") or "")) == "miscellaneous":
            return list(zone.get("files") or []) + list(zone.get("tests") or [])
    return []


def save_doc_map(doc_map: dict):
    """Persist the NORMALIZED map (atomic write): what the human validates at the y/n is
    exactly what is on disk — and therefore what a resume run will reload."""
    tmp = f"{DOC_MAP_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc_map, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp, DOC_MAP_FILE)


def peek_doc_map():
    """Best-effort loading of the map for the S0 display (never blocking)."""
    try:
        with open(DOC_MAP_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and isinstance(data.get("zones"), list) and data["zones"]:
            return data
    except Exception:
        pass
    return None


# ─── PER-FILE DEFERRED PROMPTS ────────────────────────────────────────────────

def summarize_by_directory(files: list, max_lines: int = 60) -> str:
    """Per-directory summary of the files NOT listed in the mapping prompt (assignable
    PER DIRECTORY: map entry ending with '/'; otherwise they will land in the
    « Miscellaneous » zone via coverage). Bounded to `max_lines` directories, the most
    populated first."""
    counts = {}
    for f in files:
        d = os.path.dirname(f) or "."
        counts[d] = counts.get(d, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"- {d}/ : {n} unlisted file(s)" for d, n in ordered[:max_lines]]
    if len(ordered) > max_lines:
        lines.append(f"- (+ {len(ordered) - max_lines} other directory/directories)")
    return "\n".join(lines)


def build_carto_scope_blocks(code_files: list, test_files: list) -> tuple:
    """« Files to assign » blocks of the mapper prompt, bounded to
    MAX_SCOPE_FILES_IN_CARTO in total. Returns (code_block, tests_block, overflow_block).

    The listed files are a SAMPLE representative of every directory (application code
    first), not the first N in alphabetical order — on a monorepo, those first N were 300
    icon stylesheets and zero file from src/. The surplus is summarized by directory and
    assignable PER DIRECTORY."""
    listed_code = select_carto_sample(code_files, MAX_SCOPE_FILES_IN_CARTO)
    remaining = MAX_SCOPE_FILES_IN_CARTO - len(listed_code)
    listed_tests = select_carto_sample(test_files, max(0, remaining))
    listed = set(listed_code) | set(listed_tests)
    overflow = [f for f in code_files + test_files if f not in listed]
    code_block = "\n".join(f"- {f}" for f in listed_code) or "(none)"
    tests_block = "\n".join(f"- {f}" for f in listed_tests) or "(none)"
    overflow_block = ""
    if overflow:
        overflow_block = (f"\n(⚠️ Scope of {len(code_files) + len(test_files)} files: "
                          f"{len(listed)} listed above (sample representative of every "
                          f"directory), {len(overflow)} unlisted, summarized by directory "
                          f"below. Assign them PER DIRECTORY: a files: entry whose path ends "
                          f"with '/' covers every scope file it contains (recursively, code "
                          f"and tests). What you do not assign will go mechanically to the "
                          f"« Miscellaneous » zone, which must remain a residual — not the "
                          f"bulk of the project.)\n"
                          + summarize_by_directory(overflow))
    return code_block, tests_block, overflow_block


def build_carto_prompt(grid_text: str, code_files: list, test_files: list,
                       feedback: str, attempt: int) -> str:
    sentinel = doc_sentinel("map", attempt)
    code_block, tests_block, overflow_block = build_carto_scope_blocks(code_files, test_files)
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are the functional Mapper of a documentation pipeline split by zones: you ASSIGN each
file provided below to a named and ordered functional zone.
You document NOTHING (a dedicated pass per zone handles that next) and you do not read the
project in depth: skim only the files whose name is not enough to decide.
DOCUMENTATION = READ-ONLY: you modify, fix, create NO project file.
You write ONLY two files: '{DOC_MAP_FILE}' at the root, then your end sentinel.

--- MAPPER RUBRIC ---
{grid_text}

--- FILES TO ASSIGN (discovered by the orchestrator; paths to COPY as-is) ---
A files: or tests: entry may also be a DIRECTORY (path ending with '/', e.g. "src/cart/"):
it assigns to the zone every scope file it contains that is not already assigned
elsewhere. The « Miscellaneous » zone may be omitted or declared empty: the orchestrator
mechanically places there what you will not have assigned.
CODE FILES ({len(code_files)}):
{code_block}

TEST FILES ({len(test_files)}):
{tests_block}
{overflow_block}

--- BUSINESS CONTEXT (optional) ---
{business_context_hint()}

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY DELIVERABLE ---
Write the map in '{DOC_MAP_FILE}' at the project root: PURE YAML compliant with the rubric
above (NO ``` fences, all textual values in double quotes, paths copied from the provided
lists). Do it directly through your file editing tools, without needless chatter in the
console.

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{DOC_MAP_FILE}', create the sentinel file
'{sentinel}' at the root (content: the single word done): it is the completion signal for
the orchestrator. Create it only when the map is TRULY finished.
"""
    with open(TMP_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_DOC_FILE}' at the project root and carry out "
            f"the functional mapping pass.")


def build_zone_files_block(zone: dict) -> str:
    """« Your zone » block of the documenter prompt: bounded lists (context window)."""
    files = [str(f) for f in (zone.get("files") or [])]
    tests = [str(t) for t in (zone.get("tests") or [])]
    listed_files = files[:MAX_ZONE_FILES_IN_PROMPT]
    remaining = MAX_ZONE_FILES_IN_PROMPT - len(listed_files)
    listed_tests = tests[:max(0, remaining)]
    lines = [f"Code files of your zone ({len(files)}):"]
    lines += [f"- {f}" for f in listed_files] or ["(no code file)"]
    lines.append("")
    lines.append(f"Existing TEST files of your zone ({len(tests)}) — your source of "
                 f"truth for the « Covered » status:")
    if listed_tests:
        lines += [f"- {t}" for t in listed_tests]
    else:
        lines.append("(no existing test: all acceptance tests of this zone "
                     "will be « Proposed ».)")
    overflow = (len(files) - len(listed_files)) + (len(tests) - len(listed_tests))
    if overflow > 0:
        lines.append(f"(+ {overflow} other unlisted file(s): focus on "
                     f"the main flows above.)")
    return "\n".join(lines)


def build_zone_prompt(grid_text: str, zone: dict, position: int, total: int,
                      doc_map: dict, feedback: str, attempt: int) -> str:
    zone_id = zone["id"]
    deliverable = zone_path(zone)
    sentinel = doc_sentinel(f"z{zone_id}", attempt)
    other_zones = "\n".join(f"- Z{z['id']} : {z['name']}"
                            for z in doc_map["zones"] if z is not zone) or "(no other zone)"
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are an ultra-specialized behavioral Documenter Agent, assigned to A SINGLE functional
zone: Z{zone_id} « {zone['name']} ». This is pass {position}/{total} of a documentation
split by zones.
DOCUMENTATION = READ-ONLY: you modify, fix, create NO project file.
You write ONLY two files: your zone file, then your end sentinel.
Ignore any behavior belonging to a zone OTHER than yours: a dedicated pass handles it
(documenting it here would create duplicates) — at most a one-line cross-reference « see Z<n> ».

--- DOCUMENTER RUBRIC ---
{grid_text}

--- YOUR ZONE (assigned by the human-validated mapping) ---
Zone Z{zone_id} : {zone['name']}
Announced role (intent): {zone.get('intent', '(unspecified)')}

{build_zone_files_block(zone)}

--- THE OTHER ZONES (for « see Z<n> » cross-references; do NOT document their content) ---
{other_zones}

--- BUSINESS CONTEXT (optional) ---
{business_context_hint()}

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY DELIVERABLE ---
Write your zone documentation in '{deliverable}' (create the '{DOC_DIR}/' folder if needed),
STRICTLY respecting the format of the rubric above: first line
'# Z{zone_id} : {zone['name']}', sections '## Features' and '## Summary' mandatory (Summary
in the locked format); an explicit « No user-facing feature. » if the zone is purely
technical. Do it directly through your file editing tools, without needless chatter in the
console.

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{deliverable}', create the sentinel file
'{sentinel}' at the root (content: the single word done): it is the completion signal for
the orchestrator. Create it only when the zone file is TRULY finished.
"""
    with open(TMP_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_DOC_FILE}' at the project root and document "
            f"zone Z{zone_id}.")


def extract_bilan_block(path: str) -> str:
    """Extract the lines of the '## Summary' of a zone file (a few lines per zone: the only
    quantified material the overview needs — never the whole zone)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return "(summary unreadable)"
    kept, in_bilan = [], False
    for line in lines:
        low = line.strip().lower()
        if low.startswith("## summary"):
            in_bilan = True
            continue
        if in_bilan and (line.startswith("## ") or line.startswith("# ")):
            break
        if in_bilan and line.strip():
            kept.append(line.strip())
    return "\n".join(kept) or "(summary empty)"


def build_overview_prompt(doc_map: dict, feedback: str, attempt: int) -> str:
    sentinel = doc_sentinel("overview", attempt)
    zones = doc_map["zones"]
    zone_lines = "\n".join(f"- Z{z['id']} « {z['name']} » : {z.get('intent', '')}" for z in zones)
    bilan_lines = "\n\n".join(f"Z{z['id']} « {z['name']} » — Summary:\n{extract_bilan_block(zone_path(z))}"
                              for z in zones)
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are the Writer of the overview of a behavioral documentation produced in {len(zones)}
independent passes (one per functional zone). You write the reading LEAD-IN, and nothing
else: you do NOT re-read the zone files, you do NOT re-read the project's code. ZERO
invention: only what the titles, intents and summaries below carry. You modify no project
file; you write ONLY the overview, then your sentinel.

--- THE PROJECT ---
Name: {doc_map.get('project', '(unspecified)')}

--- THE ZONES (in the reading order of the final documentation) ---
{zone_lines}

--- THE QUANTIFIED SUMMARIES PER ZONE (extracted by the orchestrator) ---
{bilan_lines}

--- MANDATORY DELIVERABLE: '{OVERVIEW_FILE}' ---
A text of 15 to 30 lines, starting EXACTLY with the line '## Overview', which gives the
reader: what the product does and for whom, the major flows (deduced from the intents
alone), and how to read the documentation (the zones in the order above; « Covered » =
acceptance tests verified by an existing cited test, « Proposed » = tests to write). User
language, no internal jargon, no file list.
Do it directly through your file editing tools, without needless chatter in the console.

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{OVERVIEW_FILE}', create the sentinel file
'{sentinel}' at the root (content: the single word done): it is the completion signal for
the orchestrator.
"""
    with open(TMP_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_DOC_FILE}' at the project root and write "
            f"the documentation overview.")


# ─── STEP S1: MAPPING (1 LLM PASS + DOUBLE VALIDATION) ────────────────────────

def print_doc_map_recap(doc_map: dict, soft: list):
    """Human recap of the map: the table validated at the y/n."""
    zones = doc_map["zones"]
    print(f"\n{'='*60}")
    print(f"🗺️  FUNCTIONAL MAP — {doc_map.get('project', '(unnamed)')}: {len(zones)} zone(s)")
    print(f"{'Id':>4} | {'Zone':<30} | {'Code':>4} | {'Tests':>5} | Intent")
    print(f"{'-'*4}-+-{'-'*30}-+-{'-'*4}-+-{'-'*5}-+-{'-'*30}")
    for zone in zones:
        name = str(zone["name"])[:30]
        intent = str(zone.get("intent", ""))[:60]
        print(f"{zone['id']:>4} | {name:<30} | {len(zone.get('files') or []):>4} | "
              f"{len(zone.get('tests') or []):>5} | {intent}")
    if soft:
        print(f"\n⚠️  Points of attention (non-blocking):")
        for warning in soft:
            print(f"   - {warning}")
    print(f"\n   ✏️  The map is EDITABLE: '{DOC_MAP_FILE}' (the order of the zones = the reading "
          f"order of the final documentation).")
    print(f"{'='*60}")


def confirm_doc_map(doc_map: dict, soft: list):
    """Human validation of the map (the y/n that arbitrates BEFORE paying for N passes)."""
    print_doc_map_recap(doc_map, soft)
    confirm = input("\n▶️  Validate this map and launch the zone-by-zone documentation? (y/n): ")
    mm_audit.event("gate", id="map", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Stopping. Edit '{DOC_MAP_FILE}' then relaunch (it will be resumed as-is), "
              f"or delete it to replay the mapping.")
        RUNNER.kill()
        sys.exit(0)


def load_and_validate_map_file(code_files: list, test_files: list) -> tuple:
    """Load + validate 'doc_map.yaml'. Returns (doc_map, fatal, soft, parse_error)."""
    try:
        with open(DOC_MAP_FILE, "r", encoding="utf-8") as f:
            doc_map = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, [], [], str(e)
    except OSError as e:
        return None, [], [], str(e)
    fatal, soft = validate_and_normalize_doc_map(doc_map, code_files, test_files)
    return doc_map, fatal, soft, ""


def run_cartography(grid_text: str, code_files: list, test_files: list) -> dict:
    """Step S1: produce (or resume) the zone map, doubly validated.

    Resume: an existing and valid 'doc_map.yaml' skips the LLM pass (recap + y/n shown
    again — this is where manual editing of the YAML is taken into account); an existing
    but structurally invalid file stops the run with instructions (fix or delete), same
    contract as the blackboard.
    """
    if os.path.exists(DOC_MAP_FILE):
        doc_map, fatal, soft, parse_error = load_and_validate_map_file(code_files, test_files)
        if parse_error:
            fail_doc(f"❌ '{DOC_MAP_FILE}' exists but is not parsable: fix it or "
                     f"delete it (the mapping will be replayed), then relaunch.",
                     details=parse_error[:1500], title="Existing map invalid")
        if fatal:
            fail_doc(f"❌ '{DOC_MAP_FILE}' exists but is structurally invalid:\n   - "
                     + "\n   - ".join(fatal)
                     + f"\n   → Fix it or delete it (the mapping will be replayed), then relaunch.",
                     details="\n".join(fatal), title="Existing map invalid")
        save_doc_map(doc_map)
        _DOC_MAP_STATE["map"] = doc_map
        print(f"♻️  '{DOC_MAP_FILE}' exists and is valid: mapping skipped (resume).")
        # Map written AFTER the stop of a run left without closure: deliverable of an
        # orphan agent, to re-read before taking it as valid.
        residual = residual_deliverable_warning(DOC_MAP_FILE, "documentation")
        if residual:
            soft = list(soft) + [residual]
        confirm_doc_map(doc_map, soft)
        return doc_map

    print(f"\n{'='*50}\n🗺️  STEP S1: FUNCTIONAL MAPPING (1 LLM pass)\n{'='*50}")
    total_scope = len(code_files) + len(test_files)
    if total_scope > MAX_SCOPE_FILES_IN_CARTO:
        print(f"   ⚠️  Scope of {total_scope} files > {MAX_SCOPE_FILES_IN_CARTO}: the "
              f"surplus will be summarized by directory in the prompt and put in the "
              f"« Miscellaneous » zone by the coverage check (accepted degradation).")
    RUNNER.start()

    attempts = 0
    doc_map, soft = None, []
    feedback = "First pass — no previous feedback."

    while doc_map is None and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Catch-up of a LATE deliverable: the agent of the previous attempt may have
        # finished writing AFTER the orchestrator's timeout. If its map has become valid
        # meanwhile, we take it as-is rather than paying a turn to redo everything.
        if attempts > 1 and os.path.exists(DOC_MAP_FILE):
            late_map, late_fatal, late_soft, late_err = load_and_validate_map_file(code_files, test_files)
            if not late_err and not late_fatal:
                print(f"   ♻️  '{DOC_MAP_FILE}' finally arrived (late deliverable): accepted.")
                doc_map, soft = late_map, late_soft
                break

        cleanup_slot_sentinels("map")
        print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Launching the functional Mapper...")

        prompt = build_carto_prompt(grid_text, code_files, test_files, feedback, attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable = wait_for_deliverable(DOC_MAP_FILE, doc_sentinel("map", attempts),
                                               structural_check=map_structural_check)
        # Read-only guard after EACH attempt (completed or not): a mapper that
        # « fixed » code along the way is restored immediately.
        enforce_readonly("Carto")

        if not got_deliverable:
            feedback = (f"On the previous pass, no deliverable was received ('{DOC_MAP_FILE}' "
                        f"missing, empty or never signaled). First write the full YAML map, "
                        f"THEN the sentinel, in that order.")
            print(f"⏱️  The mapper did not signal the end of its pass. New attempt.")
            if os.path.exists(DOC_MAP_FILE) and not map_structural_check(DOC_MAP_FILE):
                try:
                    os.remove(DOC_MAP_FILE)
                except OSError:
                    pass
            RUNNER.new_context()
            continue

        candidate, fatal, cand_soft, parse_error = load_and_validate_map_file(code_files, test_files)
        if parse_error:
            feedback = (f"Your '{DOC_MAP_FILE}' is not parsable YAML "
                        f"(error: {parse_error[:400]}). Reminders: NO ``` fences, all "
                        f"textual values in double quotes, internal quotes "
                        f"escaped (\\\"). Rewrite the file entirely.")
            print(f"⚠️  [REJECTED] Attempt {attempts}: YAML not parsable.")
        elif fatal:
            feedback = ("Your map does not respect the rubric's schema: "
                        + " ; ".join(fatal)
                        + " Reminders: paths COPIED from the provided lists (never "
                          "invented), each zone with a unique integer id, a name and at "
                          "least one existing file. Rewrite the file entirely.")
            print(f"⚠️  [REJECTED] Attempt {attempts}: structurally invalid map "
                  f"({len(fatal)} anomaly/anomalies).")
        elif divers_size(candidate) > DIVERS_RETRY_THRESHOLD and attempts < MAX_ATTEMPTS:
            # A « Miscellaneous » that contains the bulk of the project is not a mapping:
            # we replay as long as attempts remain, naming the directories to assign.
            overflow = divers_size(candidate)
            feedback = (f"Your map leaves {overflow} files in the « Miscellaneous » zone (residual), "
                        f"i.e. the bulk of the project: this is not a functional split. Assign them "
                        f"to named functional zones, PER DIRECTORY (files: entry ending "
                        f"with '/'). Directories concerned:\n"
                        + summarize_by_directory(divers_files(candidate)))
            print(f"⚠️  [REJECTED] Attempt {attempts}: {overflow} files in « Miscellaneous » "
                  f"(> {DIVERS_RETRY_THRESHOLD}) — the map does not split the project.")
        else:
            doc_map, soft = candidate, cand_soft
            break

        try:
            os.remove(DOC_MAP_FILE)
        except OSError:
            pass
        RUNNER.new_context()

    if doc_map is None:
        cleanup_all_doc_sentinels()
        print_pass_failure("Mapping", feedback)
        fail_doc(f"❌ Mapping not completed after {MAX_ATTEMPTS} attempts.", details=feedback)

    cleanup_slot_sentinels("map")
    save_doc_map(doc_map)
    _DOC_MAP_STATE["map"] = doc_map
    # Context reset before the first zone pass: the mapper's conversation must not leak
    # into the following passes.
    RUNNER.new_context()
    confirm_doc_map(doc_map, soft)
    return doc_map


# ─── STEP S2: N DOCUMENTATION PASSES (ONE PER ZONE) ───────────────────────────

def warn_orphan_zone_files(doc_map: dict):
    """Files in 'doc_zones/' matching no zone of the map (map re-edited by hand, e.g.):
    reported at the start of the step, NEVER deleted (human decision)."""
    if not os.path.isdir(DOC_DIR):
        return
    expected = {os.path.basename(zone_path(zone)) for zone in doc_map["zones"]}
    orphans = sorted(name for name in os.listdir(DOC_DIR)
                     if name.startswith("Z") and name.endswith(".md") and name not in expected)
    if orphans:
        print(f"⚠️  Orphan file(s) in '{DOC_DIR}/' (no zone of the map produces them "
              f"— map re-edited?): {', '.join(orphans)}. Not deleted; they will NOT "
              f"be assembled.")


def run_doc_passes(grid_text: str, doc_map: dict, test_scope: set):
    """The MAIsterMind core: a fresh session per zone, a context slice per pass."""
    zones = doc_map["zones"]
    total = len(zones)
    warn_orphan_zone_files(doc_map)

    for position, zone in enumerate(zones, start=1):
        zone_id = zone["id"]
        deliverable = zone_path(zone)

        # File-based resume: a usable zone file skips its pass. Two warn-only signals
        # come with the skip (never a silent nor costly replay): FRESHNESS (zone files
        # changed since) and the content discrepancies of a file from an old run
        # (produced before the guards).
        if zone_ok(deliverable):
            stale = stale_zone_sources(zone, deliverable)
            if stale:
                print(f"⏭️  Pass Z{zone_id} ({position}/{total}) already documented BUT STALE: "
                      f"{len(stale)} zone file(s) modified since "
                      f"('{stale[0]}'{'…' if len(stale) > 1 else ''}). Skipped anyway — "
                      f"delete '{deliverable}' and relaunch to re-document it.")
            else:
                print(f"⏭️  Pass Z{zone_id} ({position}/{total}) already documented ('{deliverable}'): skipped.")
            legacy_issues = zone_content_issues(deliverable, test_scope, zone.get("files"))
            if legacy_issues:
                print(f"   ⚠️  Verifiable discrepancy(ies) in this resumed file (old run?): "
                      f"{len(legacy_issues)} — delete '{deliverable}' and relaunch to "
                      f"regenerate it under guard. First discrepancy: {legacy_issues[0]}")
            continue
        if os.path.exists(deliverable):
            # Half-written residue of an interrupted run: we start over cleanly.
            try:
                os.remove(deliverable)
                print(f"🧹 Residual '{deliverable}' (incomplete) removed: the pass is replayed.")
            except OSError:
                pass

        print(f"\n{'='*50}\n📝 PASS Z{zone_id} ({position}/{total}): {zone['name']}\n{'='*50}")

        attempts = 0
        success = False
        feedback = "First pass — no previous feedback."

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1

            # Catch-up of a LATE deliverable (same logic as the audit) — accepted under
            # the SAME conditions as a nominal deliverable: structure AND content guards.
            if attempts > 1 and zone_ok(deliverable) \
                    and not zone_content_issues(deliverable, test_scope, zone.get("files")):
                print(f"   ♻️  '{deliverable}' finally arrived (late deliverable): accepted.")
                success = True
                break

            cleanup_slot_sentinels(f"z{zone_id}")
            print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Pass Z{zone_id} — launching the "
                  f"behavioral Documenter...")

            prompt = build_zone_prompt(grid_text, zone, position, total, doc_map,
                                       feedback, attempts)
            mm_audit.event("agent_task", prompt_bytes=len(prompt))
            RUNNER.send_task(prompt)

            got_deliverable = wait_for_deliverable(deliverable,
                                                   doc_sentinel(f"z{zone_id}", attempts),
                                                   structural_check=zone_structural_check)
            # Read-only guard after EACH attempt (completed or not): a documenter that
            # « fixed » code while reading it is restored immediately.
            enforce_readonly(f"Z{zone_id}")

            if not got_deliverable:
                feedback = ("On the previous pass, no deliverable was received (zone file "
                            "missing, empty or never signaled). First write the full zone "
                            "file, THEN the sentinel, in that order.")
                print(f"⏱️  The documenter did not signal the end of pass Z{zone_id}. "
                      f"New attempt.")
                RUNNER.new_context()
                continue

            # Structural floor AFTER the fact, even when the sentinel arrived: the sentinel
            # path of wait_for_deliverable does not check the structure, and an off-format
            # file would be unassemblable (unreadable Summary, missing sections).
            if not zone_structural_check(deliverable):
                feedback = (f"Your file '{deliverable}' does not respect the requested format: "
                            f"the sections '## Features' (with features in the rubric's "
                            f"format, or the single line « No user-facing feature. ») and "
                            f"'## Summary' (two lines in the locked format) are MANDATORY. "
                            f"Rewrite it entirely in the right format.")
                try:
                    os.remove(deliverable)
                except OSError:
                    pass
                print(f"⚠️  [REJECTED] Attempt {attempts}: off-format zone file "
                      f"(mandatory sections missing).")
                RUNNER.new_context()
                continue

            # ── CONTENT GUARDS (mechanical, zero LLM) ──: cited sources existing,
            # « Covered » resting on a real test, Summary equal to the real count. The
            # EXACT discrepancy is sent back to the documenter — not a judgment, a
            # verifiable fact.
            issues = zone_content_issues(deliverable, test_scope, zone.get("files"))
            if issues:
                feedback = ("Your zone file contains VERIFIABLE discrepancies:\n- "
                            + "\n- ".join(issues)
                            + "\nFix them then rewrite the file entirely (zero "
                              "invention: only cite real paths of your zone).")
                try:
                    os.remove(deliverable)
                except OSError:
                    pass
                print(f"🛡️  [REJECTED] Attempt {attempts}: {len(issues)} verifiable "
                      f"discrepancy(ies) in the zone file (cited source not found, "
                      f"« Covered » without a real test or wrong Summary).")
                RUNNER.new_context()
                continue

            success = True
            warn_uncited_zone_files(deliverable, zone)

        if not success:
            reason = feedback
            cleanup_all_doc_sentinels()
            print_pass_failure(f"Z{zone_id} : {zone['name']}", reason)
            fail_doc(f"❌ Pass Z{zone_id} not completed after {MAX_ATTEMPTS} attempts.",
                     details=reason)

        print(f"✅ Pass Z{zone_id} completed: documentation in '{deliverable}'.")
        cleanup_slot_sentinels(f"z{zone_id}")
        RUNNER.new_context()


# ─── STEP S3: OVERVIEW (READING LEAD-IN) ──────────────────────────────────────

def mechanical_overview(doc_map: dict) -> str:
    """100% Python fallback for the overview: the lead-in's failure must never invalidate
    N successful passes — the valuable content is already in the zones."""
    zones = doc_map["zones"]
    lines = ["## Overview", "",
             f"The project « {doc_map.get('project', '(unnamed)')} » is documented in "
             f"{len(zones)} functional zones, presented in the reading order below. "
             f"(Overview generated mechanically: the writing pass did not complete.)", ""]
    for zone in zones:
        lines.append(f"- **Z{zone['id']} — {zone['name']}** : {zone.get('intent', '')}")
    lines += ["",
              "Each zone describes its features then their acceptance tests: « Covered » "
              "means verified by an existing project test (cited); « Proposed » means "
              "an acceptance test to write."]
    return "\n".join(lines) + "\n"


def run_overview(doc_map: dict):
    """Step S3: the only content of the final deliverable that demands genuine cross-cutting
    writing — short, therefore safe to entrust to an agent without saturation risk.
    ALWAYS replayed (it must reflect the up-to-date zones)."""
    print(f"\n{'='*50}\n🪧 STEP S3: OVERVIEW (READING LEAD-IN)\n{'='*50}")

    if os.path.exists(OVERVIEW_FILE):
        try:
            os.remove(OVERVIEW_FILE)
            print(f"   🧹 Residual '{OVERVIEW_FILE}' removed (the overview is regenerated).")
        except OSError:
            pass

    attempts = 0
    success = False
    feedback = "First pass — no previous feedback."
    while not success and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Catch-up of a LATE deliverable (same logic as the other passes).
        if attempts > 1 and os.path.exists(OVERVIEW_FILE) \
                and os.path.getsize(OVERVIEW_FILE) > 0 \
                and overview_structural_check(OVERVIEW_FILE):
            print(f"   ♻️  '{OVERVIEW_FILE}' finally arrived (late deliverable): accepted.")
            success = True
            break

        cleanup_slot_sentinels("overview")
        print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Launching the Overview Writer...")

        prompt = build_overview_prompt(doc_map, feedback, attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable = wait_for_deliverable(OVERVIEW_FILE,
                                               doc_sentinel("overview", attempts),
                                               structural_check=overview_structural_check)
        enforce_readonly("Overview")

        if not got_deliverable or not overview_structural_check(OVERVIEW_FILE):
            if os.path.exists(OVERVIEW_FILE) and not overview_structural_check(OVERVIEW_FILE):
                try:
                    os.remove(OVERVIEW_FILE)
                except OSError:
                    pass
            feedback = (f"On the previous pass, the overview was missing or off-format: "
                        f"the file '{OVERVIEW_FILE}' must start EXACTLY with the line "
                        f"'## Overview' (15 to 30 lines in total).")
            print("⏱️  Overview missing or off-format. New attempt.")
            RUNNER.new_context()
            continue
        success = True

    cleanup_slot_sentinels("overview")
    if not success:
        # GRACEFUL DEGRADATION (accepted difference from the blocking audit synthesis):
        # the lead-in's failure must not invalidate N successful passes — mechanical fallback.
        print(f"⚠️  Overview not completed after {MAX_ATTEMPTS} attempts: MECHANICAL "
              f"fallback (list of zones and intents). The valuable content is in the zones.")
        with open(OVERVIEW_FILE, "w", encoding="utf-8") as f:
            f.write(mechanical_overview(doc_map))
        RUNNER.new_context()
        return

    print(f"✅ Overview ready: '{OVERVIEW_FILE}'.")


# ─── STEP S4: ASSEMBLY (DETERMINISTIC PYTHON, ZERO LLM, ZERO LOSS) ────────────
# The « you copy, you do not invent » contract of the blackboard compiler, turned into
# CODE: concatenation in the doc_map order, table of contents and counters generated
# mechanically. No loss possible, whatever the volume (decision D2).

# The two lines of the Summary, locked by the doc-zone rubric (TOLERANT parse;
# straight/typographic apostrophes accepted). Since the content guards, the declared
# Summary is CONFRONTED with the mechanical count (count_zone_content): at production
# time a discrepancy is a rejection, at assembly time the recounted counters ALWAYS
# prevail (a file from an old run never corrupts the coverage appendix).
BILAN_FEATURES_RE = re.compile(r"^\s*-\s*\**Features\**\s*:\s*(\d+)", re.IGNORECASE)
BILAN_ATS_RE = re.compile(
    r"^\s*-\s*\**Acceptance tests?\**\s*:\s*(\d+)\s*"
    r"\(\s*covered\s*:\s*(\d+)\s*[,;]\s*proposed\s*:\s*(\d+)\s*\)", re.IGNORECASE)

FEATURE_HEADING_RE = re.compile(r"^###\s+(F\d+\s*[—–-].+)$")


def iter_lines_with_fence_state(content: str):
    """Iterate (line, in_fence): the lines inside ``` / ~~~ blocks are marked so that
    neither heading shifting nor extractions apply to them."""
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            yield line, True
            continue
        yield line, in_fence


def shift_headings(content: str) -> str:
    """Shift all headings by one level ('# ' → '## ', etc.) to keep a single H1 in the
    final document. Lines inside fences are ignored."""
    out = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if not in_fence and line.startswith("#"):
            out.append("#" + line)
        else:
            out.append(line)
    return "\n".join(out)


def linkify_cited_paths(content: str) -> str:
    """Make the backticked cited paths clickable (`src/x.ts:42` →
    [`src/x.ts:42`](src/x.ts#L42)) when the file exists. Purely cosmetic and
    best-effort: never inside fences, never on an already-linked token, never on a
    path that cannot be found (a dead link would be worse than no link).
    'documentation.md' living at the root, the scope's relative paths are directly
    the right ones."""
    def replace(match):
        token = match.group(1)
        if not looks_like_path(token):
            return match.group(0)
        clean = clean_cited(token)
        if not os.path.exists(clean):
            return match.group(0)
        line_match = re.search(r":L?(\d+)", token)
        anchor = f"#L{line_match.group(1)}" if line_match else ""
        return f"[`{token}`]({clean}{anchor})"

    out = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence or "`" not in line:
            out.append(line)
            continue
        out.append(re.sub(r"(?<!\[)`([^`\n]+)`(?!\]\()", replace, line))
    return "\n".join(out)


def parse_zone_bilan(content: str) -> dict:
    """Counters of a zone's '## Summary'. Missing value → None (displayed « ? »)."""
    result = {"features": None, "ats": None, "covered": None, "proposed": None}
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        match = BILAN_FEATURES_RE.match(line)
        if match:
            result["features"] = int(match.group(1))
            continue
        match = BILAN_ATS_RE.match(line)
        if match:
            result["ats"] = int(match.group(1))
            result["covered"] = int(match.group(2))
            result["proposed"] = int(match.group(3))
    return result


def extract_feature_titles(content: str) -> list:
    """Feature titles ('### F1 — …') of a zone file, for the table of contents."""
    titles = []
    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        match = FEATURE_HEADING_RE.match(line.strip())
        if match:
            titles.append(match.group(1).strip())
    return titles


def extract_zone_heading(content: str, zone: dict) -> str:
    """Real H1 title of the zone file (the one the assembled document carries), falling
    back to the title computed from the map."""
    for line, in_fence in iter_lines_with_fence_state(content):
        if not in_fence and line.startswith("# ") :
            return line[2:].strip()
    return f"Z{zone.get('id')} : {zone.get('name')}"


def github_anchor(heading: str) -> str:
    """GitHub-style heading anchor, best-effort (purely cosmetic, never blocking):
    lowercase, spaces → dashes, punctuation removed."""
    out = []
    for ch in str(heading).strip().lower():
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch == " ":
            out.append("-")
    return "".join(out)


def fmt_count(value) -> str:
    """Tolerant display of a Summary counter: None → « ? »."""
    return "?" if value is None else str(value)


def escape_md_cell(text: str) -> str:
    """Neutralize vertical bars in a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def assemble_documentation(doc_map: dict) -> dict:
    """Step S4: the final « compiler ». ALWAYS replayed at the end of the run (reflects the
    up-to-date zones). Returns the stats for the final banner."""
    print(f"\n{'='*50}\n🧩 STEP S4: MECHANICAL ASSEMBLY → '{DOC_FILE}'\n{'='*50}")
    zones = doc_map["zones"]
    project = str(doc_map.get("project") or os.path.basename(os.getcwd()))

    entries = []
    for zone in zones:
        path = zone_path(zone)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            content = ""
        if not content:
            # Should not happen (the passes guarantee the files): explicit placeholder
            # rather than a silent hole.
            content = (f"# Z{zone.get('id')} : {zone.get('name')}\n\n"
                       f"*(Zone file missing or empty at assembly time: "
                       f"delete '{path}' and relaunch to replay this pass.)*")
            print(f"   ⚠️  '{path}' missing or empty: section replaced by a placeholder.")
        # The displayed counters are ALWAYS recounted mechanically from the content
        # (count_zone_content): a falsely declared Summary — file from an old run, manual
        # edit — is reported but never corrupts the map nor the appendix.
        mech = count_zone_content(content)
        declared = parse_zone_bilan(content)
        if any(declared[key] is not None and declared[key] != mech[key] for key in mech):
            print(f"   ⚠️  '{path}': declared Summary ≠ real content — the assembly displays "
                  f"the RECOUNTED counters ({mech['features']} feature(s), {mech['ats']} AT(s) "
                  f"of which {mech['covered']} covered).")
        entries.append({
            "zone": zone,
            "content": content,
            "heading": extract_zone_heading(content, zone),
            "features": extract_feature_titles(content),
            "bilan": mech,
        })

    def total_of(key):
        return sum(e["bilan"][key] for e in entries if e["bilan"][key] is not None)

    stats = {"zones": len(zones), "features": total_of("features"), "ats": total_of("ats"),
             "covered": total_of("covered"), "proposed": total_of("proposed")}

    annexe_title = "Appendix — Acceptance test coverage"
    parts = [f"# Behavioral documentation — {project}", "", DOC_MARKER, "",
             f"*Generated on {time.strftime('%Y-%m-%d')} by `Documentation.py` — "
             f"{stats['zones']} zone(s), {stats['features']} feature(s), {stats['ats']} "
             f"acceptance test(s) ({stats['covered']} covered, {stats['proposed']} proposed); "
             f"counters recounted mechanically at assembly.*", ""]

    # Overview: content of the lead-in (it already carries its title '## Overview'),
    # or mechanical fallback if the file is missing/off-format.
    if os.path.exists(OVERVIEW_FILE) and overview_structural_check(OVERVIEW_FILE):
        with open(OVERVIEW_FILE, "r", encoding="utf-8") as f:
            parts.append(f.read().strip())
    else:
        parts.append(mechanical_overview(doc_map).strip())
    parts.append("")

    # Table of contents: zones → features, best-effort anchor links (GitHub slug rule).
    parts += ["## Contents", "",
              "- [Zone map](#zone-map)"]
    for entry in entries:
        parts.append(f"- [{entry['heading']}](#{github_anchor(entry['heading'])})")
        for feat in entry["features"]:
            parts.append(f"  - [{feat}](#{github_anchor(feat)})")
    parts.append(f"- [{annexe_title}](#{github_anchor(annexe_title)})")
    parts.append("")

    # Zone map: the quick-read table (counters parsed from the Summaries).
    parts += ["## Zone map", "",
              "| Zone | Role | Features | Acceptance tests (covered / proposed) |",
              "|---|---|---|---|"]
    for entry in entries:
        zone, bilan = entry["zone"], entry["bilan"]
        parts.append(f"| [{escape_md_cell(entry['heading'])}](#{github_anchor(entry['heading'])}) "
                     f"| {escape_md_cell(zone.get('intent', ''))} "
                     f"| {fmt_count(bilan['features'])} "
                     f"| {fmt_count(bilan['ats'])} ({fmt_count(bilan['covered'])} / "
                     f"{fmt_count(bilan['proposed'])}) |")
    parts.append("")

    # The body: the zones shifted by one heading level, in the doc_map order
    # (this is the zone-level sort, decided in S1 and validated by the human), with the
    # cited sources made clickable (cosmetic, best-effort).
    for entry in entries:
        parts += ["---", "", shift_headings(linkify_cited_paths(entry["content"])), ""]

    # Coverage appendix: totals per zone + grand total.
    parts += ["---", "", f"## {annexe_title}", "",
              "« Covered »: the acceptance test is verified by an EXISTING project test "
              "(cited in the zone). « Proposed »: acceptance test still TO BE WRITTEN.", "",
              "| Zone | Acceptance tests | Covered | Proposed |",
              "|---|---|---|---|"]
    for entry in entries:
        bilan = entry["bilan"]
        parts.append(f"| {escape_md_cell(entry['heading'])} | {fmt_count(bilan['ats'])} "
                     f"| {fmt_count(bilan['covered'])} | {fmt_count(bilan['proposed'])} |")
    parts.append(f"| **Total** | **{stats['ats']}** | **{stats['covered']}** "
                 f"| **{stats['proposed']}** |")
    parts.append("")

    # ATOMIC write: temporary file INSIDE the project (not /tmp — 3-OS constraint)
    # then os.replace — a Ctrl+C during the write never leaves a truncated deliverable.
    tmp = f"{DOC_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    os.replace(tmp, DOC_FILE)
    print(f"✅ '{DOC_FILE}' assembled: {stats['zones']} zone(s), {stats['features']} feature(s), "
          f"{stats['ats']} acceptance test(s).")
    return stats


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "documentation", RUNNER.name,
                   model=RUNNER.configured_model())
    # A residual failReport.md from a previous run must not be mistaken for the current
    # run's: we purge it at startup (same contract as the factory).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # The grids are the reference for the WHOLE pipeline: their absence is an immediate
    # failure (without them, the agents would improvise — exactly what the factory forbids).
    map_grid = load_grid(DOC_MAP_SKILL_FILE)
    zone_grid = load_grid(DOC_ZONE_SKILL_FILE)
    missing_grids = [path for path, text in ((DOC_MAP_SKILL_FILE, map_grid),
                                             (DOC_ZONE_SKILL_FILE, zone_grid))
                     if not text.strip()]
    if missing_grids:
        print(f"❌ Missing or empty grid(s): {', '.join(missing_grids)}.")
        write_fail_report("Documentation grid missing",
                          f"Not found or empty: {', '.join(missing_grids)} — impossible "
                          f"to document without a reference.")
        sys.exit(1)

    # Step S0: scope discovered by PYTHON (deterministic), shown to the human BEFORE
    # paying for a single agent turn.
    code_files, test_files = discover_code_scope()
    if not code_files and not test_files:
        print("❌ No code file found in this directory (extensions searched: "
              + ", ".join(sorted(CODE_EXTENSIONS)) + ").")
        print("   → Launch the documentation from the root of the project to document.")
        write_fail_report("Empty documentation scope",
                          "No code file detected in the current directory.")
        sys.exit(1)

    existing_map = peek_doc_map()
    manual_doc = False
    if os.path.exists(DOC_FILE):
        try:
            with open(DOC_FILE, "r", encoding="utf-8") as f:
                _doc_txt = f.read()
                manual_doc = DOC_MARKER not in _doc_txt and DOC_MARKER_LEGACY not in _doc_txt
        except OSError:
            manual_doc = True

    preview_code = code_files[:15]
    preview_tests = test_files[:5]

    print(f"\n{'='*50}")
    print(f"📚 BEHAVIORAL DOCUMENTATION — Discovered scope:")
    print(f"   Directory: {os.getcwd()}")
    print(f"   {len(code_files)} code file(s) + {len(test_files)} test file(s). Preview:")
    for f in preview_code:
        print(f"      - {f}")
    if len(code_files) > len(preview_code):
        print(f"      … and {len(code_files) - len(preview_code)} other code file(s).")
    if preview_tests:
        print(f"   Tests (source of truth for the « Covered » status):")
        for f in preview_tests:
            print(f"      - {f}")
        if len(test_files) > len(preview_tests):
            print(f"      … and {len(test_files) - len(preview_tests)} other test file(s).")
    context = business_context_file()
    if context:
        print(f"   Business context: '{context}' detected (pointed to the agents as optional reading).")
    else:
        print(f"   Business context: none ('{SPEC_FILE}'/'{NEED_FILE}' absent) — the behavior "
              f"is documented as the code shows it.")
    if existing_map:
        done = documented_count(existing_map)
        print(f"   Resume: existing map ({len(existing_map['zones'])} zone(s)), "
              f"{done}/{len(existing_map['zones'])} zone(s) already documented in '{DOC_DIR}/'.")
        # FRESHNESS (warn-only): a documented zone whose code changed since is probably
        # stale. The human decides — they can delete the listed files NOW, before
        # answering y: only the missing zones are replayed.
        stale_zones = [(zone, stale_zone_sources(zone, zone_path(zone)))
                       for zone in existing_map["zones"]
                       if isinstance(zone, dict) and zone_ok(zone_path(zone))]
        stale_zones = [(zone, stale) for zone, stale in stale_zones if stale]
        if stale_zones:
            print(f"   ⚠️  {len(stale_zones)} documented zone(s) probably STALE "
                  f"(code modified after their documentation):")
            for zone, stale in stale_zones:
                print(f"      - Z{zone.get('id')} « {zone.get('name')} »: {len(stale)} "
                      f"file(s) modified (e.g. {stale[0]}) → delete "
                      f"'{zone_path(zone)}' to re-document it.")
            print(f"      You can delete them now, before validating: only the missing "
                  f"zones will be replayed.")
    zones_label = f"{len(existing_map['zones'])}" if existing_map else "N (determined by the map)"
    print(f"   Flow: 1 mapping (skipped if '{DOC_MAP_FILE}' valid) + {zones_label} documentation "
          f"pass(es) (one per zone, context reset between each) + 1 overview + Python "
          f"assembly → '{DOC_FILE}' (root).")
    if manual_doc:
        print(f"   ⚠️  WARNING: a '{DOC_FILE}' WITHOUT a factory marker exists at the root "
              f"(hand-written documentation?). The final assembly will OVERWRITE it — back it up "
              f"before validating if you want to keep it.")
    print(f"{'='*50}")

    confirm = input("\n▶️  Launch the documentation on this scope? (y/n): ")
    mm_audit.event("gate", id="scope", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print("⏹️  Cancelled by the user.")
        sys.exit(0)

    # Read-only guard: baseline captured BEFORE the first agent.
    init_readonly_guard()

    # Step S1: mapping (LLM only if necessary — file-based resume), doubly validated
    # (Python schema + human y/n, map editable before validating).
    doc_map = run_cartography(map_grid, code_files, test_files)

    # 🚀 Boot the harness Data Center in tmux (no-op if the mapping already launched it).
    RUNNER.start()

    # Step S2: the N documentation passes (a fresh session per zone). The test scope is
    # passed to the content guards (a « Covered » must cite a real test).
    run_doc_passes(zone_grid, doc_map, set(test_files))

    # Step S3: overview (non-blocking: mechanical fallback after 3 failures).
    run_overview(doc_map)

    # Step S4: mechanical assembly of the final deliverable.
    stats = assemble_documentation(doc_map)

    # Last pass of the read-only guard: covers the window between a pass's last enforce
    # and the end of the run (notably the « late deliverable accepted » path).
    enforce_readonly("final")

    # Cleanup of the temporary files and sentinels, then a clean shutdown.
    for tmp_f in [TMP_DOC_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_doc_sentinels()
    RUNNER.kill()
    # Successful run: no failure report must remain.
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    print(f"""
🏁 [CONGRATULATIONS] Behavioral documentation completed!
   📄 Deliverable: '{DOC_FILE}' (root) — {stats['zones']} zone(s), {stats['features']} feature(s),
      {stats['ats']} acceptance test(s) ({stats['covered']} covered, {stats['proposed']} proposed).
   🗂️  Per-zone detail: '{DOC_DIR}/'; zone map: '{DOC_MAP_FILE}'.
   ♻️  To re-document ONE zone (after the code evolves, e.g.): delete its file
      in '{DOC_DIR}/' and relaunch — only the missing one is replayed, the assembly is redone.
      To redo everything (map included): delete '{DOC_DIR}/' and '{DOC_MAP_FILE}' then relaunch.""")
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
