#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_mm_core — génération de mm_core.py et migration des orchestrateurs (Lot 4a-2)
──────────────────────────────────────────────────────────────────────────────────────
Trois gardes de conception, toutes mécaniques :

1. EXTRACTIBLE = AST identique CHAÎNES COMPRISES (un message qui diffère entre deux
   copies rend la classe non extractible : les goldens changeraient).
2. COHÉRENCE DES APPELÉES (point fixe) : une fonction n'est retirée d'un fichier que
   si toutes les fonctions extraites qu'elle référence y ont AUSSI l'empreinte de la
   classe extraite — sinon la version mm_core appellerait un helper différent de la
   version locale (changement de comportement silencieux).
3. PARITÉ FR/ENG : l'ensemble extrait est l'INTERSECTION des ensembles extractibles
   des deux langues À COUVERTURE DE FICHIERS IDENTIQUE — l'AST FR/ENG de mm_core et
   des orchestrateurs reste équivalent par construction.

Usage :
    python3 tools/migrate_mm_core.py --plan                 # rapport, rien n'est écrit
    python3 tools/migrate_mm_core.py --generate             # écrit FR et ENG mm_core.py (Ubuntu)
    python3 tools/migrate_mm_core.py --migrate <Orch>       # migre UN orchestrateur (FR + ENG Ubuntu)
"""

import ast
import builtins
import hashlib
import symtable
import sys
import textwrap
from pathlib import Path

BUILTINS = set(dir(builtins))
ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATORS = ["Coding", "Coding-Without-Tests", "Test-First", "Acceptance-First",
                 "Design-Prototype",
                 "Spec", "Challenge-Need", "Technical-Plan", "Documentation",
                 "Audit-Design", "Pre-Audit-A11Y-RGAA", "Skills-Adaptation", "Guided-Fix"]
KNOWN_MODULES = {"os", "re", "sys", "time", "signal", "subprocess", "shlex", "shutil",
                 "yaml", "json", "hashlib", "unicodedata", "mm_audit"}
SKIP = {"main"}
# Constantes utilisées dans des ARGUMENTS PAR DÉFAUT : elles doivent exister dans
# mm_core AVANT les def (liaison au moment du def). Valeurs identiques dans tous les
# orchestrateurs (vérifié au Lot 4a) ; configure() peut les écraser pour les usages
# dans les CORPS, les défauts restant liés à la même valeur canonique.
DEFAULT_CONSTS = {
    "MAX_PHASE_TIMEOUT": 'resolve_timeout("phase", 600)',
    "VERIFY_TIMEOUT": 'resolve_timeout("verify", 300)',
    "VERIFY_FEEDBACK_LIMIT": "4000",
    "MUTATION_TIMEOUT": "300",
}

HEADER = {
    "FR": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_core — les fonctions PARTAGÉES des orchestrateurs (plan-big-last, Lot 4a)
─────────────────────────────────────────────────────────────────────────────
Module embarqué (JAMAIS un point d'entrée : exclu de la boucle Nuitka de build.yml,
comme mm_runner et mm_audit). Chaque fonction de ce fichier était dupliquée à
l'identique — AST ET CHAÎNES — dans plusieurs orchestrateurs : l'extraction est une
RECOPIE, générée et vérifiée par tools/migrate_mm_core.py, jamais une réécriture.
Un correctif de logique se fait désormais ICI, une fois (× 2 langues), au lieu de
N fichiers × 6 variantes.

Contrat de configuration : les fonctions référencent des constantes et objets de
l'orchestrateur (RUNNER, BLACKBOARD_FILE, _GIT…). Chaque orchestrateur appelle UNE
fois, en fin de module (tous ses noms sont alors définis, rien n'est encore exécuté) :

    mm_core.configure(RUNNER=RUNNER, BLACKBOARD_FILE=BLACKBOARD_FILE, ...)

Un processus = un orchestrateur : cet état module-level ne peut pas entrer en
conflit. Les objets MUTABLES (_GIT, _TEST_COUNT…) sont PARTAGÉS par référence :
les deux côtés voient les mêmes mutations, comme avant l'extraction.
"""

