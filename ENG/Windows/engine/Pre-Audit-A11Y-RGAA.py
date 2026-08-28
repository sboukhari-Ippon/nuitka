#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Orchestrator - ACCESSIBILITY PRE-AUDIT factory with an agent harness + tmux (RGAA 4.1.2)
─────────────────────────────────────────────────────────────────────────────────
"A11Y PRE-AUDIT" VARIANT: it writes NO code — it evaluates an EXISTING web interface
against the 106 criteria of RGAA 4.1.2 (13 topics) and delivers a consolidated report
'accessibility_pre_audit_report.md' (C/NC/NA/AVM statuses per criterion, conformance
rate as a range, localized findings) plus a short results summary
('accessibility_pre_audit_summary.md'). It is a static PRE-audit: neither a conformity
audit nor an accessibility declaration.

This applies the MAIsterMind logic — slice the context window per phase to make small or
medium models reliable over time — to a TWO-DIMENSIONAL audit: the reference framework is
too large for one pass (106 criteria) AND so is the code. The split therefore crosses both
axes:
  - REFERENCE axis: 13 thematic "packs" (one grid file per pack,
    './.agents/pipeline/audit-a11y/packs/'), routed by DETERMINISTIC regex triggers
    declared in 'packs.yaml' (no video in the code → the Multimedia pack is never
    paid for: its criteria are declared NA mechanically);
  - CODE axis: an interface map ('a11y_map.yaml', validated by a Python schema THEN by
    the human) splits the UI files into a BASE (audited once), shared COMPONENTS
    (audited once, screens inherit) and screen ZONES.
Each audit pass = ONE pack × ONE compartment, in a fresh session (/new), which
receives ONLY the common trunk of the grid, ITS pack and ITS files.

Pipeline:
  - Step S0: UI scope + pack routing by PYTHON (deterministic, zero LLM),
    contrast measurements on the literal CSS pairs, then human confirmation (y/n)
    BEFORE paying for a single agent turn.
  - Step S1: interface mapping (1 LLM pass, skipped if 'a11y_map.yaml' is valid),
    doubly validated: Python schema (full coverage guaranteed by a mechanical "Misc"
    zone) then human y/n — the map displays the EXACT count of passes before validating.
  - Step S2: N audit passes (one per routed pack × compartment). No executable verdict
    (an audit has neither build nor test): liveness net (3 attempts) +
    verdict PARSER (exact set of the pack's criteria, C/NC/NA/AVM statuses, each
    NC found and localized, coherent Summary) — STRONG structural floor, the parser's
    errors feed the next attempt's feedback.
  - Step S3: executive summary (1 short LLM pass on the aggregated figures,
    non-blocking MECHANICAL fallback — a failed header never invalidates N passes).
  - Step S4: aggregation and 100% Python report (consolidation NC > AVM > C > NA,
    conformance rate as a range — the AVM counted NC for the floor, C for the
    ceiling —, scope/routing/contrast/limits annexes, atomic write) +
    short results summary.

Resume by files, like the other variants: a valid map skips the mapping; a verdicts file
that PASSES THE PARSER skips its pass; the summary, the aggregation and the report are
ALWAYS replayed. To redo a full audit: delete 'pre_audit_a11y/' (and 'a11y_map.yaml' to replay
the map) and relaunch.

READ-ONLY guard (best-effort, if the project is already a git repo): an audit does not
modify the audited project. Any tracked file modified by an auditor is restored
(git checkout) and reported; any file created outside the audit deliverables is reported
(never deleted: decision left to the human). Without git, the ban stays carried by the
prompts (graceful degradation, as everywhere else in the factory).

