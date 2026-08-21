#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_audit — run journal (black box): every run leaves a self-sufficient trace
───────────────────────────────────────────────────────────────────────────────────────
Module imported by the orchestrators (NEVER an entry point: excluded from build.yml's
Nuitka loop, embedded by import tracking like mm_runner.py).

Every run writes, as it goes, a `.mm-runs/<UTC yyyymmdd-hhmmss>-<orch>/` folder
at the root of the piloted project:

    run.json        manifest: orchestrator, harness, start/end, final status,
                    aggregated counters (solicitations, verdicts, gates, guards)
    events.jsonl    append-only, one line = one event, flushed on every write
                    (crash-safe: the journal survives a kill -9 of the run)
    artifacts/      frozen copies (approved spec, blackboard at each transition…)
    summary.md      readable digest, generated 100% by Python at the end

CONTRACT (non-negotiable):
- The audit NEVER makes a run fail: every public method swallows its exceptions
  and moves on. A full disk, a missing permission → the journal goes quiet.
- FULL no-op when disabled: `MM_AUDIT=0` in the environment, or initialization
  failure.
- PURELY additive: file-based resumption keeps working on the artifacts at the
  project root, NEVER on `.mm-runs/` — this folder is a log, not a state.
- Retention: on `start()`, the oldest runs beyond RETENTION_RUNS are pruned
  (maintainer's decision B: 20).

Events (kinds frozen at v1 — the JSONL is additive: FIELDS may be added later
without migration, never removed):
    run_start    {orchestrator, distro_version, harness, model}
    step_start   {step} · phase_start {id, name, skills}
    agent_task   {role, attempt, prompt_bytes}
    sentinel     {path, declared_files}
    verdict      {cmd, exit, duration_s, output_bytes, attempt}
    guard        {name, action}
    gate         {id, gate_kind, answer}
    phase_status {id, status} · run_end {status, totals}
"""

import json
import os
import shutil
import time

RUNS_DIR       = ".mm-runs"
RETENTION_RUNS = 20     # decision B: we keep the last 20 runs per piloted project

# Current run state (module-level singleton). dir=None ⇔ journal disabled.
_STATE = {"dir": None, "events_path": None, "started": None, "meta": {}, "counters": {}}


def _reset():
    _STATE.update(dir=None, events_path=None, started=None, meta={}, counters={})


def enabled() -> bool:
    """Is the journal active for this run?"""
    return _STATE["dir"] is not None


def run_dir() -> str:
    """Path of the current run folder ('' if the journal is disabled) — for the
    orchestrators' final digest line."""
    return _STATE["dir"] or ""


def _prune(runs_root: str):
    """Prunes the oldest runs beyond RETENTION_RUNS (folder names start with the
    UTC timestamp: lexicographic order is chronological order). Best-effort: a
    stubborn folder is left in place."""
    try:
        entries = sorted(e for e in os.listdir(runs_root)
                         if os.path.isdir(os.path.join(runs_root, e)))
    except OSError:
        return
    for stale in entries[:-RETENTION_RUNS] if len(entries) > RETENTION_RUNS else []:
        shutil.rmtree(os.path.join(runs_root, stale), ignore_errors=True)


def start(project_dir: str, orchestrator_id: str, harness_name: str,
          distro_version: str = "", model: str = ""):
    """Opens the run journal. Silent and no-op if MM_AUDIT=0 or if anything at all
    fails (the run knows nothing about it and carries on)."""
    try:
        _reset()
        if os.environ.get("MM_AUDIT", "").strip() == "0":
            return
        runs_root = os.path.join(project_dir, RUNS_DIR)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        run_path = os.path.join(runs_root, f"{stamp}-{orchestrator_id}")
        suffix = 1
        while os.path.exists(run_path):   # two runs within the same second
            suffix += 1
            run_path = os.path.join(runs_root, f"{stamp}-{orchestrator_id}-{suffix}")
        os.makedirs(os.path.join(run_path, "artifacts"))
        _STATE.update(dir=run_path,
                      events_path=os.path.join(run_path, "events.jsonl"),
                      started=time.time(),
                      meta={"orchestrator": orchestrator_id,
                            "distro_version": distro_version,
                            "harness": harness_name,
                            "model": model,
                            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         time.gmtime())},
                      counters={})
        _prune(runs_root)
        event("run_start", orchestrator=orchestrator_id, distro_version=distro_version,
              harness=harness_name, model=model)
    except Exception:
        _reset()


