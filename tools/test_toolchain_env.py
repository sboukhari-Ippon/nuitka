#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_toolchain_env — tests unitaires des fonctions pures ajoutées à mm_core après
l'incident du 23/08/2026 (verdict rendu sous Node 18 pendant que l'agent voyait Node 22)
─────────────────────────────────────────────────────────────────────────────────────────
Couvre : sonde du PATH du shell de login (bouchonnée), fusion en tête et mémoïsation,
lecture de la version de Node attendue, comparaison de contrainte semver réduite,
signature d'incompatibilité de runtime, échantillon représentatif de cartographie,
entrées répertoire, attente adaptative (prolongation si l'agent travaille, arrêt si
demande de permission), avertissement de livrable résiduel.

Cible FR/Ubuntu uniquement (suffisant par construction : tools/check_variants_sync.py
garantit l'identité intra-langue et l'AST FR/ENG chaînes masquées).

    python3 tools/test_toolchain_env.py        # stdlib pure (unittest) + pyyaml (mm_core)
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENGINE = os.path.join(REPO, "FR", "Ubuntu", "engine")

sys.path.insert(0, ENGINE)   # mm_runner, mm_audit (imports de mm_core)
spec = importlib.util.spec_from_file_location("mm_core", os.path.join(ENGINE, "mm_core.py"))
mm_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm_core)


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class StaticRunner:
    """RUNNER minimal : écran constant, ou séquence d'écrans."""

    def __init__(self, screens):
        self.screens = list(screens)

    def capture(self):
        if len(self.screens) > 1:
            return self.screens.pop(0)
        return self.screens[0]