DELIVERABLE HONESTY: this is an automated STATIC PRE-AUDIT — it does not replace an
enforceable RGAA conformance audit (keyboard tests, screen readers, 200% zoom, real
rendering). Everything the code alone does not prove is explicitly marked AVM
("requires manual verification") and the conformance rate is given as a range.
"""

import os
import re
import sys
import time
import signal
import subprocess
import shutil
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
RUNNER = resolve_runner(os.getcwd(), role="a11y", messages={
    "reuse":    None,
    "boot":     "⏳ Waiting for the {tui} cloud TUI to boot ({wait}s)...",
    "follow":   "   👀 Follow the audit live in another terminal: tmux attach -t {session}",
    "new_warn": "   ⚠️  The TUI may not have been reset ('/new' still literally "
                "on screen): if the run drifts, check with tmux attach.",
})

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
NEED_FILE             = "need.md"
SPEC_FILE             = "spec.md"
A11Y_DIR              = "pre_audit_a11y"                       # intermediate verdicts (one file per pass)
A11Y_MAP_FILE         = "a11y_map.yaml"                    # interface map (the equivalent of the blackboard)
A11Y_REPORT_FILE      = "accessibility_pre_audit_report.md"    # consolidated final deliverable, at the ROOT
A11Y_SUMMARY_FILE = "accessibility_pre_audit_summary.md"  # short results summary, at the ROOT
SYNTHESIS_FILE        = f"{A11Y_DIR}/_synthese.md"         # written header (executive summary)
FAIL_REPORT_FILE      = "failReport.md"                    # persistent stop report (same contract as the factory)
A11Y_TRUNK_SKILL_FILE = "./.agents/pipeline/audit-a11y/SKILL.md"
A11Y_PACKS_FILE       = "./.agents/pipeline/audit-a11y/packs.yaml"
A11Y_PACKS_DIR        = "./.agents/pipeline/audit-a11y/packs"
A11Y_MAP_SKILL_FILE   = "./.agents/pipeline/a11y-map/SKILL.md"
DOC_MAP_FILE          = "doc_map.yaml"                     # documentation pipeline map: optional HINT
AGENT_CONFIG_FILE     = RUNNER.config_file

# Temporary context routing file (offloaded prompt, named by the harness)
TMP_A11Y_FILE         = RUNNER.tmp_file("a11y")

# Buffer file for the prompt sent to the TUI via tmux. RELATIVE path to the project: it's the
# only valid choice on all 3 OSes (Windows has no /tmp).
TMP_PROMPT_BUFFER     = RUNNER.prompt_buffer

# tmux session name, suffixed with a digest of the project directory: two factories
# running on the same machine must NEVER share a session. Prefix DISTINCT from the
# other variants (oc-factory / oc-proto / oc-audit / oc-doc): an accessibility
# audit can coexist with a production, a documentation or a Nielsen audit on ANOTHER
# project with no risk of session collision.
TMUX_SESSION          = RUNNER.session

# Invisible HTML marker of the generated deliverables: it distinguishes a factory report
# (overwritable) from a hand-written document (announced before the y/n, same contract
# as the documentation's DOC_MARKER).
A11Y_MARKER           = "<!-- généré par Pre-Audit-A11Y-RGAA -->"
A11Y_MARKER_LEGACY    = "<!-- généré par MAIsterMind_audit-a11y -->"

MAX_ATTEMPTS          = 3              # Attempts per pass (liveness net + parser)
POLL_INTERVAL         = 2
MAX_PHASE_TIMEOUT     = resolve_timeout("phase", 600)            # 10 min max per pass (safety net)
STABLE_POLLS_FALLBACK = 15             # sentinel-less net: deliverable accepted if it stayed
                                       # stable for N consecutive checks (N × POLL_INTERVAL seconds)

# Audit pass circuit breaker. Passes are INDEPENDENT by construction (distinct
# verdict files and sentinels): a failed pass no longer kills the run — its criteria
# fall back to cautious AVM and the final report is marked PARTIAL. UNLESS the model
# stalls systematically: beyond these thresholds, we stop as before (no point burning
# the remaining turns). An ISOLATED failure never trips the breaker (the ratio is
# only armed from 2 failures on).
MAX_CONSECUTIVE_PASS_FAILURES = 2      # consecutive pass failures → stop
MAX_PASS_FAILURE_RATIO        = 0.30   # share of failures among processed passes → stop

# Grace granted to a sentinel WITHOUT a deliverable (the agent may have created the two
# files in the wrong order): beyond it, the attempt fails right away — an agent that
# answered fast but wrong no longer costs a full timeout.
GRACE_POLLS_AFTER_SENTINEL = 3         # checks (× POLL_INTERVAL seconds)

# Context window bounds (same families as the other variants):
MAX_SCOPE_FILES_IN_CARTO   = 400   # beyond, the scope surplus is summarized per directory
                                   # (assignable PER DIRECTORY: map entry ending with '/')
DIVERS_RETRY_THRESHOLD     = 100   # beyond N files in "Miscellaneous", the map is REPLAYED (as
                                   # long as attempts remain): 697 files in the residual = 28
                                   # slices × 13 packs = 364 passes on an "unclassified"
MAX_BUCKET_FILES_IN_PROMPT = 150   # beyond, a pass's file list is truncated in the prompt
SOFT_MAX_FILES_PER_ZONE    = 25    # warn (non-blocking) beyond — ALIGNED with the mapper
                                   # grid's bound ("25 files maximum per zone"):
                                   # a single truth, the pass risks saturating beyond

# Bounds of the deterministic scan (S0):
MAX_TRIGGER_FILE_BYTES = 512 * 1024  # beyond, only the start of the file is scanned (triggers)
MAX_CONTRAST_PAIRS     = 40          # contrast pairs reported to the Colours pass (worst first)
MAX_TRIGGER_HITS_IN_PROMPT = 20      # pattern hits announced to a pass (PATTERNS block, bounded)
# L7: a compartment is SPLIT into slices. Two bounds, the first one reached cuts:
# a BYTE budget (what actually saturates a context window) and a file cap (the
# model's attention). 25 files per slice, whatever their size, made 509 passes on a
# monorepo whose median file weighs 1.3 KB.
MAX_FILES_PER_PASS = 40
MAX_PASS_BYTES     = 80 * 1024

STATUSES = ("C", "NC", "NA", "AVM")  # the four verdict statuses (display order)


# ─── SENTINELS (AUDITOR → ORCHESTRATOR CHANNEL) ───────────────────────────────
# '.a11y_' prefix DISTINCT from the '.phase_' / '.pipeline_' / '.audit_' / '.doc_' of the
# other variants: a leftover from an old run of another pipeline cannot be taken for
# a signal of this one, and vice versa.

def reset_agent_session():
    """GUARANTEED isolation between passes (L5): kill + start of the harness — cost
    boot_wait (~6 s real, ~4 min over 40 passes), negligible next to passes of
    several minutes. The warn-only /new was the weak link of the "fresh session
    per pass" promise: a failed reset silently contaminates the following
    verdicts. Can be disabled via MM_A11Y_HARD_RESET=0 (back to /new)."""
    if os.environ.get("MM_A11Y_HARD_RESET", "").strip() == "0":
        RUNNER.new_context()
        return
    RUNNER.kill()
    RUNNER.start()


def a11y_sentinel(slot: str, attempt: int) -> str:
    """File written by the auditor at the very end of a pass (signal 'I'm done').

    'slot' identifies the pass ('map', 't11-z3', 't8-socle', 'synthese'…). The attempt
    number is included in the name: a sentinel written late by the agent of a previous
    attempt cannot be taken for the signal of the current attempt.
    """
    return f".a11y_{slot}.attempt{attempt}.done"


def cleanup_slot_sentinels(slot: str):
    """Remove all sentinels (all attempts) of a pass."""
    prefix = f".a11y_{slot}.attempt"
    for name in os.listdir("."):
        if name.startswith(prefix) and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


def cleanup_all_a11y_sentinels():
    """Final cleanup of all leftover audit sentinels."""
    for name in os.listdir("."):
        if name.startswith(".a11y_") and name.endswith(".done"):
            try:
                os.remove(name)
            except OSError:
                pass


# ─── SYNCHRONIZATION VIA FILE MONITOR ─────────────────────────────────────────

def wait_for_deliverable(filepath: str, sentinel: str, timeout: int = MAX_PHASE_TIMEOUT,
                         structural_check=None) -> tuple:
    """Wait for an audit deliverable signaled by a SENTINEL (same contract as the other
    variants' pipeline: the agent creates the .done AFTER saving the deliverable).

    Returns (ok, reason), reason ∈ {"ok", "timeout", "sentinelle_sans_livrable",
    "stable_hors_format"}: callers turn the reason into a DEDICATED feedback
    (an agent that answered fast but wrong no longer costs a full timeout).

    NET for an agent that forgets the sentinel: if the deliverable exists, is non-empty and
    has not moved for STABLE_POLLS_FALLBACK consecutive checks, we accept it with a
    warning (graceful degradation). The optional 'structural_check' hardens this
    net: a stable but off-format deliverable gets a SECOND stability tier
    (the agent may still be writing), then the attempt fails right away.

    A sentinel present WITHOUT a deliverable is an explicit end signal: after a grace
    of GRACE_POLLS_AFTER_SENTINEL checks (the agent may have created the two files
    out of order a second apart), the attempt fails immediately — the faulty
    sentinel is consumed so it does not pollute the next attempt.
    """
    start = time.time()
    print(f"   ⏳ Waiting for '{filepath}' (end signal: '{sentinel}')...")
    stable_streak = 0
    last_size = -1
    structural_warned = False
    sentinel_alone_streak = 0
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
            return True, "ok"
        if os.path.exists(sentinel) and not file_ready:
            sentinel_alone_streak += 1
            if sentinel_alone_streak >= GRACE_POLLS_AFTER_SENTINEL:
                try:
                    os.remove(sentinel)
                except OSError:
                    pass
                print(f"   ⛔ Sentinel '{sentinel}' present but deliverable absent or empty: "
                      f"immediate attempt failure (no point waiting for the timeout).")
                return False, "sentinelle_sans_livrable"
            continue
        sentinel_alone_streak = 0
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
                    if stable_streak >= 2 * STABLE_POLLS_FALLBACK:
                        print(f"   ⛔ '{filepath}' stayed stable AND off-format for "
                              f"{2 * STABLE_POLLS_FALLBACK * POLL_INTERVAL}s: immediate "
                              f"attempt failure (the agent finished off-format).")
                        return False, "stable_hors_format"
                    continue
                print(f"   ⚠️  Sentinel '{sentinel}' absent but '{filepath}' has been stable for "
                      f"{STABLE_POLLS_FALLBACK * POLL_INTERVAL}s: deliverable accepted (safety net).")
                return True, "ok"
    return False, "timeout"


# ─── CUMULATIVE RETRY FEEDBACK ────────────────────────────────────────────────
# An attempt's feedback used to be overwritten each turn: if attempt 1 failed on
# "missing criterion" and attempt 2 on "inconsistent Summary", attempt 3's prompt no
# longer mentioned the criterion — the model could reintroduce the fixed error.
# Each loop now accumulates its failures and the composer bounds the reminder.

MAX_PREVIOUS_ERRORS_IN_FEEDBACK = 4    # previous errors reminded at most
MAX_PREVIOUS_ERROR_CHARS        = 200  # size of each reminder (summary, not the detail)


def compose_retry_feedback(error_history: list) -> str:
    """An attempt's feedback block: the LAST attempt's error in detail, then the
    DISTINCT errors of earlier attempts as a bounded summary (not to be
    reintroduced). PURE function (unit-tested)."""
    if not error_history:
        return "First pass — no previous feedback."
    feedback = error_history[-1]
    older = []
    for err in error_history[:-1]:
        short = " ".join(str(err).split())[:MAX_PREVIOUS_ERROR_CHARS]
        if short and short not in older:
            older.append(short)
    older = older[-MAX_PREVIOUS_ERRORS_IN_FEEDBACK:]
    if older:
        feedback += ("\nErrors already met in previous attempts, NOT to be "
                     "reintroduced:\n" + "\n".join(f"- {o}" for o in older))
    return feedback


# ─── LIGHT STRUCTURAL FLOORS & NAMING TOOLS ───────────────────────────────────
# The LIGHT floor serves as structural_check for wait_for_deliverable (sections present);
# the STRONG check is the verdict parser (below), replayed after each delivery.

def findings_structural_check(path: str) -> bool:
    """Minimal structural floor of a verdicts file: its mandatory sections
    '## Verdicts' and '## Summary' must be present (a half-written file — or
    off-format chatter — stops before)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().lower()
        return "## verdicts" in content and "## summary" in content
    except OSError:
        return False


def synthesis_structural_check(path: str) -> bool:
    """Minimal structural floor of the summary: it starts with its title."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip().lower().startswith("## executive summary")
    except OSError:
        return False


def map_structural_check(path: str) -> bool:
    """Minimal structural floor of the map: parsable YAML AND non-empty zones.
    Serves as structural_check for wait_for_deliverable during mapping."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return isinstance(data, dict) and isinstance(data.get("zones"), list) and bool(data["zones"])
    except (OSError, yaml.YAMLError):
        return False


def slugify(name: str) -> str:
    """File slug derived by PYTHON (never by the model — one error source
    fewer): lowercase, transliterated accents, kebab-case."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "zone"


# ─── GRIDS: LOADING ───────────────────────────────────────────────────────────
# The common trunk (SKILL.md) and the mapper's grid are sent WHOLE; a pass's
# context "slice" comes from its PACK (one dedicated file per topic) and from its
# COMPARTMENT (the map's files) — the split is carried by the file structure,
# not by a text slicing.

def load_grid(path: str) -> str:
    """Load a grid (SKILL.md, pack). Its absence is an IMMEDIATE failure: without
    a grid, the auditors would improvise — exactly what the factory forbids."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def pack_grid_path(pack: dict) -> str:
    """Path of a pack's grid file (computed, never provided by the manifest)."""
    return f"{A11Y_PACKS_DIR}/T{pack['id']:02d}-{pack['slug']}.md"


def load_packs_manifest() -> tuple:
    """Load and validate 'packs.yaml' (the routing manifest). Returns (packs, fatal).

    The manifest is DATA distributed next to the binary (the scripts are shipped
    compiled): its validation is therefore as strict as that of a map produced by
    an LLM — a hand-edited manifest must never derail the run in
    silence. Fatal: invalid structure, inconsistent id/slug/criteres, uncompilable
    regex, missing pack file.
    """
    fatal = []
    try:
        with open(A11Y_PACKS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return [], [f"'{A11Y_PACKS_FILE}' unreadable or invalid YAML: {str(e)[:300]}"]

    if not isinstance(data, dict) or not isinstance(data.get("packs"), list) or not data["packs"]:
        return [], [f"'{A11Y_PACKS_FILE}': 'packs' block missing or empty."]

    packs, seen_ids, seen_slugs = [], set(), set()
    for idx, raw in enumerate(data["packs"]):
        if not isinstance(raw, dict):
            fatal.append(f"packs[{idx}] is not a mapping.")
            continue
        try:
            pack_id = int(raw.get("id"))
        except (TypeError, ValueError):
            fatal.append(f"packs[{idx}].id missing or not an integer.")
            continue
        slug = str(raw.get("slug") or "").strip()
        if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
            fatal.append(f"pack T{pack_id}: slug missing or invalid (expected: kebab-case).")
            continue
        if pack_id in seen_ids or slug in seen_slugs:
            fatal.append(f"pack T{pack_id} '{slug}': duplicate id or slug.")
            continue
        seen_ids.add(pack_id)
        seen_slugs.add(slug)
        nom = str(raw.get("nom") or "").strip() or slug
        criteres = [str(c).strip() for c in (raw.get("criteres") or []) if str(c).strip()]
        if not criteres:
            fatal.append(f"pack T{pack_id} '{slug}': empty 'criteres' list.")
            continue
        bad = [c for c in criteres if not c.startswith(f"{pack_id}.")]
        if bad:
            fatal.append(f"pack T{pack_id} '{slug}': criterion(s) outside the topic: {', '.join(bad)}.")
            continue
        regexes = []
        for pattern in (raw.get("declencheurs") or []):
            try:
                regexes.append(re.compile(str(pattern), re.IGNORECASE))
            except re.error as e:
                fatal.append(f"pack T{pack_id} '{slug}': uncompilable trigger '{pattern}' ({e}).")
        # NC probes: quasi-certain hints detectable by regex (OPTIONAL block).
        # Compiled here, validated like the rest of the manifest; never a verdict.
        sondes = []
        for raw_sonde in (raw.get("sondes") or []):
            motif = str((raw_sonde or {}).get("motif") or "")
            crit_s = str((raw_sonde or {}).get("critere") or "").strip()
            conf = str((raw_sonde or {}).get("confiance") or "").strip()
            try:
                compiled = re.compile(motif, re.IGNORECASE)
            except re.error as e:
                fatal.append(f"pack T{pack_id} '{slug}': uncompilable probe '{motif}' ({e}).")
                continue
            if crit_s not in criteres:
                fatal.append(f"pack T{pack_id} '{slug}': probe on criterion outside the pack ({crit_s}).")
                continue
            if conf not in ("quasi-certain", "probable", "candidat"):
                fatal.append(f"pack T{pack_id} '{slug}': probe confidence outside the enum ({conf}).")
                continue
            sondes.append({"regex": compiled, "motif": motif, "critere": crit_s,
                           "confiance": conf})
        # Per-criterion testability: MANDATORY data (copied from the grids' suffixes).
        # It is what makes the iron rule enforceable ("a C on a manual criterion is
        # not provable statically" — requalified at aggregation).
        testabilite = raw.get("testabilite")
        if not isinstance(testabilite, dict):
            fatal.append(f"pack T{pack_id} '{slug}': 'testabilite' block missing "
                         f"(a mapping criterion → static|partial|manual).")
            testabilite = {}
        else:
            testabilite = {str(k).strip(): str(v).strip() for k, v in testabilite.items()}
            missing_t = [c for c in criteres if c not in testabilite]
            extra_t = [c for c in testabilite if c not in criteres]
            bad_values = sorted({v for v in testabilite.values()
                                 if v not in ("static", "partial", "manual")})
            if missing_t:
                fatal.append(f"pack T{pack_id} '{slug}': testability missing for "
                             f"{', '.join(missing_t)}.")
            if extra_t:
                fatal.append(f"pack T{pack_id} '{slug}': testability for criterion(s) "
                             f"outside the pack: {', '.join(extra_t)}.")
            if bad_values:
                fatal.append(f"pack T{pack_id} '{slug}': testability outside the enum "
                             f"(expected static|partial|manual): {', '.join(bad_values)}.")
        pack = {
            "id": pack_id,
            "slug": slug,
            "nom": nom,
            "criteres": criteres,
            "toujours": bool(raw.get("toujours")),
            "testabilite": testabilite,
            "sondes": sondes,
            "regexes": regexes,
            "grid_path": "",
            "grid_text": "",
        }
        pack["grid_path"] = pack_grid_path(pack)
        pack["grid_text"] = load_grid(pack["grid_path"])
        if not pack["grid_text"].strip():
            fatal.append(f"pack T{pack_id} '{slug}': grid missing or empty ('{pack['grid_path']}').")
        packs.append(pack)

    packs.sort(key=lambda p: p["id"])

    # WHOLE-SET checks (each pack is already validated in isolation): a criterion declared
    # in TWO packs would be audited and consolidated twice — fatal. A union that
    # does not reach the 106 criteria of RGAA 4.1.2 is possible (manifest edited or
    # deliberately trimmed) but never silent: console warning, and the report's
    # "Method and limits" annex displays the count actually audited.
    owner_by_criterion = {}
    for pack in packs:
        for crit in pack["criteres"]:
            if crit in owner_by_criterion:
                fatal.append(f"criterion {crit} declared twice "
                             f"(pack T{owner_by_criterion[crit]} then pack T{pack['id']}): "
                             f"it would be audited and consolidated twice.")
            else:
                owner_by_criterion[crit] = pack["id"]
    version = str(data.get("version") or "").strip()
    if not fatal and version == "rgaa-4.1.2" and len(owner_by_criterion) != 106:
        print(f"⚠️  Manifest '{A11Y_PACKS_FILE}': {len(owner_by_criterion)} criterion(s) in "
              f"total instead of the 106 of RGAA 4.1.2 (manifest edited?). Non-blocking, but "
              f"the audit will cover ONLY these criteria (noted in the report's annex).")
    return packs, fatal


# ─── SCOPE DISCOVERY (PYTHON, DETERMINISTIC, ZERO LLM) ────────────────────────
# The scope is established by the orchestrator, never by an agent: a stable,
# reproducible list, shown to the human BEFORE paying for a single LLM turn.
# Compared to the Nielsen audit, the list ALSO covers server templates
# (.php, .erb, .jsp, .liquid…): a PHP/Rails site has an interface to audit.

UI_EXTENSIONS = {".html", ".htm", ".css", ".scss", ".sass", ".less",
                 ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                 ".vue", ".svelte", ".astro", ".ejs", ".hbs", ".njk", ".twig",
                 ".php", ".erb", ".jsp", ".liquid", ".mustache", ".pug",
                 ".haml", ".slim", ".cshtml", ".razor"}

# Directories excluded by NAME; any hidden directory ('.git', '.agents', '.opencode'/'.codex',
# '.venv', '.next'…) is excluded outright by the walk's startswith('.') filter.
# End-to-end test directories (e2e, cypress, playwright) contain .ts/.js that are NOT
# the interface: excluded.
EXCLUDED_DIR_NAMES = {"node_modules", "dist", "build", "out", "coverage", "target",
                      "vendor", "__pycache__", "e2e", "cypress", "playwright", A11Y_DIR}

# MECHANICALLY out of scope (traced in the report's appendix, never silent):
# - third-party assets shipped with the project (DSFR design system distributed in public/,
#   legacy bundles, icon sheets): auditing the library is not auditing the project — its
#   overrides, though, live in src/ and stay in scope;
# - PURE LOGIC files (.ts/.js with no markup, no DOM, no ARIA): a service, an API route,
#   a utility are not an interface. Markup-bearing extensions (.tsx, .jsx, .vue, .html…)
#   ALWAYS stay in scope, even without a signal (a page that merely composes components
#   is one).
VENDOR_PATH_RE = re.compile(r"(^|/)(public|static|assets)/|/dsfr/|/vendors?/|\.legacy\.|\.bundle\.",
                            re.IGNORECASE)
LOGIC_EXTENSIONS = {".ts", ".js", ".mjs", ".cjs"}
UI_SIGNAL_RE = re.compile(
    r"<(div|span|a|p|img|svg|button|input|form|label|select|option|textarea|table|thead|tbody|"
    r"tr|td|th|ul|ol|li|nav|header|footer|main|section|article|aside|h[1-6]|iframe|video|audio|"
    r"dialog|details|summary|fieldset|legend|canvas|figure|picture|source|template)\b"
    r"|<[A-Z][A-Za-z0-9]*(\s[a-zA-Z]|\s*/>)"           # JSX component with an attribute or self-closing
    r"|\baria-[a-z]+|\brole\s*=|\bclassName\b|\bhtmlFor\b|\btabIndex\b|\btabindex\b"
    r"|\bdangerouslySetInnerHTML\b|\binnerHTML\b|\bdocument\.|\bcreateElement\b"
    r"|\bquerySelector|\bgetElementBy|\baddEventListener\b|\.classList\b|\.setAttribute\b"
    r"|\bonClick\b|\bonKeyDown\b|\.focus\(\)", re.IGNORECASE)
# Exclusions of the latest scope discovery (for the S0 screen and the appendix).
SCOPE_EXCLUSIONS = {"vendor": [], "logic": []}


def is_vendor_asset(rel_path: str) -> bool:
    """Does the path designate a shipped third-party asset (design system, legacy bundle…)?"""
    return bool(VENDOR_PATH_RE.search(rel_path))


def is_logic_without_ui_signal(rel_path: str) -> bool:
    """File with a LOGIC extension (.ts/.js/.mjs/.cjs) and no interface signal at all
    (tag, JSX component, ARIA, DOM) in its content. A markup-bearing extension
    (.tsx, .vue, .html…) is never concerned."""
    if os.path.splitext(rel_path)[1].lower() not in LOGIC_EXTENSIONS:
        return False
    return not UI_SIGNAL_RE.search(read_file_prefix(rel_path))


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


def discover_ui_scope() -> list:
    """Sorted list (relative paths, '/' separator) of UI files to audit.

    Third-party assets and pure logic without an interface signal are set aside
    MECHANICALLY and recorded in SCOPE_EXCLUSIONS (S0 screen + report appendix):
    an exclusion must always be reviewable and contestable."""
    scope = []
    SCOPE_EXCLUSIONS["vendor"] = []
    SCOPE_EXCLUSIONS["logic"] = []
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
            if is_vendor_asset(rel):
                SCOPE_EXCLUSIONS["vendor"].append(rel)
                continue
            if is_logic_without_ui_signal(rel):
                SCOPE_EXCLUSIONS["logic"].append(rel)
                continue
            scope.append(rel)
    SCOPE_EXCLUSIONS["vendor"].sort()
    SCOPE_EXCLUSIONS["logic"].sort()
    return sorted(scope)


def summarize_by_directory(files: list, max_lines: int = 60) -> str:
    """Per-directory summary (the most populated first, bounded to `max_lines`)."""
    counts = {}
    for f in files:
        d = os.path.dirname(f) or "."
        counts[d] = counts.get(d, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"- {d}/: {n} file(s)" for d, n in ordered[:max_lines]]
    if len(ordered) > max_lines:
        lines.append(f"- (+ {len(ordered) - max_lines} other directory/directories)")
    return "\n".join(lines)


def business_context_file() -> str:
    """Available business context file ('spec.md' first, otherwise 'need.md'),
    or empty string. The audit does NOT need it to run: it's an optional bonus."""
    for candidate in (SPEC_FILE, NEED_FILE):
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


def business_context_hint() -> str:
    """OPTIONAL pointer to the business context: we never inline the spec in the
    audit prompt (context window), we only indicate where to find it."""
    context = business_context_file()
    if context:
        return (f"The file '{context}' (business context) exists at the root: consult it "
                f"ONLY if a flow is incomprehensible to you without it (save your context).")
    return "(no business context file detected: audit the interface as it presents itself)"


# ─── TRIGGER SCAN (PYTHON, DETERMINISTIC, ZERO LLM) ───────────────────────────
# Pack routing is computed by the code, never by an agent: each UI file
# is read ONCE and checked against the manifest's regexes. Result: file → triggered
# packs, the basis of the pass count shown to the human BEFORE paying.

def read_file_prefix(path: str, limit: int = MAX_TRIGGER_FILE_BYTES) -> str:
    """Read (at most 'limit' bytes of) 'path' tolerating exotic encodings:
    an unreadable byte must never bring down the deterministic scan."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def scan_triggers(scope_files: list, packs: list) -> tuple:
    """Check each scope file against each pack's triggers.

    Returns (triggers, hits):
    - triggers: {path: set(triggered pack ids)} (files with no match included,
      with an empty set: the scope coverage stays visible);
    - hits: {(pack id, path): (line, pattern)} — the FIRST match per
      (pack, file), kept to anchor the passes (the prompt's DETECTED PATTERNS
      block) and to document the suspicious-passes annex. Zero cost: regex.search
      already computed the match object, we read it instead of discarding it.
    """
    triggers, hits = {}, {}
    for path in scope_files:
        content = read_file_prefix(path)
        found = set()
        if content:
            for pack in packs:
                for regex in pack["regexes"]:
                    match = regex.search(content)
                    if match:
                        found.add(pack["id"])
                        hits[(pack["id"], path)] = (
                            content.count("\n", 0, match.start()) + 1, regex.pattern)
                        break
        triggers[path] = found
    return triggers, hits


def scan_sondes(scope_files: list, packs: list) -> dict:
    """Checks each scope file against the packs' NC PROBES (H3).
    Returns {(pack id, path): [(line, motif, criterion, confidence), …]} — the
    FIRST match of each probe per file. A HINT, never a verdict."""
    hits = {}
    for path in scope_files:
        content = read_file_prefix(path)
        if not content:
            continue
        for pack in packs:
            for sonde in pack["sondes"]:
                match = sonde["regex"].search(content)
                if match:
                    line = content.count("\n", 0, match.start()) + 1
                    hits.setdefault((pack["id"], path), []).append(
                        (line, sonde["motif"], sonde["critere"], sonde["confiance"]))
    return hits


# ─── CONTRAST MEASUREMENTS (PURE PYTHON, HINT FOR THE COLORS PASS) ────────────
# SAFE subset only: the literal color / background(-color) pairs
# declared in the SAME CSS block. Everything else (variables, themes, inheritance, images)
# is up to the agent (and most often an AVM status). Never an automatic verdict:
# a numeric HINT provided to the Colours pass and reused in the report's annex.

CSS_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "orange": (255, 165, 0), "purple": (128, 0, 128), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "silver": (192, 192, 192), "maroon": (128, 0, 0),
    "navy": (0, 0, 128), "teal": (0, 128, 128), "olive": (128, 128, 0),
    "lime": (0, 255, 0), "aqua": (0, 255, 255), "cyan": (0, 255, 255),
    "fuchsia": (255, 0, 255), "magenta": (255, 0, 255),
}

CSS_DECL_RE = re.compile(r"(?:^|[;{\s])(color|background-color|background)\s*:\s*([^;}{]+)",
                         re.IGNORECASE)


def parse_css_color(token: str):
    """Convert a CSS literal into (r, g, b), or None if non-literal / non-opaque.
    Covers: #rgb, #rrggbb, rgb(), rgba() at alpha 1, and the basic named colors.
    Semi-transparent rgba() are EXCLUDED (the effective color depends on the background)."""
    token = str(token).strip().strip(";").strip().lower()
    match = re.fullmatch(r"#([0-9a-f]{3})", token)
    if match:
        h = match.group(1)
        return tuple(int(c * 2, 16) for c in h)
    match = re.fullmatch(r"#([0-9a-f]{6})", token)
    if match:
        h = match.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    match = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([0-9.]+)\s*)?\)", token)
    if match:
        r, g, b = (min(int(match.group(i)), 255) for i in (1, 2, 3))
        alpha = match.group(4)
        if alpha is not None and float(alpha) < 1.0:
            return None
        return (r, g, b)
    return CSS_NAMED_COLORS.get(token)

def relative_luminance(rgb: tuple) -> float:
    """WCAG relative luminance of an sRGB color (official formula)."""
    def channel(value):
        c = value / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb1: tuple, rgb2: tuple) -> float:
    """WCAG contrast ratio between two colors (1.0 to 21.0)."""
    l1, l2 = relative_luminance(rgb1), relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def measure_css_contrasts(scope_files: list) -> list:
    """Measures the LITERAL color/background pairs of a single CSS block in the scope's
    stylesheets. Returns a list of dicts sorted from worst to best ratio,
    capped at MAX_CONTRAST_PAIRS. Best-effort by design: naive parser over '{...}' blocks,
    silent on anything it does not understand (never blocking)."""
    results = []
    css_files = [f for f in scope_files
                 if os.path.splitext(f)[1] in (".css", ".scss", ".sass", ".less")]
    for path in css_files:
        content = read_file_prefix(path)
        if not content:
            continue
        # Naive split into blocks: 'selector { declarations }'. Nested at-rules
        # (@media) leave fragments with no declarations: harmless.
        for block_match in re.finditer(r"([^{}]{1,400})\{([^{}]*)\}", content):
            selector = " ".join(block_match.group(1).split())[-120:]
            body = block_match.group(2)
            color, background = None, None
            for decl in CSS_DECL_RE.finditer(body):
                prop = decl.group(1).lower()
                value = decl.group(2)
                if prop == "color":
                    color = parse_css_color(value)
                else:
                    # 'background' shorthand: keep only if the WHOLE value is a
                    # literal color (a shorthand with image/position is ignored).
                    parsed = parse_css_color(value)
                    if parsed is not None:
                        background = parsed
            if color is not None and background is not None:
                line = content[:block_match.start()].count("\n") + 1
                results.append({
                    "file": path,
                    "line": line,
                    "selector": selector,
                    "ratio": round(contrast_ratio(color, background), 2),
                })
    results.sort(key=lambda r: r["ratio"])
    return results[:MAX_CONTRAST_PAIRS]


def build_contrast_block(contrasts: list) -> str:
    """The "CONTRAST MEASUREMENTS" block injected into the Colours pass prompt (and
    reproduced in the report appendix). Empty string if no pair measured."""
    if not contrasts:
        return ""
    lines = ["Literal color/background pairs measured mechanically (WCAG ratio; thresholds: "
             "4.5:1 body text, 3:1 large text and components) — these measurements are "
             "authoritative for THESE pairs; everything else is up to your analysis:"]
    for c in contrasts:
        lines.append(f"- {c['ratio']}:1 — {c['file']}:{c['line']} ({c['selector']})")
    return "\n".join(lines)


# ─── READ-ONLY GUARD (GIT, BEST-EFFORT) ───────────────────────────────────────
# "Python verifies what is verifiable": forbidding modification of the audited project
# is carried by the prompts (unverifiable alone) AND by this mechanical diff when a git
# repository preexists. Like the Nielsen audit: NEVER a 'git init' nor a commit — an audit
# must leave NO trace in the audited project beyond its deliverables.

_GIT = {"enabled": False, "baseline_untracked": set(), "baseline_dirty": set()}

# Identity passed to every command: the factory must not depend on the machine's git config.
GIT_IDENTITY = ["-c", "user.name=MAIsterMind", "-c", "user.email=factory@local"]


def run_git(args: list, timeout: int = 60) -> tuple:
    """Runs a git command. Returns (ok, stripped stdout). Never raises."""
    try:
        proc = subprocess.run(["git"] + GIT_IDENTITY + args,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip()
    except Exception:
        return False, ""


# Deliverables and artifacts of the AUDIT itself: the only files the auditor is
# allowed to produce — never restored nor flagged by the read-only guard.
_A11Y_BASENAMES = {A11Y_REPORT_FILE, A11Y_SUMMARY_FILE, A11Y_MAP_FILE,
                   FAIL_REPORT_FILE, TMP_A11Y_FILE, TMP_PROMPT_BUFFER,
                   os.path.basename(__file__)}


def is_a11y_artifact(path: str) -> bool:
    """Is 'path' an audit deliverable/artifact (and not a file of the audited project)?"""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if not p:
        return False
    segments = p.split("/")
    base = segments[-1]
    if base in _A11Y_BASENAMES:
        return True
    if segments[0] == A11Y_DIR:
        return True
    # Sentinels and ephemeral buffers, wherever they are in the tree.
    if base.startswith(".a11y_") and base.endswith(".done"):
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
    """Activates the read-only guard if (and only if) the project is ALREADY a git repository.

    TWO baselines are captured now, BEFORE the first agent:
      - preexisting untracked files: without this baseline, the user's untracked
        files would be flagged at every pass as "created by the
        auditor" (permanent false positive);
      - tracked files ALREADY MODIFIED (dirty worktree): without this baseline, the
        'git checkout' restore would DESTROY uncommitted human work predating
        the audit — unacceptable. These files leave the guard for the whole run
        (assumed trade-off: never destroying human work prevails over the guard).
    """
    if shutil.which("git") is None or not os.path.isdir(".git"):
        print("ℹ️  No preexisting git repository: the mechanical read-only guard is inactive "
              "(forbidding modification of the project remains carried by the prompts).")
        return
    _GIT["enabled"] = True
    ok, out = run_git(["ls-files", "--others", "--exclude-standard"])
    if ok:
        _GIT["baseline_untracked"] = {line.strip() for line in out.splitlines() if line.strip()}
    ok_dirty, dirty_out = run_git(["diff", "--name-only", "HEAD"])
    if ok_dirty:
        _GIT["baseline_dirty"] = {line.strip() for line in dirty_out.splitlines() if line.strip()}
    print("✓ Git repository detected: read-only guard active (any tracked file modified by "
          "an auditor will be restored).")
    dirty_project = sorted(f for f in _GIT["baseline_dirty"] if not is_a11y_artifact(f))
    if dirty_project:
        print(f"   ⚠️  {len(dirty_project)} file(s) already modified BEFORE the audit (work in "
              f"progress?): they are excluded from the guard (never restored by default) — "
              f"{', '.join(dirty_project[:10])}{'…' if len(dirty_project) > 10 else ''}")


def enforce_readonly(label: str):
    """Restores the TRACKED files modified during a pass and flags files created
    outside audit deliverables (best-effort, after EVERY pass).

    Restore by default for modifications (an audit does not fix); simple
    FLAGGING for creations (we never delete a file we did not create:
    decision left to the human).
    """
    if not _GIT["enabled"]:
        return
    ok_diff, diff_out = run_git(["diff", "--name-only", "HEAD"])
    # 'baseline_dirty' excluded from restoration: a file already modified BEFORE the audit
    # carries uncommitted human work — restoring it would DESTROY it (cf. init).
    touched = sorted(f for f in diff_out.splitlines()
                     if f.strip() and not is_a11y_artifact(f.strip())
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
            if not is_a11y_artifact(f))
        if strays:
            print(f"⚠️  [{label}] File(s) created outside audit deliverables (not deleted, "
                  f"to inspect): {', '.join(strays)}")


# ─── FAILURE REPORT & FAILURE MESSAGE ─────────────────────────────────────────


# Shared state for the failure report: the list of built passes (known after
# the map) — enables a failReport indexed on real progress.
_RUN_STATE = {"passes": []}


def audited_count() -> int:
    """Number of passes whose verdicts file is already usable."""
    return sum(1 for p in _RUN_STATE["passes"] if findings_ok(p["findings_path"], p["pack"]))


def write_fail_report(title: str, reason: str, details: str = ""):
    """Writes a persistent stop report at the root (same contract as the factory:
    every NON-nominal stop produces one). Best-effort: NEVER raises."""
    try:
        lines = ["# Failure report — MAIsterMind (accessibility audit)", "",
                 f"## {title}", "", "### Cause", reason.strip(), ""]
        passes = _RUN_STATE["passes"]
        if passes:
            lines.append("### Progress")
            lines.append(f"- Usable audit passes: {audited_count()}/{len(passes)}")
            for p in passes:
                mark = "✅" if findings_ok(p["findings_path"], p["pack"]) else "⏳"
                lines.append(f"  - {mark} {p['label']}")
            lines.append("")
        if details.strip():
            lines.append("### Details")
            lines.append(details.strip()[:4000])
            lines.append("")
        lines.append("### Recommended action")
        lines.append("Fix the cause above (or bring in a model one notch higher via /model or "
                     f"'{AGENT_CONFIG_FILE}'), then relaunch: the already-usable passes "
                     "will be resumed automatically.")
        with open(FAIL_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"   🧾 Failure report written to '{FAIL_REPORT_FILE}'.")
    except Exception:
        pass


def fail_a11y(message: str, details: str = "", title: str = "Audit pass failure"):
    """Single exit point for failures. Always kills the tmux session BEFORE quitting:
    an exit that leaves the agent alive lets it finish writing its deliverable AFTER
    the orchestrator gives up (misleading resume state on relaunch)."""
    # Closing the run journal on the failure side. NOT in write_fail_report here:
    # the audit also calls it for the PARTIAL report, which does not quit the run.
    mm_audit.end("failed")
    print(message)
    write_fail_report(title, message, details)
    RUNNER.kill()
    sys.exit(1)


def print_pass_failure(label: str, reason: str):
    model = RUNNER.configured_model()
    done = audited_count()
    total = len(_RUN_STATE["passes"]) or "?"
    print(f"""
{'='*60}
❌ The "{label}" pass did not succeed after {MAX_ATTEMPTS} attempts.

   Cause: {reason}

💡 The current model ({model}) is stuck on this pass (often a tool-calling
   problem: the verdicts file or the sentinel are never created, or the
   locked format is not respected).
   Most effective: relaunch after bringing in a model one notch above,
   either via /model in the TUI, or in '{AGENT_CONFIG_FILE}'.

   No stress: the {done}/{total} already-usable pass(es) will be resumed
   automatically, you don't start from scratch. See you soon! 🚀
{'='*60}
""")


# ─── STEP S1: MAPPING — SCHEMA VALIDATION (PYTHON) ────────────────────────────

def norm_rel(path) -> str:
    """Normalizes a path provided by the model to the scope format
    (relative, '/' separator, without './')."""
    p = str(path).strip().strip("'\"`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _normalize_bucket_block(a11y_map: dict, key: str, label: str, soft: list) -> dict:
    """Normalizes the 'socle' or 'composants' block of the map: mapping {intent, files}.
    Absent or malformed → empty block (soft), never fatal: these buckets are optional."""
    block = a11y_map.get(key)
    if not isinstance(block, dict):
        if block is not None:
            soft.append(f"Block '{key}' malformed: replaced by an empty block.")
        block = {}
    files = block.get("files")
    block = {"intent": str(block.get("intent") or f"({label} not provided)").strip(),
             "files": files if isinstance(files, list) else []}
    a11y_map[key] = block
    return block


def validate_and_normalize_a11y_map(a11y_map, scope_files: list) -> tuple:
    """Checks and normalizes the map. Returns (fatal, soft) and MUTATES a11y_map in place.

    The map comes from a small fallible LLM; two classes of problems (same family as
    validate_and_normalize_doc_map from the documentation pipeline):
      - fatal: STRUCTURAL gaps (not a mapping, missing zones, zone without id/name,
        duplicate ids — shared sentinels —, zone where NONE of the listed files exists).
      - soft: gaps recovered MECHANICALLY here (invented paths removed, assignment
        duplicates deduplicated — first bucket wins —, coverage completed
        by a "Miscellaneous" zone, intent/project filled in): flagged, never blocking.
    The model proposes, the code verifies, the human decides (at the y/n that follows).
    """
    fatal, soft = [], []
    if not isinstance(a11y_map, dict):
        return ["The map is not a valid YAML mapping."], []
    zones = a11y_map.get("zones")
    if not isinstance(zones, list) or not zones:
        return ["Block 'zones' missing or empty: nothing to audit per screen."], []

    if not str(a11y_map.get("project") or "").strip():
        a11y_map["project"] = os.path.basename(os.getcwd()) or "Project"
        soft.append(f"Field 'project' missing: filled in with \"{a11y_map['project']}\" (display only).")

    socle = _normalize_bucket_block(a11y_map, "socle", "base", soft)
    composants = _normalize_bucket_block(a11y_map, "composants", "components", soft)

    scope = set(scope_files)
    seen_paths = {}   # path -> label of the first bucket that assigns it
    seen_ids = set()

    def absorb_files(entries, owner_label):
        """Filters a list of paths: out-of-scope removed, duplicates deduplicated.
        A DIRECTORY entry (path ending with '/') expands to every scope file it
        contains, not yet assigned (mapping a monorepo without copying thousands of
        paths — and without the surplus falling into "Miscellaneous")."""
        kept, removed = [], []
        for entry in entries or []:
            p = norm_rel(entry)
            expanded = expand_dir_entry(p, scope_files, seen_paths)
            if expanded:
                for f in expanded:
                    seen_paths[f] = owner_label
                    kept.append(f)
                continue
            if p not in scope:
                removed.append(p)
                continue
            if p in seen_paths:
                soft.append(f"'{p}' assigned to several buckets: kept in "
                            f"{seen_paths[p]} (first assignment), removed from {owner_label}.")
                continue
            seen_paths[p] = owner_label
            kept.append(p)
        return kept, removed

    for key, block, label in (("socle", socle, "the base"), ("composants", composants, "the components")):
        kept, removed = absorb_files(block["files"], label)
        block["files"] = kept
        if removed:
            shown = ", ".join(removed[:10]) + ("…" if len(removed) > 10 else "")
            soft.append(f"Bucket '{key}': {len(removed)} out-of-scope path(s) "
                        f"removed mechanically ({shown}).")

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
            fatal.append(f"zones[].id duplicated ({zone['id']}): the sentinels of this zone's "
                         f"passes would be SHARED between two zones.")
        seen_ids.add(zone["id"])
        if not str(zone.get("name") or "").strip():
            fatal.append(f"zones[{idx}].name missing.")
            continue
        zone["name"] = str(zone["name"]).strip()
        if not str(zone.get("intent") or "").strip():
            zone["intent"] = "(not provided)"
            soft.append(f"Zone Z{zone['id']} \"{zone['name']}\": 'intent' missing (filled in).")

        declared = len(zone.get("files") or []) if isinstance(zone.get("files"), list) else 0
        kept, removed = absorb_files(zone.get("files") if isinstance(zone.get("files"), list) else [],
                                     f"Z{zone['id']}")
        zone["files"] = kept
        if removed:
            shown = ", ".join(removed[:10]) + ("…" if len(removed) > 10 else "")
            soft.append(f"Zone Z{zone['id']} \"{zone['name']}\": {len(removed)} out-of-scope "
                        f"path(s) removed mechanically ({shown}).")
        if declared and not zone["files"]:
            fatal.append(f"Zone Z{zone['id']} \"{zone['name']}\": NONE of the listed files "
                         f"exists in the scope (invented paths?).")
        elif not declared:
            # An empty "Miscellaneous" is not a fault: the prompt asks NOT to copy the
            # surplus into it (coverage fills it) — rejecting it contradicted the instruction.
            if slugify(zone["name"]) == "miscellaneous":
                soft.append(f"Zone Z{zone['id']} \"{zone['name']}\" declared empty: completed "
                            f"by the coverage check (or removed if nothing remains).")
            else:
                fatal.append(f"Zone Z{zone['id']} \"{zone['name']}\": no file assigned.")
        if len(zone["files"]) > SOFT_MAX_FILES_PER_ZONE:
            soft.append(f"Zone Z{zone['id']} \"{zone['name']}\": {len(zone['files'])} files "
                        f"(> {SOFT_MAX_FILES_PER_ZONE}) — this zone's passes may "
                        f"saturate their window; re-split the map before validating if possible.")

    if fatal:
        return fatal, soft

    ids = sorted(seen_ids)
    if ids != list(range(1, len(ids) + 1)):
        soft.append(f"zones[].id is not a contiguous 1..N sequence "
                    f"({', '.join(str(i) for i in ids)}): tolerated, the YAML order is authoritative.")

    # TOTAL COVERAGE: every scope file absent from the map is added
    # MECHANICALLY to a "Miscellaneous" zone (created if needed) — the audit leaves no
    # silent blind spot.
    missing = [f for f in scope_files if f not in seen_paths]
    if missing:
        divers = next((z for z in zones if isinstance(z, dict)
                       and slugify(str(z.get("name") or "")) == "miscellaneous"), None)
        if divers is None:
            divers = {"id": max(seen_ids) + 1, "name": "Miscellaneous",
                      "intent": "Interface remainder with no identified screen "
                                "(completed mechanically by the coverage check).",
                      "files": []}
            zones.append(divers)
        divers["files"] = list(divers.get("files") or []) + missing
        soft.append(f"Coverage: {len(missing)} scope file(s) absent from the "
                    f"map — added mechanically to the \"Miscellaneous\" zone (Z{divers['id']}).")

    # A "Miscellaneous" declared empty and still empty after coverage has no reason to
    # exist any more (an audit pass on zero files would make no sense).
    zones[:] = [z for z in zones
                if not (isinstance(z, dict) and slugify(str(z.get("name") or "")) == "miscellaneous"
                        and not z.get("files"))]

    return fatal, soft


def divers_files(a11y_map: dict) -> list:
    """Files placed in the "Miscellaneous" zone — [] if absent."""
    for zone in a11y_map.get("zones") or []:
        if isinstance(zone, dict) and slugify(str(zone.get("name") or "")) == "miscellaneous":
            return list(zone.get("files") or [])
    return []


def save_a11y_map(a11y_map: dict):
    """Persists the NORMALIZED map (atomic write): what the human validates at the y/n
    is exactly what is on disk — and therefore what a resume run will reload."""
    tmp = f"{A11Y_MAP_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(a11y_map, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp, A11Y_MAP_FILE)


def peek_a11y_map():
    """Best-effort load of the map for the S0 display (never blocking)."""
    try:
        with open(A11Y_MAP_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and isinstance(data.get("zones"), list) and data["zones"]:
            return data
    except Exception:
        pass
    return None


def load_and_validate_map_file(scope_files: list) -> tuple:
    """Loads + validates 'a11y_map.yaml'. Returns (a11y_map, fatal, soft, parse_error)."""
    try:
        with open(A11Y_MAP_FILE, "r", encoding="utf-8") as f:
            a11y_map = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return None, [], [], str(e)
    except OSError as e:
        return None, [], [], str(e)
    fatal, soft = validate_and_normalize_a11y_map(a11y_map, scope_files)
    return a11y_map, fatal, soft, ""


# ─── BUCKETS AND PASS MATRIX (PYTHON, DETERMINISTIC) ──────────────────────────
# The validated map becomes an ordered list of buckets (socle, composants,
# zones), crossed with the triggered packs: this is the pass matrix. Everything is
# computed here — sentinel slots, deliverable paths, labels — the model NEVER
# provides a filename.

def build_buckets(a11y_map: dict) -> list:
    """Ordered buckets of the audit. Socle and composants are omitted if they are
    empty (no pass to pay on a bucket without files)."""
    buckets = []
    socle = a11y_map.get("socle") or {}
    if socle.get("files"):
        buckets.append({"kind": "socle", "slot": "socle", "label": "BASE",
                        "name": "Base (layout, global navigation, global styles)",
                        "intent": socle.get("intent", ""), "files": socle["files"]})
    composants = a11y_map.get("composants") or {}
    if composants.get("files"):
        buckets.append({"kind": "composants", "slot": "comp", "label": "COMPONENTS",
                        "name": "Shared components (design system)",
                        "intent": composants.get("intent", ""), "files": composants["files"]})
    for zone in a11y_map["zones"]:
        buckets.append({"kind": "zone", "slot": f"z{zone['id']}",
                        "label": f"Z{zone['id']:02d}_{slugify(zone['name'])}",
                        "name": f"Z{zone['id']}: {zone['name']}",
                        "intent": zone.get("intent", ""), "files": zone["files"]})
    return buckets


def triggered_pack_ids_for_bucket(bucket: dict, triggers: dict) -> set:
    """Packs triggered by the CONTENT of the bucket's files (manifest regex
    only, WITHOUT the 'toujours' clause): serves routing and the anti
    rubber-stamping warning — when a pattern exists in the files, a 100% NA pass is
    incoherent by construction."""
    hits = set()
    for path in bucket["files"]:
        hits |= triggers.get(path, set())
    return hits


def active_pack_ids_for_bucket(bucket: dict, packs: list, triggers: dict) -> set:
    """Packs active on a bucket: union of its files' triggers, plus
    the 'toujours' packs on the BASE (the absence of their patterns is itself a
    potential finding: missing structure, focus never styled…)."""
    active = set(triggered_pack_ids_for_bucket(bucket, triggers))
    if bucket["kind"] == "socle":
        active |= {p["id"] for p in packs if p["toujours"]}
    return active


def invalidated_passes(passes: list, changed_files: list) -> list:
    """Diff-aware resumption (L8): passes where at least one compartment file
    appears in the git diff. PURE function (tested). The mtime variant is
    DISCARDED: a global touch or a branch switch would over-invalidate everything."""
    changed = {norm_rel(f) for f in changed_files}
    return [p for p in passes if changed & set(p["bucket"]["files"])]


def slice_bucket_files(files: list) -> list:
    """Slices of a compartment: we fill up to MAX_PASS_BYTES bytes OR MAX_FILES_PER_PASS
    files, the first bound reached cuts; a file bigger than the budget occupies its
    slice alone. File order preserved (deterministic)."""
    slices, current, size = [], [], 0
    for path in files:
        try:
            file_size = os.path.getsize(path)
        except OSError:
            file_size = 0
        if current and (len(current) >= MAX_FILES_PER_PASS or size + file_size > MAX_PASS_BYTES):
            slices.append(current)
            current, size = [], 0
        current.append(path)
        size += file_size
    if current:
        slices.append(current)
    return slices or [list(files)]


def pass_needs_agent(audit_pass: dict) -> bool:
    """A pass is handed to the agent if its slice carries at least one pattern of the pack —
    otherwise its criteria are NA by deterministic routing and we do not pay an LLM turn
    to have it observe that (this is also where it hallucinated Cs). Exception: the
    "always" packs on the BASE, where the ABSENCE of a pattern is itself a potential
    finding (missing structure, focus never styled…)."""
    if audit_pass.get("declenche"):
        return True
    return audit_pass["bucket"].get("kind") == "socle" and bool(audit_pass["pack"].get("toujours"))


def mechanical_na_passes(passes: list) -> list:
    """Passes declared NA mechanically (slice without a pattern, outside the "always" base)."""
    return [p for p in passes if not pass_needs_agent(p)]


def write_mechanical_na_findings(audit_pass: dict):
    """Write the findings file of a mechanical-NA slice, in the FORMAT of the agent
    passes: the parser, the consolidation (NA yields to any other verdict from the other
    slices), the resumption and the report treat it like any other pass."""
    pack = audit_pass["pack"]
    reason = (f"no pattern of pack T{pack['id']:02d} in this slice "
              f"(deterministic routing, no agent pass)")
    lines = [f"# T{pack['id']}: {pack['nom']} — {audit_pass['bucket']['name']}"
             + (" — mechanical NA" if True else ""), "",
             "<!-- Slice declared NA by the deterministic routing: no trigger of the pack "
             "in its files. No agent requested. -->", "",
             "## Verdicts"]
    lines += [f"- {crit}: NA — {reason}" for crit in pack["criteres"]]
    lines += ["", "## Findings", "No findings.", "", "## Summary",
              f"- Verdicts: C: 0, NC: 0, NA: {len(pack['criteres'])}, AVM: 0", ""]
    os.makedirs(os.path.dirname(audit_pass["findings_path"]) or ".", exist_ok=True)
    tmp = audit_pass["findings_path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, audit_pass["findings_path"])


def build_pass_list(buckets: list, packs: list, triggers: dict) -> list:
    """The pass matrix: for each compartment (map order), each active pack (id
    order). Each pass carries its sentinel slot and its deliverable.

    L7: a compartment beyond MAX_FILES_PER_PASS is SPLIT into slices (slots
    t10-z3a, t10-z3b, …) — silent window saturation was the pipeline's most real
    coverage hole (beyond the prompt truncation, surplus files were never audited,
    with no trace). Consolidation natively supports several passes per pack
    (NC > AVM > C > NA, findings dedup); the count shown at the y/n is computed
    AFTER splitting, hence exact. 'declenche' is recomputed PER SLICE (a slice
    with no pattern is legitimately 100% NA)."""
    passes = []
    packs_by_id = {p["id"]: p for p in packs}
    for bucket in buckets:
        slices = slice_bucket_files(bucket["files"])
        for pack_id in sorted(active_pack_ids_for_bucket(bucket, packs, triggers)):
            pack = packs_by_id[pack_id]
            for idx, slice_files in enumerate(slices):
                multi = len(slices) > 1
                suffix = chr(ord("a") + idx) if multi and idx < 26 else (f"x{idx}" if multi else "")
                slice_bucket = dict(bucket, files=slice_files) if multi else bucket
                slice_triggered = any(pack_id in triggers.get(f, set()) for f in slice_files)
                passes.append({
                    "pack": pack,
                    "bucket": slice_bucket,
                    "slot": f"t{pack['id']}-{bucket['slot']}{suffix}",
                    "findings_path": f"{A11Y_DIR}/T{pack['id']:02d}_{pack['slug']}__"
                                     f"{bucket['label']}{('-' + suffix) if suffix else ''}.md",
                    "label": f"T{pack['id']:02d} {pack['nom']} × {bucket['name']}"
                             + (f" — slice {idx + 1}/{len(slices)}" if multi else ""),
                    "declenche": slice_triggered,
                })
    return passes


def skipped_packs(passes: list, packs: list) -> list:
    """Packs with NO pass (no trigger anywhere): their criteria will be
    declared NA mechanically, with the reason in the report appendix."""
    active_ids = {p["pack"]["id"] for p in passes}
    return [p for p in packs if p["id"] not in active_ids]


# ─── VERDICTS PARSER (THE STRONG FLOOR OF THIS PIPELINE) ──────────────────────
# Unlike the Nielsen audit (free-form findings), RGAA has CLOSED criterion
# identifiers and ENUMERABLE statuses: the structural check can therefore be a
# real parser — exact set of the pack's criteria, statuses within the enum, each NC observed
# and located, coherent Summary. Its errors feed the retry feedback.

VERDICT_LINE_RE = re.compile(r"^\s*-\s*(\d{1,2}\.\d{1,2})\s*:\s*(C|NC|NA|AVM)\b\s*(?:[—–-]\s*(.*))?$")
CONSTAT_HEADING_RE = re.compile(r"^###\s+K(\d+)\s*[—–-]\s*(\d{1,2}\.\d{1,2})\s*[—–-]\s*(.+)$")
CONSTAT_FIELD_RE = re.compile(r"^\s*-\s*\*\*(Impact|Location|Excerpt|Finding|User impact|Fix)\s*:\*\*\s*(.*)$")
BILAN_LINE_RE = re.compile(
    r"^\s*-\s*Verdicts\s*:\s*C\s*:\s*(\d+)\s*[,;]\s*NC\s*:\s*(\d+)\s*[,;]\s*"
    r"NA\s*:\s*(\d+)\s*[,;]\s*AVM\s*:\s*(\d+)", re.IGNORECASE)

# ':line' suffix (or ':start-end') of a location — removed before the file
# existence check on disk.
LOCATION_LINE_SUFFIX_RE = re.compile(r":\d+(?:[-–]\d+)?$")


def extract_location_paths(localisation: str) -> list:
    """File paths extracted from a Location line (best-effort): segments
    between backticks if any (the common-trunk format), otherwise split by
    commas; norm_rel normalization then ':line' suffix removed. A fragment with
    spaces (free comment such as "Cart screen") is not a path: ignored."""
    text = str(localisation or "")
    tokens = re.findall(r"`([^`]+)`", text) or text.split(",")
    paths = []
    for token in tokens:
        p = LOCATION_LINE_SUFFIX_RE.sub("", norm_rel(token)).strip()
        if p and " " not in p:
            paths.append(p)
    return paths


def iter_lines_with_fence_state(content: str):
    """Iterates (line, in_fence): lines inside ``` / ~~~ blocks are
    flagged so the parser never takes a quoted example for content."""
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            yield line, True
            continue
        yield line, in_fence


def normalize_extract(text: str) -> str:
    """Normalized whitespace for excerpt comparison: the model copies a line it
    actually read — indentation and spacing are not hallucinations, the CONTENT is."""
    return " ".join(str(text).split())


def locate_extrait(extrait: str, paths: list) -> tuple:
    """MATERIAL truth of a finding (H1): the exact copy of the offending line
    (whitespace normalized) must appear in one of the cited files. Returns
    (found, path, line) — (False, "", 0) otherwise. PURE function (tested)."""
    needle = normalize_extract(extrait)
    if not needle:
        return False, "", 0
    for candidate in paths:
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if needle in normalize_extract(line):
                        return True, candidate, lineno
        except OSError:
            continue
    return False, "", 0


def parse_findings_file(path: str, pack: dict) -> tuple:
    """Parse a findings file. Returns (data, fatal, soft).

    data = {"verdicts": {criterion: {"statut", "note"}}, "constats": [dicts], "bilan": dict}
    fatal: what makes the file unusable by the aggregation (incomplete verdicts,
    statuses outside the enum, NC with no finding, inconsistent Summary) → the pass is replayed.
    soft: tolerated imperfections (missing finding field, empty location) →
    reported, the aggregation displays them as "?".
    """
    fatal, soft = [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return None, [f"unreadable file ({e})"], []

    expected = set(pack["criteres"])
    verdicts, constats, bilan = {}, [], None
    section = None          # None | 'verdicts' | 'constats' | 'bilan'
    current = None          # finding currently being collected

    for line, in_fence in iter_lines_with_fence_state(content):
        if in_fence:
            continue
        low = line.strip().lower()
        if low.startswith("## "):
            if low.startswith("## verdicts"):
                section = "verdicts"
            elif low.startswith("## findings"):
                section = "constats"
            elif low.startswith("## summary"):
                section = "bilan"
            else:
                section = None
            current = None
            continue

        if section == "verdicts":
            match = VERDICT_LINE_RE.match(line)
            if match:
                crit, statut, note = match.group(1), match.group(2), (match.group(3) or "").strip()
                if crit in verdicts:
                    soft.append(f"duplicate verdict for {crit} (the first one is kept)")
                    continue
                verdicts[crit] = {"statut": statut, "note": note}
            elif line.strip().startswith("-"):
                soft.append(f"unrecognized verdict line: \"{line.strip()[:80]}\"")

        elif section == "constats":
            match = CONSTAT_HEADING_RE.match(line.strip())
            if match:
                current = {"k": int(match.group(1)), "critere": match.group(2),
                           "titre": match.group(3).strip(), "impact": None,
                           "localisation": "", "constat": "", "impact_utilisateur": "",
                           "correction": "", "localisation_verifiee": True,
                           "extrait": "", "extrait_verifie": False}
                constats.append(current)
                continue
            if current is not None:
                field = CONSTAT_FIELD_RE.match(line)
                if field:
                    key = {"Impact": "impact", "Location": "localisation",
                           "Excerpt": "extrait",
                           "Finding": "constat", "User impact": "impact_utilisateur",
                           "Fix": "correction"}[field.group(1)]
                    value = field.group(2).strip()
                    if key == "impact":
                        digit = re.match(r"^([1-4])\b", value)
                        current["impact"] = int(digit.group(1)) if digit else None
                        if digit is None:
                            soft.append(f"finding K{current['k']}: unreadable impact (\"{value[:40]}\")")
                    else:
                        current[key] = value

        elif section == "bilan" and bilan is None:
            match = BILAN_LINE_RE.match(line)
            if match:
                bilan = {"C": int(match.group(1)), "NC": int(match.group(2)),
                         "NA": int(match.group(3)), "AVM": int(match.group(4))}

    # ── FATAL checks: the exact set of criteria, the enum, the reported NCs, the Summary.
    got = set(verdicts)
    missing = sorted(expected - got, key=lambda c: [int(x) for x in c.split(".")])
    unknown = sorted(got - expected, key=lambda c: [int(x) for x in c.split(".")])
    if missing:
        fatal.append(f"MISSING verdict(s) for: {', '.join(missing)}")
    if unknown:
        fatal.append(f"criterion(s) OUTSIDE THE PACK: {', '.join(unknown)}")

    nc_criteria = {c for c, v in verdicts.items() if v["statut"] == "NC"}
    constated = {c["critere"] for c in constats}
    unconstated = sorted(nc_criteria - constated, key=lambda c: [int(x) for x in c.split(".")])
    if unconstated:
        fatal.append(f"NC criterion(s) with no associated finding: {', '.join(unconstated)}")
    for constat in constats:
        if constat["critere"] not in expected:
            fatal.append(f"finding K{constat['k']}: criterion outside the pack ({constat['critere']})")
        if not constat["localisation"]:
            soft.append(f"finding K{constat['k']} ({constat['critere']}): missing location")
        else:
            # Anti-hallucination, kept SOFT (we don't replay a pass for a badly
            # formatted path): every cited file must exist on disk. A path that
            # can't be found marks the finding — the report suffixes its Location line.
            missing_paths = [p for p in extract_location_paths(constat["localisation"])
                             if not os.path.exists(p)]
            if missing_paths:
                constat["localisation_verifiee"] = False
                soft.append(f"finding K{constat['k']} ({constat['critere']}): location "
                            f"file(s) not found on disk "
                            f"({', '.join(missing_paths[:3])})")
        # ── MATERIAL TRUTH (H1) ── : the excerpt is the proof that the offending code
        # exists AS DESCRIBED. Found → "verified" finding (badge in the report);
        # missing or not found → soft (the report marks "to verify"); the pass-level
        # fatal (no excerpt found at all) is checked after the loop.
        if not constat["extrait"]:
            soft.append(f"finding K{constat['k']} ({constat['critere']}): Excerpt field "
                        f"missing — the finding's materiality cannot be verified")
        else:
            cited = extract_location_paths(constat["localisation"])
            found, seen_path, seen_line = locate_extrait(constat["extrait"], cited)
            constat["extrait_verifie"] = found
            if not found:
                soft.append(f"finding K{constat['k']} ({constat['critere']}): excerpt NOT "
                            f"found in the cited files — finding to verify")
            else:
                announced = re.search(r":(\d+)", constat["localisation"] or "")
                if announced and abs(int(announced.group(1)) - seen_line) > 5:
                    soft.append(f"finding K{constat['k']} ({constat['critere']}): announced "
                                f"line {announced.group(1)}, excerpt seen at line "
                                f"{seen_line} ({seen_path})")

    # OUTRIGHT hallucination (H1): a pass whose findings have NO materially found
    # excerpt describes code that does not exist as such — rejection, dedicated
    # feedback (the other checks leave partial cases as soft).
    if constats and not any(c["extrait_verifie"] for c in constats):
        fatal.append("no Excerpt of this pass is found in the cited files: every "
                     "finding MUST copy EXACTLY (verbatim) an offending line "
                     "you actually read in the cited file")

    if bilan is None:
        fatal.append("Summary line missing or malformed "
                     "(expected: '- Verdicts: C: <a>, NC: <b>, NA: <c>, AVM: <d>')")
    else:
        counted = {s: sum(1 for v in verdicts.values() if v["statut"] == s) for s in STATUSES}
        if counted != bilan:
            fatal.append(f"inconsistent Summary: announced {bilan}, counted {counted}")

    data = {"verdicts": verdicts, "constats": constats, "bilan": bilan}
    return data, fatal, soft


def bilan_only_fatals(fatal: list) -> bool:
    """Do ALL the fatal anomalies concern the Summary line alone?
    (Summary absent or inconsistent: the verdicts, for their part, passed every
    other check — the file is mechanically repairable, cf. repair_bilan_line.)"""
    return bool(fatal) and all(f.startswith("inconsistent Summary")
                               or f.startswith("Summary line") for f in fatal)


def repair_bilan_line(path: str, verdicts: dict) -> bool:
    """Mechanically rewrite the Summary line from the parsed verdicts.

    Counting is the notorious weak point of small models, and this line carries NO
    information: the aggregation recounts everything (that is precisely how the
    parser detects the inconsistency). The requirement stays in the prompt (checksum
    effect: force the model to reread itself), but a wrong Summary no longer costs a
    whole pass. Replaces the first Summary line outside fences, or appends the
    section if absent. Returns True if the file was rewritten."""
    counted = {s: sum(1 for v in verdicts.values() if v["statut"] == s) for s in STATUSES}
    line = (f"- Verdicts: C: {counted['C']}, NC: {counted['NC']}, "
            f"NA: {counted['NA']}, AVM: {counted['AVM']}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    out, replaced = [], False
    for text_line, in_fence in iter_lines_with_fence_state(content):
        if not replaced and not in_fence and BILAN_LINE_RE.match(text_line):
            out.append(line)
            replaced = True
        else:
            out.append(text_line)
    if not replaced:
        if not any(l.strip().lower().startswith("## summary") for l in out):
            out += ["", "## Summary"]
        out.append(line)
    atomic_write(path, "\n".join(out) + "\n")
    return True


def findings_ok(path: str, pack: dict) -> bool:
    """Is a findings file usable (present, non-empty, and does it PASS the
    parser without a fatal error)? Used for resumption (skipped pass), progress
    display and the failure report."""
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return False
    _data, fatal, _soft = parse_findings_file(path, pack)
    return not fatal


def findings_all_na(data) -> bool:
    """ALL the (parsed) verdicts of a pass are NA."""
    return bool(data and data["verdicts"]) and \
        all(v["statut"] == "NA" for v in data["verdicts"].values())


def findings_all_c(data) -> bool:
    """The DUAL fill-in mode of 100% NA (H5): overwhelmingly C verdicts (≥ 90%),
    zero findings, zero AVM — the typical pattern of fabricated compliance."""
    if not (data and data["verdicts"]):
        return False
    statuts = [v["statut"] for v in data["verdicts"].values()]
    return (statuts.count("C") / len(statuts) >= 0.9
            and not data["constats"] and "AVM" not in statuts)


def suspicious_all_c_passes(passes: list) -> list:
    """SUSPICIOUS passes on the C side (H5): TRIGGERED pack yet overwhelmingly C
    verdicts with no finding and no AVM. Mirror of the 100% NA: never an automatic
    retry, warning + appendix, arbitration stays human. Thresholds to calibrate."""
    suspicious = []
    for audit_pass in passes:
        if not audit_pass.get("declenche"):
            continue
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"],
                                                 audit_pass["pack"])
        if not fatal and findings_all_c(data):
            suspicious.append(audit_pass)
    return suspicious


def suspicious_c_verdicts(passes: list, sonde_hits: dict) -> list:
    """DOWNSTREAM probe confrontation (H3): positive probe on a compartment file +
    C verdict on the probed criterion = "suspicious C verdict" — the exact mirror
    of the 100% NA anti rubber-stamping. Never an automatic retry: a warning in
    the appendix, arbitration stays human."""
    suspects = []
    for audit_pass in passes:
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"],
                                                 audit_pass["pack"])
        if fatal or data is None:
            continue
        pack_id = audit_pass["pack"]["id"]
        for path in audit_pass["bucket"]["files"]:
            for line, motif, crit, conf in (sonde_hits or {}).get((pack_id, path), []):
                verdict = data["verdicts"].get(crit)
                if verdict and verdict["statut"] == "C":
                    suspects.append({"pass": audit_pass, "critere": crit, "path": path,
                                     "line": line, "motif": motif, "confiance": conf})
    return suspects


def suspicious_all_na_passes(passes: list) -> list:
    """SUSPICIOUS passes (anti rubber-stamping): pack routed by a TRIGGER — its patterns
    therefore exist in the compartment's files — yet 100% of the verdicts are NA,
    inconsistent by construction (an auditor who "fills in" to finish). The 'toujours'
    passes that come without a trigger are legitimately 100% NA (they run to record
    an absence): out of scope. Re-reads the findings files: also covers passes
    resumed from a previous run. Never an automatic retry: arbitration stays
    human (console warning + report appendix)."""
    suspicious = []
    for audit_pass in passes:
        if not audit_pass.get("declenche"):
            continue
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"], audit_pass["pack"])
        if not fatal and findings_all_na(data):
            suspicious.append(audit_pass)
    return suspicious


# ─── PROMPTS OFFLOADED TO A FILE ───────────────────────────────────────────────

def build_carto_scope_block(scope_files: list) -> str:
    """The "UI files to assign" block of the cartographer prompt, capped at
    MAX_SCOPE_FILES_IN_CARTO. The listed files are a SAMPLE representative of every
    directory (application code first), not the first N in alphabetical order — on a
    monorepo, those first N were 300 icon stylesheets and zero file from src/. The
    overflow is summarized per directory and assignable PER DIRECTORY."""
    listed = select_carto_sample(scope_files, MAX_SCOPE_FILES_IN_CARTO)
    block = "\n".join(f"- {f}" for f in listed) or "(none)"
    listed_set = set(listed)
    overflow = [f for f in scope_files if f not in listed_set]
    if overflow:
        block += (f"\n(⚠️ Scope of {len(scope_files)} files: {len(listed)} listed above "
                  f"(sample representative of every directory), {len(overflow)} unlisted, "
                  f"summarized per directory below. Assign them PER DIRECTORY: a files: "
                  f"entry whose path ends with '/' covers every scope file it contains "
                  f"(recursively). What you do not assign will go mechanically to the "
                  f"\"Miscellaneous\" zone, which must remain a residual — not the bulk of "
                  f"the project.)\n"
                  + summarize_by_directory(overflow))
    return block


def doc_map_hint() -> str:
    """OPTIONAL hint taken from the documentation pipeline map ('doc_map.yaml'):
    the functional zone names already validated by a human help the cartographer
    name the journeys — we pass ONLY the names/intents, never the files
    (the a11y split by screens is a different axis from the functional split)."""
    try:
        with open(DOC_MAP_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        zones = data.get("zones") if isinstance(data, dict) else None
        if not isinstance(zones, list) or not zones:
            return ""
        lines = [f"A FUNCTIONAL map of the project exists ('{DOC_MAP_FILE}', another pipeline). "
                 f"Its zones can inspire your journey names (WITHOUT obligation: your "
                 f"split by screens prevails):"]
        for z in zones[:12]:
            if isinstance(z, dict) and z.get("name"):
                lines.append(f"- {z['name']}: {str(z.get('intent') or '')[:100]}")
        return "\n".join(lines)
    except Exception:
        return ""


def build_carto_prompt(grid_text: str, scope_files: list, feedback: str, attempt: int) -> str:
    sentinel = a11y_sentinel("map", attempt)
    hint = doc_map_hint()
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are the Interface Cartographer of a split accessibility-audit pipeline: you
ASSIGN each UI file provided below to the SOCLE, to shared COMPOSANTS or to a
named-screen ZONE. You audit NOTHING (dedicated passes handle it afterwards) and you
do not read the project in depth: skim only the files whose name does not settle
the case.
AUDIT = READ-ONLY: you do not modify, fix or create ANY project file.
You write ONLY two files: '{A11Y_MAP_FILE}' at the root, then your end sentinel.

--- CARTOGRAPHER GRID ---
{grid_text}

--- UI FILES TO ASSIGN ({len(scope_files)}, discovered by the orchestrator; paths to be COPIED verbatim) ---
A files: entry may also be a DIRECTORY (path ending with '/', e.g. "src/pages/"): it assigns
to the bucket every scope file it contains that is not already assigned elsewhere. The
"Miscellaneous" zone may be omitted or declared empty: the orchestrator mechanically
places there what you will not have assigned.
{build_carto_scope_block(scope_files)}

--- HINT (optional) ---
{hint or "(no existing functional map)"}

--- BUSINESS CONTEXT (optional) ---
{business_context_hint()}

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY DELIVERABLE ---
Write the map to '{A11Y_MAP_FILE}' at the project root: PURE YAML matching the grid
above (NO ``` fences, every text value in double quotes,
paths copied from the provided list). Do it directly via your file-editing tools,
without needless chatter in the console.

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{A11Y_MAP_FILE}', create the sentinel file
'{sentinel}' at the root (content: the single word done): it is the end signal
for the orchestrator. Only create it once the map is TRULY finished.
"""
    with open(TMP_A11Y_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_A11Y_FILE}' at the project root and run "
            f"the interface cartography pass.")


def build_bucket_files_block(bucket: dict) -> str:
    """The "your scope" block of the audit prompt: bounded list (context window)."""
    files = bucket["files"]
    listed = files[:MAX_BUCKET_FILES_IN_PROMPT]
    lines = [f"- {f}" for f in listed]
    overflow = len(files) - len(listed)
    if overflow > 0:
        lines.append(f"(+ {overflow} other unlisted file(s): focus on "
                     f"the files above.)")
    return "\n".join(lines)


def build_trigger_hits_block(audit_pass: dict, trigger_hits: dict) -> str:
    """The "DETECTED PATTERNS" block of a TRIGGERED pass: the pack's first match in
    each compartment file (bounded). Triple effect: anchoring (the agent reads the
    right files first), anti-wrong-NA (answering 100% NA while the prompt lists the
    patterns becomes a visible contradiction), and the human arbitration of
    suspicious passes has the hits in plain sight. A HINT, never a verdict."""
    if not audit_pass.get("declenche") or not trigger_hits:
        return ""
    pack_id = audit_pass["pack"]["id"]
    lines = []
    for path in audit_pass["bucket"]["files"]:
        hit = trigger_hits.get((pack_id, path))
        if hit:
            lines.append(f"- {path}:{hit[0]} — pattern \"{hit[1]}\"")
    if not lines:
        return ""
    shown = lines[:MAX_TRIGGER_HITS_IN_PROMPT]
    overflow = len(lines) - len(shown)
    if overflow > 0:
        shown.append(f"(+ {overflow} other unlisted triggering file(s))")
    return ("\n--- PATTERNS DETECTED BY THE ORCHESTRATOR (mechanical scan: confirm or refute each one) ---\n"
            "These files of your scope contain patterns of YOUR theme; read them first.\n"
            + "\n".join(shown) + "\n")


def build_sonde_hits_block(audit_pass: dict, sonde_hits: dict) -> str:
    """The "NC PROBES" block of a pass (H3): the quasi-certain hints detected in the
    compartment's files for THIS pack. The agent confirms or refutes each one —
    the orchestrator will confront its verdicts with these hints (suspicious-C appendix)."""
    if not sonde_hits:
        return ""
    pack_id = audit_pass["pack"]["id"]
    lines = []
    for path in audit_pass["bucket"]["files"]:
        for line, motif, crit, conf in sonde_hits.get((pack_id, path), []):
            lines.append(f"- criterion {crit}: {path}:{line} — pattern \"{motif}\" (NC {conf})")
    if not lines:
        return ""
    shown = lines[:MAX_TRIGGER_HITS_IN_PROMPT]
    overflow = len(lines) - len(shown)
    if overflow > 0:
        shown.append(f"(+ {overflow} other unlisted hint(s))")
    return ("\n--- NC PROBES (mechanical hints: confirm or refute EACH ONE in your verdicts) ---\n"
            "These quasi-certain patterns were detected by the orchestrator; a C verdict "
            "on a probed criterion that ignores the hint will be flagged SUSPICIOUS in the report.\n"
            + "\n".join(shown) + "\n")


def build_auditor_prompt(audit_pass: dict, trunk_text: str, position: int, total: int,
                         contrast_block: str, feedback: str, attempt: int,
                         trigger_hits: dict = None, sonde_hits: dict = None) -> str:
    pack, bucket = audit_pass["pack"], audit_pass["bucket"]
    sentinel = a11y_sentinel(audit_pass["slot"], attempt)
    findings_file = audit_pass["findings_path"]
    criteria_line = ", ".join(pack["criteres"])
    hits_section = build_trigger_hits_block(audit_pass, trigger_hits or {})
    sonde_section = build_sonde_hits_block(audit_pass, sonde_hits or {})
    contrast_section = ""
    if pack["id"] == 3 and contrast_block:
        contrast_section = f"\n--- CONTRAST MEASUREMENTS (computed mechanically by the orchestrator) ---\n{contrast_block}\n"
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are an ultra-specialized accessibility Auditor Agent, assigned to ONE SINGLE RGAA
theme: T{pack['id']} "{pack['nom']}", on ONE SINGLE scope: {bucket['name']}.
This is pass {position}/{total} of a split accessibility audit.
AUDIT = READ-ONLY: you do not modify, fix or create ANY project file.
You write ONLY two files: your findings file, then your end sentinel.
Ignore any problem belonging to ANOTHER theme or ANOTHER scope: a dedicated
pass handles it (reporting it here would create duplicates in the report).

--- AUDIT GRID (common trunk: statuses, iron rules, output format) ---
{trunk_text}

--- YOUR THEMATIC PACK ---
{pack['grid_text']}

--- YOUR SCOPE: {bucket['name']} ({len(bucket['files'])} file(s), assigned by the human-validated cartography) ---
Stated role (intent): {bucket['intent'] or '(unspecified)'}
{build_bucket_files_block(bucket)}
{hits_section}{sonde_section}{contrast_section}
--- BUSINESS CONTEXT (optional) ---
{business_context_hint()}

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY DELIVERABLE ---
Write your verdicts to '{findings_file}' (create the '{A11Y_DIR}/' folder if needed),
STRICTLY following the common-trunk format:
- first line: '# T{pack['id']} : {pack['nom']} — {bucket['name']}' ;
- section '## Verdicts': one verdict (C, NC, NA or AVM) for EACH of these criteria,
  in this order, no other: {criteria_line} ;
- section '## Findings': one '### K<i> — <criterion> — <title>' block per non-conformity
  (or the single line "No findings.") ;
- section '## Summary': the locked line '- Verdicts: C: <a>, NC: <b>, NA: <c>, AVM: <d>'.
Do it directly via your file-editing tools, without needless chatter in the console.

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{findings_file}', create the sentinel file
'{sentinel}' at the root (content: the single word done): it is the end signal
for the orchestrator. Only create it once the findings file is TRULY finished.
"""
    with open(TMP_A11Y_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_A11Y_FILE}' at the project root and run "
            f"the T{pack['id']} × {bucket['label']} audit pass.")


