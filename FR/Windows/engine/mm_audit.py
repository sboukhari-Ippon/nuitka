#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_audit — journal de run (boîte noire) : chaque run laisse une trace auto-suffisante
───────────────────────────────────────────────────────────────────────────────────────
Module importé par les orchestrateurs (JAMAIS un point d'entrée : exclu de la boucle
Nuitka de build.yml, embarqué par suivi d'imports comme mm_runner.py).

Chaque run écrit, au fil de l'eau, un dossier `.mm-runs/<UTC yyyymmdd-hhmmss>-<orch>/`
à la racine du projet piloté :

    run.json        manifeste : orchestrateur, harness, début/fin, statut final,
                    compteurs agrégés (sollicitations, verdicts, portes, gardes)
    events.jsonl    append-only, une ligne = un événement, flush à chaque écriture
                    (crash-safe : le journal survit à un kill -9 du run)
    artifacts/      copies figées (spec approuvée, blackboard à chaque transition…)
    summary.md      bilan lisible, généré 100 % Python à la fin

CONTRAT (non négociable) :
- L'audit ne fait JAMAIS échouer un run : chaque méthode publique avale ses
  exceptions et poursuit. Un disque plein, un droit manquant → le journal se tait.
- No-op INTÉGRAL si désactivé : `MM_AUDIT=0` dans l'environnement, ou échec de
  l'initialisation.
- PUREMENT additif : la reprise par fichiers continue de se faire sur les artefacts
  à la racine du projet, JAMAIS sur `.mm-runs/` — ce dossier est un log, pas un état.
- Rétention : au `start()`, les runs les plus anciens au-delà de RETENTION_RUNS sont
  élagués (décision B du mainteneur : 20).

Événements (kinds figés v1 — le JSONL est additif : des CHAMPS pourront s'ajouter
plus tard sans migration, jamais en retirer) :
    run_start    {orchestrator, distro_version, harness, model}
    step_start   {step} · phase_start {id, name, skills}
    agent_task   {role, attempt, prompt_bytes}
    sentinel     {path, declared_files}
    verdict      {cmd, exit, duration_s, output_bytes, attempt}
    toolchain    {node_path, node_version, expected, login_path_probed}
    guard        {name, action}
    gate         {id, gate_kind, answer}
    phase_status {id, status} · run_end {status, totals}
                 status ∈ success | failed | interrupted (Ctrl-C, kill de la session)
                 | aborted (sortie sans clôture, filet atexit)

Le dossier porte aussi `orchestrator.log` : copie de tout ce que l'orchestrateur a
affiché (le pane tmux est recyclé au run suivant, ce fichier reste).
"""

import atexit
import json
import os
import shutil
import sys
import time

RUNS_DIR       = ".mm-runs"
RETENTION_RUNS = 20     # décision B : on garde les 20 derniers runs par projet piloté
EQUIP_MARKER   = ".mm-equip.json"   # source de distro_version quand l'orchestrateur ne la passe pas
ORCH_LOG       = "orchestrator.log" # copie de tout ce que l'orchestrateur affiche (le pane tmux
                                    # est recyclé au run suivant ; ce fichier reste)

# État du run courant (singleton module-level). dir=None ⇔ journal désactivé.
_STATE = {"dir": None, "events_path": None, "started": None, "meta": {}, "counters": {},
          "tee": None}


class _Tee:
    """Duplique ce qu'écrit l'orchestrateur (stdout/stderr) dans ORCH_LOG. Transparent :
    mêmes octets vers la sortie d'origine, aucune exception remontée depuis le log."""

    def __init__(self, stream, log):
        self._stream = stream
        self._log = log

    def write(self, data):
        written = self._stream.write(data)
        try:
            self._log.write(data)
            self._log.flush()
        except Exception:
            pass
        return written

    def flush(self):
        self._stream.flush()
        try:
            self._log.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_tee(run_path: str):
    """Branche la copie stdout/stderr → orchestrator.log (best-effort)."""
    try:
        log = open(os.path.join(run_path, ORCH_LOG), "a", encoding="utf-8", errors="replace")
    except OSError:
        return
    _STATE["tee"] = (sys.stdout, sys.stderr, log)
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)


def _remove_tee():
    tee = _STATE.get("tee")
    if not tee:
        return
    out, err, log = tee
    sys.stdout, sys.stderr = out, err
    try:
        log.close()
    except Exception:
        pass
    _STATE["tee"] = None