def event(kind: str, /, **fields):
    """Appends one line to events.jsonl (immediate flush: crash-safe). Swallows all.
    'ts' and 'kind' are reserved (a gate's type is named 'gate_kind')."""
    if not enabled():
        return
    try:
        for reserved in ("ts", "kind"):
            fields.pop(reserved, None)
        record = {"ts": round(time.time() - _STATE["started"], 3), "kind": str(kind)}
        record.update(fields)
        with open(_STATE["events_path"], "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            f.flush()
        _STATE["counters"][kind] = _STATE["counters"].get(kind, 0) + 1
    except Exception:
        pass


def snapshot(path: str):
    """Frozen copy of an artifact into artifacts/ (timestamped: several snapshots of
    the same file coexist). Swallows all — a missing file is not an error."""
    if not enabled():
        return
    try:
        if not os.path.isfile(path):
            return
        stamp = time.strftime("%H%M%S", time.gmtime())
        name = f"{stamp}__{os.path.basename(path)}"
        dest = os.path.join(_STATE["dir"], "artifacts", name)
        suffix = 1
        while os.path.exists(dest):
            suffix += 1
            dest = os.path.join(_STATE["dir"], "artifacts",
                                f"{stamp}__{suffix}__{os.path.basename(path)}")
        shutil.copyfile(path, dest)
        event("snapshot", path=path, saved_as=os.path.basename(dest))
    except Exception:
        pass


def end(status: str):
    """Closes the journal: final event, run.json, summary.md. Swallows all."""
    if not enabled():
        return
    try:
        totals = dict(_STATE["counters"])
        event("run_end", status=str(status), totals=totals)
        meta = dict(_STATE["meta"])
        meta.update(status=str(status),
                    ended_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    duration_s=round(time.time() - _STATE["started"], 1),
                    counters=_STATE["counters"])
        with open(os.path.join(_STATE["dir"], "run.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        with open(os.path.join(_STATE["dir"], "summary.md"), "w", encoding="utf-8") as f:
            f.write(_summary_text(meta))
    except Exception:
        pass
    finally:
        _reset()


def _summary_text(meta: dict) -> str:
    """Readable digest of the run (100% Python: copies the counters, invents nothing)."""
    counters = meta.get("counters") or {}
    lines = [f"# Run journal — {meta.get('orchestrator', '?')}",
             "",
             f"- Final status: **{meta.get('status', '?')}**",
             f"- Start (UTC): {meta.get('started_utc', '?')} — duration: "
             f"{meta.get('duration_s', '?')} s",
             f"- Harness: {meta.get('harness', '?')}"
             + (f" — model: {meta['model']}" if meta.get("model") else ""),
             "",
             "## Event counters", ""]
    for kind in sorted(counters):
        lines.append(f"- {kind}: {counters[kind]}")
    lines += ["",
              "## Where to look", "",
              "- `events.jsonl`: the full chronology (one JSON line per event).",
              "- `artifacts/`: the frozen copies (approved spec, blackboard at "
              "transitions…).",
              "",
              "*Purely additive journal: resuming a run relies on the artifacts at "
              "the project root, never on this folder. Can be disabled via "
              "`MM_AUDIT=0`; retention: "
              f"{RETENTION_RUNS} runs.*", ""]
    return "\n".join(lines)