import os
import re
import sys
import time
import subprocess
import shlex
import shutil
import yaml

from mm_runner import resolve_timeout

import mm_audit

# Constantes canoniques utilisées dans des arguments PAR DÉFAUT (liées au def) —
# mêmes valeurs que dans tous les orchestrateurs, calculées au même moment (import).
''',
    "ENG": '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_core — the SHARED functions of the orchestrators (plan-big-last, Lot 4a)
─────────────────────────────────────────────────────────────────────────────
Embedded module (NEVER an entry point: excluded from build.yml's Nuitka loop,
like mm_runner and mm_audit). Every function in this file was duplicated
identically — AST AND STRINGS — across several orchestrators: the extraction is a
COPY, generated and verified by tools/migrate_mm_core.py, never a rewrite.
A logic fix now happens HERE, once (× 2 languages), instead of
N files × 6 variants.

Configuration contract: the functions reference the orchestrator's constants and
objects (RUNNER, BLACKBOARD_FILE, _GIT…). Each orchestrator calls ONCE, at the end
of its module (all its names are then defined, nothing has run yet):

    mm_core.configure(RUNNER=RUNNER, BLACKBOARD_FILE=BLACKBOARD_FILE, ...)

One process = one orchestrator: this module-level state cannot conflict.
MUTABLE objects (_GIT, _TEST_COUNT…) are SHARED by reference:
both sides see the same mutations, exactly as before the extraction.
"""

import os
import re
import sys
import time
import subprocess
import shlex
import shutil
import yaml

from mm_runner import resolve_timeout

import mm_audit

# Canonical constants used in DEFAULT arguments (bound at def time) —
# same values as in every orchestrator, computed at the same moment (import).
''',
}

CONFIGURE = {
    "FR": '''

def configure(**names):
    """Injecte les constantes et objets de l'orchestrateur (appelée UNE fois par
    orchestrateur, en fin de module). Volontairement brutal : l'extraction est une
    recopie à l'identique, les fonctions lisent les mêmes NOMS qu'avant."""
    globals().update(names)

''',
    "ENG": '''

def configure(**names):
    """Injects the orchestrator's constants and objects (called ONCE per
    orchestrator, at the end of its module). Deliberately blunt: the extraction is
    an identical copy, the functions read the same NAMES as before."""
    globals().update(names)

''',
}


# Référence git FIGÉE des sources d'extraction : l'état du dépôt au commit du socle
# mm_core. Les migrations ne recalculent JAMAIS les classes sur les sources courantes
# (elles dérivent au fil des migrations : l'ensemble pourrait basculer vers une autre
# variante que celle figée dans mm_core — corruption silencieuse, vue sur
# apply_blackboard_defaults de Coding et réparée par l'audit d'intégrité).
SOCLE_REF = "6e40327"
MANIFEST_PATH = ROOT / "tools" / "mm_core_manifest.json"


def engine_dir(lang: str) -> Path:
    return ROOT / lang / "Ubuntu" / "engine"


def read_source(lang: str, orch: str, ref: str = None) -> str:
    rel = f"{lang}/Ubuntu/engine/{orch}.py"
    if ref:
        import subprocess
        return subprocess.run(["git", "show", f"{ref}:{rel}"], capture_output=True,
                              text=True, check=True, cwd=ROOT).stdout
    return (ROOT / rel).read_text(encoding="utf-8")