def build_synthesis_prompt(stats: dict, top_ncs: list, feedback: str, attempt: int) -> str:
    sentinel = a11y_sentinel("synthese", attempt)
    topics_lines = "\n".join(
        f"- T{t['id']:02d} {t['nom']}: C {t['C']}, NC {t['NC']}, NA {t['NA']}, AVM {t['AVM']}"
        for t in stats["topics"])
    ncs_lines = "\n".join(
        f"- Criterion {n['critere']} ({n['nom_pack']}), impact {n['impact'] if n['impact'] else '?'}: {n['titre']}"
        for n in top_ncs) or "(no non-conformity found)"
    full_context = f"""--- BEHAVIORAL CONTRACT ---
You are an accessibility Lead in charge of WRITING the executive summary of an RGAA
pre-audit carried out in independent passes. You re-audit NOTHING and you do NOT re-read
the project code: you write 8 to 15 lines readable by an executive board, ONLY from the
figures and findings below. ZERO invention: no figure or finding that does not appear
here. You modify no project file; you write ONLY the summary, then your sentinel.

--- AGGREGATED FIGURES (computed mechanically, they are authoritative) ---
RGAA 4.1.2 criteria: {stats['totals']['C']} compliant, {stats['totals']['NC']} non-compliant,
{stats['totals']['NA']} not applicable, {stats['totals']['AVM']} to verify manually.
Demonstrable conformance: {stats['rate_central']}% (range {stats['rate_floor']}% to {stats['rate_ceiling']}%
depending on the outcome of the manual checks).

By theme:
{topics_lines}

--- MAIN NON-CONFORMITIES (decreasing impact) ---
{ncs_lines}

--- DELIVERABLE TO PRODUCE: '{SYNTHESIS_FILE}' ---
MANDATORY structure: the file begins EXACTLY with the line '## Executive summary'
then 8 to 15 lines: general state, the 2 or 3 priority work streams (rely on the
highest-impact non-conformities), the share of manual checks remaining, and the
one-sentence reminder that this is a static pre-audit (not a certification).

--- ORCHESTRATOR FEEDBACK TO FIX (if any) ---
{feedback}

--- MANDATORY END INSTRUCTION ---
As your very LAST action, after saving '{SYNTHESIS_FILE}', create the sentinel file
'{sentinel}' at the root (content: the single word done).
"""
    with open(TMP_A11Y_FILE, "w", encoding="utf-8") as f:
        f.write(full_context)

    return (f"Read the instructions file '{TMP_A11Y_FILE}' at the project root and write "
            f"the executive summary of the audit.")


