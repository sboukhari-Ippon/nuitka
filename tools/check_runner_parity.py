#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_runner_parity — la migration vers mm_runner.py n'a RIEN changé de visible
──────────────────────────────────────────────────────────────────────────────
Le chantier « harness unifié » a sorti la couche tmux des orchestrateurs
pour la mettre dans 'engine/mm_runner.py'. Cet outil prouve que l'extraction est
neutre, sans exécuter ni tmux ni le moindre LLM :

pour chaque orchestrateur × chaque harness, il exécute la couche harness de l'ANCIEN
fork et celle de la version MIGRÉE avec 'subprocess.run' et 'time.sleep' bouchonnés,
puis compare deux choses, à l'octet :

  1. la SORTIE CONSOLE (chaque print, emoji et espace compris) ;
  2. la SÉQUENCE DE COMMANDES tmux (argv exact, dans l'ordre).

Un écart = une régression. Il n'y a pas de tolérance : ni reformulation, ni
réordonnancement, ni commande tmux en plus ou en moins.

    python3 tools/check_runner_parity.py                 # les deux harness
    python3 tools/check_runner_parity.py --harness codex
    python3 tools/check_runner_parity.py -v              # détail des écarts

Les deux forks d'origine sont attendus à côté de ce dépôt (--base / --ref) et sont
ouverts en LECTURE SEULE. Absents → SKIP explicite, jamais un faux vert.
"""

import argparse
import importlib.util
import io
import os
import contextlib
import json
import subprocess
import sys
import tempfile
import time

# Les forks source sont en LECTURE SEULE : importer un de leurs orchestrateurs ne
# doit pas y déposer de __pycache__. À poser AVANT tout import de module cible.
sys.dont_write_bytecode = True

HERE      = os.path.dirname(os.path.abspath(__file__))
REPO      = os.path.dirname(HERE)
SIBLINGS  = os.path.dirname(REPO)
BASE_DIR  = os.path.join(SIBLINGS, "MAIsterMind_App-opencode")   # fork OpenCode d'origine
REF_DIR   = os.path.join(SIBLINGS, "MAIsterMind_App-codex")      # fork Codex d'origine

# Un orchestrateur : son rôle de session et les méthodes de harness qu'il utilisait.
# 'legacy_start' diffère d'un fork à l'autre (tmux_start_opencode / tmux_start_codex).
ORCHESTRATORS = [
    # (script,                          rôle,       new_context, capture, model)
    ("Safe-Coding.py",                  "factory",  True,  True,  True),
    ("Spec.py",             "spec",     False, False, False),
    ("Technical-Plan.py",        "techplan", True,  True,  False),
    ("Coding-Without-Tests.py",             "factory",  True,  True,  True),
    ("Safe-TDD.py",              "tdd",      True,  True,  True),
    ("Design-Prototype.py",            "proto",    True,  True,  True),
    ("Documentation.py",    "doc",      True,  True,  True),
    ("Audit-Design.py",     "audit",    True,  True,  True),
    ("Audit-A11Y-RGAA.py",  "a11y",     True,  True,  True),
    ("Guided-Fix.py",              "fix",      True,  False, True),
]

HARNESS = {
    "opencode": {"fork": BASE_DIR, "start": "tmux_start_opencode",
                 "config": (".opencode/opencode.json", '{"model": "anthropic/claude-sonnet-4"}'),
                 "model": "anthropic/claude-sonnet-4"},
    "codex":    {"fork": REF_DIR,  "start": "tmux_start_codex",
                 "config": (".codex/config.toml", 'model = "gpt-5.3-codex"\n'),
                 "model": "gpt-5.3-codex"},
}

# Écran de pane rendu par le bouchon à 'tmux capture-pane'. Choisi pour NE PAS
# déclencher le warn-only du /new (pas de '/new' littéral) ni l'écran « trust »
# de Codex : ces deux chemins-là ont leurs scénarios dédiés.
PANE_IDLE  = "▌ prêt\n"
PANE_NEW   = "> /new\n"
PANE_TRUST = "  Do you trust this directory?\n  > Yes, allow full access\n"


class TmuxStub:
    """Remplace subprocess.run : enregistre l'argv, ne lance rien."""

    def __init__(self, has_session=False, pane=PANE_IDLE):
        self.has_session = has_session
        self.pane = pane
        self.calls = []

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        rc, out = 0, ""
        if args[:2] == ["tmux", "has-session"]:
            rc = 0 if self.has_session else 1
        elif args[:2] == ["tmux", "capture-pane"]:
            out = self.pane
        return subprocess.CompletedProcess(args, rc, out, "")


@contextlib.contextmanager
def stubbed(stub):
    """subprocess.run et time.sleep neutralisés le temps d'un scénario."""
    real_run, real_sleep = subprocess.run, time.sleep
    subprocess.run, time.sleep = stub.run, lambda *_a, **_k: None
    try:
        yield
    finally:
        subprocess.run, time.sleep = real_run, real_sleep


def load_module(path, name, syspath=()):
    """Charge un .py sous un nom de module choisi (deux versions du même fichier
    doivent pouvoir cohabiter). Les effets de bord d'import des orchestrateurs se
    limitent à un signal.signal() et, pour la version migrée, à resolve_runner()."""
    added = [p for p in syspath if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for p in added:
            sys.path.remove(p)


def run_scenario(fn, stub):
    """(sortie console, séquence de commandes tmux) d'un appel de la couche harness."""
    buf = io.StringIO()
    with stubbed(stub), contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue(), stub.calls


def scenarios(legacy, migrated, harness, spec):
    """Les scénarios comparables pour un orchestrateur donné.

    Chaque entrée : (nom, appel côté ancien, appel côté migré, état initial du stub).
    """
    _, role, has_new_context, has_capture, has_model = spec
    start_name = HARNESS[harness]["start"]
    L, M = legacy, migrated
    runner = M.RUNNER
    out = [
        ("start (session absente)",
         getattr(L, start_name), runner.start, dict(has_session=False)),
        ("start (session déjà active)",
         getattr(L, start_name), runner.start, dict(has_session=True)),
        ("send_task",
         lambda: L.tmux_send_prompt("PROMPT DE TEST\nligne 2\n"),
         lambda: runner.send_task("PROMPT DE TEST\nligne 2\n"), dict(has_session=True)),
        ("kill (session active)",
         L.tmux_kill, runner.kill, dict(has_session=True)),
        ("kill (session absente)",
         L.tmux_kill, runner.kill, dict(has_session=False)),
        ("is_running",
         L.tmux_is_running, runner.is_running, dict(has_session=True)),
    ]
    if harness == "codex":
        # Premier boot dans un dossier non « trusted » : l'écran de confiance doit
        # être détecté et validé, sinon le premier prompt serait avalé.
        out.append(("start (écran trust Codex)",
                    getattr(L, start_name), runner.start,
                    dict(has_session=False, pane=PANE_TRUST)))
    if has_new_context:
        out.append(("new_context (reset propre)",
                    L.tmux_new_session, runner.new_context, dict(has_session=True)))
        out.append(("new_context ('/new' encore à l'écran)",
                    L.tmux_new_session, runner.new_context,
                    dict(has_session=True, pane=PANE_NEW)))
    if has_capture:
        out.append(("capture",
                    L.tmux_capture_output, runner.capture, dict(has_session=True)))
    return out


def compare_pair(name, legacy_fn, migrated_fn, stub_kwargs, verbose):
    """True si l'ancien et le migré sortent la même chose, mot pour mot."""
    old_out, old_calls = run_scenario(legacy_fn, TmuxStub(**stub_kwargs))
    new_out, new_calls = run_scenario(migrated_fn, TmuxStub(**stub_kwargs))
    problems = []
    if old_out != new_out:
        problems.append("sortie console")
        if verbose:
            problems.append(f"      ancien : {old_out!r}")
            problems.append(f"      migré  : {new_out!r}")
    if old_calls != new_calls:
        problems.append("séquence tmux")
        if verbose:
            problems.append(f"      ancien : {old_calls!r}")
            problems.append(f"      migré  : {new_calls!r}")
    if problems:
        print(f"    ✗ {name} : {problems[0]}")
        for extra in problems[1:]:
            print(extra)
        return False
    return True


def compare_model(legacy, migrated, harness, project, verbose):
    """read_configured_model() (ancien) vs RUNNER.configured_model() (migré), avec
    et sans config de projet. HOME est déplacé dans le projet jetable pour le cas
    « aucune config » : la version migrée sait lire la config GLOBALE (le plan le
    demande), il ne faut pas qu'une vraie config de la machine fausse le test."""
    rel, body = HARNESS[harness]["config"]
    expected = HARNESS[harness]["model"]
    ok = True

    path = os.path.join(project, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    old, new = legacy.read_configured_model(), migrated.RUNNER.configured_model()
    if not (old == new == expected):
        print(f"    ✗ configured_model (config de projet) : ancien={old!r} migré={new!r} attendu={expected!r}")
        ok = False
    os.remove(path)

    real_home = os.environ.get("HOME")
    os.environ["HOME"] = project
    try:
        old, new = legacy.read_configured_model(), migrated.RUNNER.configured_model()
    finally:
        if real_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = real_home
    if old != new:
        print(f"    ✗ configured_model (aucune config) : ancien={old!r} migré={new!r}")
        ok = False
    return ok


def check_orchestrator(spec, harness, variant, target, verbose):
    """(statut, détail) pour un orchestrateur × un harness.
    statut ∈ {'ok', 'ko', 'skip-fork', 'skip-not-migrated'}"""
    script, role = spec[0], spec[1]
    fork = HARNESS[harness]["fork"]
    legacy_path   = os.path.join(fork, variant, "engine", script)
    migrated_path = os.path.join(target, variant, "engine", script)
    if not os.path.isfile(legacy_path):
        return "skip-fork", f"fork absent : {legacy_path}"
    if not os.path.isfile(migrated_path):
        return "skip-fork", f"cible absente : {migrated_path}"

    engine_dir = os.path.join(target, variant, "engine")
    stem = script[:-3].replace("-", "_").replace(".", "_")
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="mm-parity-") as project:
        os.chdir(project)                     # la session tmux hache le cwd : le MÊME pour les deux
        os.environ["MM_AGENT_HARNESS"] = harness
        try:
            legacy   = load_module(legacy_path,   f"legacy_{harness}_{stem}",
                                   syspath=[os.path.join(fork, variant, "engine")])
            if hasattr(legacy, "resolve_runner"):
                return "skip-fork", "le fork source semble déjà migré"
            migrated = load_module(migrated_path, f"migrated_{harness}_{stem}",
                                   syspath=[engine_dir])
            if not hasattr(migrated, "RUNNER"):
                return "skip-not-migrated", "pas encore migré (aucun RUNNER)"
            for leftover in ("tmux_start_opencode", "tmux_start_codex", "tmux_send_prompt",
                             "tmux_new_session", "tmux_kill", "tmux_is_running",
                             "tmux_capture_output", "tmux_trust_project_dir"):
                if hasattr(migrated, leftover):
                    return "ko", f"couche tmux résiduelle : {leftover}()"
            if migrated.RUNNER.session != legacy.TMUX_SESSION:
                return "ko", (f"nom de session changé : {legacy.TMUX_SESSION} "
                              f"→ {migrated.RUNNER.session}")
            if migrated.TMUX_SESSION != legacy.TMUX_SESSION:
                return "ko", "TMUX_SESSION du script ≠ session du runner"
            if migrated.RUNNER.role != role:
                return "ko", f"rôle inattendu : {migrated.RUNNER.role!r} (attendu {role!r})"
            if migrated.TMP_PROMPT_BUFFER != legacy.TMP_PROMPT_BUFFER:
                return "ko", (f"tampon de prompt changé : {legacy.TMP_PROMPT_BUFFER} "
                              f"→ {migrated.TMP_PROMPT_BUFFER}")

            ok = True
            for name, legacy_fn, migrated_fn, kwargs in scenarios(legacy, migrated, harness, spec):
                ok = compare_pair(name, legacy_fn, migrated_fn, kwargs, verbose) and ok
            if spec[4]:
                ok = compare_model(legacy, migrated, harness, project, verbose) and ok
            return ("ok" if ok else "ko"), ""
        finally:
            os.chdir(cwd)
            os.environ.pop("MM_AGENT_HARNESS", None)
            for name in list(sys.modules):
                if name.startswith(("legacy_", "migrated_")) or name == "mm_runner":
                    del sys.modules[name]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=BASE_DIR, help="fork OpenCode d'origine (lecture seule)")
    parser.add_argument("--ref", default=REF_DIR, help="fork Codex d'origine (lecture seule)")
    parser.add_argument("--target", default=REPO, help="dépôt migré à vérifier")
    parser.add_argument("--variant", default=os.path.join("FR", "Ubuntu"))
    parser.add_argument("--harness", choices=sorted(HARNESS), action="append",
                        help="ne vérifier que ce(s) harness (défaut : les deux)")
    parser.add_argument("-v", "--verbose", action="store_true", help="montrer les écarts")
    args = parser.parse_args()

    HARNESS["opencode"]["fork"] = args.base
    HARNESS["codex"]["fork"] = args.ref
    wanted = args.harness or sorted(HARNESS)

    print(f"Parité de la couche harness — variante {args.variant}")
    print(f"  cible : {args.target}")
    tally = {"ok": 0, "ko": 0, "skip-fork": 0, "skip-not-migrated": 0}
    failures, skips = [], []
    for harness in wanted:
        print(f"\n── {harness}  (fork : {HARNESS[harness]['fork']})")
        for spec in ORCHESTRATORS:
            status, detail = check_orchestrator(spec, harness, args.variant,
                                                args.target, args.verbose)
            tally[status] += 1
            mark = {"ok": "✓", "ko": "✗", "skip-fork": "∅", "skip-not-migrated": "…"}[status]
            print(f"  {mark} {spec[0]:<34} {detail}")
            if status == "ko":
                failures.append(f"{harness}/{spec[0]}" + (f" — {detail}" if detail else ""))
            elif status.startswith("skip"):
                skips.append(f"{harness}/{spec[0]} — {detail}")

    print(f"\n{'='*60}")
    print(f"  {tally['ok']} vert(s) · {tally['ko']} rouge(s) · "
          f"{tally['skip-fork'] + tally['skip-not-migrated']} skip(s)")
    for line in failures:
        print(f"  ✗ {line}")
    if skips:
        print("  Skips (déclarés, jamais comptés verts) :")
        for line in skips:
            print(f"    ∅ {line}")
    print('='*60)
    return 1 if tally["ko"] else 0


if __name__ == "__main__":
    sys.exit(main())
