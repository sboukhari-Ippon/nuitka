#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_documentation_units — tests unitaires des fonctions PURES du pipeline documentation
────────────────────────────────────────────────────────────────────────────────────────
Cas limites du validateur de carte (`Documentation.py`) apparus sur un monorepo de
1 639 fichiers (22/08/2026) : zone « Divers » déclarée vide (le prompt le demandait, le
validateur la rejetait), entrées RÉPERTOIRE, taille du résiduel, échantillon du prompt
de cartographie. Même patron de chargement que test_audit_units.py (harness mock, cwd
dans un bac à sable : l'import du module ne touche jamais le dépôt).

Cible : la variante FR/Ubuntu uniquement (tools/check_variants_sync.py garantit le reste).

    python3 tools/test_documentation_units.py        # stdlib pure (unittest) + pyyaml
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENGINE = os.path.join(REPO, "FR", "Ubuntu", "engine")
DOC_PATH = os.path.join(ENGINE, "Documentation.py")


def load_doc_module():
    """Charge Documentation.py comme module sous harness MOCK : aucun tmux, aucun LLM, et
    l'import ne touche pas au dépôt (cwd déplacé dans un bac à sable AVANT l'exec)."""
    workspace = tempfile.mkdtemp(prefix="mm-doc-units-")
    scenario = os.path.join(workspace, "scenario.json")
    with open(scenario, "w", encoding="utf-8") as f:
        json.dump({"steps": []}, f)
    os.environ["MM_AGENT_HARNESS"] = "mock"
    os.environ["MM_MOCK_SCENARIO"] = scenario
    os.environ["MM_MOCK_JOURNAL"] = os.path.join(workspace, "journal.jsonl")
    sys.path.insert(0, ENGINE)   # mm_runner, mm_core, mm_audit
    sys.path.insert(0, HERE)     # mm_mock_runner
    os.chdir(workspace)
    spec = importlib.util.spec_from_file_location("documentation", DOC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = load_doc_module()

CODE = ["src/auth/login.ts", "src/auth/session.ts", "src/cart/cart.ts", "src/cart/totals.ts",
        "src/shared/format.ts"]
TESTS = ["src/auth/login.spec.ts", "src/cart/totals.spec.ts"]


def zone(zid, name, files=None, tests=None, intent="i"):
    z = {"id": zid, "name": name, "intent": intent}
    if files is not None:
        z["files"] = files
    if tests is not None:
        z["tests"] = tests
    return z


class TestDiversFacultative(unittest.TestCase):

    def test_divers_vide_est_completee_par_la_couverture(self):
        doc_map = {"project": "p", "zones": [
            zone(1, "Authentification", ["src/auth/login.ts", "src/auth/session.ts"], ["src/auth/login.spec.ts"]),
            zone(2, "Divers", [], []),
        ]}
        fatal, soft = D.validate_and_normalize_doc_map(doc_map, CODE, TESTS)
        self.assertEqual(fatal, [])
        divers = doc_map["zones"][1]
        self.assertEqual(sorted(divers["files"]), ["src/cart/cart.ts", "src/cart/totals.ts", "src/shared/format.ts"])
        self.assertEqual(divers["tests"], ["src/cart/totals.spec.ts"])
        self.assertTrue(any("déclarée vide" in s for s in soft))

    def test_divers_vide_et_couverture_complete_est_retiree(self):
        doc_map = {"project": "p", "zones": [
            zone(1, "Tout", list(CODE), list(TESTS)),
            zone(2, "Divers", [], []),
        ]}
        fatal, _soft = D.validate_and_normalize_doc_map(doc_map, CODE, TESTS)
        self.assertEqual(fatal, [])
        self.assertEqual([z["name"] for z in doc_map["zones"]], ["Tout"])

    def test_zone_non_divers_vide_reste_fatale(self):
        doc_map = {"project": "p", "zones": [
            zone(1, "Authentification", list(CODE), list(TESTS)),
            zone(2, "Panier", [], []),
        ]}
        fatal, _ = D.validate_and_normalize_doc_map(doc_map, CODE, TESTS)
        self.assertTrue(any("Panier" in f and "aucun fichier" in f for f in fatal))


class TestEntreesRepertoire(unittest.TestCase):

    def test_repertoire_etend_code_et_tests(self):
        doc_map = {"project": "p", "zones": [
            zone(1, "Panier", ["src/cart/"], []),
            zone(2, "Reste", ["src/auth/", "src/shared/format.ts"], []),
        ]}
        fatal, _ = D.validate_and_normalize_doc_map(doc_map, CODE, TESTS)
        self.assertEqual(fatal, [])
        panier, reste = doc_map["zones"]
        self.assertEqual(sorted(panier["files"]), ["src/cart/cart.ts", "src/cart/totals.ts"])
        self.assertEqual(panier["tests"], ["src/cart/totals.spec.ts"])
        self.assertEqual(sorted(reste["files"]), ["src/auth/login.ts", "src/auth/session.ts", "src/shared/format.ts"])
        self.assertEqual(reste["tests"], ["src/auth/login.spec.ts"])
        self.assertEqual(len(doc_map["zones"]), 2, "couverture complète : pas de Divers ajoutée")

    def test_repertoire_ne_reprend_pas_les_fichiers_deja_assignes(self):
        doc_map = {"project": "p", "zones": [
            zone(1, "Connexion", ["src/auth/login.ts"], []),
            zone(2, "Auth", ["src/auth/"], []),
        ]}
        fatal, _ = D.validate_and_normalize_doc_map(doc_map, CODE, TESTS)
        self.assertEqual(fatal, [])
        self.assertEqual(doc_map["zones"][1]["files"], ["src/auth/session.ts"])

    def test_repertoire_inconnu_est_retire_comme_un_chemin_invente(self):
        doc_map = {"project": "p", "zones": [zone(1, "X", ["nope/", "src/auth/login.ts"], [])]}
        fatal, soft = D.validate_and_normalize_doc_map(doc_map, CODE, TESTS)
        self.assertEqual(fatal, [])
        self.assertTrue(any("hors périmètre" in s for s in soft))


class TestResiduel(unittest.TestCase):

    def test_divers_size_et_files(self):
        doc_map = {"zones": [zone(1, "A", ["x"], []), zone(2, "Divers", ["a", "b"], ["t"])]}
        self.assertEqual(D.divers_size(doc_map), 3)
        self.assertEqual(D.divers_files(doc_map), ["a", "b", "t"])
        self.assertEqual(D.divers_size({"zones": [zone(1, "A", ["x"], [])]}), 0)


class TestEchantillonCarto(unittest.TestCase):

    def test_le_code_applicatif_est_liste_et_le_surplus_assignable_par_repertoire(self):
        public = [f"packages/app/public/dsfr/icons/icon-{i:04d}.css" for i in range(700)]
        src = [f"packages/app/src/modules/m{i % 20}/file-{i:04d}.ts" for i in range(300)]
        tests = [f"packages/app/src/modules/m{i % 20}/file-{i:04d}.spec.ts" for i in range(60)]
        code_block, tests_block, overflow_block = D.build_carto_scope_blocks(sorted(public + src), sorted(tests))
        self.assertEqual(code_block.count("\n- ") + 1, D.MAX_SCOPE_FILES_IN_CARTO)
        self.assertEqual(code_block.count("/src/"), 300, "tout src/ est dans l'échantillon")
        self.assertEqual(tests_block, "(aucun)", "budget épuisé par le code : tests résumés dans le surplus")
        self.assertIn("PAR RÉPERTOIRE", overflow_block)
        self.assertIn("packages/app/public/dsfr/icons/ : 600", overflow_block, "400 = 300 src + 100 public")

    def test_sous_la_borne_tout_est_liste(self):
        code_block, tests_block, overflow_block = D.build_carto_scope_blocks(CODE, TESTS)
        self.assertEqual(code_block.count("- "), len(CODE))
        self.assertEqual(tests_block.count("- "), len(TESTS))
        self.assertEqual(overflow_block, "")

    def test_summarize_by_directory_borne_et_trie_par_taille(self):
        files = [f"d{i}/f.py" for i in range(70)] + ["big/a.py", "big/b.py", "big/c.py"]
        summary = D.summarize_by_directory(files, max_lines=5)
        self.assertTrue(summary.startswith("- big/ : 3 fichier(s)"))
        self.assertIn("autre(s) répertoire(s)", summary)


class TestGardeDesSources(unittest.TestCase):
    """Garde des sources citées (28/08/2026) : une zone de scripts d'orchestration parle de
    branches git, de globs, de chemins créés à l'exécution — 8 faux positifs sur 11, trois
    tentatives brûlées. Seuls les chemins ANCRÉS à la racine du projet sont des sources."""

    def setUp(self):
        self.old_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="mm-doc-sources-")
        os.chdir(self.tmp)
        os.makedirs("scripts/orchestration")
        os.makedirs("src")
        for f in ("scripts/orchestration/dispatch_plan.sh", "src/app.py"):
            open(f, "w", encoding="utf-8").close()

    def tearDown(self):
        os.chdir(self.old_cwd)

    def zone_file(self, features_body, bilan_ats_label="Tests d'acceptance"):
        path = "zone.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Z1 — Orchestration\n\n## Features\n\n### F1 — Boucle\n"
                    + features_body + "\n- **AT1 — Proposé :** x\n\n## Bilan\n- Features : 1\n"
                    f"- {bilan_ats_label} : 1 (couverts : 0, proposés : 1)\n")
        return path

    def test_motifs_et_placeholders_ne_sont_pas_des_citations(self):
        for token in ("docs/*.md", "epic/<KEY>", "origin/epic/<KEY>", "tick_*_agent_<TICKET>.json",
                      ".claude/state/run/<agent-id>.log", "epic/<KEY> → main"):
            self.assertFalse(D.looks_like_path(token), token)
        self.assertTrue(D.looks_like_path("scripts/orchestration/dispatch_plan.sh"))
        self.assertTrue(D.looks_like_path("dispatch_plan.sh"))

    def test_chemins_hors_racine_ignores_typos_dans_le_projet_rejetees(self):
        body = ("Lit `scripts/orchestration/dispatch_plan.sh`, pousse `origin/epic/KEY`, écrit dans "
                "`docs/`, expose `/report`, mais cite `scripts/orchestration/agent_drilldown.h` et "
                "`scripts/orchestrationdispatch_plan.sh`.")
        issues = D.zone_content_issues(self.zone_file(body), set(), ["scripts/orchestration/dispatch_plan.sh"])
        joined = "\n".join(issues)
        self.assertIn("`scripts/orchestration/agent_drilldown.h` n'existe pas", joined)
        self.assertIn("`scripts/orchestrationdispatch_plan.sh` n'existe pas", joined)
        for runtime in ("origin/", "docs/", "/report"):
            self.assertNotIn(runtime, joined)
        self.assertEqual(len(issues), 2, joined)

    def test_basename_nu_suggere_le_chemin_exact(self):
        issues = D.zone_content_issues(self.zone_file("Voir `dispatch_plan.sh`."), set(),
                                       ["scripts/orchestration/dispatch_plan.sh"])
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("nom de fichier nu", issues[0])
        self.assertIn("`scripts/orchestration/dispatch_plan.sh`", issues[0])

    def test_basename_ambigu_ou_hors_zone_garde_le_message_generique(self):
        issues = D.zone_content_issues(self.zone_file("Voir `dispatch_plan.sh`."), set(), None)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("n'existe pas dans le projet", issues[0])

    def test_bilan_accepte_acceptation_et_acceptance(self):
        for label in ("Tests d'acceptance", "Tests d'acceptation", "**Tests d'acceptation**"):
            issues = D.zone_content_issues(self.zone_file("Rien à citer.", label), set(), None)
            self.assertEqual(issues, [], label)


if __name__ == "__main__":
    unittest.main(verbosity=1)
