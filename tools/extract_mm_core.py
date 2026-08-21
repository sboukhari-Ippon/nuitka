#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_mm_core — analyse préparatoire à l'extraction de mm_core.py (Lot 4a-2)
──────────────────────────────────────────────────────────────────────────────
Classes de fonctions extractibles = AST identique CHAÎNES COMPRISES (deux copies
qui divergent ne serait-ce que par un message ne sont PAS extractibles : les
goldens changeraient). Pour chaque classe : les NOMS GLOBAUX réellement
référencés (analyse de portée symtable — les attributs comme re.IGNORECASE sont
exclus), séparés en « fournis par mm_core » (autres fonctions extraites, modules)
et « à injecter via configure() » (constantes/objets de l'orchestrateur).

    python3 tools/extract_mm_core.py             # rapport lisible
    python3 tools/extract_mm_core.py --json      # manifeste machine (extraction)
"""

import ast
import builtins
import hashlib
import json
import symtable
import sys
import textwrap
from pathlib import Path

BUILTINS = set(dir(builtins))

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "FR" / "Ubuntu" / "engine"
ORCHESTRATORS = ["Safe-Coding", "Coding-Without-Tests", "Safe-TDD", "Safe-ATDD",
                 "Design-Prototype", "Advanced-Coding", "Advanced-TDD", "Advanced-ATDD",
                 "Spec", "Challenge-Need", "Technical-Plan", "Documentation",
                 "Audit-Design", "Audit-A11Y-RGAA", "Skills-Adaptation", "Guided-Fix"]
# Modules que mm_core importe lui-même.
KNOWN_MODULES = {"os", "re", "sys", "time", "signal", "subprocess", "shlex", "shutil",
                 "yaml", "json", "hashlib", "unicodedata", "mm_audit"}
# Jamais extraites : point d'entrée, et les mains/flux top-niveau propres.
SKIP = {"main"}


def top_level_functions(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            digest = hashlib.sha256(ast.dump(node).encode()).hexdigest()[:12]
            source = "".join(lines[node.lineno - 1:node.end_lineno])
            yield node.name, digest, node, source


def global_reads(source: str) -> set:
    """Noms GLOBAUX référencés par la fonction (symtable : portée réelle, attributs
    exclus), fonctions imbriquées comprises."""
    table = symtable.symtable(textwrap.dedent(source), "<f>", "exec")
    fn = table.get_children()[0]
    names = set()
    # Les expressions d'arguments PAR DÉFAUT s'évaluent dans la portée module (au
    # moment du def) : leurs noms sont portés par le bloc module du snippet.
    for sym in table.get_symbols():
        if sym.get_name() != fn.get_name() and not sym.is_assigned():
            names.add(sym.get_name())

    def walk(t):
        for sym in t.get_symbols():
            # is_global couvre aussi les scopes imbriqués (GLOBAL_IMPLICIT) ; les
            # variables libres d'une closure sont des locales du parent, pas des globaux.
            if sym.is_global():
                names.add(sym.get_name())
        for child in t.get_children():
            walk(child)

    walk(fn)
    return names - BUILTINS


def global_writes(source: str) -> set:
    """Noms déclarés `global` ET assignés (rebinding de module : dangereux à extraire
    si l'orchestrateur relit le nom localement)."""
    tree = ast.parse(textwrap.dedent(source))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            out.update(node.names)
    return out


def main() -> int:
    as_json = "--json" in sys.argv
    classes = {}
    for orch in ORCHESTRATORS:
        for fname, digest, node, source in top_level_functions(ENGINE / f"{orch}.py"):
            if fname in SKIP:
                continue
            key = (fname, digest)
            entry = classes.setdefault(key, {"files": [], "source": source})
            entry["files"].append(orch)

    # Un même NOM peut avoir plusieurs classes : seule la plus large est extractible
    # (les autres fichiers gardent leur définition locale, donc aucun conflit).
    best = {}
    for (fname, digest), entry in classes.items():
        if len(entry["files"]) < 2:
            continue
        if fname not in best or len(entry["files"]) > len(best[fname][1]["files"]):
            best[fname] = ((fname, digest), entry)

    extracted_names = set(best)
    report = {}
    for fname, ((_, digest), entry) in sorted(best.items()):
        reads = global_reads(entry["source"])
        writes = global_writes(entry["source"])
        internal = reads & (extracted_names | KNOWN_MODULES)
        report[fname] = {
            "fingerprint": digest,
            "files": sorted(entry["files"]),
            "count": len(entry["files"]),
            "needs_config": sorted(reads - internal),
            "uses_internal": sorted((internal - KNOWN_MODULES) - {fname}),
            "global_writes": sorted(writes),
            "source": entry["source"],
        }

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    all_config = {}
    for fname, info in report.items():
        for name in info["needs_config"]:
            all_config.setdefault(name, []).append(fname)
    dup_lines = sum((info["count"] - 1) * info["source"].count("\n")
                    for info in report.values())
    print(f"{len(report)} fonctions extractibles (AST identique CHAÎNES COMPRISES), "
          f"~{dup_lines} lignes dupliquées éliminables\n")
    writers = {f: i["global_writes"] for f, i in report.items() if i["global_writes"]}
    if writers:
        print(f"⚠️  Fonctions avec `global` assigné (à traiter à la main) : {writers}\n")
    print("── Noms à injecter via configure() ──")
    for name in sorted(all_config):
        users = sorted(set(all_config[name]))
        print(f"  {name:32s} {', '.join(users[:5])}" + ("…" if len(users) > 5 else ""))
    print("\n── Fonctions extractibles ──")
    for fname, info in sorted(report.items(), key=lambda kv: -kv[1]["count"]):
        print(f"  {fname:38s} ×{info['count']:2d} config={info['needs_config']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