def has_input_call(node: ast.AST) -> bool:
    """Les fonctions PORTEUSES DE PORTES (appel input()) ne s'extraient jamais :
    leurs libellés sont la surface contractuelle du binaire (check_gate_labels
    cherche les prompts dans le fichier de l'orchestrateur)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "input":
            return True
    return False


def top_level_functions_src(src: str):
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            digest = hashlib.sha256(ast.dump(node).encode()).hexdigest()[:12]
            source = "".join(lines[node.lineno - 1:node.end_lineno])
            yield node.name, digest, node, source, node.lineno, node.end_lineno


def top_level_functions(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            digest = hashlib.sha256(ast.dump(node).encode()).hexdigest()[:12]
            source = "".join(lines[node.lineno - 1:node.end_lineno])
            yield node.name, digest, node, source, node.lineno, node.end_lineno


def global_reads(source: str) -> set:
    table = symtable.symtable(textwrap.dedent(source), "<f>", "exec")
    fn = table.get_children()[0]
    names = set()
    for sym in table.get_symbols():
        if sym.get_name() != fn.get_name() and not sym.is_assigned():
            names.add(sym.get_name())

    def walk(t):
        for sym in t.get_symbols():
            if sym.is_global():
                names.add(sym.get_name())
        for child in t.get_children():
            walk(child)

    walk(fn)
    return names - BUILTINS


def language_classes(lang: str, ref: str = None) -> dict:
    """{nom: {"fingerprint", "files": set, "source", "reads": set}} — la classe la
    plus large par nom, ≥ 2 fichiers, chaînes comprises, portes exclues."""
    classes = {}
    for orch in ORCHESTRATORS:
        for fname, digest, node, source, _a, _b in top_level_functions_src(read_source(lang, orch, ref)):
            if fname in SKIP or has_input_call(node):
                continue
            entry = classes.setdefault((fname, digest), {"files": set(), "source": source})
            entry["files"].add(orch)
    best = {}
    for (fname, digest), entry in classes.items():
        if len(entry["files"]) < 2:
            continue
        if fname not in best or len(entry["files"]) > len(best[fname]["files"]):
            best[fname] = {"fingerprint": digest, "files": entry["files"],
                           "source": entry["source"]}
    for fname, info in best.items():
        info["reads"] = global_reads(info["source"])
    return best


def final_extraction_set(ref: str = None):
    """Intersection FR/ENG à couverture de fichiers IDENTIQUE (garde n°3)."""
    fr = language_classes("FR", ref)
    eng = language_classes("ENG", ref)
    common, dropped = {}, []
    for fname in sorted(set(fr) & set(eng)):
        if fr[fname]["files"] == eng[fname]["files"]:
            common[fname] = {"FR": fr[fname], "ENG": eng[fname]}
        else:
            dropped.append((fname, sorted(fr[fname]["files"]), sorted(eng[fname]["files"])))
    only_fr = sorted(set(fr) - set(eng))
    only_eng = sorted(set(eng) - set(fr))
    return common, dropped, only_fr, only_eng


def removable_for_file(lang: str, orch: str, common: dict) -> list:
    """Fonctions retirables de CE fichier : empreinte de classe + point fixe sur les
    appelées internes (garde n°2)."""
    local = {}
    for fname, digest, _n, _s, a, b in top_level_functions(engine_dir(lang) / f"{orch}.py"):
        local[fname] = (digest, a, b)
    candidates = {f for f, info in common.items()
                  if orch in info[lang.upper() if lang in ("fr", "eng") else lang]["files"]
                  and f in local and local[f][0] == info[lang]["fingerprint"]}
    # point fixe : F reste candidate ssi chacune de ses appelées EXTRAITES qu'elle
    # référence est elle-même candidate dans ce fichier (ou n'existe pas localement).
    changed = True
    while changed:
        changed = False
        for f in sorted(candidates):
            reads = common[f][lang]["reads"]
            for callee in reads & set(common):
                if callee == f:
                    continue
                if callee in local and callee not in candidates:
                    candidates.discard(f)
                    changed = True
                    break
    return sorted(candidates)


def config_names_for(lang: str, removed: list, common: dict) -> list:
    names = set()
    for f in removed:
        reads = common[f][lang]["reads"]
        names |= reads - set(common) - KNOWN_MODULES - set(DEFAULT_CONSTS)
    return sorted(names)


def generate_mm_core():
    import json as _json
    common, dropped, only_fr, only_eng = final_extraction_set(SOCLE_REF)
    manifest = {}
    for lang in ("FR", "ENG"):
        manifest[lang] = {
            fname: {"fingerprint": info[lang]["fingerprint"],
                    "files": sorted(info[lang]["files"]),
                    "reads": sorted(info[lang]["reads"])}
            for fname, info in common.items()}
    MANIFEST_PATH.write_text(_json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"{MANIFEST_PATH.relative_to(ROOT)} : manifeste FIGÉ écrit "
          f"(ref {SOCLE_REF}, {len(common)} fonctions)")
    for lang in ("FR", "ENG"):
        parts = [HEADER[lang]]
        for name, expr in DEFAULT_CONSTS.items():
            parts.append(f"{name} = {expr}\n")
        # Surface de configuration : chaque nom injecté par configure() est déclaré
        # ici en placeholder (documentation + résolution statique pour les checkers).
        all_config = set()
        for fname in common:
            all_config |= (common[fname][lang]["reads"] - set(common)
                           - KNOWN_MODULES - set(DEFAULT_CONSTS))
        if lang == "FR":
            parts.append("\n# Noms injectés par configure() — placeholders écrasés par"
                         " l'orchestrateur au chargement :\n")
        else:
            parts.append("\n# Names injected by configure() — placeholders overwritten"
                         " by the orchestrator at load time:\n")
        for name in sorted(all_config):
            parts.append(f"{name} = None\n")
        parts.append(CONFIGURE[lang])
        for fname in sorted(common):
            parts.append("\n" + common[fname][lang]["source"].rstrip() + "\n")
        out = engine_dir(lang) / "mm_core.py"
        out.write_text("".join(parts), encoding="utf-8")
        compile(out.read_text(encoding="utf-8"), str(out), "exec")
        print(f"{out.relative_to(ROOT)} : {len(common)} fonctions, "
              f"{sum(1 for _ in open(out, encoding='utf-8'))} lignes")
    if dropped:
        print("\nÉcartées (couverture FR ≠ ENG — dérive de traduction, consignée) :")
        for fname, ffr, feng in dropped:
            print(f"  {fname}: FR={ffr} ENG={feng}")
    if only_fr or only_eng:
        print(f"\nÉcartées (classe partagée dans UNE seule langue) : "
              f"FR seul={only_fr} ENG seul={only_eng}")


IMPORT_COMMENT = {
    "FR": "# Fonctions partagées extraites au Lot 4a (plan-big-last) : voir mm_core.py.\n"
          "# La configuration (constantes/objets de CE module) est injectée en fin de\n"
          "# fichier via mm_core.configure(...) — tous les noms y sont alors définis.\n",
    "ENG": "# Shared functions extracted at Lot 4a (plan-big-last): see mm_core.py.\n"
           "# The configuration (THIS module's constants/objects) is injected at the end\n"
           "# of the file via mm_core.configure(...) — all names are defined by then.\n",
}


def load_manifest() -> dict:
    import json as _json
    return _json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def removable_from_manifest(lang: str, orch: str, manifest: dict) -> list:
    """Comme removable_for_file, mais contre le manifeste FIGÉ : empreinte exacte de
    la classe extraite + point fixe sur les appelées internes."""
    entries = manifest[lang]
    local = {}
    for fname, digest, _n, _s, a, b in top_level_functions(engine_dir(lang) / f"{orch}.py"):
        local[fname] = digest
    candidates = {f for f, info in entries.items()
                  if orch in info["files"] and local.get(f) == info["fingerprint"]}
    changed = True
    while changed:
        changed = False
        for f in sorted(candidates):
            for callee in set(entries[f]["reads"]) & set(entries):
                if callee == f:
                    continue
                if callee in local and callee not in candidates:
                    candidates.discard(f)
                    changed = True
                    break
    return sorted(candidates)


def migrate(orch: str):
    manifest = load_manifest()
    for lang in ("FR", "ENG"):
        path = engine_dir(lang) / f"{orch}.py"
        removed = removable_from_manifest(lang, orch, manifest)
        if not removed:
            print(f"[{lang}] {orch}: rien d'extractible, fichier inchangé")
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines(keepends=True)
        spans = []
        for fname, _d, _n, _s, a, b in top_level_functions(path):
            if fname in removed:
                spans.append((a, b))
        # retirer du bas vers le haut (les numéros de ligne restent valides)
        for a, b in sorted(spans, reverse=True):
            # avaler les lignes vides qui suivaient la fonction
            end = b
            while end < len(lines) and lines[end].strip() == "":
                end += 1
            del lines[a - 1:end]
        src = "".join(lines)

        # N'importer que les noms retirés ENCORE référencés par le code restant
        # (un nom dont tous les appelants sont eux aussi partis n'a plus d'import) ;
        # et purger les `import X` stdlib devenus morts après le retrait.
        remaining = ast.parse(src)
        used = set()
        for n in ast.walk(remaining):
            if isinstance(n, ast.Name):
                used.add(n.id)
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                used.add(n.value.id)
        still_used = [n for n in removed if n in used]
        dead_simple_imports = []
        for n in remaining.body:
            if isinstance(n, ast.Import) and len(n.names) == 1 \
                    and n.names[0].asname is None and "." not in n.names[0].name:
                if n.names[0].name not in used:
                    dead_simple_imports.append(f"import {n.names[0].name}\n")
        for dead in dead_simple_imports:
            src = src.replace(dead, "", 1)
        removed_imports = still_used

        # import après l'import mm_audit (présent dans les 16 depuis le Lot 2)
        anchor = "import mm_audit\n"
        assert anchor in src, f"{path}: ancre d'import absente"
        names_per_line = [removed_imports[i:i + 4] for i in range(0, len(removed_imports), 4)]
        import_block = ("\n" + IMPORT_COMMENT[lang] + "import mm_core\n"
                        + "from mm_core import (\n"
                        + "".join("    " + ", ".join(chunk) + ",\n" for chunk in names_per_line)
                        + ")\n")
        src = src.replace(anchor, anchor + import_block, 1)

        # configure(...) en fin de module, avant le bloc __main__
        cfg_names = set()
        for f in removed:
            cfg_names |= (set(manifest[lang][f]["reads"]) - set(manifest[lang])
                          - KNOWN_MODULES - set(DEFAULT_CONSTS))
        cfg_names = sorted(cfg_names)
        cfg_kwargs = "".join(f"    {n}={n},\n" for n in cfg_names)
        tail_anchor = 'if __name__ == "__main__":'
        assert tail_anchor in src, f"{path}: bloc __main__ absent"
        cfg_block = ("mm_core.configure(\n" + cfg_kwargs + ")\n\n\n")
        src = src.replace(tail_anchor, cfg_block + tail_anchor, 1)

        path.write_text(src, encoding="utf-8")
        compile(src, str(path), "exec")
        print(f"[{lang}] {orch}: {len(removed)} fonction(s) retirée(s), "
              f"{len(cfg_names)} nom(s) configurés")


def plan():
    common, dropped, only_fr, only_eng = final_extraction_set()
    dup = sum((len(i['FR']['files']) - 1) * i['FR']['source'].count("\n")
              for i in common.values())
    print(f"{len(common)} fonctions extractibles FR∩ENG, ~{dup} lignes dupliquées éliminables")
    if dropped:
        print(f"écartées (couverture FR≠ENG) : {[d[0] for d in dropped]}")
    if only_fr or only_eng:
        print(f"écartées (une seule langue) : FR={only_fr} ENG={only_eng}")
    for orch in ORCHESTRATORS:
        r_fr = removable_for_file("FR", orch, common)
        r_eng = removable_for_file("ENG", orch, common)
        mark = "" if r_fr == r_eng else "  ⚠️ FR≠ENG !"
        print(f"  {orch:22s} retire {len(r_fr):3d} fonction(s){mark}")


if __name__ == "__main__":
    if "--plan" in sys.argv:
        plan()
    elif "--generate" in sys.argv:
        generate_mm_core()
    elif "--migrate" in sys.argv:
        migrate(sys.argv[sys.argv.index("--migrate") + 1])
    else:
        print(__doc__)
