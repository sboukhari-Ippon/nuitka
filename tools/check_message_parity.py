#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_message_parity — aucune chaîne visible n'a changé de valeur
────────────────────────────────────────────────────────────────
check_runner_parity.py prouve que la COUCHE HARNESS extraite produit la même chose
qu'avant. Il ne dit rien du reste des scripts : messages d'échec, aides « /model »,
corps de .gitignore, prompts d'agents. Or la migration y a remplacé des littéraux
(« .opencode_po.md », « ./.opencode/opencode.json ») par des expressions du runner
— et une expression peut rendre une valeur PROCHE mais différente.

C'est arrivé : dans Spec.py, deux messages citaient
'.opencode/opencode.json' en dur, alors que RUNNER.config_file vaut
'./.opencode/opencode.json'. Le './' se serait glissé dans un conseil affiché à
l'utilisateur, invisible à toute autre vérification.

Méthode, sans rien exécuter : les deux fichiers sont NORMALISÉS avec la même table
de valeurs (chaque expression du runner et chaque constante renommée est remplacée
par la VALEUR qu'elle rend pour le harness visé), puis chaque ligne touchée du script
migré doit se retrouver dans l'original. On compare donc des valeurs, jamais des
écritures : deux formulations qui rendent la même chaîne passent, deux formulations
identiques qui rendent des chaînes différentes échouent.

    python3 tools/check_message_parity.py
    python3 tools/check_message_parity.py --harness codex -v

Les lignes structurelles de la migration (bloc RUNNER, entêtes, commentaires
réécrits) n'ont par construction aucun équivalent dans l'original : elles sont
listées dans EXPECTED_NEW et comptées à part, jamais silencieusement ignorées.
"""

import argparse
import os
import re
import sys

HERE     = os.path.dirname(os.path.abspath(__file__))
REPO     = os.path.dirname(HERE)
SIBLINGS = os.path.dirname(REPO)

SCRIPTS = [
    ("Safe-Coding.py", "factory"),
    ("Spec.py", "spec"),
    ("Technical-Plan.py", "techplan"),
    ("Coding-Without-Tests.py", "factory"),
    ("Safe-TDD.py", "tdd"),
    ("Design-Prototype.py", "proto"),
    ("Documentation.py", "doc"),
    ("Audit-Design.py", "audit"),
    ("Audit-A11Y-RGAA.py", "a11y"),
    ("Guided-Fix.py", "fix"),
]

HARNESS = {
    "opencode": {
        "fork": os.path.join(SIBLINGS, "MAIsterMind_App-opencode"),
        "tmp_prefix": "opencode", "buffer_prefix": "oc", "session_prefix": "oc-",
        "config": "./.opencode/opencode.json", "equip_dir": ".opencode",
        "start_fn": "tmux_start_opencode",
    },
    "codex": {
        "fork": os.path.join(SIBLINGS, "MAIsterMind_App-codex"),
        "tmp_prefix": "codex", "buffer_prefix": "cx", "session_prefix": "cx-",
        "config": "./.codex/config.toml", "equip_dir": ".codex",
        "start_fn": "tmux_start_codex",
    },
}

# Lignes du script migré dont on SAIT qu'elles n'ont pas d'équivalent dans l'original :
# elles portent la migration elle-même. Repérées par sous-chaîne.
EXPECTED_NEW = [
    "from mm_runner import resolve_runner",
    "RUNNER = resolve_runner(",
    "# ─── HARNESS D'AGENT",
    "# ─── AGENT HARNESS",
    "AGENT_CONFIG_FILE     = RUNNER.config_file",
    "GITIGNORE_BODY = f\"\"\"",
]


# Une ligne « touchée » par la migration : elle porte une expression du runner ou la
# constante de config renommée. Les autres lignes sont, par construction, inchangées.
TOUCHED = re.compile(r"RUNNER\.|AGENT_CONFIG_FILE|\{TMP_PROMPT_BUFFER\}")


def value_map(harness, role, config_display):
    """Substitutions appliquées AUX DEUX CÔTÉS, pour comparer des VALEURS et non des
    noms : deux écritures différentes qui rendent la même chaîne sont équivalentes,
    deux écritures identiques qui rendent des chaînes différentes ne le sont pas.
    C'est ce second cas qui a fait passer un './' dans un message de spec.py."""
    h = HARNESS[harness]
    buf = f'.{h["buffer_prefix"]}_short_prompt.txt'
    glob = f'.{h["tmp_prefix"]}_*.md'
    cfg_name = "OPENCODE_CONFIG_FILE" if harness == "opencode" else "CODEX_CONFIG_FILE"
    return [
        # Interpolations de f-string : on remplace par la VALEUR rendue.
        ("{TMP_PROMPT_BUFFER}", buf),
        ("{RUNNER.tmp_glob}", glob),
        ("{AGENT_CONFIG_FILE}", config_display),
        ("{" + cfg_name + "}", h["config"]),
        # Littéraux dérivés du runner : on remplace par la valeur du harness.
        ("RUNNER.prompt_buffer", f'"{buf}"'),
        ("RUNNER.tmp_glob", glob),
        ("RUNNER.tmp_dot_prefix", f'".{h["tmp_prefix"]}_"'),
        ("RUNNER.equip_dir", f'"{h["equip_dir"]}"'),
        ('RUNNER.config_file.removeprefix("./")', f'"{config_display}"'),
        ("RUNNER.config_file", f'"{h["config"]}"'),
        # Identifiants renommés : réduits au MÊME jeton des deux côtés.
        ("AGENT_CONFIG_FILE", "«CFG»"), (cfg_name, "«CFG»"),
        ("RUNNER.session", "«SESSION»"),
        (f'"{h["session_prefix"]}{role}-" + hashlib.sha1(os.getcwd().encode("utf-8"))'
         '.hexdigest()[:8]', "«SESSION»"),
        ("RUNNER.start()", "«START»"), (f'{h["start_fn"]}()', "«START»"),
        ("RUNNER.send_task(", "«SEND»("), ("tmux_send_prompt(", "«SEND»("),
        ("RUNNER.new_context()", "«NEW»"), ("tmux_new_session()", "«NEW»"),
        ("RUNNER.capture()", "«CAPTURE»"), ("tmux_capture_output()", "«CAPTURE»"),
        ("RUNNER.kill()", "«KILL»"), ("tmux_kill()", "«KILL»"),
        ("RUNNER.is_running()", "«RUNNING»"), ("tmux_is_running()", "«RUNNING»"),
        ("RUNNER.configured_model()", "«MODEL»"), ("read_configured_model()", "«MODEL»"),
    ]


def normalize(line, mapping):
    for old, new in mapping:
        line = line.replace(old, new)
    # Le préfixe f d'un littéral est neutre pour la VALEUR : une chaîne devenue
    # f-string pour interpoler la config rend exactement la même chose qu'avant, une
    # fois l'interpolation remplacée par sa valeur. On le retire des deux côtés.
    return re.sub(r'(?<![A-Za-z0-9_])f(["\'])', r"\1", line)


def render_tmp(line, harness):
    h = HARNESS[harness]
    return re.sub(r'RUNNER\.tmp_file\("(\w+)"\)',
                  lambda m: f'".{h["tmp_prefix"]}_{m.group(1)}.md"', line)


def config_display_of(path):
    """Valeur d'AGENT_CONFIG_FILE dans ce script : avec ou sans le './' de tête,
    selon la forme que ses messages citaient déjà dans le fork d'origine."""
    src = open(path, encoding="utf-8").read()
    return "STRIP" if 'RUNNER.config_file.removeprefix("./")' in src else "KEEP"


def check(script, role, harness, variant, repo, verbose):
    base_path = os.path.join(HARNESS[harness]["fork"], variant, "engine", script)
    migr_path = os.path.join(repo, variant, "engine", script)
    if not os.path.isfile(base_path):
        return "skip", f"fork absent : {base_path}"
    if not os.path.isfile(migr_path):
        return "skip", f"cible absente : {migr_path}"

    cfg = HARNESS[harness]["config"]
    display = cfg.removeprefix("./") if config_display_of(migr_path) == "STRIP" else cfg
    mapping = value_map(harness, role, display)

    base_norm = set()
    base_loose = set()
    for line in open(base_path, encoding="utf-8").read().splitlines():
        norm = normalize(line, mapping)
        base_norm.add(norm)
        base_loose.add(re.sub(r"\s+", " ", norm).strip())

    problems, structural = [], 0
    for num, line in enumerate(open(migr_path, encoding="utf-8").read().splitlines(), 1):
        if not TOUCHED.search(line):
            continue
        if any(marker in line for marker in EXPECTED_NEW):
            structural += 1
            continue
        norm = normalize(render_tmp(line, harness), mapping)
        if norm in base_norm:
            continue
        if re.sub(r"\s+", " ", norm).strip() in base_loose:
            continue
        problems.append((num, line.strip(), norm.strip()))

    if problems:
        print(f"    ✗ {script} — {len(problems)} ligne(s) sans équivalent dans l'original")
        for num, raw, norm in problems:
            print(f"      l.{num} : {raw}")
            if verbose:
                print(f"        normalisé : {norm}")
        return "ko", ""
    return "ok", f"{structural} ligne(s) structurelle(s) de migration"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=REPO, help="dépôt migré à vérifier")
    parser.add_argument("--base", default=HARNESS["opencode"]["fork"],
                        help="fork OpenCode d'origine (lecture seule)")
    parser.add_argument("--ref", default=HARNESS["codex"]["fork"],
                        help="fork Codex d'origine (lecture seule)")
    parser.add_argument("--variant", default=os.path.join("FR", "Ubuntu"))
    parser.add_argument("--harness", choices=sorted(HARNESS), action="append")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    HARNESS["opencode"]["fork"] = args.base
    HARNESS["codex"]["fork"] = args.ref

    print(f"Parité des chaînes visibles — variante {args.variant}")
    print(f"  cible : {args.repo}")
    tally = {"ok": 0, "ko": 0, "skip": 0}
    for harness in (args.harness or sorted(HARNESS)):
        print(f"\n── {harness}")
        for script, role in SCRIPTS:
            status, detail = check(script, role, harness, args.variant,
                                   args.repo, args.verbose)
            tally[status] += 1
            if status != "ko":
                mark = "✓" if status == "ok" else "∅"
                print(f"  {mark} {script:<34} {detail}")
    print(f"\n{'='*60}")
    print(f"  {tally['ok']} vert(s) · {tally['ko']} rouge(s) · {tally['skip']} skip(s)")
    print('='*60)
    return 1 if tally["ko"] else 0


if __name__ == "__main__":
    sys.exit(main())
