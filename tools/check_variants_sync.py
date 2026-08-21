#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Garde anti-divergence des 6 variantes (FR/ENG × Ubuntu/MacOS/Windows).

Le dépôt livre 6 dossiers quasi identiques ; chaque correctif doit être propagé
partout. L'histoire a prouvé que la discipline manuelle ne suffit pas (divergence
du fichier tampon tmux, PO_SKILL_FILE absent de la branche ENG, manifestes
orchestrators.json désalignés entre plateformes du dépôt App_v3). Ce script
transforme la règle en GARANTIE :

  1. INTRA-LANGUE : les scripts engine/*.py d'une même langue — mm_runner.py compris
     — sont identiques OCTET PAR OCTET sur les 3 OS. Idem pour les
     skills et les artefacts des DEUX harness (.opencode/, .codex/, AGENTS.md).
  2. INTER-LANGUES : FR et ENG ont la même STRUCTURE de code — AST identique une
     fois toutes les chaînes masquées (seule la langue des textes peut différer).
  3. ARBORESCENCE : les 6 variantes exposent les mêmes chemins relatifs pour les
     scripts, les skills et les artefacts d'équipement des DEUX harness — chaque
     variante doit pouvoir équiper un projet en OpenCode COMME en Codex.
  4. COUCHE APP (nouveau dépôt unifié) : MAIsterMind_App.py (bilingue à bord),
     engine/orchestrators.json (regex FR|ENG en alternance) et install.sh
     (détection d'OS dynamique) sont identiques sur LES SIX variantes ; les
     lanceurs double-clic sont identiques entre les deux variantes de leur OS
     (MAIsterMind.bat sur Windows, bundle MAIsterMind.app sur MacOS) et absents
     ailleurs.

Sort avec le code 1 et un rapport lisible à la moindre divergence. Aucune
dépendance hors bibliothèque standard : exécutable tel quel en CI.
"""

import ast
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANGS = ["FR", "ENG"]
OSES = ["Ubuntu", "MacOS", "Windows"]
SCRIPTS = [
    "engine/Safe-Coding.py",
    "engine/Coding-Without-Tests.py",
    "engine/Safe-TDD.py",
    "engine/Safe-ATDD.py",
    "engine/Audit-Design.py",
    "engine/Audit-A11Y-RGAA.py",
    "engine/Documentation.py",
    "engine/Guided-Fix.py",
    "engine/Design-Prototype.py",
    "engine/Skills-Adaptation.py",
    "engine/Spec.py",
    "engine/Challenge-Need.py",
    "engine/Technical-Plan.py",
    # Orchestrateurs Yolo (base + surcouche : revue d'impact, vérificateur LLM, triage des
    # cassures). Mêmes règles que leurs bases : c'est justement la famille de scripts la
    # plus exposée à la divergence, puisque chacun est une COPIE greffée d'un autre.
    "engine/Advanced-Coding.py",
    "engine/Advanced-TDD.py",
    "engine/Advanced-ATDD.py",
    # Le harness vit ici : mêmes règles que les orchestrateurs
    # (identité octet intra-langue, AST identique FR/ENG modulo les chaînes).
    "engine/mm_runner.py",
    # Le journal de run (boîte noire) vit ici depuis le Lot 2 de plan-big-last :
    # module embarqué comme mm_runner, mêmes règles de synchronisation.
    "engine/mm_audit.py",
    # Les fonctions partagées extraites au Lot 4a : module embarqué, mêmes règles.
    "engine/mm_core.py",
]
# Fichiers de consignes/config à garder synchronisés (contenu identique intra-langue).
# Les artefacts d'équipement des DEUX harness sont embarqués : le choix se fait à
# l'équipement, pas à la compilation.
SYNCED_GLOBS = ["engine/.agents/**/*.md", "engine/.agents/**/*.yaml",
                "engine/.opencode/agents/*.md", "engine/.opencode/opencode.json",
                "engine/.codex/config.toml", "engine/AGENTS.md"]

# Artefacts d'équipement qui doivent EXISTER dans les 6 variantes, par harness. Un
# harness dont les artefacts manquent d'une variante produirait un « Distribution
# incomplète » à l'équipement, sur cette variante seulement — le pire des bugs de
# distribution : invisible chez soi, systématique chez l'utilisateur.
HARNESS_ARTEFACTS = {
    "opencode": ["engine/.opencode/opencode.json", "engine/.opencode/agents/factory.md"],
    "codex":    ["engine/.codex/config.toml", "engine/AGENTS.md"],
}
# Couche app : identique sur LES SIX variantes (l'app est bilingue à bord, le
# manifeste porte les deux langues en alternance de regex, install.sh détecte l'OS).
UNIFIED_FILES = ["MAIsterMind_App.py", "engine/orchestrators.json", "install.sh"]
# Lanceurs double-clic : par OS (identiques entre FR et ENG de cet OS, absents ailleurs).
OS_LAUNCHERS = {
    "Windows": ["MAIsterMind.bat"],
    "MacOS": ["MAIsterMind.app/Contents/Info.plist",
              "MAIsterMind.app/Contents/MacOS/MAIsterMind"],
    "Ubuntu": [],   # le lanceur (.desktop) est généré par install.sh, rien à livrer
}


def variant_dir(lang: str, os_name: str) -> Path:
    return ROOT / lang / os_name


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StringMasker(ast.NodeTransformer):
    """Remplace toute constante chaîne par un même placeholder.

    Après masquage, les AST FR et ENG doivent être STRICTEMENT identiques : la
    traduction ne touche que des chaînes (prints, prompts, docstrings). Tout
    autre écart (constante manquante, appel différent, branche absente) est une
    divergence de code réelle.
    """

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value="·"), node)
        return node


