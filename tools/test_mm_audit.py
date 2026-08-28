#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_mm_audit — tests unitaires du journal de run (boîte noire, plan-big-last Lot 2)
────────────────────────────────────────────────────────────────────────────────────
Cible FR/Ubuntu uniquement (suffisant par construction : les autres variantes sont
identiques octet par octet intra-langue et AST chaînes masquées, garanti par
tools/check_variants_sync.py).

    python3 tools/test_mm_audit.py        # stdlib pure (unittest)
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

spec = importlib.util.spec_from_file_location("mm_audit", os.path.join(ENGINE, "mm_audit.py"))
mm_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm_audit)


class MmAuditCase(unittest.TestCase):

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="mm-audit-test-")
        os.environ.pop("MM_AUDIT", None)

    def tearDown(self):
        mm_audit._reset()
        os.environ.pop("MM_AUDIT", None)

    def events(self):
        path = os.path.join(mm_audit.run_dir(), "events.jsonl")
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_cycle_nominal(self):
        mm_audit.start(self.project, "safe-coding", "mock", distro_version="3.0.0")
        self.assertTrue(mm_audit.enabled())
        run_path = mm_audit.run_dir()
        self.assertTrue(run_path.startswith(os.path.join(self.project, ".mm-runs")))
        mm_audit.event("verdict", cmd="python3 verify.py", exit=0, duration_s=1.2)
        mm_audit.event("gate", id="spec", gate_kind="yn", answer="y")
        events = self.events()
        self.assertEqual([e["kind"] for e in events], ["run_start", "verdict", "gate"])
        self.assertEqual(events[0]["orchestrator"], "safe-coding")
        mm_audit.end("success")
        self.assertFalse(mm_audit.enabled())   # état remis à zéro
        with open(os.path.join(run_path, "run.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["status"], "success")
        self.assertEqual(meta["counters"]["verdict"], 1)
        with open(os.path.join(run_path, "summary.md"), "r", encoding="utf-8") as f:
            summary = f.read()
        self.assertIn("safe-coding", summary)
        self.assertIn("success", summary)
        # run_end est bien la DERNIÈRE ligne du jsonl
        with open(os.path.join(run_path, "events.jsonl"), "r", encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual(last["kind"], "run_end")

    def test_desactive_par_env(self):
        os.environ["MM_AUDIT"] = "0"
        mm_audit.start(self.project, "safe-coding", "mock")
        self.assertFalse(mm_audit.enabled())
        self.assertEqual(mm_audit.run_dir(), "")
        # toutes les méthodes publiques sont des no-op silencieux
        mm_audit.event("verdict", exit=0)
        mm_audit.snapshot(__file__)
        mm_audit.end("success")
        self.assertFalse(os.path.isdir(os.path.join(self.project, ".mm-runs")))

    def test_methodes_muettes_hors_run(self):
        # Aucune exception, jamais, même sans start().
        mm_audit.event("verdict", exit=0)
        mm_audit.snapshot("n-existe-pas.md")
        mm_audit.end("failed")

    def test_snapshot(self):
        mm_audit.start(self.project, "safe-coding", "mock")
        src = os.path.join(self.project, "spec.md")
        with open(src, "w", encoding="utf-8") as f:
            f.write("# Spécification\n")
        mm_audit.snapshot(src)
        mm_audit.snapshot(src)   # deux snapshots du même fichier coexistent
        artifacts = os.listdir(os.path.join(mm_audit.run_dir(), "artifacts"))
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(all(a.endswith("spec.md") for a in artifacts))
        mm_audit.snapshot("n-existe-pas.md")   # absent : pas une erreur
        self.assertEqual(len(os.listdir(os.path.join(mm_audit.run_dir(), "artifacts"))), 2)

    def test_retention(self):
        runs_root = os.path.join(self.project, ".mm-runs")
        os.makedirs(runs_root)
        for i in range(mm_audit.RETENTION_RUNS + 5):
            os.makedirs(os.path.join(runs_root, f"20250101-{i:06d}-vieux"))
        mm_audit.start(self.project, "safe-coding", "mock")
        entries = [e for e in os.listdir(runs_root)
                   if os.path.isdir(os.path.join(runs_root, e))]
        self.assertEqual(len(entries), mm_audit.RETENTION_RUNS)
        # le run courant (nom le plus récent) a survécu à l'élagage
        self.assertIn(os.path.basename(mm_audit.run_dir()), entries)

    def test_collision_meme_seconde(self):
        mm_audit.start(self.project, "safe-coding", "mock")
        first = mm_audit.run_dir()
        mm_audit.start(self.project, "safe-coding", "mock")   # même seconde probable
        second = mm_audit.run_dir()
        self.assertNotEqual(first, second)



class MmAuditRobustesseCase(unittest.TestCase):
    """Clôtures non nominales, copie de la sortie, distro_version depuis le marqueur."""

    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="mm-audit-test-")
        os.environ.pop("MM_AUDIT", None)

    def tearDown(self):
        mm_audit._reset()

    def read_meta(self, run_path):
        with open(os.path.join(run_path, "run.json"), "r", encoding="utf-8") as f:
            return json.load(f)

    def test_end_interrupted(self):
        mm_audit.start(self.project, "documentation", "mock")
        run_path = mm_audit.run_dir()
        mm_audit.end("interrupted")
        self.assertEqual(self.read_meta(run_path)["status"], "interrupted")
        self.assertFalse(mm_audit.enabled())

    def test_atexit_clot_en_aborted_et_reste_no_op_apres_end(self):
        mm_audit.start(self.project, "documentation", "mock")
        run_path = mm_audit.run_dir()
        mm_audit._at_exit()
        self.assertEqual(self.read_meta(run_path)["status"], "aborted")
        mm_audit._at_exit()   # déjà clos : aucun effet, aucune exception
        self.assertEqual(self.read_meta(run_path)["status"], "aborted")

    def test_distro_version_lue_dans_le_marqueur(self):
        with open(os.path.join(self.project, ".mm-equip.json"), "w", encoding="utf-8") as f:
            json.dump({"distro_version": "3.0.1", "harness": "mock"}, f)
        mm_audit.start(self.project, "acceptance-first", "mock")
        run_path = mm_audit.run_dir()
        mm_audit.end("success")
        self.assertEqual(self.read_meta(run_path)["distro_version"], "3.0.1")

    def test_distro_version_explicite_prime(self):
        with open(os.path.join(self.project, ".mm-equip.json"), "w", encoding="utf-8") as f:
            json.dump({"distro_version": "3.0.1"}, f)
        mm_audit.start(self.project, "acceptance-first", "mock", distro_version="9.9.9")
        run_path = mm_audit.run_dir()
        mm_audit.end("success")
        self.assertEqual(self.read_meta(run_path)["distro_version"], "9.9.9")

    def test_orchestrator_log_copie_la_sortie(self):
        import io, sys
        original = sys.stdout
        buffer = io.StringIO()
        sys.stdout = buffer
        try:
            mm_audit.start(self.project, "documentation", "mock")
            run_path = mm_audit.run_dir()
            print("ligne visible dans le pane")
            mm_audit.end("success")
        finally:
            sys.stdout = original
        self.assertIn("ligne visible dans le pane", buffer.getvalue(), "la sortie d'origine est intacte")
        with open(os.path.join(run_path, "orchestrator.log"), "r", encoding="utf-8") as f:
            self.assertIn("ligne visible dans le pane", f.read())
        self.assertIs(sys.stdout, original)

    def test_desactive_pas_de_tee(self):
        import sys
        os.environ["MM_AUDIT"] = "0"
        before = sys.stdout
        mm_audit.start(self.project, "documentation", "mock")
        self.assertIs(sys.stdout, before)
        os.environ.pop("MM_AUDIT", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