# ─── STEP S1: CARTOGRAPHY (1 LLM PASS + DOUBLE VALIDATION) ────────────────────

def print_a11y_map_recap(a11y_map: dict, soft: list, packs: list, triggers: dict):
    """Human-readable recap of the map AND of the pass matrix it implies:
    the EXACT count of what will be paid for, shown BEFORE the y/n."""
    buckets = build_buckets(a11y_map)
    passes = build_pass_list(buckets, packs, triggers)
    mechanical = mechanical_na_passes(passes)
    paid = [p for p in passes if pass_needs_agent(p)]
    per_bucket = {}
    for p in paid:
        per_bucket.setdefault(p["bucket"]["label"], []).append(f"T{p['pack']['id']:02d}")
    print(f"\n{'='*60}")
    print(f"🗺️  INTERFACE MAP — {a11y_map.get('project', '(unnamed)')}: "
          f"{len(buckets)} compartment(s), {len(paid)} audit pass(es)"
          + (f" (+ {len(mechanical)} mechanical NA slice(s), no agent)" if mechanical else ""))
    print(f"{'Compartment':<34} | {'Files':>8} | Audited packs")
    print(f"{'-'*34}-+-{'-'*8}-+-{'-'*30}")
    for bucket in buckets:
        packs_label = " ".join(per_bucket.get(bucket["label"], [])) or "(none)"
        print(f"{bucket['name'][:34]:<34} | {len(bucket['files']):>8} | {packs_label}")
    skipped = skipped_packs(passes, packs)
    if skipped:
        print(f"\n   ⏭️  Pack(s) never triggered (criteria declared NA mechanically): "
              + ", ".join(f"T{p['id']:02d} {p['nom']}" for p in skipped))
    if soft:
        print(f"\n⚠️  Points of attention (non-blocking):")
        for warning in soft:
            print(f"   - {warning}")
    print(f"\n   ✏️  The map is EDITABLE: '{A11Y_MAP_FILE}' (the order of zones = the report's "
          f"reading order; move a file to another compartment to change the routing).")
    print(f"{'='*60}")