def structural_dump(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree = StringMasker().visit(tree)
    return ast.dump(tree)


def synced_files(lang: str, os_name: str) -> dict:
    """Chemins relatifs → empreintes des fichiers de consignes/config d'une variante."""
    base = variant_dir(lang, os_name)
    result = {}
    for pattern in SYNCED_GLOBS:
        for path in sorted(base.glob(pattern)):
            if path.is_file():
                result[str(path.relative_to(base))] = file_digest(path)
    return result


def main() -> int:
    errors = []

    # ── Règle 0 : les 6 dossiers, leurs scripts et la couche app existent ──
    for lang in LANGS:
        for os_name in OSES:
            base = variant_dir(lang, os_name)
            if not base.is_dir():
                errors.append(f"Variante absente : {base.relative_to(ROOT)}")
                continue
            for rel in SCRIPTS + UNIFIED_FILES + OS_LAUNCHERS[os_name]:
                if not (base / rel).is_file():
                    errors.append(f"Fichier absent : {(base / rel).relative_to(ROOT)}")
            # Les DEUX harness doivent être équipables depuis CHAQUE variante.
            for harness, artefacts in HARNESS_ARTEFACTS.items():
                for rel in artefacts:
                    if not (base / rel).is_file():
                        errors.append(f"Artefact du harness {harness} absent : "
                                      f"{(base / rel).relative_to(ROOT)}")
    if errors:
        report(errors)
        return 1

    # ── Règle 1 : identité octet par octet intra-langue (scripts + consignes/config) ──
    for lang in LANGS:
        ref_os = OSES[0]
        for script in SCRIPTS:
            ref = file_digest(variant_dir(lang, ref_os) / script)
            for os_name in OSES[1:]:
                if file_digest(variant_dir(lang, os_name) / script) != ref:
                    errors.append(
                        f"[{lang}] {script} diverge entre {ref_os} et {os_name} "
                        f"(les scripts d'une même langue doivent être identiques octet par octet)."
                    )
        ref_files = synced_files(lang, ref_os)
        for os_name in OSES[1:]:
            other = synced_files(lang, os_name)
            for rel in sorted(set(ref_files) | set(other)):
                if rel not in ref_files:
                    errors.append(f"[{lang}] {rel} existe sur {os_name} mais pas sur {ref_os}.")
                elif rel not in other:
                    errors.append(f"[{lang}] {rel} existe sur {ref_os} mais pas sur {os_name}.")
                elif ref_files[rel] != other[rel]:
                    errors.append(f"[{lang}] {rel} diverge entre {ref_os} et {os_name}.")

    # ── Règle 2 : équivalence structurelle FR vs ENG (AST modulo chaînes) ──
    for script in SCRIPTS:
        dumps = {}
        for lang in LANGS:
            path = variant_dir(lang, OSES[0]) / script
            try:
                dumps[lang] = structural_dump(path)
            except SyntaxError as exc:
                errors.append(f"[{lang}] {script} : erreur de syntaxe ({exc}).")
        if len(dumps) == len(LANGS) and dumps["FR"] != dumps["ENG"]:
            errors.append(
                f"{script} : la STRUCTURE du code diffère entre FR et ENG (au-delà des chaînes "
                f"traduites). Constante, fonction ou branche oubliée lors de la propagation ? "
                f"C'est exactement la famille de bugs PO_SKILL_FILE/tmp_file."
            )

    # ── Règle 3 : mêmes chemins de consignes/config entre FR et ENG ──
    fr_paths = set(synced_files("FR", OSES[0]))
    eng_paths = set(synced_files("ENG", OSES[0]))
    for rel in sorted(fr_paths - eng_paths):
        errors.append(f"{rel} existe en FR mais pas en ENG.")
    for rel in sorted(eng_paths - fr_paths):
        errors.append(f"{rel} existe en ENG mais pas en FR.")

    # ── Règle 4 : couche app identique sur les 6 ; lanceurs identiques par OS ──
    for rel in UNIFIED_FILES:
        digests = {f"{lang}/{os_name}": file_digest(variant_dir(lang, os_name) / rel)
                   for lang in LANGS for os_name in OSES}
        if len(set(digests.values())) > 1:
            errors.append(f"{rel} diverge entre variantes (il doit être IDENTIQUE sur les 6) : "
                          + ", ".join(sorted(digests)) + ".")
    for os_name, launchers in OS_LAUNCHERS.items():
        for rel in launchers:
            digs = {lang: file_digest(variant_dir(lang, os_name) / rel) for lang in LANGS}
            if len(set(digs.values())) > 1:
                errors.append(f"{rel} diverge entre FR/{os_name} et ENG/{os_name}.")
            for lang in LANGS:
                for other_os in OSES:
                    if other_os != os_name and (variant_dir(lang, other_os) / rel).exists():
                        errors.append(f"{rel} n'a rien à faire dans {lang}/{other_os} "
                                      f"(lanceur réservé aux variantes {os_name}).")

    report(errors)
    return 1 if errors else 0


def report(errors: list):
    if errors:
        print(f"❌ {len(errors)} divergence(s) entre variantes :")
        for err in errors:
            print(f"   - {err}")
        print("\n→ Workflow attendu : éditer la variante Ubuntu d'une langue, recopier le .py")
        print("  tel quel vers MacOS/Windows (cp), porter la traduction dans l'autre langue ;")
        print("  app/manifeste/install.sh : recopier LE MÊME fichier vers les 6 variantes ;")
        print("  puis relancer ce script jusqu'au vert.")
    else:
        print("✅ Les 6 variantes sont synchronisées (scripts identiques par langue, structure "
              "FR/ENG équivalente, consignes alignées, couche app unifiée, lanceurs par OS).")


if __name__ == "__main__":
    sys.exit(main())
