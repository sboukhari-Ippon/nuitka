#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_gate_labels — les libellés des portes y/n collent au manifeste, à l'octet
──────────────────────────────────────────────────────────────────────────────
L'app cockpit détecte les portes humaines en cherchant les 'prompt_regex' de
'engine/orchestrators.json' dans l'écran de l'orchestrateur. Conséquence brutale :
un seul caractère changé dans un prompt y/n rend la porte INVISIBLE à l'app —
l'utilisateur voit un run figé, sans bouton, sans explication.

Cet outil verrouille la correspondance dans les DEUX sens, par lecture AST (aucun
lancement, aucun tmux) :

  1. chaque porte déclarée au manifeste trouve son prompt dans le script ;
  2. chaque prompt y/n d'un script déclaré au manifeste est couvert par une porte.

Le second sens est le plus utile : il attrape la porte ajoutée à un orchestrateur
sans entrée correspondante au manifeste — celle que personne ne pourra valider
depuis l'app.

    python3 tools/check_gate_labels.py                    # les 6 variantes
    python3 tools/check_gate_labels.py --variant FR/Ubuntu

Portes v1.1 : une porte peut déclarer 'kind': 'choice' ou 'text' (triage r/e/o de
Guided-Fix, questionnaire de Skills-Adaptation). Ces prompts-là ne portent
pas le marqueur '(y/n)' : le sens 1 les cherche dans TOUS les prompts du script ;
le sens 2 (orphelins) reste limité aux prompts y/n, seule famille au marqueur fiable.
"""

import argparse
import ast
import json
import os
import re
import sys

HERE     = os.path.dirname(os.path.abspath(__file__))
REPO     = os.path.dirname(HERE)
VARIANTS = [os.path.join(lang, os_name)
            for lang in ("FR", "ENG")
            for os_name in ("Ubuntu", "MacOS", "Windows")]

# Un prompt de porte se reconnaît à ce marqueur : c'est la convention des 10
# orchestrateurs depuis la V2 (« ▶️  <question> ? (y/n) : »).
GATE_MARKER = "(y/n)"


def literal_prompts(path):
    """Prompts littéraux passés à input() dans un script. Un prompt construit
    dynamiquement (f-string à substitution) est renvoyé avec ses trous béants :
    aucune porte du manifeste n'en a, et on préfère le signaler que l'ignorer."""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "input" and node.args):
            continue
        found.append((getattr(node, "lineno", 0), _flatten(node.args[0])))
    return found


def _flatten(node):
    """Texte d'un littéral, d'une concaténation implicite ou d'une f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_flatten(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return "�"                      # trou : une valeur calculée à l'exécution
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left) + _flatten(node.right)
    return "�"


def check_variant(repo, variant, verbose):
    engine = os.path.join(repo, variant, "engine")
    manifest_path = os.path.join(engine, "orchestrators.json")
    if not os.path.isfile(manifest_path):
        print(f"  ∅ {variant} : manifeste absent ({manifest_path})")
        return None
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    problems = []
    gates_seen = 0
    for orch in manifest["orchestrators"]:
        script = os.path.join(engine, orch["binary"] + ".py")
        if not os.path.isfile(script):
            problems.append(f"{orch['id']} : source absente ({orch['binary']}.py)")
            continue
        prompts = literal_prompts(script)
        yn = [(line, text) for line, text in prompts if GATE_MARKER in text]

        # Sens 1 : chaque porte déclarée trouve son prompt. Les portes 'choice'/'text'
        # (v1.1) n'ont pas le marqueur '(y/n)' : elles se cherchent dans TOUS les prompts.
        matched = set()
        for gate in orch.get("gates", []):
            gates_seen += 1
            regex = gate["prompt_regex"]
            pool = yn if gate.get("kind") in (None, "yn") else prompts
            hits = [i for i, (_line, text) in enumerate(pool) if re.search(regex, text)]
            if not hits:
                problems.append(f"{orch['id']}/{gate['id']} : aucun prompt du script ne "
                                f"correspond à la regex du manifeste")
                if verbose:
                    problems.append(f"      regex   : {regex}")
                    for line, text in pool:
                        problems.append(f"      l.{line} : {text!r}")
            if pool is yn:
                matched.update(hits)

        # Sens 2 : aucun prompt y/n orphelin (porte que l'app ne saurait pas voir).
        for i, (line, text) in enumerate(yn):
            if i not in matched:
                problems.append(f"{orch['id']} : prompt y/n l.{line} non couvert par une "
                                f"porte du manifeste → {text.strip()!r}")

    if problems:
        print(f"  ✗ {variant} ({gates_seen} portes déclarées)")
        for line in problems:
            print(f"    {line}" if line.startswith("      ") else f"    ✗ {line}")
        return False
    print(f"  ✓ {variant} — {gates_seen} portes déclarées, correspondance exacte "
          f"dans les deux sens")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--variant", action="append",
                        help="limiter à cette variante (répétable ; défaut : les 6)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    print("Portes y/n ↔ orchestrators.json")
    results = [check_variant(args.repo, v, args.verbose)
               for v in (args.variant or VARIANTS)]
    bad = [r for r in results if r is False]
    skipped = [r for r in results if r is None]
    print(f"\n{len(results) - len(bad) - len(skipped)} variante(s) verte(s), "
          f"{len(bad)} en écart, {len(skipped)} sautée(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