def confirm_a11y_map(a11y_map: dict, soft: list, packs: list, triggers: dict):
    """Human validation of the map (the y/n that arbitrates BEFORE paying for N passes)."""
    print_a11y_map_recap(a11y_map, soft, packs, triggers)
    confirm = input("\n▶️  Validate this map and launch the accessibility pre-audit? (y/n): ")
    mm_audit.event("gate", id="map", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print(f"⏹️  Stopping. Edit '{A11Y_MAP_FILE}' then relaunch (it will be resumed as is), "
              f"or delete it to replay the cartography.")
        RUNNER.kill()
        sys.exit(0)


def run_cartography(grid_text: str, scope_files: list, packs: list, triggers: dict) -> dict:
    """Step S1: produces (or resumes) the interface map, doubly validated.

    Resumption: an existing and valid 'a11y_map.yaml' skips the LLM pass (recap +
    y/n shown again — this is where manual editing of the YAML is taken into
    account); an existing but structurally invalid file stops the run with
    instructions (fix or delete), same contract as the blackboard.
    """
    if os.path.exists(A11Y_MAP_FILE):
        a11y_map, fatal, soft, parse_error = load_and_validate_map_file(scope_files)
        if parse_error:
            fail_a11y(f"❌ '{A11Y_MAP_FILE}' exists but is not parsable: fix it or "
                      f"delete it (the cartography will be replayed), then relaunch.",
                      details=parse_error[:1500], title="Existing map invalid")
        if fatal:
            fail_a11y(f"❌ '{A11Y_MAP_FILE}' exists but is structurally invalid:\n   - "
                      + "\n   - ".join(fatal)
                      + f"\n   → Fix it or delete it (the cartography will be replayed), then relaunch.",
                      details="\n".join(fatal), title="Existing map invalid")
        save_a11y_map(a11y_map)
        print(f"♻️  '{A11Y_MAP_FILE}' exists and is valid: cartography skipped (resumption).")
        # Map written AFTER the stop of a run left without closure: deliverable of an
        # orphan agent, to re-read before taking it as valid.
        residual = residual_deliverable_warning(A11Y_MAP_FILE, "pre-audit-a11y")
        if residual:
            soft = list(soft) + [residual]
        confirm_a11y_map(a11y_map, soft, packs, triggers)
        return a11y_map

    print(f"\n{'='*50}\n🗺️  STEP S1: INTERFACE CARTOGRAPHY (1 LLM pass)\n{'='*50}")
    if len(scope_files) > MAX_SCOPE_FILES_IN_CARTO:
        print(f"   ⚠️  Scope of {len(scope_files)} files > {MAX_SCOPE_FILES_IN_CARTO}: the "
              f"overflow will be summarized per directory in the prompt and placed in the \"Miscellaneous\" zone "
              f"by the coverage check (assumed degradation).")
    RUNNER.start()

    attempts = 0
    a11y_map, soft = None, []
    error_history = []   # failures of previous attempts (cumulative feedback)

    while a11y_map is None and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Catch-up for a LATE deliverable: the agent from the previous attempt may have
        # finished writing AFTER the orchestrator's timeout. If its map became valid in
        # the meantime, we take it as is rather than paying a round to redo everything.
        if attempts > 1 and os.path.exists(A11Y_MAP_FILE):
            late_map, late_fatal, late_soft, late_err = load_and_validate_map_file(scope_files)
            if not late_err and not late_fatal:
                print(f"   ♻️  '{A11Y_MAP_FILE}' finally arrived (late deliverable): accepted.")
                a11y_map, soft = late_map, late_soft
                break

        cleanup_slot_sentinels("map")
        print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Launching the Interface Cartographer...")

        prompt = build_carto_prompt(grid_text, scope_files,
                                    compose_retry_feedback(error_history), attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable, wait_reason = wait_for_deliverable(
            A11Y_MAP_FILE, a11y_sentinel("map", attempts),
            structural_check=map_structural_check)
        # Read-only guard after EACH attempt (successful or not): a cartographer that
        # "fixed" code along the way is restored immediately.
        enforce_readonly("Carto")

        if not got_deliverable:
            if wait_reason == "sentinelle_sans_livrable":
                error_history.append(
                    f"On the previous pass, you created the sentinel WITHOUT the deliverable: "
                    f"write '{A11Y_MAP_FILE}' in full FIRST, the sentinel as the very "
                    f"LAST action.")
            elif wait_reason == "stable_hors_format":
                error_history.append(
                    f"On the previous pass, '{A11Y_MAP_FILE}' stayed off-format: "
                    f"the YAML must be parsable, with a non-empty 'zones' list. "
                    f"Rewrite the file entirely, then create the sentinel.")
            else:
                error_history.append(
                    f"On the previous pass, no deliverable was received ('{A11Y_MAP_FILE}' "
                    f"absent, empty or never signaled). First write the complete YAML map, "
                    f"THEN the sentinel, in that order.")
            print(f"⏱️  The cartographer did not signal the end of its pass. Retrying.")
            if os.path.exists(A11Y_MAP_FILE) and not map_structural_check(A11Y_MAP_FILE):
                try:
                    os.remove(A11Y_MAP_FILE)
                except OSError:
                    pass
            reset_agent_session()
            continue

        candidate, fatal, cand_soft, parse_error = load_and_validate_map_file(scope_files)
        if parse_error:
            error_history.append(
                f"Your '{A11Y_MAP_FILE}' is not parsable YAML "
                f"(error: {parse_error[:400]}). Reminders: NO ``` fences, all "
                f"text values in double quotes, internal quotes "
                f"escaped (\\\"). Rewrite the file entirely.")
            print(f"⚠️  [REJECT] Attempt {attempts}: YAML not parsable.")
        elif fatal:
            error_history.append(
                "Your map does not match the grid schema: "
                + " ; ".join(fatal)
                + " Reminders: paths COPIED from the provided list (never "
                  "invented), each zone with a unique integer id, a name and at "
                  "least one existing file. Rewrite the file entirely.")
            print(f"⚠️  [REJECT] Attempt {attempts}: structurally invalid map "
                  f"({len(fatal)} issue(s)).")
        elif len(divers_files(candidate)) > DIVERS_RETRY_THRESHOLD and attempts < MAX_ATTEMPTS:
            # A "Miscellaneous" that contains the bulk of the project is not a cartography:
            # we replay as long as attempts remain, naming the directories to assign.
            overflow = len(divers_files(candidate))
            error_history.append(
                f"Your map leaves {overflow} files in the \"Miscellaneous\" zone (residual), i.e. "
                f"the bulk of the project: this is not a split by screens. Assign them to the "
                f"foundation, the components or named screen zones, PER DIRECTORY (files: entry "
                f"ending with '/'). Directories concerned:\n"
                + summarize_by_directory(divers_files(candidate)))
            print(f"⚠️  [REJECT] Attempt {attempts}: {overflow} files in \"Miscellaneous\" "
                  f"(> {DIVERS_RETRY_THRESHOLD}) — the map does not split the project.")
        else:
            a11y_map, soft = candidate, cand_soft
            break

        try:
            os.remove(A11Y_MAP_FILE)
        except OSError:
            pass
        reset_agent_session()

    if a11y_map is None:
        cleanup_all_a11y_sentinels()
        reason = compose_retry_feedback(error_history)
        print_pass_failure("Cartography", reason)
        fail_a11y(f"❌ Cartography not completed after {MAX_ATTEMPTS} attempts.", details=reason)

    cleanup_slot_sentinels("map")
    save_a11y_map(a11y_map)
    # Context reset before the first audit pass: the cartographer's conversation
    # must not leak into the following passes.
    reset_agent_session()
    confirm_a11y_map(a11y_map, soft, packs, triggers)
    return a11y_map


# ─── STEP S2: THE AUDIT PASSES (ONE PER PACK × COMPARTMENT) ───────────────────

def warn_orphan_findings(passes: list):
    """Files in 'pre_audit_a11y/' matching no pass of the matrix (map re-edited by
    hand, e.g.): reported at the start of the step, NEVER deleted (human
    decision); they will NOT be aggregated."""
    if not os.path.isdir(A11Y_DIR):
        return
    expected = {os.path.basename(p["findings_path"]) for p in passes}
    expected.add(os.path.basename(SYNTHESIS_FILE))
    orphans = sorted(name for name in os.listdir(A11Y_DIR)
                     if name.endswith(".md") and name not in expected)
    if orphans:
        print(f"⚠️  Orphan file(s) in '{A11Y_DIR}/' (no pass of the matrix "
              f"produces them — map re-edited?): {', '.join(orphans)}. Not deleted; they "
              f"will NOT be aggregated.")


def pass_failure_breaker(consecutive: int, failed_count: int, treated: int) -> bool:
    """Should the circuit breaker stop the run? PURE function (unit-tested):
    consecutive failures, or failure ratio among processed passes — but never on
    an isolated failure (the ratio is only armed from 2 failures on)."""
    return (consecutive >= MAX_CONSECUTIVE_PASS_FAILURES
            or (failed_count >= 2 and failed_count > MAX_PASS_FAILURE_RATIO * treated))


def run_audit_passes(passes: list, trunk_text: str, contrast_block: str,
                     trigger_hits: dict = None, sonde_hits: dict = None) -> list:
    """The MAIsterMind core: a fresh session per pass, a context slice per
    pass (common trunk + ONE pack + ONE compartment), a parser as a strong floor.

    Returns the list of FAILED passes (empty on the nominal path): passes being
    independent, a failure no longer kills the run — except for the circuit
    breaker (consecutive failures or failure ratio, cf. constants)."""
    total = len(passes)
    warn_orphan_findings(passes)
    failed = []           # failed passes: [{"label", "findings_path", "reason"}]
    consecutive_failures = 0

    for position, audit_pass in enumerate(passes, start=1):
        findings_file = audit_pass["findings_path"]
        pack = audit_pass["pack"]
        slot = audit_pass["slot"]

        # File-based resumption: a findings file that PASSES THE PARSER skips its pass.
        if findings_ok(findings_file, pack):
            print(f"⏭️  Pass {position}/{total} ({audit_pass['label']}) already audited "
                  f"('{findings_file}'): skipped.")
            continue
        if os.path.exists(findings_file):
            # Half-written or malformed residue from an interrupted run: we start clean.
            try:
                os.remove(findings_file)
                print(f"🧹 residual '{findings_file}' (incomplete or malformed) deleted: "
                      f"the pass is replayed.")
            except OSError:
                pass

        if not pass_needs_agent(audit_pass):
            # Slice with no pattern of the pack: NA verdicts by deterministic routing, written
            # in the agent passes' format — no LLM turn paid, traced in the appendix.
            write_mechanical_na_findings(audit_pass)
            print(f"⏭️  Pass {position}/{total} ({audit_pass['label']}): no pattern of the pack "
                  f"in this slice → mechanical NA ('{findings_file}'), no agent requested.")
            continue

        print(f"\n{'='*50}\n🔎 PASS {position}/{total}: {audit_pass['label']}\n{'='*50}")

        attempts = 0
        success = False
        error_history = []   # failures of previous attempts (cumulative feedback)

        while not success and attempts < MAX_ATTEMPTS:
            attempts += 1

            # Catch-up for a LATE deliverable: the agent from the previous attempt may
            # have finished writing AFTER the orchestrator's timeout.
            if attempts > 1 and findings_ok(findings_file, pack):
                print(f"   ♻️  '{findings_file}' finally arrived (late deliverable): accepted.")
                success = True
                break

            cleanup_slot_sentinels(slot)
            print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] {audit_pass['label']} — "
                  f"launching the accessibility Auditor...")

            prompt = build_auditor_prompt(audit_pass, trunk_text, position, total,
                                          contrast_block,
                                          compose_retry_feedback(error_history), attempts,
                                          trigger_hits, sonde_hits)
            mm_audit.event("agent_task", prompt_bytes=len(prompt))
            RUNNER.send_task(prompt)

            got_deliverable, wait_reason = wait_for_deliverable(
                findings_file, a11y_sentinel(slot, attempts),
                structural_check=findings_structural_check)
            # Read-only guard after EACH attempt (successful or not): an auditor that
            # "fixed" code along the way is restored immediately.
            enforce_readonly(audit_pass["label"])

            if not got_deliverable:
                if wait_reason == "sentinelle_sans_livrable":
                    error_history.append(
                        f"On the previous pass, you created the sentinel WITHOUT the findings "
                        f"file: write '{findings_file}' in full FIRST, the "
                        f"sentinel as the very LAST action.")
                elif wait_reason == "stable_hors_format":
                    error_history.append(
                        f"On the previous pass, '{findings_file}' stayed off-format: "
                        f"the '## Verdicts', '## Findings' and '## Summary' sections are "
                        f"MANDATORY. Rewrite the file entirely in the common trunk's "
                        f"locked format, then create the sentinel.")
                    # Off-format residue: we start clean (same hygiene as the
                    # parser's rejection).
                    try:
                        os.remove(findings_file)
                    except OSError:
                        pass
                else:
                    error_history.append(
                        "On the previous pass, no deliverable was received (findings "
                        "file absent, empty or never signaled). First write the complete "
                        "findings file, THEN the sentinel, in that order.")
                print(f"⏱️  The auditor did not signal the end of the pass. Retrying.")
                reset_agent_session()
                continue

            # STRONG floor afterwards: the findings parser. Its errors become the
            # feedback of the next attempt (the locked format is re-explained).
            _data, fatal, soft = parse_findings_file(findings_file, pack)
            if fatal and _data is not None and bilan_only_fatals(fatal) \
                    and repair_bilan_line(findings_file, _data["verdicts"]):
                # Sole anomaly: the Summary (a checksum with no information). Python
                # rewrites it from the parsed verdicts and re-parses: the pass is not
                # replayed — no risk, the verdicts passed every other check.
                print(f"   🔧 Summary repaired mechanically ({' ; '.join(fatal[:2])}): "
                      f"the verdicts passed every other check, the pass is not "
                      f"replayed.")
                _data, fatal, soft = parse_findings_file(findings_file, pack)
            if fatal:
                error_history.append(
                    f"Your file '{findings_file}' does not pass the mechanical check: "
                    + " ; ".join(fatal[:6])
                    + f". Reminders: the '## Verdicts' section lists EXACTLY these criteria "
                      f"({', '.join(pack['criteres'])}) with a status C, NC, NA or AVM ; "
                      f"each NC has a finding '### K<i> — <criterion> — <title>' with its "
                      f"'- **Excerpt:**' field copying EXACTLY a line of the cited file ; the Summary "
                      f"is locked '- Verdicts: C: <a>, NC: <b>, NA: <c>, AVM: <d>'. "
                      f"Rewrite the file entirely.")
                try:
                    os.remove(findings_file)
                except OSError:
                    pass
                print(f"⚠️  [REJECT] Attempt {attempts}: verdicts malformed "
                      f"({len(fatal)} issue(s): {' ; '.join(fatal[:3])}).")
                reset_agent_session()
                continue
            if soft:
                print(f"   ℹ️  Tolerated imperfections ({len(soft)}): {' ; '.join(soft[:3])}"
                      + ("…" if len(soft) > 3 else ""))
            if audit_pass.get("declenche") and findings_all_c(_data):
                print(f"   ⚠️  SUSPICIOUS PASS (C): overwhelmingly C verdicts with no "
                      f"finding and no AVM on a TRIGGERED pack — possible fabricated "
                      f"compliance. No automatic retry (arbitration stays human): re-read "
                      f"'{findings_file}'; the report flags it in an appendix.")
            if audit_pass.get("declenche") and findings_all_na(_data):
                print(f"   ⚠️  SUSPICIOUS PASS: this pack was routed by a TRIGGER (its patterns "
                      f"exist in the compartment's files) yet 100% of the verdicts "
                      f"are NA — inconsistent by construction. No automatic retry "
                      f"(arbitration stays human): re-read '{findings_file}' and delete it "
                      f"to replay the pass if needed; the report flags it in an appendix.")

            success = True

        if not success:
            # Passes are independent: we MARK the failure and CONTINUE (this pass's
            # criteria will fall back to cautious AVM at aggregation, the final
            # report will be marked PARTIAL) — except for the circuit breaker below.
            reason = compose_retry_feedback(error_history)
            failed.append({"label": audit_pass["label"],
                           "findings_path": findings_file, "reason": reason})
            consecutive_failures += 1
            print_pass_failure(audit_pass["label"], reason)
            if pass_failure_breaker(consecutive_failures, len(failed), position):
                cleanup_all_a11y_sentinels()
                fail_a11y(f"❌ Circuit breaker: {len(failed)} failed pass(es) "
                          f"(including {consecutive_failures} consecutive) out of {position} "
                          f"processed — the model is stalling systematically, stopping before the "
                          f"{total - position} remaining pass(es).",
                          details="\n".join(f"- {f['label']}: {f['reason']}" for f in failed),
                          title="Audit circuit breaker")
            print(f"⚠️  Pass {position}/{total} not completed: the run CONTINUES (independent "
                  f"passes). Its criteria will come out as cautious AVM; relaunch the "
                  f"pipeline after the run to replay it.")
            cleanup_slot_sentinels(slot)
            continue

        consecutive_failures = 0
        print(f"✅ Pass {position}/{total} completed: verdicts in '{findings_file}'.")
        cleanup_slot_sentinels(slot)
        reset_agent_session()
    return failed


