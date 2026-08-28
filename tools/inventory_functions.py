#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inventory_functions — matrice fonction × orchestrateur (plan-big-last, Lot 4a)
──────────────────────────────────────────────────────────────────────────────
Pour chaque fonction top-level des orchestrateurs FR/Ubuntu : empreinte de son AST
chaînes masquées (précédent méthodologique : MIGRATION_INVENTORY.md). Deux fonctions
qui partagent une empreinte sont STRICTEMENT identiques au code près (seules les
chaînes peuvent différer) : candidates à l'extraction dans mm_core.py.

    python3 tools/inventory_functions.py            # rapport lisible
    python3 tools/inventory_functions.py --tsv      # matrice brute (tabulations)
"""

import ast
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "FR" / "Ubuntu" / "engine"
ORCHESTRATORS = ["Coding", "Coding-Without-Tests", "Test-First", "Acceptance-First",
                 "Design-Prototype",
                 "Spec", "Challenge-Need", "Technical-Plan", "Documentation",
                 "Audit-Design", "Pre-Audit-A11Y-RGAA", "Skills-Adaptation", "Guided-Fix"]


class StringMasker(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value="·"), node)
        return node


def function_fingerprints(path: Path) -> dict:
    """{nom de fonction top-level: (empreinte AST chaînes masquées, nb de lignes)}."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            masked = StringMasker().visit(node)
            digest = hashlib.sha256(ast.dump(masked).encode()).hexdigest()[:12]
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            out[node.name] = (digest, span)
    return out


def main() -> int:
    tsv = "--tsv" in sys.argv
    per_orch = {}
    for name in ORCHESTRATORS:
        per_orch[name] = function_fingerprints(ENGINE / f"{name}.py")

    # nom de fonction -> {empreinte -> [orchestrateurs]}
    by_name = {}
    for orch, funcs in per_orch.items():
        for fname, (digest, span) in funcs.items():
            by_name.setdefault(fname, {}).setdefault(digest, []).append((orch, span))

    if tsv:
        print("fonction\tempreinte\tlignes\torchestrateurs")
        for fname in sorted(by_name):
            for digest, owners in sorted(by_name[fname].items()):
                print(f"{fname}\t{digest}\t{owners[0][1]}\t"
                      + ",".join(o for o, _ in owners))
        return 0

    identical, divergent, unique = [], [], []
    for fname, variants in sorted(by_name.items()):
        owners_total = sum(len(v) for v in variants.values())
        if owners_total == 1:
            unique.append(fname)
        elif len(variants) == 1:
            owners = next(iter(variants.values()))
            identical.append((fname, len(owners), owners[0][1]))
        else:
            divergent.append((fname, {d: [o for o, _ in v] for d, v in variants.items()}))

    total_lines = sum(span * (count - 1) for _f, count, span in identical)
    print(f"Inventaire — {len(ORCHESTRATORS)} orchestrateurs, "
          f"{sum(len(f) for f in per_orch.values())} fonctions top-level\n")
    print(f"── STRICTEMENT IDENTIQUES (candidates mm_core) : {len(identical)} fonctions, "
          f"~{total_lines} lignes dupliquées éliminables ──")
    for fname, count, span in sorted(identical, key=lambda x: -x[1] * x[2]):
        print(f"  {fname:38s} ×{count:2d}  ({span} l. chacune)")
    print(f"\n── MÊME NOM, CODE DIVERGENT (à paramétrer ou à laisser) : {len(divergent)} ──")
    for fname, variants in divergent:
        groups = " | ".join(f"[{len(orchs)}] " + ",".join(sorted(orchs)[:4])
                            + ("…" if len(orchs) > 4 else "")
                            for orchs in variants.values())
        print(f"  {fname:38s} {len(variants)} variantes : {groups}")
    print(f"\n── PROPRES À UN SEUL ORCHESTRATEUR : {len(unique)} (non listées) ──")
    return 0


if __name__ == "__main__":
    sys.exit(main())
