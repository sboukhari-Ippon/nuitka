#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_mock_scenarios — les pipelines tournent encore, de bout en bout, sans LLM
────────────────────────────────────────────────────────────────────────────
Chaque scénario monte un projet JETABLE, l'équipe avec les skills de la variante,
lance un orchestrateur avec `MM_AGENT_HARNESS=mock` et un stdin pré-alimenté, puis
asserte sur l'ÉTAT FINAL : fichiers produits, statuts du blackboard, sentinelles
nettoyées, code de sortie, journal des sollicitations.

Le `verify_cmd` des scénarios lance de VRAIES commandes sur les fichiers écrits par
le mock : le verdict reste l'exécution, jamais un avis. Un scénario qui « passe »
sans que la commande de vérification ait réellement tourné n'aurait aucune valeur.

    python3 tools/run_mock_scenarios.py                  # tous
    python3 tools/run_mock_scenarios.py nominal reprise   # au choix
    python3 tools/run_mock_scenarios.py -k                # garder les projets jetables
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE      = os.path.dirname(os.path.abspath(__file__))
REPO      = os.path.dirname(HERE)
FIXTURES  = os.path.join(HERE, "fixtures")
GOLDENS   = os.path.join(HERE, "goldens")


# ─── Transcripts goldens (caractérisation, prérequis de la factorisation) ─────
# `--golden record` capture le stdout NORMALISÉ de chaque scénario dans
# tools/goldens/<scenario>.txt (versionné) ; `--golden check` rejoue et diffe
# strictement : toute différence = échec. Les orchestrateurs sont séquentiels :
# le stdout normalisé DOIT être déterministe — si un scénario ne l'est pas, on
# corrige la NORMALISATION, jamais le pipeline.

def normalize_transcript(stdout: str, workspace: str) -> str:
    """Efface du transcript tout ce qui varie légitimement d'un run à l'autre :
    chemins des projets jetables, noms de session (hachés sur le chemin), dates,
    durées, SHA git. Liste à compléter si un nouveau non-déterminisme apparaît."""
    text = stdout.replace(workspace, "<WS>")
    text = re.sub(r"mm-mock-[A-Za-z0-9_-]+", "<WSDIR>", text)      # résidus de tmpdir
    text = re.sub(r"\bmk-[A-Za-z0-9_-]+\b", "<SESSION>", text)     # sessions du mock
    text = re.sub(r"\b\d{8}-\d{6}\b", "<RUNID>", text)             # id de dossier .mm-runs/
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<DATE>", text)
    text = re.sub(r"\b\d{2}:\d{2}(?::\d{2})?\b", "<TIME>", text)
    text = re.sub(r"\b\d+(?:[.,]\d+)?\s*s\b", "<T>", text)         # durées « 12.3s » / « 30 s »
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "<SHA>", text)            # SHA git en contexte
    return text


def golden_path(name: str) -> str:
    return os.path.join(GOLDENS, f"{name}.txt")


def handle_golden(name: str, transcript: str, mode: str) -> list:
    """Applique le mode golden au transcript normalisé. Renvoie une liste d'échecs
    (vide si conforme ou en mode record)."""
    if mode == "record":
        os.makedirs(GOLDENS, exist_ok=True)
        with open(golden_path(name), "w", encoding="utf-8") as f:
            f.write(transcript)
        return []
    # mode == "check"
    try:
        with open(golden_path(name), "r", encoding="utf-8") as f:
            expected = f.read()
    except OSError:
        return [f"golden absent : {golden_path(name)} (lance --golden record)"]
    if transcript == expected:
        return []
    diff = "\n".join(difflib.unified_diff(
        expected.splitlines(), transcript.splitlines(),
        fromfile=f"goldens/{name}.txt", tofile="transcript courant", lineterm="", n=2))
    excerpt = "\n".join(diff.splitlines()[:40])
    return [f"transcript ≠ golden :\n{excerpt}"]