# ─── STEP S4 (COMPUTE): 100% PYTHON AGGREGATION ───────────────────────────────
# The final "compiler": consolidation of verdicts per criterion (NC > AVM > C > NA),
# range-based rate, findings copied over — zero LLM, zero loss, atomic write.

CONSOLIDATION_ORDER = {"NC": 0, "AVM": 1, "C": 2, "NA": 3}


def criteria_sort_key(critere: str):
    return [int(x) for x in critere.split(".")]


def aggregate(passes: list, packs: list) -> dict:
    """Re-reads every verdict file (already validated by the parser) and consolidates.

    Returns {"criteria": {criterion: {...}}, "topics": [...], "totals": {...},
             "rate_floor"/"rate_central"/"rate_ceiling", "top_ncs": [...],
             "unreadable": [...]} — the sole material for the report and the summary.
    """
    criteria = {}
    for pack in packs:
        for crit in pack["criteres"]:
            criteria[crit] = {"statut": None, "notes": [], "constats": [],
                              "impact_max": None, "pack": pack, "passes": 0}

    unreadable = []
    requalified_manual_c = 0   # C requalified as AVM by the testability rule (H4)
    seen_constats = set()   # (criterion, location) already reported: a single defect seen
                            # by two passes (shared component) appears only once
    for audit_pass in passes:
        pack = audit_pass["pack"]
        data, fatal, _soft = parse_findings_file(audit_pass["findings_path"], pack)
        if fatal or data is None:
            # Should not happen (the passes guarantee the parser): a file that degrades
            # AFTER the fact is flagged, its criteria fall back to a cautious AVM.
            unreadable.append(audit_pass["label"])
            for crit in pack["criteres"]:
                entry = criteria[crit]
                entry["passes"] += 1
                if entry["statut"] is None or CONSOLIDATION_ORDER["AVM"] < CONSOLIDATION_ORDER[entry["statut"]]:
                    entry["statut"] = "AVM"
                entry["notes"].append(f"unreadable verdicts at aggregation ({audit_pass['label']}): "
                                      f"delete '{audit_pass['findings_path']}' and re-run")
            continue
        for crit, verdict in data["verdicts"].items():
            entry = criteria[crit]
            entry["passes"] += 1
            statut = verdict["statut"]
            # The grid's iron rule, enforced by the CODE (H4): a C on a criterion of
            # "manual" testability is not provable statically — requalified as a
            # cautious AVM, with a visible note. NC (observed, located) and NA
            # stay accepted as they are.
            if statut == "C" and pack["testabilite"].get(crit) == "manual":
                statut = "AVM"
                requalified_manual_c += 1
                entry["notes"].append(f"C not provable statically (testability: "
                                      f"manual): requalified as AVM "
                                      f"({audit_pass['bucket']['label']})")
            if entry["statut"] is None or CONSOLIDATION_ORDER[statut] < CONSOLIDATION_ORDER[entry["statut"]]:
                entry["statut"] = statut
            if verdict["note"] and statut in ("AVM", "NA"):
                entry["notes"].append(f"{verdict['note']} ({audit_pass['bucket']['label']})")
        for constat in data["constats"]:
            entry = criteria[constat["critere"]]
            key = (constat["critere"], constat["localisation"])
            if constat["localisation"] and key in seen_constats:
                continue  # same defect already reported by another pass (shared component)
            seen_constats.add(key)
            constat = dict(constat)
            constat["origine"] = audit_pass["bucket"]["name"]
            entry["constats"].append(constat)
            if constat["impact"] is not None:
                entry["impact_max"] = max(entry["impact_max"] or 0, constat["impact"])

    # Criteria never covered by a pass: mechanical NA (pack not triggered or
    # empty compartments) — the reason appears in the report, never a silent gap.
    for crit, entry in criteria.items():
        if entry["statut"] is None:
            if entry["pack"]["toujours"]:
                # H10 (minimal): the 'toujours' guarantee only lives on the socle — an
                # empty socle left it silent, and these STRUCTURAL criteria (lang,
                # <title>, skip link) wrongly came out "NA" although the ABSENCE of
                # the patterns is precisely the potential defect.
                entry["statut"] = "AVM"
                entry["notes"].append("structural pack never executed (empty socle or no "
                                      "trigger): check the host document")
            else:
                entry["statut"] = "NA"
                entry["notes"].append("no trigger for this pack detected in the scope "
                                      "(deterministic routing): content absent")

    topics = []
    for pack in packs:
        counts = {s: 0 for s in STATUSES}
        for crit in pack["criteres"]:
            counts[criteria[crit]["statut"]] += 1
        topics.append({"id": pack["id"], "nom": pack["nom"], **counts})

    totals = {s: sum(t[s] for t in topics) for s in STATUSES}

    def rate(numerator, denominator):
        return round(100.0 * numerator / denominator, 1) if denominator else 100.0

    c, nc, avm = totals["C"], totals["NC"], totals["AVM"]
    stats = {
        "criteria": criteria,
        "topics": topics,
        "totals": totals,
        "rate_central": rate(c, c + nc),
        "rate_floor": rate(c, c + nc + avm),
        "rate_ceiling": rate(c + avm, c + nc + avm),
        "unreadable": unreadable,
        "requalified_manual_c": requalified_manual_c,
    }

    ncs = [{"critere": crit, "nom_pack": entry["pack"]["nom"],
            "impact": entry["impact_max"],
            "titre": (entry["constats"][0]["titre"] if entry["constats"] else "(untitled finding)")}
           for crit, entry in criteria.items() if entry["statut"] == "NC"]
    ncs.sort(key=lambda n: (-(n["impact"] or 0), criteria_sort_key(n["critere"])))
    stats["top_ncs"] = ncs[:10]
    return stats


# ─── STEP S3: EXECUTIVE SUMMARY (SHORT LLM, MECHANICAL FALLBACK) ─────────────