class ToolchainEnvCase(unittest.TestCase):

    def setUp(self):
        self._path = os.environ.get("PATH", "")
        self._probe = os.environ.pop("MM_TOOLCHAIN_PROBE", None)
        self._harness = os.environ.pop("MM_AGENT_HARNESS", None)
        self._run = subprocess.run
        mm_core._TOOLCHAIN.update(probed=False, login_path=None, preflight_done=False)

    def tearDown(self):
        os.environ["PATH"] = self._path
        for key, value in (("MM_TOOLCHAIN_PROBE", self._probe), ("MM_AGENT_HARNESS", self._harness)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        subprocess.run = self._run
        mm_core.subprocess.run = self._run
        mm_core._TOOLCHAIN.update(probed=False, login_path=None, preflight_done=False)

    # ─── sonde du shell de login ──────────────────────────────────────────────

    def test_probe_extrait_le_path_entre_marqueurs(self):
        mm_core.subprocess.run = lambda *a, **k: FakeProc(
            "motd bavard\n\n__MM_PATH_B__\n/home/u/.nvm/versions/node/v22/bin:/usr/bin\n__MM_PATH_E__\n")
        self.assertEqual(mm_core.probe_login_path(), "/home/u/.nvm/versions/node/v22/bin:/usr/bin")

    def test_probe_sans_marqueurs_rend_none(self):
        mm_core.subprocess.run = lambda *a, **k: FakeProc("bash: erreur\n")
        self.assertIsNone(mm_core.probe_login_path())

    def test_probe_exception_rend_none(self):
        def boom(*a, **k):
            raise OSError("no shell")
        mm_core.subprocess.run = boom
        self.assertIsNone(mm_core.probe_login_path())

    def test_unify_place_le_path_de_login_en_tete_et_memoise(self):
        os.environ["PATH"] = "/usr/local/bin:/usr/bin"
        calls = []

        def fake(*a, **k):
            calls.append(a)
            return FakeProc("\n__MM_PATH_B__\n/home/u/.nvm/v22/bin:/usr/bin\n__MM_PATH_E__\n")
        mm_core.subprocess.run = fake
        self.assertEqual(mm_core.unify_toolchain_env(), "/home/u/.nvm/v22/bin:/usr/bin")
        self.assertEqual(os.environ["PATH"].split(os.pathsep),
                         ["/home/u/.nvm/v22/bin", "/usr/bin", "/usr/local/bin"])
        mm_core.unify_toolchain_env()
        self.assertEqual(len(calls), 1, "la sonde ne tourne qu'une fois par processus")

    def test_unify_desactivee_par_env(self):
        os.environ["MM_TOOLCHAIN_PROBE"] = "0"
        mm_core.subprocess.run = lambda *a, **k: self.fail("la sonde ne doit pas tourner")
        self.assertIsNone(mm_core.unify_toolchain_env())

    def test_unify_court_circuitee_sous_mock(self):
        os.environ["MM_AGENT_HARNESS"] = "mock"
        mm_core.subprocess.run = lambda *a, **k: self.fail("la sonde ne doit pas tourner")
        self.assertIsNone(mm_core.unify_toolchain_env())

    def test_unify_en_echec_garde_le_path(self):
        os.environ["PATH"] = "/usr/bin"
        mm_core.subprocess.run = lambda *a, **k: FakeProc("")
        self.assertIsNone(mm_core.unify_toolchain_env())
        self.assertEqual(os.environ["PATH"], "/usr/bin")

    # ─── Node attendu / vu ────────────────────────────────────────────────────

    def test_node_version_matches(self):
        ok = mm_core.node_version_matches
        self.assertTrue(ok("v22.22.2", "22"))
        self.assertTrue(ok("v22.22.2", "v22.1.0"))
        self.assertFalse(ok("v18.19.1", "22"))
        self.assertTrue(ok("v22.0.0", ">=20.19"))
        self.assertFalse(ok("v18.0.0", ">=20.19"))
        self.assertTrue(ok("v22.0.0", "^20.19.0 || >=22.12.0"))
        self.assertFalse(ok("v21.0.0", "^20.19.0 || >=22.12.0"))
        self.assertTrue(ok("v24.1.0", ">= 24"))
        self.assertTrue(ok("v18.0.0", "lts/*"), "contrainte non comparable : jamais d'alarme")
        self.assertTrue(ok("", "22"), "version illisible : jamais d'alarme")

    def test_node_expected_version_lit_nvmrc_puis_engines(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(mm_core.node_expected_version(), "")
                with open("package.json", "w", encoding="utf-8") as f:
                    json.dump({"engines": {"node": ">=24"}}, f)
                self.assertEqual(mm_core.node_expected_version(), ">=24")
                with open(".nvmrc", "w", encoding="utf-8") as f:
                    f.write("22\n")
                self.assertEqual(mm_core.node_expected_version(), "22", ".nvmrc prime")
            finally:
                os.chdir(cwd)

    def test_toolchain_failure_hint(self):
        os.environ["MM_TOOLCHAIN_PROBE"] = "0"
        self.assertEqual(mm_core.toolchain_failure_hint("FAIL src/a.test.ts\nexpected 1 to be 2"), "")
        hint = mm_core.toolchain_failure_hint(
            "SyntaxError: The requested module 'node:util' does not provide an export named 'styleText'")
        self.assertIn("RUNTIME", hint)
        self.assertIn("node --version", hint)

    def test_js_toolchain_detection(self):
        self.assertTrue(mm_core._JS_TOOLCHAIN_RE.search("npx tsc -b && npx vitest run"))
        self.assertTrue(mm_core._JS_TOOLCHAIN_RE.search("pnpm test"))
        self.assertFalse(mm_core._JS_TOOLCHAIN_RE.search("python3 verify.py"))
        self.assertFalse(mm_core._JS_TOOLCHAIN_RE.search("mvn -q test"))

    # ─── cartographie ─────────────────────────────────────────────────────────

    def test_select_carto_sample_privilegie_le_code(self):
        public = [f"packages/app/public/dsfr/icons/icon-{i:04d}.css" for i in range(700)]
        src = [f"packages/app/src/modules/m{i % 30}/file-{i:04d}.ts" for i in range(300)]
        files = sorted(public + src)
        sample = mm_core.select_carto_sample(files, 400)
        self.assertEqual(len(sample), 400)
        self.assertEqual(len([f for f in sample if "/src/" in f]), 300, "tout src/ est listé")
        self.assertEqual(sample, [f for f in files if f in set(sample)], "ordre du périmètre conservé")
        self.assertEqual(sample, mm_core.select_carto_sample(files, 400), "déterministe")

    def test_select_carto_sample_tourniquet_par_repertoire(self):
        files = [f"lib/a/f{i}.py" for i in range(50)] + [f"lib/b/f{i}.py" for i in range(50)]
        sample = mm_core.select_carto_sample(sorted(files), 10)
        self.assertEqual(len([f for f in sample if "/a/" in f]), 5)
        self.assertEqual(len([f for f in sample if "/b/" in f]), 5)

    def test_select_carto_sample_sous_la_borne(self):
        files = ["a.py", "b.py"]
        self.assertEqual(mm_core.select_carto_sample(files, 400), files)

    def test_expand_dir_entry(self):
        scope = ["src/cart/a.ts", "src/cart/sub/b.ts", "src/auth/c.ts"]
        self.assertEqual(mm_core.expand_dir_entry("src/cart/", scope, {}),
                         ["src/cart/a.ts", "src/cart/sub/b.ts"])
        self.assertEqual(mm_core.expand_dir_entry("src/cart/", scope, {"src/cart/a.ts": 1}),
                         ["src/cart/sub/b.ts"])
        self.assertEqual(mm_core.expand_dir_entry("src/cart/a.ts", scope, {}), [], "pas un répertoire")
        self.assertEqual(mm_core.expand_dir_entry("nope/", scope, {}), [])

    # ─── attente adaptative ───────────────────────────────────────────────────

    def test_wait_ecran_fige_expire_au_budget_nominal(self):
        mm_core.RUNNER = StaticRunner(["écran ⠇ constant"])
        activity = {}
        start = time.time() - 10
        self.assertFalse(mm_core.wait_should_continue(start, 5, activity))
        self.assertEqual(activity["stop"], "timeout")

    def test_wait_premiere_observation_nest_pas_une_activite(self):
        mm_core.RUNNER = StaticRunner(["écran constant"])
        start = time.time() - 10
        self.assertFalse(mm_core.wait_should_continue(start, 5, {}))

    def test_wait_ecran_actif_prolonge_jusqu_au_plafond(self):
        mm_core.RUNNER = StaticRunner(["écran 1", "écran 2", "écran 3", "écran 4"])
        activity = {}
        now = time.time()
        self.assertTrue(mm_core.wait_should_continue(now, 5, activity), "premier tour : référence")
        # Le temps passe (budget nominal dépassé), l'écran a changé : prolongation.
        self.assertTrue(mm_core.wait_should_continue(now - 10, 5, activity), "écran changé : prolongé")
        self.assertTrue(activity.get("extended_warned"))
        # Plafond dur : 3 × timeout, même avec un écran actif.
        self.assertFalse(mm_core.wait_should_continue(now - 100, 5, activity),
                         "au-delà de 3 × timeout, même actif, on s'arrête")
        self.assertEqual(activity["stop"], "timeout")

    def test_wait_spinner_nest_pas_une_activite(self):
        mm_core.RUNNER = StaticRunner(["Thinking ⠇  ⬝⬝⬝", "Thinking ⠏  ⬝⬝⬝⬝", "Thinking ⠹ ⬝"])
        activity = {}
        start = time.time() - 10
        mm_core.wait_should_continue(start, 5, activity)
        self.assertFalse(mm_core.wait_should_continue(start, 5, activity))

    def test_wait_demande_de_permission_arrete_vite(self):
        mm_core.RUNNER = StaticRunner(["△ Permission required\n  Allow once  Allow always  Reject"])
        activity = {}
        start = time.time()
        self.assertTrue(mm_core.wait_should_continue(start, 600, activity))
        self.assertTrue(mm_core.wait_should_continue(start, 600, activity))
        self.assertFalse(mm_core.wait_should_continue(start, 600, activity))
        self.assertEqual(activity["stop"], "permission")

    # ─── livrable résiduel ────────────────────────────────────────────────────

    def test_residual_deliverable_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                run = os.path.join(".mm-runs", "20260822-214708-documentation")
                os.makedirs(run)
                with open(os.path.join(run, "events.jsonl"), "w", encoding="utf-8") as f:
                    f.write('{"ts": 0, "kind": "run_start"}\n')
                old = time.time() - 600
                os.utime(os.path.join(run, "events.jsonl"), (old, old))
                with open("doc_map.yaml", "w", encoding="utf-8") as f:
                    f.write("zones: []\n")
                warning = mm_core.residual_deliverable_warning("doc_map.yaml", "documentation")
                self.assertIn("orphelin", warning)
                self.assertIn("20260822-214708-documentation", warning)
                # run clos → pas d'avertissement
                with open(os.path.join(run, "run.json"), "w", encoding="utf-8") as f:
                    f.write("{}")
                self.assertEqual(mm_core.residual_deliverable_warning("doc_map.yaml", "documentation"), "")
                # autre orchestrateur → ignoré
                self.assertEqual(mm_core.residual_deliverable_warning("doc_map.yaml", "pre-audit-a11y"), "")
            finally:
                os.chdir(cwd)


class PlannedTestChangesCase(unittest.TestCase):
    """Canal « tests planifiés » : tests_to_remove / tests_to_update déclarés par le plan."""

    def setUp(self):
        self._git = mm_core._GIT
        self._bb_file = mm_core.BLACKBOARD_FILE
        mm_core._GIT = {"enabled": False}
        # Noms normalement injectés par configure() de l'orchestrateur.
        self._rules, self._seen = mm_core.REQUIRED_GLOBAL_RULES, mm_core._PHASE_STATUS_SEEN
        mm_core.REQUIRED_GLOBAL_RULES = ("target",)
        mm_core._PHASE_STATUS_SEEN = {}
        self.tmp = tempfile.mkdtemp(prefix="mm-planned-tests-")
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        mm_core.BLACKBOARD_FILE = os.path.join(self.tmp, "blackboard.yaml")

    def tearDown(self):
        os.chdir(self._cwd)
        mm_core._GIT = self._git
        mm_core.BLACKBOARD_FILE = self._bb_file
        mm_core.REQUIRED_GLOBAL_RULES, mm_core._PHASE_STATUS_SEEN = self._rules, self._seen

    def test_normalisation_et_absence(self):
        self.assertEqual(mm_core.planned_test_changes({}), ([], []))
        self.assertEqual(mm_core.planned_test_changes({"tests_to_remove": ["./tests/a.test.ts", " 'b_test.py' "],
                                                       "tests_to_update": "pas-une-liste"}),
                         (["tests/a.test.ts", "b_test.py"], []))

    def test_allowed_test_edits_union(self):
        phase = {"tests_to_remove": ["tests/old.py"], "tests_to_update": ["tests/evol.py"]}
        self.assertEqual(mm_core.allowed_test_edits(phase, {"_yolo_deleted_tests": ["tests/x.py"]}),
                         {"tests/old.py", "tests/evol.py", "tests/x.py"})
        self.assertEqual(mm_core.allowed_test_edits({}, {}), set())

    def test_policy_texte(self):
        self.assertEqual(mm_core.planned_test_changes_policy({}), "")
        text = mm_core.planned_test_changes_policy({"tests_to_remove": ["tests/old.py"],
                                                    "tests_to_update": ["tests/evol.py"]})
        self.assertIn("EXCEPTION PLANIFIÉE", text)
        self.assertIn("tests/old.py", text)
        self.assertIn("tests/evol.py", text)

    def test_schema_refuse_un_fichier_de_production(self):
        blackboard = {"project": "p", "verify_cmd": "python3 verify.py",
                      "global_rules": {k: "x" for k in (mm_core.REQUIRED_GLOBAL_RULES or [])},
                      "phases": [{"id": 1, "name": "A", "tasks": ["t"], "nature": "feature",
                                  "tests_to_remove": ["src/prod.py", "tests/test_old.py"]},
                                 {"id": 2, "name": "B", "tasks": ["t"], "nature": "feature",
                                  "tests_to_update": "tests/x.py"}]}
        fatal, _soft = mm_core.validate_blackboard_schema(blackboard)
        self.assertTrue(any("tests_to_remove" in f and "src/prod.py" in f for f in fatal), fatal)
        self.assertTrue(any("tests_to_update" in f and "liste" in f for f in fatal), fatal)
        ok = {"project": "p", "verify_cmd": "python3 verify.py",
              "global_rules": {k: "x" for k in (mm_core.REQUIRED_GLOBAL_RULES or [])},
              "phases": [{"id": 1, "name": "A", "tasks": ["t"], "nature": "feature",
                          "tests_to_remove": ["tests/test_old.py"]}]}
        fatal, _ = mm_core.validate_blackboard_schema(ok)
        self.assertEqual([f for f in fatal if "tests_to" in f], [])

    def test_remove_planned_obsolete_tests(self):
        os.makedirs("tests"); os.makedirs("src")
        for path in ("tests/test_old.py", "src/prod.py"):
            with open(path, "w", encoding="utf-8") as f:
                f.write("x = 1\n")
        blackboard = {"phases": [], "last_test_count": 4,
                      "protected_test_files": ["tests/test_old.py", "tests/test_keep.py"]}
        phase = {"id": 3, "tests_to_remove": ["tests/test_old.py", "src/prod.py", "tests/absent.py"]}
        deleted = mm_core.remove_planned_obsolete_tests(phase, blackboard)
        self.assertEqual(deleted, ["tests/test_old.py"])
        self.assertFalse(os.path.exists("tests/test_old.py"))
        self.assertTrue(os.path.exists("src/prod.py"), "jamais de code de production")
        self.assertNotIn("last_test_count", blackboard, "re-baseline de la garde de non-décroissance")
        self.assertEqual(blackboard["protected_test_files"], ["tests/test_keep.py"])
        self.assertEqual(blackboard["_yolo_deleted_tests"], ["tests/test_old.py"])
        self.assertTrue(os.path.exists(mm_core.BLACKBOARD_FILE), "blackboard sauvegardé")
        self.assertEqual(mm_core.remove_planned_obsolete_tests({"id": 4}, blackboard), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