def load_scenarios():
    """Les scénarios sont des fichiers JSON de tools/fixtures/, nommés <id>.json."""
    out = {}
    if not os.path.isdir(FIXTURES):
        return out
    for name in sorted(os.listdir(FIXTURES)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
            out[name[:-5]] = json.load(f)
    return out


def equip(project, variant):
    """Équipe le projet jetable comme le ferait l'app : skills + artefacts."""
    engine = os.path.join(REPO, variant, "engine")
    shutil.copytree(os.path.join(engine, ".agents"), os.path.join(project, ".agents"))
    shutil.copytree(os.path.join(engine, ".opencode"), os.path.join(project, ".opencode"))
    return engine


def run_orchestrator(project, engine, script, scenario_path, journal_path,
                    stdin_text, env_extra=None):
    """Lance l'orchestrateur en mode terminal, cwd = projet. Aucun tmux, aucun LLM."""
    env = dict(os.environ)
    env["MM_AGENT_HARNESS"] = "mock"
    env["MM_MOCK_SCENARIO"] = scenario_path
    env["MM_MOCK_JOURNAL"] = journal_path
    # tools/ sur le PYTHONPATH : c'est ce qui rend `mm_mock_runner` importable, et
    # rien d'autre du dépôt n'en dépend.
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, "-u", os.path.join(engine, script)],
                          cwd=project, input=stdin_text, capture_output=True,
                          text=True, timeout=300, env=env)
    return proc