def mechanical_synthesis(stats: dict) -> str:
    """100% Python fallback for the summary: a failed header must never
    invalidate N successful passes — the valuable content is already aggregated."""
    totals = stats["totals"]
    lines = ["## Executive summary", "",
             f"Static RGAA 4.1.2 pre-audit: {totals['C']} compliant criterion(s), "
             f"{totals['NC']} non-compliant, {totals['NA']} not applicable and "
             f"{totals['AVM']} requiring manual verification. Demonstrable compliance: "
             f"{stats['rate_central']}% (range {stats['rate_floor']}% to "
             f"{stats['rate_ceiling']}% depending on the outcome of manual checks).",
             ""]
    if stats["top_ncs"]:
        lines.append("Priority work items (highest-impact non-conformities):")
        for n in stats["top_ncs"][:3]:
            lines.append(f"- Criterion {n['critere']} ({n['nom_pack']}): {n['titre']}")
        lines.append("")
    lines.append("(Summary generated mechanically: the drafting pass did not complete. "
                 "This document is a static pre-audit, not a certificate of compliance.)")
    return "\n".join(lines) + "\n"


def run_synthesis(stats: dict):
    """Step S3: the only report content that requires genuine cross-cutting writing —
    short, so entrustable to an agent without saturation risk. ALWAYS replayed (it
    must reflect the up-to-date verdicts); NON-blocking (mechanical fallback)."""
    print(f"\n{'='*50}\n🪧 STEP S3: EXECUTIVE SUMMARY (REPORT HEADER)\n{'='*50}")

    if os.path.exists(SYNTHESIS_FILE):
        try:
            os.remove(SYNTHESIS_FILE)
            print(f"   🧹 Residual '{SYNTHESIS_FILE}' deleted (the summary is regenerated).")
        except OSError:
            pass

    attempts = 0
    success = False
    error_history = []   # failures of previous attempts (cumulative feedback)
    while not success and attempts < MAX_ATTEMPTS:
        attempts += 1

        # Recovery of a LATE deliverable (same logic as the other passes).
        if attempts > 1 and os.path.exists(SYNTHESIS_FILE) \
                and os.path.getsize(SYNTHESIS_FILE) > 0 \
                and synthesis_structural_check(SYNTHESIS_FILE):
            print(f"   ♻️  '{SYNTHESIS_FILE}' finally arrived (late deliverable): accepted.")
            success = True
            break

        cleanup_slot_sentinels("synthese")
        print(f"\n🚀 [ATTEMPT {attempts}/{MAX_ATTEMPTS}] Launching the Summary Writer...")

        prompt = build_synthesis_prompt(stats, stats["top_ncs"],
                                        compose_retry_feedback(error_history), attempts)
        mm_audit.event("agent_task", prompt_bytes=len(prompt))
        RUNNER.send_task(prompt)

        got_deliverable, wait_reason = wait_for_deliverable(
            SYNTHESIS_FILE, a11y_sentinel("synthese", attempts),
            structural_check=synthesis_structural_check)
        enforce_readonly("Summary")

        if not got_deliverable or not synthesis_structural_check(SYNTHESIS_FILE):
            if os.path.exists(SYNTHESIS_FILE) and not synthesis_structural_check(SYNTHESIS_FILE):
                try:
                    os.remove(SYNTHESIS_FILE)
                except OSError:
                    pass
            if wait_reason == "sentinelle_sans_livrable":
                error_history.append(
                    f"On the previous pass, you created the sentinel WITHOUT the summary: "
                    f"write '{SYNTHESIS_FILE}' FIRST, the sentinel as the very LAST "
                    f"action.")
            else:
                error_history.append(
                    f"On the previous pass, the summary was missing or out of format: "
                    f"the file '{SYNTHESIS_FILE}' must begin EXACTLY with the line "
                    f"'## Executive summary' (8 to 15 lines in total).")
            print("⏱️  Summary missing or out of format. Retrying.")
            reset_agent_session()
            continue
        success = True

    cleanup_slot_sentinels("synthese")
    if not success:
        # GRACEFUL DEGRADATION (same contract as the documentation overview):
        # a failed header must not invalidate N successful passes — mechanical fallback.
        print(f"⚠️  Summary not completed after {MAX_ATTEMPTS} attempts: MECHANICAL fallback. "
              f"The valuable content is already in the aggregated verdicts.")
        with open(SYNTHESIS_FILE, "w", encoding="utf-8") as f:
            f.write(mechanical_synthesis(stats))
        reset_agent_session()
        return

    print(f"✅ Executive summary ready: '{SYNTHESIS_FILE}'.")


# ─── STEP S4 (DELIVERABLES): REPORT & SUMMARY (PYTHON, ATOMIC WRITE) ─────────

IMPACT_LABELS = {4: "Blocking", 3: "Major", 2: "Moderate", 1: "Minor", None: "Impact not specified"}


