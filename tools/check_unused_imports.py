#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_unused_imports — aucun import mort, aucun nom manquant
────────────────────────────────────────────────────────────
La sortie de la couche tmux des orchestrateurs a rendu inutiles des imports qui ne
servaient QU'À elle ('subprocess' dans Spec.py, 'hashlib' partout,
'json' là où seul read_configured_model l'utilisait). Les retirer au jugé, dans
10 fichiers de 400 à 2 600 lignes, c'est se garantir un NameError en pleine
production — une erreur que py_compile ne voit pas.

Cet outil décide par lecture AST, sans exécuter le code :

  1. IMPORTS MORTS : un nom importé jamais référencé ailleurs → à retirer ;
  2. NOMS INCONNUS : un nom référencé qui n'est ni importé, ni défini, ni un
     builtin → l'import a été retiré à tort (le vrai risque de la migration).

Le second contrôle est la raison d'être de l'outil : il ferme le trou laissé par
py_compile, qui compile sans broncher un module dont un import manque.

    python3 tools/check_unused_imports.py                       # les 6 variantes
    python3 tools/check_unused_imports.py FR/Ubuntu/engine/Coding.py
"""

import argparse
import ast
import builtins
import os
import sys

HERE     = os.path.dirname(os.path.abspath(__file__))
REPO     = os.path.dirname(HERE)
VARIANTS = [os.path.join(lang, os_name)
            for lang in ("FR", "ENG")
            for os_name in ("Ubuntu", "MacOS", "Windows")]

BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__spec__"}


class Scanner(ast.NodeVisitor):
    """Noms importés, noms liés (def/class/assign/for/with/except/comprehensions,
    paramètres) et noms lus. Volontairement à plat : on ne cherche pas la portée
    exacte, seulement « ce nom existe-t-il quelque part dans ce module »."""

    def __init__(self):
        self.imported = {}      # nom local -> (ligne, texte de l'import)
        self.bound = set()
        self.loaded = set()

    # ─── imports ──────────────────────────────────────────────────────────────
    def visit_Import(self, node):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.imported[local] = (node.lineno, f"import {alias.name}"
                                    + (f" as {alias.asname}" if alias.asname else ""))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.imported[local] = (node.lineno,
                                    f"from {node.module} import {alias.name}"
                                    + (f" as {alias.asname}" if alias.asname else ""))
        self.generic_visit(node)

    # ─── liaisons ─────────────────────────────────────────────────────────────
    def visit_FunctionDef(self, node):
        self.bound.add(node.name)
        self._params(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._params(node.args)
        self.generic_visit(node)

    def _params(self, args):
        for group in (args.posonlyargs, args.args, args.kwonlyargs):
            for arg in group:
                self.bound.add(arg.arg)
        for arg in (args.vararg, args.kwarg):
            if arg:
                self.bound.add(arg.arg)

    def visit_ClassDef(self, node):
        self.bound.add(node.name)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.bound.update(node.names)
        self.generic_visit(node)

    visit_Nonlocal = visit_Global

    # ─── usages ───────────────────────────────────────────────────────────────
    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)
        else:
            self.bound.add(node.id)
        self.generic_visit(node)


def scan(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    scanner = Scanner()
    scanner.visit(tree)
    dead = {name: where for name, where in scanner.imported.items()
            if name not in scanner.loaded}
    known = set(scanner.imported) | scanner.bound | BUILTINS
    unknown = sorted(n for n in scanner.loaded if n not in known)
    return dead, unknown


def check_file(path, root):
    dead, unknown = scan(path)
    label = os.path.relpath(path, root)
    if not dead and not unknown:
        return True
    print(f"  ✗ {label}")
    for name, (line, text) in sorted(dead.items(), key=lambda kv: kv[1][0]):
        print(f"      import mort   l.{line} : {text}")
    for name in unknown:
        print(f"      nom inconnu           : {name}  ← import retiré à tort ?")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="fichiers à contrôler (défaut : engine/*.py des 6 variantes)")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args()

    if args.paths:
        targets = [p if os.path.isabs(p) else os.path.join(args.repo, p) for p in args.paths]
    else:
        targets = []
        for variant in VARIANTS:
            engine = os.path.join(args.repo, variant, "engine")
            if not os.path.isdir(engine):
                continue
            targets += sorted(os.path.join(engine, n) for n in os.listdir(engine)
                              if n.endswith(".py"))
    if not targets:
        print("Aucun fichier à contrôler.")
        return 1

    print(f"Imports morts / noms inconnus — {len(targets)} fichier(s)")
    bad = [p for p in targets if not check_file(p, args.repo)]
    if bad:
        print(f"\n✗ {len(bad)} fichier(s) en écart sur {len(targets)}")
        return 1
    print(f"\n✓ {len(targets)} fichier(s) : aucun import mort, aucun nom inconnu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