def journal(path):
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def check_asserts(project, proc, spec, records):
    """Assertions déclaratives du scénario. Renvoie la liste des échecs."""
    fails = []
    expect = spec.get("assert", {})

    if "exit_code" in expect and proc.returncode != expect["exit_code"]:
        fails.append(f"code de sortie {proc.returncode}, attendu {expect['exit_code']}")

    for name in expect.get("files_present", []):
        if not os.path.exists(os.path.join(project, name)):
            fails.append(f"fichier attendu absent : {name}")
    for name in expect.get("files_absent", []):
        if os.path.exists(os.path.join(project, name)):
            fails.append(f"fichier qui devrait avoir disparu : {name}")

    for needle in expect.get("stdout_contains", []):
        if needle not in proc.stdout:
            fails.append(f"sortie sans {needle!r}")
    for needle in expect.get("stdout_excludes", []):
        if needle in proc.stdout:
            fails.append(f"sortie contenant {needle!r} alors qu'elle ne devrait pas")

    # Canaris : une chaîne présente dans un fichier SEMÉ doit y être encore. C'est la
    # preuve qu'il a été repris tel quel et non régénéré — plus fiable qu'un mtime,
    # qu'une simple réécriture identique ne trahirait pas.
    for name, needle in (expect.get("canaries") or {}).items():
        path = os.path.join(project, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                if needle not in f.read():
                    fails.append(f"{name} a été RÉGÉNÉRÉ (canari {needle!r} perdu)")
        except OSError:
            fails.append(f"{name} illisible pour le contrôle de canari")

    # Contenu d'un fichier PRODUIT par le pipeline (rapport, verdicts…) : chaque
    # aiguille de la liste doit y figurer. Distinct des canaris (qui prouvent la
    # reprise d'un fichier semé) : ici on vérifie ce que l'orchestrateur a écrit.
    for name, needles in (expect.get("files_contain") or {}).items():
        path = os.path.join(project, name)
        if isinstance(needles, str):
            needles = [needles]
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            fails.append(f"{name} illisible pour le contrôle de contenu")
            continue
        for needle in needles:
            if needle not in content:
                fails.append(f"{name} ne contient pas {needle!r}")

    tasks = [r for r in records if r["event"] == "task"]
    if "tasks" in expect and len(tasks) != expect["tasks"]:
        fails.append(f"{len(tasks)} sollicitation(s) d'agent, attendu {expect['tasks']}")

    # Journal de run (.mm-runs/, plan-big-last Lot 2) : présence ordonnée d'événements
    # (sous-séquence) et statut final du run.json du DERNIER run du projet jetable.
    audit = expect.get("audit_expects")
    if audit:
        runs_root = os.path.join(project, ".mm-runs")
        run_dirs = sorted(os.listdir(runs_root)) if os.path.isdir(runs_root) else []
        if not run_dirs:
            fails.append("aucun dossier .mm-runs/ (journal de run absent)")
        else:
            run_path = os.path.join(runs_root, run_dirs[-1])
            events = []
            try:
                with open(os.path.join(run_path, "events.jsonl"), "r", encoding="utf-8") as f:
                    events = [json.loads(line) for line in f if line.strip()]
            except OSError:
                fails.append("events.jsonl absent du journal de run")
            kinds = [e.get("kind") for e in events]
            remaining = iter(kinds)
            for wanted in audit.get("events_subsequence", []):
                if not any(k == wanted for k in remaining):
                    fails.append(f"événement {wanted!r} absent (ou hors ordre) du journal — "
                                 f"kinds : {kinds}")
                    break
            wanted_status = audit.get("final_status")
            if wanted_status:
                try:
                    with open(os.path.join(run_path, "run.json"), "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("status") != wanted_status:
                        fails.append(f"statut du journal {meta.get('status')!r}, "
                                     f"attendu {wanted_status!r}")
                except OSError:
                    fails.append("run.json absent du journal de run")

    phases = expect.get("phases")
    if phases:
        path = os.path.join(project, "blackboard.yaml")
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                blackboard = yaml.safe_load(f) or {}
        except Exception as exc:
            fails.append(f"blackboard illisible ({exc.__class__.__name__})")
            blackboard = {}
        by_id = {str(p.get("id")): p for p in (blackboard.get("phases") or [])}
        for pid, wanted in phases.items():
            phase = by_id.get(str(pid))
            if phase is None:
                fails.append(f"phase {pid} absente du blackboard")
                continue
            for key, value in wanted.items():
                if str(phase.get(key)) != str(value):
                    fails.append(f"phase {pid}.{key} = {phase.get(key)!r}, "
                                 f"attendu {value!r}")
    return fails


def run_one(name, spec, variant, keep, verbose, golden=None):
    # Le projet ne contient QUE ce qu'un vrai projet contiendrait : scénario et
    # journal du mock vivent à côté, sinon les gardes git du pipeline les verraient.
    workspace = tempfile.mkdtemp(prefix=f"mm-mock-{name}-")
    project = os.path.join(workspace, "projet")
    os.makedirs(project)
    scenario_path = os.path.join(workspace, "scenario.json")
    journal_path = os.path.join(workspace, "journal.jsonl")
    try:
        engine = equip(project, variant)
        for rel, content in (spec.get("seed") or {}).items():
            path = os.path.join(project, rel)
            os.makedirs(os.path.dirname(path) or project, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        with open(scenario_path, "w", encoding="utf-8") as f:
            json.dump({"steps": spec.get("steps", [])}, f, ensure_ascii=False)

        proc = run_orchestrator(project, engine, spec["script"], scenario_path,
                                journal_path, spec.get("stdin", ""), spec.get("env"))
        records = journal(journal_path)
        fails = check_asserts(project, proc, spec, records)
        if golden:
            fails += handle_golden(name, normalize_transcript(proc.stdout, workspace),
                                   golden)

        if fails or verbose:
            print(f"  {'✗' if fails else '✓'} {name:<22} {spec.get('title','')}")
            for f_ in fails:
                print(f"      · {f_}")
            if fails:
                tail = "\n".join(proc.stdout.splitlines()[-25:])
                print(f"      ── sortie (fin) ──\n{tail}")
                if proc.stderr.strip():
                    err = "\n".join(proc.stderr.splitlines()[-15:])
                    print(f"      ── stderr (fin) ──\n{err}")
                print(f"      ── espace conservé : {workspace}")
        else:
            print(f"  ✓ {name:<22} {spec.get('title','')} "
                  f"({len([r for r in records if r['event']=='task'])} sollicitation(s))")
        return not fails, (workspace if (fails or keep) else None)
    finally:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("only", nargs="*", help="identifiants de scénarios à jouer")
    parser.add_argument("--variant", default=os.path.join("FR", "Ubuntu"))
    parser.add_argument("-k", "--keep", action="store_true",
                        help="conserver les projets jetables (diagnostic)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--golden", choices=("record", "check"),
                        help="record : (ré)écrit tools/goldens/<scenario>.txt (stdout "
                             "normalisé) ; check : rejoue ET diffe strictement contre "
                             "les goldens (toute différence = échec)")
    args = parser.parse_args()

    scenarios = load_scenarios()
    if not scenarios:
        print(f"Aucun scénario dans {FIXTURES}.")
        return 1
    chosen = {k: v for k, v in scenarios.items() if not args.only or k in args.only}
    unknown = [k for k in args.only if k not in scenarios]
    if unknown:
        print(f"Scénario(s) inconnu(s) : {', '.join(unknown)}")
        print(f"Disponibles : {', '.join(sorted(scenarios))}")
        return 1

    print(f"Scénarios MockRunner — variante {args.variant}"
          + (f" (golden {args.golden})" if args.golden else ""))
    kept, ok = [], 0
    for name in sorted(chosen):
        good, project = run_one(name, chosen[name], args.variant, args.keep, args.verbose,
                                args.golden)
        ok += good
        if project:
            kept.append(project)
        elif not args.keep:
            shutil.rmtree(project if project else "", ignore_errors=True)
    # Les projets sans échec et non conservés ont déjà été supprimés ; ceux qui
    # restent sont volontairement là.
    print(f"\n{'='*60}")
    print(f"  {ok}/{len(chosen)} scénario(s) vert(s)")
    for project in kept:
        print(f"  · espace conservé : {project}")
    print('='*60)
    return 0 if ok == len(chosen) else 1


if __name__ == "__main__":
    sys.exit(main())