def escape_md_cell(text: str) -> str:
    """Neutralizes vertical bars in a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def atomic_write(path: str, content: str):
    """ATOMIC write: temporary file INSIDE the project (not /tmp — 3-OS
    constraint) then os.replace — a Ctrl+C during the write never leaves a
    truncated deliverable."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def manual_deliverable_exists(path: str) -> bool:
    """Does a deliverable exist WITHOUT the factory marker (hand-written)?"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            _a11y_txt = f.read()
            return A11Y_MARKER not in _a11y_txt and A11Y_MARKER_LEGACY not in _a11y_txt
    except OSError:
        return True


def read_synthesis_or_fallback(stats: dict) -> str:
    """Header content (it already carries its title '## Executive summary'), or
    mechanical fallback if the file is missing/out of format."""
    if os.path.exists(SYNTHESIS_FILE) and synthesis_structural_check(SYNTHESIS_FILE):
        with open(SYNTHESIS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return mechanical_synthesis(stats).strip()


def assemble_report(stats: dict, a11y_map: dict, passes: list, packs: list,
                    contrasts: list, scope_files: list, failed_passes: list = None,
                    trigger_hits: dict = None, trigger_sondes: dict = None) -> None:
    """The report's final "compiler". ALWAYS replayed at end of run (reflects the
    up-to-date verdicts). Copies, computes, invents nothing."""
    print(f"\n{'='*50}\n🧩 STEP S4: MECHANICAL ASSEMBLY → '{A11Y_REPORT_FILE}'\n{'='*50}")
    criteria = stats["criteria"]
    totals = stats["totals"]
    failed_passes = failed_passes or []
    project = str(a11y_map.get("project") or os.path.basename(os.getcwd()))
    buckets = build_buckets(a11y_map)
    skipped = skipped_packs(passes, packs)
    mechanical = mechanical_na_passes(passes)
    paid = [p for p in passes if pass_needs_agent(p)]

    parts = [f"# Accessibility pre-audit (RGAA 4.1.2) — {project}", "", A11Y_MARKER, "",
             f"*Generated on {time.strftime('%Y-%m-%d')} by `Pre-Audit-A11Y-RGAA.py` — "
             f"automated STATIC pre-audit: {len(paid)} audit pass(es) "
             f"(pack × compartment)"
             + (f" + {len(mechanical)} NA slice(s) by deterministic routing" if mechanical else "")
             + f", {len(scope_files)} UI file(s), "
             f"{len(criteria)} RGAA criteria evaluated.*", "",
             f"> ⚠️ **Status of this document**: pre-audit performed by static analysis of "
             f"the source code. It does NOT replace an RGAA conformity audit (keyboard "
             f"tests, screen readers, 200% zoom, actual rendering): {totals['AVM']} "
             f"criterion(s) remain \"Requires manual verification\" (AVM), listed in the appendix. "
             f"The compliance rate is therefore given as a range.", ""]
    if failed_passes:
        parts += [f"> 🚧 **PARTIAL report**: {len(failed_passes)} audit pass(es) not "
                  f"completed after {MAX_ATTEMPTS} attempts (their criteria are consolidated "
                  f"as cautious AVM): "
                  + " ; ".join(escape_md_cell(f["label"]) for f in failed_passes)
                  + ". Relaunch the pipeline to replay them: usable passes "
                  "are resumed as they are.", ""]

    # Executive summary (LLM header or mechanical fallback).
    parts.append(read_synthesis_or_fallback(stats))
    parts.append("")

    # Compliance rate + per-topic table.
    parts += ["## Compliance rate", "",
              f"- Consolidated verdicts: **{totals['C']} C** (compliant), **{totals['NC']} NC** "
              f"(non-compliant), **{totals['NA']} NA** (not applicable), **{totals['AVM']} AVM** "
              f"(requires manual verification).",
              f"- **Demonstrable compliance: {stats['rate_central']}%** "
              f"(compliant criteria / (compliant + non-compliant)).",
              f"- **Range depending on the outcome of manual checks: "
              f"{stats['rate_floor']}% to {stats['rate_ceiling']}%** (AVM counted as non-"
              f"compliant for the floor, compliant for the ceiling).", "",
              "| Topic | C | NC | NA | AVM |",
              "|---|---|---|---|---|"]
    for topic in stats["topics"]:
        parts.append(f"| T{topic['id']:02d} {escape_md_cell(topic['nom'])} "
                     f"| {topic['C']} | {topic['NC']} | {topic['NA']} | {topic['AVM']} |")
    parts.append(f"| **Total** | **{totals['C']}** | **{totals['NC']}** "
                 f"| **{totals['NA']}** | **{totals['AVM']}** |")
    parts.append("")
    if stats["unreadable"]:
        parts.append(f"*(⚠️ {len(stats['unreadable'])} pass(es) unreadable at aggregation — "
                     f"criteria fell back to a cautious AVM: {', '.join(stats['unreadable'])}. "
                     f"Delete their files in '{A11Y_DIR}/' and re-run to replay them.)*")
        parts.append("")

    # Non-conformities, grouped by decreasing impact, findings copied verbatim.
    parts += ["## Non-conformities", ""]
    nc_criteria = [(crit, entry) for crit, entry in criteria.items() if entry["statut"] == "NC"]
    if not nc_criteria:
        parts += ["No non-conformity demonstrated on the audited static scope.", ""]
    for impact_level in (4, 3, 2, 1, None):
        level_entries = [(c, e) for c, e in nc_criteria if e["impact_max"] == impact_level]
        if not level_entries:
            continue
        title = IMPACT_LABELS[impact_level]
        prefix = f"Impact {impact_level} — " if impact_level else ""
        parts += [f"### {prefix}{title}", ""]
        for crit, entry in sorted(level_entries, key=lambda x: criteria_sort_key(x[0])):
            parts.append(f"#### Criterion {crit} — {entry['pack']['nom']}")
            for constat in entry["constats"]:
                loc = constat["localisation"] or "(not specified)"
                if not constat.get("localisation_verifiee", True):
                    loc += " — ⚠️ file not found in the project: to be verified"
                parts += [f"- **{escape_md_cell(constat['titre'])}** "
                          f"(impact {constat['impact'] if constat['impact'] else '?'}, "
                          f"scope: {constat['origine']})",
                          f"  - Location: {loc}",
                          ("  - Excerpt: `" + constat["extrait"] + "` — "
                           + ("✓ verified (found in the cited file)"
                              if constat.get("extrait_verifie")
                              else "⚠️ NOT found: finding to verify")
                           if constat.get("extrait")
                           else "  - Excerpt: (missing — materiality not verified)"),
                          f"  - Finding: {constat['constat'] or '(not specified)'}",
                          f"  - User impact: {constat['impact_utilisateur'] or '(not specified)'}",
                          f"  - Fix: {constat['correction'] or '(not specified)'}"]
            parts.append("")

    # Remaining manual verifications: the verification debt, explicit and actionable.
    parts += ["## Remaining manual verifications (AVM)", "",
              "These criteria cannot be decided from the code alone (visual rendering, "
              "screen reader, keyboard, zoom). To be covered during a manual verification "
              "to turn the range into a firm rate:", ""]
    avm_rows = [(crit, entry) for crit, entry in sorted(criteria.items(), key=lambda x: criteria_sort_key(x[0]))
                if entry["statut"] == "AVM"]
    if avm_rows:
        parts += ["| Criterion | Topic | To verify |", "|---|---|---|"]
        for crit, entry in avm_rows:
            note = " ; ".join(dict.fromkeys(entry["notes"]))[:220] or "(see the topic's pack)"
            parts.append(f"| {crit} | {escape_md_cell(entry['pack']['nom'])} | {escape_md_cell(note)} |")
    else:
        parts.append("None: all applicable criteria were decided statically.")
    parts.append("")

    # Compliant and not applicable: quick read, one line each. The NAs are
    # split into two families: "not detected statically" ≠ "absent" — a ROUTING NA
    # (no pass: no pack trigger in the scope) may hide content generated outside the
    # sources (CMS, dynamic), whereas a PASS NA was actually observed.
    conformes = sorted((c for c, e in criteria.items() if e["statut"] == "C"), key=criteria_sort_key)
    nas_passe = sorted((c for c, e in criteria.items()
                        if e["statut"] == "NA" and e["passes"] > 0), key=criteria_sort_key)
    nas_routage = sorted((c for c, e in criteria.items()
                          if e["statut"] == "NA" and e["passes"] == 0), key=criteria_sort_key)
    parts += ["## Compliant and not applicable criteria", "",
              f"- **Compliant ({len(conformes)})**: {', '.join(conformes) or '(none)'}",
              f"- **NA observed in a pass ({len(nas_passe)})**: {', '.join(nas_passe) or '(none)'}",
              f"- **NA not detected by static routing — dynamic/CMS content possible "
              f"({len(nas_routage)})**: {', '.join(nas_routage) or '(none)'}", ""]

    # Scope & routing appendix: what was audited, what was skipped and WHY.
    parts += ["## Appendix — Scope and routing", "",
              f"Scope: {len(scope_files)} UI file(s) discovered mechanically "
              f"(interface extensions, tests and tooling excluded). Map: '{A11Y_MAP_FILE}' "
              f"(editable, validated during the run). Detailed verdicts: one file per pass "
              f"in '{A11Y_DIR}/'.", "",
              "| Compartment | Files | Audited packs |", "|---|---|---|"]
    per_bucket = {}
    for p in paid:
        per_bucket.setdefault(p["bucket"]["label"], []).append(f"T{p['pack']['id']:02d}")
    for bucket in buckets:
        packs_label = " ".join(per_bucket.get(bucket["label"], [])) or "(none)"
        parts.append(f"| {escape_md_cell(bucket['name'])} | {len(bucket['files'])} | {packs_label} |")
    parts.append("")
    if mechanical:
        parts.append(f"Slices declared NA by the deterministic routing ({len(mechanical)}, no "
                     f"agent requested: no trigger of the pack in their files; findings file "
                     f"written mechanically in '{A11Y_DIR}/'): "
                     + ", ".join(escape_md_cell(p["label"]) for p in mechanical[:40])
                     + (f" … (+ {len(mechanical) - 40})" if len(mechanical) > 40 else "") + ".")
        parts.append("")
    if SCOPE_EXCLUSIONS["vendor"]:
        parts += [f"Out of scope — shipped third-party assets ({len(SCOPE_EXCLUSIONS['vendor'])} file(s): "
                  f"public/, static/, assets/, dsfr/, legacy bundles — the library is not the "
                  f"project; its overrides in the sources remain audited):", "",
                  summarize_by_directory(SCOPE_EXCLUSIONS["vendor"], 30), ""]
    if SCOPE_EXCLUSIONS["logic"]:
        parts += [f"Out of scope — pure logic without an interface signal "
                  f"({len(SCOPE_EXCLUSIONS['logic'])} .ts/.js file(s) with no tag, component, "
                  f"ARIA nor DOM access):", "",
                  summarize_by_directory(SCOPE_EXCLUSIONS["logic"], 30), ""]
    if skipped:
        parts.append("Packs never triggered (no pattern detected in the scope — their "
                     "criteria are declared NA by the deterministic routing): "
                     + ", ".join(f"T{p['id']:02d} {p['nom']}" for p in skipped) + ".")
        parts.append("")
    suspicious = suspicious_all_na_passes(passes)
    if suspicious:
        parts.append("⚠️ SUSPICIOUS pass(es) — pack routed by trigger (patterns present "
                     "in the compartment files) yet 100% of verdicts NA, "
                     "inconsistent by construction: to be reviewed; to replay a pass, "
                     "delete its verdicts file and re-run. Passes concerned: "
                     + ", ".join(p["label"] for p in suspicious) + ".")
        parts.append("")
        # The scan's hits in plain sight of the human arbitrator: the contradiction
        # (patterns present / 100% NA) is judged on evidence.
        for suspect in suspicious:
            shown = []
            for path in suspect["bucket"]["files"]:
                hit = (trigger_hits or {}).get((suspect["pack"]["id"], path))
                if hit:
                    shown.append(f"  - `{path}:{hit[0]}` — pattern \"{escape_md_cell(hit[1])}\"")
            if shown:
                parts.append(f"Patterns detected for {escape_md_cell(suspect['label'])}:")
                parts += shown[:MAX_TRIGGER_HITS_IN_PROMPT]
                parts.append("")
    all_c = suspicious_all_c_passes(passes)
    if all_c:
        parts.append("⚠️ SUSPICIOUS pass(es) on the C side — triggered pack yet "
                     "overwhelmingly C verdicts with no finding and no AVM (possible "
                     "fabricated compliance): to be reviewed; to replay a pass, delete "
                     "its verdicts file and re-run. Passes concerned: "
                     + ", ".join(p["label"] for p in all_c) + ".")
        parts.append("")
    c_suspects = suspicious_c_verdicts(passes, trigger_sondes)
    if c_suspects:
        parts.append("⚠️ SUSPICIOUS C verdict(s) — a mechanical probe detects a quasi-certain "
                     "non-compliance pattern, yet the pass answered C on the probed "
                     "criterion. Never an automatic verdict: to be reviewed, arbitration is human:")
        for suspect in c_suspects[:MAX_TRIGGER_HITS_IN_PROMPT]:
            parts.append(f"  - criterion {suspect['critere']} (C) versus `{suspect['path']}:"
                         f"{suspect['line']}` — pattern \"{escape_md_cell(suspect['motif'])}\" "
                         f"(NC {suspect['confiance']}) — pass "
                         f"{escape_md_cell(suspect['pass']['label'])}")
        parts.append("")
    if contrasts:
        parts += ["Mechanical contrast measurements (literal color/background pairs from the "
                  "same CSS block; hint provided to the Colours pass, never an automatic "
                  "verdict):", ""]
        parts += [f"- {c['ratio']}:1 — `{c['file']}:{c['line']}` ({escape_md_cell(c['selector'])})"
                  for c in contrasts]
        parts.append("")

    # Method & limits appendix: the deliverable's honesty, spelled out in full.
    # The Standard line shows the count ACTUALLY audited: an edited pack manifest
    # (union ≠ 106 criteria) is never silent in the report.
    if len(criteria) == 106:
        referentiel_line = ("- Standard: RGAA 4.1.2 (DINUM, Licence Ouverte 2.0), "
                            "106 criteria, 13 topics — WCAG 2.1 equivalences indicated "
                            "in the pack grids.")
    else:
        referentiel_line = (f"- Standard: RGAA 4.1.2 (DINUM, Licence Ouverte 2.0) — audit "
                            f"on {len(criteria)} criterion(s) of the standard, "
                            f"{len(packs)} topic(s) (edited pack manifest: not all 106 "
                            f"criteria are covered) — WCAG 2.1 equivalences "
                            f"indicated in the pack grids.")
    parts += ["## Appendix — Method and limits", "",
              "- Method: static audit of the source code, split into independent passes "
              "(one RGAA topic pack × one compartment of the interface map), context "
              "reset between each pass; verdicts checked mechanically (exact set "
              "of criteria, statuses, localized findings); aggregation and rates computed by "
              "code, with no model intervention.",
              referentiel_line]
    if stats.get("requalified_manual_c"):
        parts.append(f"- Testability enforced by the code: {stats['requalified_manual_c']} "
                     f"C verdict(s) requalified as AVM (criterion of \"manual\" testability: "
                     f"C not provable statically — the note appears on each criterion "
                     f"concerned).")
    parts += [
              "- Routing: the topic packs are activated by pattern detection in "
              "the source files; content generated outside these sources (CMS, back-office, "
              "dynamic data) may escape this detection. A criterion marked \"NA not detected "
              "by static routing\" means \"pattern not detected in the sources\", not "
              "\"content guaranteed absent from the service\".",
              "- Limits: static analysis sees neither the rendering, nor the generated DOM, nor the "
              "keyboard or screen-reader behavior; any criterion not decidable "
              "from the code is marked AVM rather than guessed. A C status means "
              "\"demonstrated on the audited files\", not \"guaranteed on the rendered page\".",
              "- STRUCTURAL blind spots of the compartment split (no cross-compartment "
              "check exists): label consistency across zones (11.3), cross-compartment "
              "CSS inheritance (10.5: color set in a zone, background inherited from the "
              "socle), navigation redefined screen by screen (12.2). These criteria stay "
              "AVM at best: cover them during the manual verification.",
              "- This document is a PRE-AUDIT AND REMEDIATION TOOL. It constitutes neither "
              "an enforceable conformity audit nor a certificate, and is not enough to publish "
              "an accessibility declaration.", ""]

    atomic_write(A11Y_REPORT_FILE, "\n".join(parts))
    print(f"✅ '{A11Y_REPORT_FILE}' assembled: {totals['NC']} NC, {totals['AVM']} AVM, "
          f"demonstrable compliance {stats['rate_central']}% "
          f"({stats['rate_floor']}–{stats['rate_ceiling']}%).")


def write_summary(stats: dict, a11y_map: dict, scope_files: list, passes: list) -> None:
    """Short results summary, 100% Python (copies and counts, invents nothing): key
    figures, demonstrated non-conformities (one line + fix each), remaining manual checks
    per topic. No regulatory frame, no administrative fields: this is a static pre-audit,
    not an accessibility declaration."""
    totals = stats["totals"]
    criteria = stats["criteria"]
    project = str(a11y_map.get("project") or os.path.basename(os.getcwd()))
    paid = [p for p in passes if pass_needs_agent(p)]
    ncs = sorted((c for c, e in criteria.items() if e["statut"] == "NC"),
                 key=lambda c: (-(criteria[c]["impact_max"] or 0), criteria_sort_key(c)))

    parts = [f"# Accessibility pre-audit summary (RGAA 4.1.2) — {project}", "",
             A11Y_MARKER, "",
             f"*Automated static pre-audit of {time.strftime('%Y-%m-%d')}: "
             f"{len(scope_files)} UI file(s), {len(paid)} pass(es), {len(criteria)} criteria "
             f"evaluated. Details (findings, locations, fixes, annexes): "
             f"'{A11Y_REPORT_FILE}'.*", "",
             "## Key figures", "",
             "| Compliant | Non-compliant | Not applicable | To verify manually |",
             "|---|---|---|---|",
             f"| {totals['C']} | {totals['NC']} | {totals['NA']} | {totals['AVM']} |", "",
             f"- Demonstrable compliance: **{stats['rate_central']}%** "
             f"(compliant / (compliant + non-compliant)).",
             f"- Range depending on the outcome of the manual checks: "
             f"**{stats['rate_floor']}% to {stats['rate_ceiling']}%**.", "",
             f"## Demonstrated non-conformities ({totals['NC']})", ""]
    if not ncs:
        parts += ["None on the static scope audited.", ""]
    for crit in ncs:
        entry = criteria[crit]
        constat = entry["constats"][0] if entry["constats"] else None
        impact = f", impact {entry['impact_max']}" if entry["impact_max"] is not None else ""
        titre = constat["titre"] if constat else "(untitled finding)"
        line = f"- **Criterion {crit}** ({entry['pack']['nom']}{impact}): {titre}"
        if constat and constat.get("localisation"):
            line += f" — {constat['localisation']}"
        parts.append(line)
        if constat and constat.get("correction"):
            parts.append(f"  - Fix: {constat['correction']}")
    parts.append("")

    avm_topics = sorted((t for t in stats["topics"] if t["AVM"]), key=lambda t: -t["AVM"])
    parts += [f"## Remaining manual checks ({totals['AVM']} criterion(s))", ""]
    if avm_topics:
        parts += ["Per topic: "
                  + ", ".join(f"{t['nom']} ({t['AVM']})" for t in avm_topics) + ".",
                  "Means: keyboard, screen reader, 200% zoom, contrasts on rendering. "
                  f"The criterion-by-criterion list is in the annex of '{A11Y_REPORT_FILE}'.", ""]
    else:
        parts += ["None: every criterion could be decided from the code.", ""]
    parts += ["*Static pre-audit: neither a conformity audit nor an accessibility declaration.*", ""]

    atomic_write(A11Y_SUMMARY_FILE, "\n".join(parts))
    print(f"✅ Results summary: '{A11Y_SUMMARY_FILE}' "
          f"({totals['NC']} NC, {totals['AVM']} AVM).")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

signal.signal(signal.SIGINT, signal_handler)


def main():
    # Run journal (black box): self-sufficient trace in .mm-runs/.
    mm_audit.start(os.getcwd(), "pre-audit-a11y", RUNNER.name,
                   model=RUNNER.configured_model())
    # A residual failReport.md from a previous run must not be mistaken for the current
    # run's one: we purge it at startup (same contract as the factory).
    if os.path.exists(FAIL_REPORT_FILE):
        os.remove(FAIL_REPORT_FILE)

    # The grids and the manifest are the reference for the ENTIRE audit: their absence is
    # an immediate failure (without them, the auditors would improvise — the factory forbids it).
    trunk_text = load_grid(A11Y_TRUNK_SKILL_FILE)
    map_grid = load_grid(A11Y_MAP_SKILL_FILE)
    missing_grids = [path for path, text in ((A11Y_TRUNK_SKILL_FILE, trunk_text),
                                             (A11Y_MAP_SKILL_FILE, map_grid))
                     if not text.strip()]
    if missing_grids:
        print(f"❌ Missing or empty grid(s): {', '.join(missing_grids)}.")
        write_fail_report("Missing audit grid",
                          f"Not found or empty: {', '.join(missing_grids)} — cannot "
                          f"audit without a reference standard.")
        sys.exit(1)
    packs, manifest_fatal = load_packs_manifest()
    if manifest_fatal:
        print(f"❌ Invalid pack manifest ('{A11Y_PACKS_FILE}'):")
        for problem in manifest_fatal:
            print(f"   - {problem}")
        write_fail_report("Invalid pack manifest", "\n".join(manifest_fatal))
        sys.exit(1)

    # Step S0: scope + routing by PYTHON (deterministic), shown to the human BEFORE
    # paying for a single agent turn.
    scope_files = discover_ui_scope()
    if not scope_files:
        print("❌ No interface file found in this directory (extensions searched: "
              + ", ".join(sorted(UI_EXTENSIONS)) + ").")
        print("   → Run the audit from the root of the project that contains the interface to evaluate.")
        write_fail_report("Empty audit scope",
                          "No interface file detected in the current directory.")
        sys.exit(1)

    print("🧮 Deterministic scope scan (pack triggers, contrasts)...")
    triggers, trigger_hits = scan_triggers(scope_files, packs)
    sonde_hits = scan_sondes(scope_files, packs)
    if sonde_hits:
        total_sondes = sum(len(v) for v in sonde_hits.values())
        print(f"   NC probes: {total_sondes} mechanical hint(s) detected — "
              f"injected into the relevant passes, confronted with the verdicts (appendix).")
    contrasts = measure_css_contrasts(scope_files)
    contrast_block = build_contrast_block(contrasts)

    existing_map = peek_a11y_map()
    files_per_pack = {p["id"]: sum(1 for hits in triggers.values() if p["id"] in hits)
                      for p in packs}
    preview = scope_files[:20]

    print(f"\n{'='*50}")
    print(f"♿ ACCESSIBILITY PRE-AUDIT (RGAA 4.1.2) — Discovered scope:")
    print(f"   Directory: {os.getcwd()}")
    print(f"   {len(scope_files)} UI file(s) to audit. Preview:")
    for f in preview:
        print(f"      - {f}")
    if len(scope_files) > len(preview):
        print(f"      … and {len(scope_files) - len(preview)} other(s).")
    if SCOPE_EXCLUSIONS["vendor"] or SCOPE_EXCLUSIONS["logic"]:
        print(f"   Mechanically out of scope (traced in the report's appendix): "
              f"{len(SCOPE_EXCLUSIONS['vendor'])} third-party asset(s) (public/, static/, dsfr/, legacy…), "
              f"{len(SCOPE_EXCLUSIONS['logic'])} pure-logic file(s) without an interface signal.")
    print(f"   Routing of the 13 topic packs (detected triggers):")
    for pack in packs:
        hits = files_per_pack[pack["id"]]
        always = " + guaranteed base pass" if pack["toujours"] else ""
        if hits:
            print(f"      - T{pack['id']:02d} {pack['nom']}: {hits} trigger file(s){always}")
        elif pack["toujours"]:
            print(f"      - T{pack['id']:02d} {pack['nom']}: no trigger{always}")
        else:
            print(f"      - T{pack['id']:02d} {pack['nom']}: no trigger → NA criteria (pack skipped)")
    if contrasts:
        print(f"   Contrasts: {len(contrasts)} literal CSS pair(s) measured "
              f"(worst ratio: {contrasts[0]['ratio']}:1) — provided to the Colours pass.")
    else:
        print(f"   Contrasts: no measurable literal color/background pair (the agent will handle "
              f"the Colours topic without a numeric hint).")
    context = business_context_file()
    if context:
        print(f"   Business context: '{context}' detected (pointed out to auditors as optional reading).")
    else:
        print(f"   Business context: none ('{SPEC_FILE}'/'{NEED_FILE}' absent) — the interface is "
              f"audited as it stands.")
    if existing_map:
        print(f"   Resume: existing map ({len(existing_map['zones'])} zone(s)) — the exact "
              f"pass count will be displayed with the map; passes already usable "
              f"in '{A11Y_DIR}/' will be skipped.")
    else:
        print(f"   Flow: 1 cartography (skipped if '{A11Y_MAP_FILE}' valid) + N audit "
              f"passes (pack × compartment, context reset between each; exact "
              f"count displayed with the map BEFORE paying) + 1 summary + Python aggregation "
              f"→ '{A11Y_REPORT_FILE}' + '{A11Y_SUMMARY_FILE}' (root).")
    for deliverable in (A11Y_REPORT_FILE, A11Y_SUMMARY_FILE):
        if manual_deliverable_exists(deliverable):
            print(f"   ⚠️  WARNING: a '{deliverable}' WITHOUT the factory marker exists at the root "
                  f"(hand-written?). The final assembly will OVERWRITE it — back it up before "
                  f"confirming if you want to keep it.")
    print(f"{'='*50}")

    confirm = input("\n▶️  Run the accessibility pre-audit on this scope? (y/n): ")
    mm_audit.event("gate", id="scope", gate_kind="yn", answer=confirm.strip().lower())
    if confirm.strip().lower() != 'y':
        print("⏹️  Cancelled by the user.")
        sys.exit(0)

    # Read-only guard: baseline captured BEFORE the first agent.
    init_readonly_guard()

    # Step S1: cartography (LLM only if needed — resume via files),
    # doubly validated (Python schema + human y/n, map editable before confirming).
    a11y_map = run_cartography(map_grid, scope_files, packs, triggers)

    # The pass matrix is frozen by the validated map: it is what indexes the
    # resume, the progress and the failure report.
    buckets = build_buckets(a11y_map)
    passes = build_pass_list(buckets, packs, triggers)
    _RUN_STATE["passes"] = passes
    # L8 (git ONLY): --rejouer-modifiees <ref> invalidates every pass where a
    # compartment file changed since <ref> — after a remediation cycle, hand-picking
    # the verdict files to delete is error-prone (a stale verdict would stay in the
    # report as if it were current).
    if "--rejouer-modifiees" in sys.argv:
        flag_idx = sys.argv.index("--rejouer-modifiees")
        since_ref = sys.argv[flag_idx + 1] if flag_idx + 1 < len(sys.argv) else "HEAD"
        if shutil.which("git") is None or not os.path.isdir(".git"):
            fail_a11y("❌ --rejouer-modifiees requires a git repository: without it, delete "
                      f"the verdict files to replay in '{A11Y_DIR}/' manually, "
                      "then relaunch.", title="Diff-aware resumption without git")
        ok_diff, diff_out = run_git(["diff", "--name-only", since_ref])
        if not ok_diff:
            fail_a11y(f"❌ 'git diff --name-only {since_ref}' failed (invalid ref?).",
                      title="Diff-aware resumption: unreadable ref")
        changed_files = [l.strip() for l in diff_out.splitlines() if l.strip()]
        stale = invalidated_passes(passes, changed_files)
        if stale:
            print(f"♻️  --rejouer-modifiees {since_ref}: {len(stale)} pass(es) invalidated "
                  f"(compartment files changed since the ref):")
            for stale_pass in stale:
                print(f"   - {stale_pass['label']}")
                try:
                    os.remove(stale_pass["findings_path"])
                except OSError:
                    pass
            mm_audit.event("guard", name="rejouer_modifiees", action="invalidation",
                           ref=since_ref, passes=len(stale))
        else:
            print(f"♻️  --rejouer-modifiees {since_ref}: no pass to invalidate.")

    if not passes:
        fail_a11y("❌ No audit pass to launch: no pack is triggered on the "
                  "map's compartments (scope with no recognized interface content?).",
                  title="Empty pass matrix")

    # 🚀 Boot the harness Data Center in tmux (no-op if cartography already launched it).
    RUNNER.start()

    # Step S2: the audit passes (a fresh session per pass).
    failed_passes = run_audit_passes(passes, trunk_text, contrast_block, trigger_hits,
                                     sonde_hits)

    # Step S4 (computation): mechanical aggregation of the verdicts.
    stats = aggregate(passes, packs)

    # Step S3: executive summary (non-blocking: mechanical fallback after 3 failures).
    run_synthesis(stats)

    # Step S4 (deliverables): report + results summary.
    assemble_report(stats, a11y_map, passes, packs, contrasts, scope_files, failed_passes,
                    trigger_hits, sonde_hits)
    write_summary(stats, a11y_map, scope_files, passes)

    # Last pass of the read-only guard: covers the window between the last enforce
    # of a pass and the end of the run (notably the "late deliverable accepted" path).
    enforce_readonly("final")

    # Cleanup of temporary files and sentinels, then clean shutdown.
    for tmp_f in [TMP_A11Y_FILE, TMP_PROMPT_BUFFER]:
        if os.path.exists(tmp_f):
            os.remove(tmp_f)
    cleanup_all_a11y_sentinels()
    RUNNER.kill()
    if failed_passes:
        # PARTIAL report: the failReport persists and lists the passes to replay
        # (file-based resumption does the rest at relaunch). Exit 0 is assumed:
        # the deliverable exists and it is honest (AVM + banner + appendix).
        write_fail_report(f"Partial pre-audit: {len(failed_passes)} pass(es) not completed",
                          "The run continued (independent passes): the report is generated "
                          "but PARTIAL — the criteria of the missing passes are consolidated "
                          "as cautious AVM. Relaunch the pipeline to replay these passes.",
                          details="\n".join(f"- {f['label']}: {f['reason']}"
                                            for f in failed_passes))
    elif os.path.exists(FAIL_REPORT_FILE):
        # Genuinely nominal run: no failure report must remain.
        os.remove(FAIL_REPORT_FILE)

    totals = stats["totals"]
    print(f"""
🏁 [CONGRATULATIONS] Accessibility pre-audit complete!
   📄 Consolidated report: '{A11Y_REPORT_FILE}' — demonstrable compliance {stats['rate_central']}%
      (range {stats['rate_floor']}–{stats['rate_ceiling']}%), {totals['NC']} NC, {totals['AVM']} AVM.
   📄 Results summary: '{A11Y_SUMMARY_FILE}' (key figures, non-conformities, remaining manual checks).
   🗂️  Detailed verdicts per pass: '{A11Y_DIR}/' ; interface map: '{A11Y_MAP_FILE}'.
   ♿ Recommended next step: cover the {totals['AVM']} AVM criterion(s) with a
      manual verification (keyboard, screen reader, zoom) to turn the range
      into a firm rate.
   ♻️  To replay ONE pass (after fixing the code, e.g.): delete its file in
      '{A11Y_DIR}/' and re-run — only the missing one is replayed, the aggregation is redone.
      To redo everything (map included): delete '{A11Y_DIR}/' and '{A11Y_MAP_FILE}' then re-run.""")
    # Closing the run journal (path captured BEFORE end, which resets the state).
    # A run with missing passes closes as "partial": the journal tells the truth.
    journal_dir = mm_audit.run_dir()
    mm_audit.end("partial" if failed_passes else "success")
    if journal_dir:
        print(f"   📁 Run journal: {os.path.relpath(journal_dir)}/")
    if failed_passes:
        print(f"""
⚠️  PARTIAL REPORT: {len(failed_passes)} pass(es) not completed out of {len(passes)} — their criteria
   are consolidated as cautious AVM. Relaunch the pipeline to replay them (usable
   passes are resumed as they are). Details: '{FAIL_REPORT_FILE}'.""")


mm_core.configure(
    RUNNER=RUNNER,
)


if __name__ == "__main__":
    main()