def _reset():
    _remove_tee()
    _STATE.update(dir=None, events_path=None, started=None, meta={}, counters={}, tee=None)


def _marker_distro_version(project_dir: str) -> str:
    """distro_version du marqueur d'équipement du projet ('' si absent/illisible)."""
    try:
        with open(os.path.join(project_dir, EQUIP_MARKER), "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("distro_version") or "")
    except Exception:
        return ""


def _at_exit():
    """Filet : un run qui se termine sans end() (sys.exit hors chemin nominal, exception
    non rattrapée) est clos 'aborted' plutôt que laissé sans run_end — un events.jsonl
    tronqué était indistinguable d'un crash."""
    if enabled():
        end("aborted")


atexit.register(_at_exit)


def enabled() -> bool:
    """Le journal est-il actif pour ce run ?"""
    return _STATE["dir"] is not None


def run_dir() -> str:
    """Chemin du dossier de run courant ('' si journal désactivé) — pour la ligne
    de Bilan des orchestrateurs."""
    return _STATE["dir"] or ""


def _prune(runs_root: str):
    """Élague les runs les plus anciens au-delà de RETENTION_RUNS (le nom des
    dossiers commence par l'horodatage UTC : l'ordre lexicographique est l'ordre
    chronologique). Best-effort : un dossier récalcitrant est laissé en place."""
    try:
        entries = sorted(e for e in os.listdir(runs_root)
                         if os.path.isdir(os.path.join(runs_root, e)))
    except OSError:
        return
    for stale in entries[:-RETENTION_RUNS] if len(entries) > RETENTION_RUNS else []:
        shutil.rmtree(os.path.join(runs_root, stale), ignore_errors=True)


def start(project_dir: str, orchestrator_id: str, harness_name: str,
          distro_version: str = "", model: str = ""):
    """Ouvre le journal du run. Silencieux et no-op si MM_AUDIT=0 ou si quoi que ce
    soit échoue (le run n'en sait rien et continue)."""
    try:
        _reset()
        if os.environ.get("MM_AUDIT", "").strip() == "0":
            return
        runs_root = os.path.join(project_dir, RUNS_DIR)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        run_path = os.path.join(runs_root, f"{stamp}-{orchestrator_id}")
        suffix = 1
        while os.path.exists(run_path):   # deux runs dans la même seconde
            suffix += 1
            run_path = os.path.join(runs_root, f"{stamp}-{orchestrator_id}-{suffix}")
        os.makedirs(os.path.join(run_path, "artifacts"))
        if not distro_version:
            distro_version = _marker_distro_version(project_dir)
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
        _install_tee(run_path)
        event("run_start", orchestrator=orchestrator_id, distro_version=distro_version,
              harness=harness_name, model=model)
    except Exception:
        _reset()


def event(kind: str, /, **fields):
    """Ajoute une ligne à events.jsonl (flush immédiat : crash-safe). Avale tout.
    'ts' et 'kind' sont réservés (le type d'une porte se nomme 'gate_kind')."""
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
    """Copie figée d'un artefact dans artifacts/ (horodatée : plusieurs snapshots du
    même fichier coexistent). Avale tout — un fichier absent n'est pas une erreur."""
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
    """Clôt le journal : événement final, run.json, summary.md. Avale tout."""
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
    """Bilan lisible du run (100 % Python : recopie les compteurs, n'invente rien)."""
    counters = meta.get("counters") or {}
    lines = [f"# Journal de run — {meta.get('orchestrator', '?')}",
             "",
             f"- Statut final : **{meta.get('status', '?')}**",
             f"- Début (UTC) : {meta.get('started_utc', '?')} — durée : "
             f"{meta.get('duration_s', '?')} s",
             f"- Harness : {meta.get('harness', '?')}"
             + (f" — modèle : {meta['model']}" if meta.get("model") else ""),
             "",
             "## Compteurs d'événements", ""]
    for kind in sorted(counters):
        lines.append(f"- {kind} : {counters[kind]}")
    lines += ["",
              "## Où regarder", "",
              "- `events.jsonl` : la chronologie complète (une ligne JSON par événement).",
              "- `artifacts/` : les copies figées (spec approuvée, blackboard aux "
              "transitions…).",
              "",
              "*Journal purement additif : la reprise d'un run se fait sur les artefacts "
              "à la racine du projet, jamais sur ce dossier. Désactivable via "
              "`MM_AUDIT=0` ; rétention : "
              f"{RETENTION_RUNS} runs.*", ""]
    return "\n".join(lines)
