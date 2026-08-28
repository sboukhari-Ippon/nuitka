#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_audit_units — tests unitaires des fonctions PURES de l'audit RGAA
──────────────────────────────────────────────────────────────────────
Complément des scénarios mock (qui testent le FLUX de bout en bout) : ici, les cas
limites des fonctions pures d'`Pre-Audit-A11Y-RGAA.py` — parseur de verdicts, scan des
déclencheurs, couleurs/contrastes, extraction de localisations. Un scénario mock
coûte un projet jetable entier pour UN chemin ; le parseur en a quinze.

Cible : la variante FR/Ubuntu UNIQUEMENT — suffisant par construction, puisque les
autres variantes sont identiques octet par octet (intra-langue) et AST chaînes
masquées (ENG), ce que tools/check_variants_sync.py garantit déjà.

    python3 tools/test_audit_units.py        # stdlib pure (unittest), aucun pytest
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENGINE = os.path.join(REPO, "FR", "Ubuntu", "engine")
AUDIT_PATH = os.path.join(ENGINE, "Pre-Audit-A11Y-RGAA.py")


def load_audit_module():
    """Charge Pre-Audit-A11Y-RGAA.py comme module (le nom de fichier contient des tirets)
    sous harness MOCK : aucun tmux, aucun LLM, et l'import ne touche pas au dépôt
    (cwd déplacé dans un bac à sable AVANT l'exec du module)."""
    workspace = tempfile.mkdtemp(prefix="mm-audit-units-")
    scenario = os.path.join(workspace, "scenario.json")
    with open(scenario, "w", encoding="utf-8") as f:
        json.dump({"steps": []}, f)
    os.environ["MM_AGENT_HARNESS"] = "mock"
    os.environ["MM_MOCK_SCENARIO"] = scenario
    os.environ["MM_MOCK_JOURNAL"] = os.path.join(workspace, "journal.jsonl")
    sys.path.insert(0, ENGINE)   # mm_runner
    sys.path.insert(0, HERE)     # mm_mock_runner
    os.chdir(workspace)
    spec = importlib.util.spec_from_file_location("audit_a11y_rgaa", AUDIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = load_audit_module()

PACK2 = {"id": 1, "slug": "images", "nom": "Images", "criteres": ["1.1", "1.2"]}


def parse_text(content, pack=PACK2):
    """Écrit 'content' dans un fichier de verdicts jetable et le parse."""
    fd, path = tempfile.mkstemp(suffix=".md", dir=os.getcwd())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return A.parse_findings_file(path, pack)


class SandboxTestCase(unittest.TestCase):
    """Chaque test tourne dans son propre répertoire jetable (les contrôles
    d'existence de fichiers cités sont relatifs au cwd)."""

    def setUp(self):
        self._prev_cwd = os.getcwd()
        self._dir = tempfile.mkdtemp(prefix="mm-audit-case-")
        os.chdir(self._dir)

    def tearDown(self):
        os.chdir(self._prev_cwd)


VALID = """# T1 : Images — test

## Verdicts
- 1.1 : NC — image sans alternative
- 1.2 : C

## Constats
### K1 — 1.1 — Image sans alternative
- **Impact :** 3 — Majeur
- **Localisation :** form.html:5
- **Extrait :** <img>
- **Constat :** La balise img n'a pas d'attribut alt.
- **Impact utilisateur :** Lecteur d'écran muet.
- **Correction :** Ajouter un alt.

## Bilan
- Verdicts : C : 1, NC : 1, NA : 0, AVM : 0
"""


class TestParseFindingsFile(SandboxTestCase):

    def test_fichier_valide(self):
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        data, fatal, soft = parse_text(VALID)
        self.assertEqual(fatal, [])
        self.assertEqual(soft, [])
        self.assertEqual(data["verdicts"]["1.1"]["statut"], "NC")
        self.assertEqual(data["verdicts"]["1.2"]["statut"], "C")
        self.assertEqual(len(data["constats"]), 1)
        self.assertEqual(data["constats"][0]["impact"], 3)
        self.assertTrue(data["constats"][0]["localisation_verifiee"])
        self.assertEqual(data["bilan"], {"C": 1, "NC": 1, "NA": 0, "AVM": 0})

    def test_critere_manquant_fatal(self):
        content = VALID.replace("- 1.2 : C\n", "")
        _d, fatal, _s = parse_text(content.replace("C : 1,", "C : 0,"))
        self.assertTrue(any("MANQUANT" in f for f in fatal), fatal)

    def test_critere_hors_pack_fatal(self):
        content = VALID.replace("- 1.2 : C", "- 1.2 : C\n- 9.9 : NA")
        _d, fatal, _s = parse_text(content)
        self.assertTrue(any("HORS PACK" in f for f in fatal), fatal)

    def test_statut_hors_enum_ligne_non_reconnue(self):
        # 'OK' n'est pas dans l'enum : la ligne n'est pas reconnue (soft) et le
        # critère manque à l'appel (fatal).
        content = VALID.replace("- 1.2 : C", "- 1.2 : OK")
        _d, fatal, soft = parse_text(content)
        self.assertTrue(any("MANQUANT" in f for f in fatal), fatal)
        self.assertTrue(any("non reconnue" in s for s in soft), soft)

    def test_nc_sans_constat_fatal(self):
        content = VALID.replace("### K1 — 1.1 — Image sans alternative",
                                "### K1 — 1.2 — Autre constat")
        _d, fatal, _s = parse_text(content)
        self.assertTrue(any("NC sans constat" in f for f in fatal), fatal)

    def test_bilan_absent_fatal(self):
        content = VALID.replace("- Verdicts : C : 1, NC : 1, NA : 0, AVM : 0\n", "")
        _d, fatal, _s = parse_text(content)
        self.assertTrue(any("Bilan absente ou hors format" in f
                            or "ligne de Bilan" in f for f in fatal), fatal)

    def test_bilan_incoherent_fatal(self):
        content = VALID.replace("C : 1, NC : 1", "C : 2, NC : 0")
        _d, fatal, _s = parse_text(content)
        self.assertTrue(any("Bilan incohérent" in f for f in fatal), fatal)

    def test_bloc_fence_ignore(self):
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        # Un exemple cité dans un bloc ``` ne doit JAMAIS être parsé comme verdict.
        content = VALID.replace(
            "## Constats",
            "Exemple hors format :\n```\n- 1.2 : NA\n- Verdicts : C : 9, NC : 9, NA : 9, AVM : 9\n```\n\n## Constats")
        data, fatal, _s = parse_text(content)
        self.assertEqual(fatal, [])
        self.assertEqual(data["verdicts"]["1.2"]["statut"], "C")

    def test_verdict_duplique_soft_premier_conserve(self):
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        content = VALID.replace("- 1.2 : C", "- 1.2 : C\n- 1.2 : NA")
        data, fatal, soft = parse_text(content)
        self.assertEqual(fatal, [])
        self.assertTrue(any("dupliqué" in s for s in soft), soft)
        self.assertEqual(data["verdicts"]["1.2"]["statut"], "C")

    def test_localisation_introuvable_soft_et_marquee(self):
        # form.html n'existe PAS dans ce bac à sable : soft + constat marqué — et
        # depuis C-1 (H1), l'extrait est lui aussi introuvable → fatal de matérialité.
        data, fatal, soft = parse_text(VALID)
        self.assertTrue(any("aucun Extrait" in f for f in fatal), fatal)
        self.assertTrue(any("introuvable" in s for s in soft), soft)
        self.assertFalse(data["constats"][0]["localisation_verifiee"])

    def test_fichier_absent_illisible(self):
        data, fatal, _s = A.parse_findings_file("n-existe-pas.md", PACK2)
        self.assertIsNone(data)
        self.assertTrue(any("illisible" in f for f in fatal), fatal)


class TestBilanRepair(SandboxTestCase):
    """A3 (L3) : réparation mécanique du Bilan — la seule anomalie fatale « Bilan »
    ne coûte plus une passe entière (l'agrégation recompte tout de toute façon)."""

    def test_detecteur_bilan_seul(self):
        self.assertTrue(A.bilan_only_fatals(["Bilan incohérent : annoncé X, compté Y"]))
        self.assertTrue(A.bilan_only_fatals(["ligne de Bilan absente ou hors format (…)"]))
        self.assertFalse(A.bilan_only_fatals([]))
        self.assertFalse(A.bilan_only_fatals(["Bilan incohérent : …",
                                              "verdict(s) MANQUANT(s) pour : 1.2"]))

    def test_reparation_bilan_incoherent(self):
        content = VALID.replace("C : 1, NC : 1", "C : 9, NC : 9")
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        with open("verdicts.md", "w", encoding="utf-8") as f:
            f.write(content)
        data, fatal, _s = A.parse_findings_file("verdicts.md", PACK2)
        self.assertTrue(A.bilan_only_fatals(fatal))
        self.assertTrue(A.repair_bilan_line("verdicts.md", data["verdicts"]))
        _d, fatal2, _s2 = A.parse_findings_file("verdicts.md", PACK2)
        self.assertEqual(fatal2, [])
        with open("verdicts.md", "r", encoding="utf-8") as f:
            self.assertIn("- Verdicts : C : 1, NC : 1, NA : 0, AVM : 0", f.read())

    def test_reparation_bilan_absent(self):
        content = VALID.replace("## Bilan\n- Verdicts : C : 1, NC : 1, NA : 0, AVM : 0\n", "")
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        with open("verdicts.md", "w", encoding="utf-8") as f:
            f.write(content)
        data, fatal, _s = A.parse_findings_file("verdicts.md", PACK2)
        self.assertTrue(A.bilan_only_fatals(fatal))
        self.assertTrue(A.repair_bilan_line("verdicts.md", data["verdicts"]))
        _d, fatal2, _s2 = A.parse_findings_file("verdicts.md", PACK2)
        self.assertEqual(fatal2, [])

    def test_ligne_bilan_en_fence_non_touchee(self):
        # Une ligne de Bilan citée dans un bloc ``` ne doit pas être prise pour LA
        # ligne à remplacer : la vraie (incohérente) est bien celle réécrite.
        content = VALID.replace(
            "## Constats",
            "Exemple :\n```\n- Verdicts : C : 9, NC : 9, NA : 9, AVM : 9\n```\n\n## Constats"
        ).replace("C : 1, NC : 1", "C : 2, NC : 0")
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        with open("verdicts.md", "w", encoding="utf-8") as f:
            f.write(content)
        data, fatal, _s = A.parse_findings_file("verdicts.md", PACK2)
        self.assertTrue(A.bilan_only_fatals(fatal))
        A.repair_bilan_line("verdicts.md", data["verdicts"])
        with open("verdicts.md", "r", encoding="utf-8") as f:
            repaired = f.read()
        self.assertIn("- Verdicts : C : 9, NC : 9, NA : 9, AVM : 9", repaired)  # fence intacte
        self.assertIn("- Verdicts : C : 1, NC : 1, NA : 0, AVM : 0", repaired)  # vraie ligne
        _d, fatal2, _s2 = A.parse_findings_file("verdicts.md", PACK2)
        self.assertEqual(fatal2, [])


class TestExtraitMaterialite(SandboxTestCase):
    """C-1 (H1) : la vérité matérielle des constats — l'extrait recopié doit exister
    dans un fichier cité (espaces normalisés, tolérance ±5 sur la ligne annoncée)."""

    def _seed(self):
        with open("form.html", "w", encoding="utf-8") as f:
            f.write("<form>\n  <input type=\"text\" name=\"email\">\n"
                    "  <button>OK</button>\n</form>\n<img src=\"logo.png\">\n")

    def test_locate_normalise_les_espaces(self):
        self._seed()
        found, path, line = A.locate_extrait('<input   type="text"   name="email">',
                                             ["form.html"])
        self.assertTrue(found)
        self.assertEqual((path, line), ("form.html", 2))

    def test_locate_fragment_de_ligne(self):
        self._seed()
        found, _p, line = A.locate_extrait('name="email"', ["form.html"])
        self.assertTrue(found)
        self.assertEqual(line, 2)

    def test_locate_introuvable_et_fichier_absent(self):
        self._seed()
        self.assertEqual(A.locate_extrait('<img src="hero.webp">', ["form.html"]),
                         (False, "", 0))
        self.assertEqual(A.locate_extrait("peu importe", ["absent.html"]),
                         (False, "", 0))
        self.assertEqual(A.locate_extrait("", ["form.html"]), (False, "", 0))

    def test_parser_extrait_verifie(self):
        self._seed()
        base = VALID.replace("- **Extrait :** <img>\n", "")
        content = base.replace(
            "- **Localisation :** form.html:5\n",
            "- **Localisation :** form.html:5\n- **Extrait :** <img src=\"logo.png\">\n")
        data, fatal, soft = parse_text(content)
        self.assertEqual(fatal, [])
        self.assertEqual(soft, [])
        self.assertTrue(data["constats"][0]["extrait_verifie"])

    def test_parser_extrait_absent_soft(self):
        self._seed()
        # Extrait manquant sur le SEUL constat : soft (non vérifiable) + fatal de
        # passe (aucun extrait retrouvé) — le champ est de fait obligatoire.
        sans_extrait = VALID.replace("- **Extrait :** <img>\n", "")
        data, fatal, soft = parse_text(sans_extrait)
        self.assertTrue(any("Extrait" in s and "absent" in s for s in soft), soft)
        self.assertTrue(any("aucun Extrait" in f for f in fatal), fatal)

    def test_parser_extrait_invente_fatal(self):
        self._seed()
        base = VALID.replace("- **Extrait :** <img>\n", "")
        content = base.replace(
            "- **Localisation :** form.html:5\n",
            "- **Localisation :** form.html:5\n- **Extrait :** <img src=\"invente.png\">\n")
        data, fatal, soft = parse_text(content)
        self.assertTrue(any("aucun Extrait" in f for f in fatal), fatal)
        self.assertFalse(data["constats"][0]["extrait_verifie"])

    def test_parser_tolerance_ligne(self):
        self._seed()
        base = VALID.replace("- **Extrait :** <img>\n", "")
        # ligne annoncée 50, extrait réellement ligne 5 : écart > 5 → soft signalé.
        content = base.replace(
            "- **Localisation :** form.html:5\n",
            "- **Localisation :** form.html:50\n- **Extrait :** <img src=\"logo.png\">\n")
        data, fatal, soft = parse_text(content)
        self.assertEqual(fatal, [])
        self.assertTrue(any("ligne annoncée 50" in s for s in soft), soft)
        self.assertTrue(data["constats"][0]["extrait_verifie"])


class TestFindingsAllNa(unittest.TestCase):

    def test_tout_na(self):
        data = {"verdicts": {"1.1": {"statut": "NA"}, "1.2": {"statut": "NA"}}}
        self.assertTrue(A.findings_all_na(data))

    def test_mixte(self):
        data = {"verdicts": {"1.1": {"statut": "NA"}, "1.2": {"statut": "C"}}}
        self.assertFalse(A.findings_all_na(data))

    def test_vide_ou_none(self):
        self.assertFalse(A.findings_all_na({"verdicts": {}}))
        self.assertFalse(A.findings_all_na(None))


class TestScanTriggers(SandboxTestCase):

    PACKS = [{"id": 1, "regexes": [re.compile(r"<img\b", re.I)]},
             {"id": 11, "regexes": [re.compile(r"<input\b", re.I),
                                    re.compile(r"<form\b", re.I)]}]

    def test_routage_par_contenu(self):
        with open("a.html", "w", encoding="utf-8") as f:
            f.write("<p><IMG src='x.png'></p>\n")
        with open("b.html", "w", encoding="utf-8") as f:
            f.write("<form><input></form>\n")
        with open("c.css", "w", encoding="utf-8") as f:
            f.write("body { margin: 0 }\n")
        triggers, _hits = A.scan_triggers(["a.html", "b.html", "c.css"], self.PACKS)
        self.assertEqual(triggers["a.html"], {1})       # insensible à la casse
        self.assertEqual(triggers["b.html"], {11})
        self.assertEqual(triggers["c.css"], set())      # présent, set vide (couverture)

    def test_fichier_illisible_set_vide(self):
        triggers, hits = A.scan_triggers(["absent.html"], self.PACKS)
        self.assertEqual(triggers["absent.html"], set())
        self.assertEqual(hits, {})

    def test_positions_des_premiers_matchs(self):
        # A5 (H2) : le premier match par (pack, fichier) est conservé (ligne, motif).
        with open("a.html", "w", encoding="utf-8") as f:
            f.write("<p>rien</p>\n<div>\n<img src='x.png'>\n<img src='y.png'>\n</div>\n")
        _triggers, hits = A.scan_triggers(["a.html"], self.PACKS)
        line, pattern = hits[(1, "a.html")]
        self.assertEqual(line, 3)                        # PREMIER match seulement
        self.assertEqual(pattern, r"<img\b")


class TestTriggerHitsBlock(SandboxTestCase):
    """A5 (H2) : le bloc MOTIFS DÉTECTÉS injecté dans le prompt d'une passe déclenchée."""

    PASS = {"declenche": True, "pack": {"id": 1},
            "bucket": {"files": ["a.html", "b.html", "c.css"]}}

    def test_bloc_pour_passe_declenchee(self):
        hits = {(1, "a.html"): (3, r"<img\b")}
        block = A.build_trigger_hits_block(self.PASS, hits)
        self.assertIn("MOTIFS DÉTECTÉS PAR L'ORCHESTRATEUR", block)
        self.assertIn("- a.html:3 — motif « <img\\b »", block)
        self.assertNotIn("b.html", block)                # pas de hit, pas de ligne

    def test_passe_toujours_sans_declencheur_aucun_bloc(self):
        toujours = dict(self.PASS, declenche=False)
        hits = {(1, "a.html"): (3, r"<img\b")}
        self.assertEqual(A.build_trigger_hits_block(toujours, hits), "")

    def test_aucun_hit_du_pack_aucun_bloc(self):
        hits = {(11, "a.html"): (1, r"<form\b")}         # hit d'un AUTRE pack
        self.assertEqual(A.build_trigger_hits_block(self.PASS, hits), "")

    def test_borne_et_debordement(self):
        many = {"files": [f"f{i}.html" for i in range(A.MAX_TRIGGER_HITS_IN_PROMPT + 5)]}
        big_pass = {"declenche": True, "pack": {"id": 1}, "bucket": many}
        hits = {(1, f): (1, "x") for f in many["files"]}
        block = A.build_trigger_hits_block(big_pass, hits)
        self.assertEqual(block.count("— motif"), A.MAX_TRIGGER_HITS_IN_PROMPT)
        self.assertIn("(+ 5 autre(s) fichier(s) déclencheur(s) non listé(s))", block)


class TestCouleursContrastes(unittest.TestCase):

    def test_hex_court_et_long(self):
        self.assertEqual(A.parse_css_color("#fff"), (255, 255, 255))
        self.assertEqual(A.parse_css_color("#767676"), (0x76, 0x76, 0x76))

    def test_rgb_et_rgba(self):
        self.assertEqual(A.parse_css_color("rgb(0, 0, 0)"), (0, 0, 0))
        self.assertEqual(A.parse_css_color("rgba(0, 0, 0, 1)"), (0, 0, 0))
        self.assertIsNone(A.parse_css_color("rgba(0, 0, 0, 0.5)"))  # semi-transparent

    def test_nommees_et_non_litterales(self):
        self.assertEqual(A.parse_css_color("white"), (255, 255, 255))
        self.assertIsNone(A.parse_css_color("var(--brand)"))
        self.assertIsNone(A.parse_css_color("transparent"))
        self.assertIsNone(A.parse_css_color("inherit"))

    def test_ratio_wcag(self):
        self.assertAlmostEqual(A.contrast_ratio((0, 0, 0), (255, 255, 255)), 21.0, places=1)
        self.assertAlmostEqual(A.contrast_ratio((255, 255, 255), (255, 255, 255)), 1.0, places=1)
        # Le gris #767676 sur blanc passe tout juste AA (4.5:1).
        ratio = A.contrast_ratio((0x76, 0x76, 0x76), (255, 255, 255))
        self.assertGreater(ratio, 4.5)
        self.assertLess(ratio, 4.6)


class TestExtractLocationPaths(unittest.TestCase):

    def test_backticks_et_suffixe_ligne(self):
        self.assertEqual(A.extract_location_paths("`form.html:5`"), ["form.html"])
        self.assertEqual(A.extract_location_paths("`a.css:1-4` et `b.css`"),
                         ["a.css", "b.css"])

    def test_virgules_sans_backticks(self):
        self.assertEqual(A.extract_location_paths("form.html:5, autre.css"),
                         ["form.html", "autre.css"])

    def test_fragment_avec_espaces_ignore(self):
        self.assertEqual(A.extract_location_paths("écran Panier"), [])


class TestTestabilite(SandboxTestCase):
    """A6 (H4) : la testabilité par critère en données, et la règle de fer appliquée
    par l'agrégation (C sur critère « manuelle » → AVM requalifié)."""

    def test_manifeste_reel_valide(self):
        # Le manifeste distribué DOIT porter un bloc testabilite complet par pack.
        prev = os.getcwd()
        os.chdir(ENGINE)
        try:
            packs, fatal = A.load_packs_manifest()
        finally:
            os.chdir(prev)
        self.assertEqual(fatal, [])
        self.assertEqual(len(packs), 13)
        for pack in packs:
            self.assertEqual(set(pack["testabilite"]), set(pack["criteres"]))
        # Les 8 critères manuels du référentiel (cf. rgaa-enhanced §H4).
        manual = sorted(c for p in packs for c, t in p["testabilite"].items()
                        if t == "manuelle")
        self.assertEqual(manual, ["1.7", "10.10", "13.4", "4.2", "4.4", "4.6", "4.9", "5.2"])

    def test_bloc_manquant_fatal(self):
        # Un manifeste SANS bloc testabilite est refusé (pas d'état intermédiaire muet).
        os.makedirs(".agents/pipeline/audit-a11y/packs", exist_ok=True)
        with open(".agents/pipeline/audit-a11y/packs/T01-images.md", "w",
                  encoding="utf-8") as f:
            f.write("# grille factice\n")
        with open(".agents/pipeline/audit-a11y/packs.yaml", "w", encoding="utf-8") as f:
            f.write('packs:\n  - id: 1\n    slug: "images"\n    nom: "Images"\n'
                    '    criteres: ["1.1"]\n    toujours: false\n    declencheurs:\n'
                    '      - "<img"\n')
        _packs, fatal = A.load_packs_manifest()
        self.assertTrue(any("testabilite" in f for f in fatal), fatal)

    def test_aggregate_requalifie_c_sur_manuelle(self):
        pack = {"id": 1, "slug": "images", "nom": "Images",
                "criteres": ["1.1", "1.2"],
                "testabilite": {"1.1": "manuelle", "1.2": "statique"}}
        audit_pass = {"pack": pack, "bucket": {"label": "Z01_test", "name": "Z1 : Test"},
                      "findings_path": "v.md", "declenche": True}
        with open("v.md", "w", encoding="utf-8") as f:
            f.write("# T1\n\n## Verdicts\n- 1.1 : C\n- 1.2 : C\n\n## Constats\n"
                    "Aucun constat.\n\n## Bilan\n"
                    "- Verdicts : C : 2, NC : 0, NA : 0, AVM : 0\n")
        stats = A.aggregate([audit_pass], [pack])
        self.assertEqual(stats["criteria"]["1.1"]["statut"], "AVM")   # requalifié
        self.assertEqual(stats["criteria"]["1.2"]["statut"], "C")     # statique : accepté
        self.assertEqual(stats["requalified_manual_c"], 1)
        self.assertTrue(any("requalifié AVM" in n
                            for n in stats["criteria"]["1.1"]["notes"]))

    def test_aggregate_nc_sur_manuelle_reste_nc(self):
        # NC (constaté, localisé) reste accepté même sur un critère manuel :
        # la violation flagrante est démontrable statiquement.
        pack = {"id": 1, "slug": "images", "nom": "Images",
                "criteres": ["1.1"],
                "testabilite": {"1.1": "manuelle"}}
        audit_pass = {"pack": pack, "bucket": {"label": "Z01_test", "name": "Z1 : Test"},
                      "findings_path": "v.md", "declenche": True}
        with open("a.html", "w", encoding="utf-8") as f:
            f.write("<img>\n")
        with open("v.md", "w", encoding="utf-8") as f:
            f.write("# T1\n\n## Verdicts\n- 1.1 : NC — violation flagrante\n\n## Constats\n"
                    "### K1 — 1.1 — Violation flagrante\n- **Impact :** 3 — Majeur\n"
                    "- **Localisation :** a.html:1\n- **Extrait :** <img>\n- **Constat :** démontrable.\n"
                    "- **Impact utilisateur :** réel.\n- **Correction :** corriger.\n\n"
                    "## Bilan\n- Verdicts : C : 0, NC : 1, NA : 0, AVM : 0\n")
        stats = A.aggregate([audit_pass], [pack])
        self.assertEqual(stats["criteria"]["1.1"]["statut"], "NC")
        self.assertEqual(stats["requalified_manual_c"], 0)


class TestSondes(SandboxTestCase):
    """C-2 (H3) : sondes NC déterministes — scan, validation du manifeste réel,
    confrontation aval « verdict C suspect »."""

    def test_manifeste_reel_sondes_valides(self):
        prev = os.getcwd()
        os.chdir(ENGINE)
        try:
            packs, fatal = A.load_packs_manifest()
        finally:
            os.chdir(prev)
        self.assertEqual(fatal, [])
        total = sum(len(p["sondes"]) for p in packs)
        self.assertEqual(total, 10)
        for p in packs:
            for s in p["sondes"]:
                self.assertIn(s["critere"], p["criteres"])

    def test_scan_sondes(self):
        with open("page.html", "w", encoding="utf-8") as f:
            f.write("<html>\n<head><meta name=\"viewport\" "
                    "content=\"width=device-width, user-scalable=no\"></head>\n</html>\n")
        packs = [{"id": 8, "sondes": [{"regex": re.compile(r"<html(?![^>]{0,200}lang=)[^>]*>", re.I),
                                       "motif": "<html sans lang", "critere": "8.3",
                                       "confiance": "probable"}]},
                 {"id": 10, "sondes": [{"regex": re.compile(r"user-scalable\s*=\s*no", re.I),
                                        "motif": "user-scalable=no", "critere": "10.4",
                                        "confiance": "quasi-certain"}]}]
        hits = A.scan_sondes(["page.html"], packs)
        self.assertEqual(hits[(8, "page.html")][0][0], 1)    # ligne 1
        self.assertEqual(hits[(10, "page.html")][0][2], "10.4")

    def test_scan_sondes_html_avec_lang_muet(self):
        with open("ok.html", "w", encoding="utf-8") as f:
            f.write('<html lang="fr">\n</html>\n')
        packs = [{"id": 8, "sondes": [{"regex": re.compile(r"<html(?![^>]{0,200}lang=)[^>]*>", re.I),
                                       "motif": "<html sans lang", "critere": "8.3",
                                       "confiance": "probable"}]}]
        self.assertEqual(A.scan_sondes(["ok.html"], packs), {})

    def test_confrontation_c_suspect(self):
        with open("page.html", "w", encoding="utf-8") as f:
            f.write("<html>\n</html>\n")
        pack = {"id": 8, "slug": "elements-obligatoires", "nom": "Éléments obligatoires",
                "criteres": ["8.3"], "testabilite": {"8.3": "statique"}, "sondes": []}
        audit_pass = {"pack": pack, "bucket": {"label": "Z01", "name": "Z1",
                                               "files": ["page.html"]},
                      "findings_path": "v.md", "declenche": True}
        with open("v.md", "w", encoding="utf-8") as f:
            f.write("# T8\n\n## Verdicts\n- 8.3 : C\n\n## Constats\nAucun constat.\n\n"
                    "## Bilan\n- Verdicts : C : 1, NC : 0, NA : 0, AVM : 0\n")
        hits = {(8, "page.html"): [(1, "<html sans lang", "8.3", "probable")]}
        suspects = A.suspicious_c_verdicts([audit_pass], hits)
        self.assertEqual(len(suspects), 1)
        self.assertEqual(suspects[0]["critere"], "8.3")
        # verdict NC sur le critère sondé : rien à signaler
        with open("v.md", "w", encoding="utf-8") as f:
            f.write("# T8\n\n## Verdicts\n- 8.3 : NC — pas de lang\n\n## Constats\n"
                    "### K1 — 8.3 — Langue absente\n- **Impact :** 3 — Majeur\n"
                    "- **Localisation :** page.html:1\n- **Extrait :** <html>\n"
                    "- **Constat :** pas d'attribut lang.\n- **Impact utilisateur :** x.\n"
                    "- **Correction :** ajouter lang.\n\n"
                    "## Bilan\n- Verdicts : C : 0, NC : 1, NA : 0, AVM : 0\n")
        self.assertEqual(A.suspicious_c_verdicts([audit_pass], hits), [])


class TestAllCSuspect(SandboxTestCase):
    """C-3 (H5) : l'anti-complaisance symétrique — massivement C, zéro constat,
    zéro AVM sur une passe déclenchée."""

    def test_detecteur(self):
        allc = {"verdicts": {f"1.{i}": {"statut": "C"} for i in range(1, 11)},
                "constats": []}
        self.assertTrue(A.findings_all_c(allc))
        avec_avm = {"verdicts": {"1.1": {"statut": "C"}, "1.2": {"statut": "AVM"}},
                    "constats": []}
        self.assertFalse(A.findings_all_c(avec_avm))
        avec_constat = {"verdicts": {"1.1": {"statut": "C"}},
                        "constats": [{"k": 1}]}
        self.assertFalse(A.findings_all_c(avec_constat))
        ratio_bas = {"verdicts": {"1.1": {"statut": "C"}, "1.2": {"statut": "NA"},
                                  "1.3": {"statut": "C"}, "1.4": {"statut": "NA"}},
                     "constats": []}
        self.assertFalse(A.findings_all_c(ratio_bas))

    def test_passes_suspectes(self):
        pack = {"id": 1, "slug": "images", "nom": "Images", "criteres": ["1.1", "1.2"],
                "testabilite": {"1.1": "statique", "1.2": "statique"}, "sondes": []}
        audit_pass = {"pack": pack, "bucket": {"label": "Z01", "name": "Z1",
                                               "files": ["a.html"]},
                      "findings_path": "v.md", "declenche": True}
        with open("v.md", "w", encoding="utf-8") as f:
            f.write("# T1\n\n## Verdicts\n- 1.1 : C\n- 1.2 : C\n\n## Constats\n"
                    "Aucun constat.\n\n## Bilan\n- Verdicts : C : 2, NC : 0, NA : 0, AVM : 0\n")
        self.assertEqual(len(A.suspicious_all_c_passes([audit_pass])), 1)
        # passe 'toujours' non déclenchée : hors du champ
        audit_pass["declenche"] = False
        self.assertEqual(A.suspicious_all_c_passes([audit_pass]), [])


class TestSplitPasses(unittest.TestCase):
    """C-4 (L7) : le split mécanique des compartiments trop gros — la saturation
    silencieuse de fenêtre était le trou de couverture le plus réel du pipeline."""

    PACK = {"id": 1, "slug": "images", "nom": "Images", "criteres": ["1.1"],
            "toujours": False, "testabilite": {"1.1": "statique"}, "sondes": []}

    def test_compartiment_petit_inchange(self):
        bucket = {"kind": "zone", "slot": "z1", "label": "Z01_a", "name": "Z1 : A",
                  "intent": "", "files": [f"f{i}.html" for i in range(10)]}
        triggers = {f: {1} for f in bucket["files"]}
        passes = A.build_pass_list([bucket], [self.PACK], triggers)
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0]["slot"], "t1-z1")            # AUCUN suffixe :
        self.assertNotIn("-a", passes[0]["findings_path"])       # reprise préservée

    def test_compartiment_scinde(self):
        files = [f"f{i}.html" for i in range(60)]
        bucket = {"kind": "zone", "slot": "z1", "label": "Z01_a", "name": "Z1 : A",
                  "intent": "", "files": files}
        triggers = {f: ({1} if f == "f0.html" else set()) for f in files}
        passes = A.build_pass_list([bucket], [self.PACK], triggers)
        self.assertEqual(len(passes), 2)                         # 40 + 20 (fichiers absents : 0 octet)
        self.assertEqual([p["slot"] for p in passes], ["t1-z1a", "t1-z1b"])
        self.assertEqual(len({p["findings_path"] for p in passes}), 2)
        self.assertEqual([len(p["bucket"]["files"]) for p in passes], [40, 20])
        # 'declenche' PAR TRANCHE : seul f0.html (tranche a) porte le motif
        self.assertEqual([p["declenche"] for p in passes], [True, False])
        self.assertIn("tranche 1/2", passes[0]["label"])
        # La tranche sans motif est NA mécanique : aucun agent sollicité.
        self.assertEqual([A.pass_needs_agent(p) for p in passes], [True, False])
        self.assertEqual(len(A.mechanical_na_passes(passes)), 1)

    def test_socle_toujours_reste_confie_a_l_agent(self):
        pack = dict(self.PACK, toujours=True)
        bucket = {"kind": "socle", "slot": "socle", "label": "SOCLE", "name": "Socle",
                  "intent": "", "files": ["a.html", "b.html"]}
        passes = A.build_pass_list([bucket], [pack], {"a.html": set(), "b.html": set()})
        self.assertEqual(len(passes), 1)
        self.assertFalse(passes[0]["declenche"])
        self.assertTrue(A.pass_needs_agent(passes[0]), "l'absence de motif est un constat potentiel")

    def test_tranches_par_budget_d_octets(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i in range(6):
                path = os.path.join(tmp, f"f{i}.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("x" * (30 * 1024))          # 30 Ko chacun : 2 par tranche de 80 Ko
                files.append(path)
            big = os.path.join(tmp, "big.html")
            with open(big, "w", encoding="utf-8") as f:
                f.write("x" * (200 * 1024))             # plus gros que le budget : seul
            slices = A.slice_bucket_files(files + [big])
            self.assertEqual([len(s) for s in slices], [2, 2, 2, 1])
            self.assertEqual(slices[-1], [big])

    def test_fichier_na_mecanique_passe_le_parseur(self):
        pack = {"id": 1, "slug": "images", "nom": "Images", "criteres": ["1.1", "1.2", "1.3"],
                "toujours": False, "testabilite": {"1.1": "statique", "1.2": "statique", "1.3": "manuel"},
                "sondes": []}
        with tempfile.TemporaryDirectory() as tmp:
            audit_pass = {"pack": pack, "declenche": False,
                          "bucket": {"kind": "zone", "name": "Z1 : A", "files": ["x.html"]},
                          "findings_path": os.path.join(tmp, "pre_audit_a11y", "T01_images__Z01_a-b.md")}
            A.write_mechanical_na_findings(audit_pass)
            self.assertTrue(A.findings_ok(audit_pass["findings_path"], pack))
            data, fatal, _ = A.parse_findings_file(audit_pass["findings_path"], pack)
            self.assertEqual(fatal, [])
            self.assertTrue(A.findings_all_na(data))
            self.assertEqual(A.suspicious_all_na_passes([audit_pass]), [], "non déclenchée : pas suspecte")


class TestPerimetreMecanique(SandboxTestCase):
    """Exclusions mécaniques du périmètre : assets tiers, logique pure sans signal UI."""

    def write(self, rel, content):
        os.makedirs(os.path.dirname(rel) or ".", exist_ok=True)
        with open(rel, "w", encoding="utf-8") as f:
            f.write(content)

    def test_exclusions_et_trace(self):
        self.write("public/dsfr/dsfr.min.css", "a{}")           # .min. : déjà hors is_ui_file
        self.write("public/dsfr/utility/icons.css", ".fr-icon{}")
        self.write("src/styles/overrides.css", ".fr-btn{color:red}")
        self.write("src/api/route.ts", "export async function GET() { return Response.json({table: 1}) }")
        self.write("src/ui/menu.ts", "document.querySelector('nav').setAttribute('aria-label','Menu')")
        self.write("src/app/admin/page.tsx", "export default function Page() { return <AdminList /> }")
        self.write("src/app/page.tsx", "export default () => <main><h1>Accueil</h1></main>")
        scope = A.discover_ui_scope()
        self.assertEqual(scope, ["src/app/admin/page.tsx", "src/app/page.tsx",
                                 "src/styles/overrides.css", "src/ui/menu.ts"])
        self.assertEqual(A.SCOPE_EXCLUSIONS["vendor"], ["public/dsfr/utility/icons.css"])
        self.assertEqual(A.SCOPE_EXCLUSIONS["logic"], ["src/api/route.ts"])

    def test_tsx_sans_signal_reste_dans_le_perimetre(self):
        self.write("src/x.tsx", "export const X = 1")
        self.write("src/y.js", "module.exports = 1")
        self.assertEqual(A.discover_ui_scope(), ["src/x.tsx"])
        self.assertEqual(A.SCOPE_EXCLUSIONS["logic"], ["src/y.js"])


class TestHardReset(unittest.TestCase):
    """C-5 (L5) : le reset dur entre passes (kill + start), débrayable."""

    def tearDown(self):
        os.environ.pop("MM_A11Y_HARD_RESET", None)
        A.RUNNER.kill()

    def test_reset_dur_par_defaut(self):
        A.RUNNER.kill()
        self.assertFalse(A.RUNNER.is_running())
        A.reset_agent_session()
        self.assertTrue(A.RUNNER.is_running())    # kill + start : session relancée

    def test_opt_out_new_context(self):
        os.environ["MM_A11Y_HARD_RESET"] = "0"
        A.RUNNER.kill()
        A.reset_agent_session()
        self.assertFalse(A.RUNNER.is_running())   # simple /new : pas de redémarrage


class TestInvalidatedPasses(unittest.TestCase):
    """C-6 (L8, git uniquement) : sélection des passes à rejouer d'après le diff."""

    def test_selection(self):
        p1 = {"label": "T01 × Z1", "bucket": {"files": ["a.html", "b.css"]}}
        p2 = {"label": "T11 × Z2", "bucket": {"files": ["c.html"]}}
        out = A.invalidated_passes([p1, p2], ["b.css", "autre.js"])
        self.assertEqual(out, [p1])
        self.assertEqual(A.invalidated_passes([p1, p2], []), [])
        # normalisation des chemins (./b.css == b.css)
        self.assertEqual(A.invalidated_passes([p1, p2], ["./b.css"]), [p1])


class TestToujoursJamaisExecute(SandboxTestCase):
    """C-7 (H10 a minima) : un pack 'toujours' jamais exécuté sort en AVM motivé,
    pas en « NA : contenu absent » trompeur — l'absence des motifs est précisément
    le défaut potentiel (lang, <title>, lien d'évitement)."""

    def test_requalification(self):
        toujours = {"id": 8, "slug": "elements-obligatoires", "nom": "Éléments",
                    "criteres": ["8.3"], "toujours": True,
                    "testabilite": {"8.3": "statique"}, "sondes": []}
        declenche = {"id": 1, "slug": "images", "nom": "Images",
                     "criteres": ["1.1"], "toujours": False,
                     "testabilite": {"1.1": "statique"}, "sondes": []}
        stats = A.aggregate([], [toujours, declenche])   # aucune passe exécutée
        self.assertEqual(stats["criteria"]["8.3"]["statut"], "AVM")
        self.assertTrue(any("jamais exécuté" in n for n in stats["criteria"]["8.3"]["notes"]))
        self.assertEqual(stats["criteria"]["1.1"]["statut"], "NA")


class TestFenceState(unittest.TestCase):

    def test_bascule(self):
        lines = list(A.iter_lines_with_fence_state("a\n```\nb\n```\nc"))
        self.assertEqual([f for _l, f in lines], [False, True, True, True, False])


class TestWaitForDeliverable(SandboxTestCase):
    """A2 (L2+C2) : le contrat (ok, raison) de l'attente de livrable — testé avec des
    constantes de poll réduites injectées (le timing réel est inobservable en mock)."""

    def setUp(self):
        super().setUp()
        self._saved = (A.POLL_INTERVAL, A.STABLE_POLLS_FALLBACK)
        A.POLL_INTERVAL = 0.01
        A.STABLE_POLLS_FALLBACK = 2

    def tearDown(self):
        A.POLL_INTERVAL, A.STABLE_POLLS_FALLBACK = self._saved
        super().tearDown()

    @staticmethod
    def _touch(name, content="contenu\n"):
        with open(name, "w", encoding="utf-8") as f:
            f.write(content)

    def test_ok_livrable_et_sentinelle(self):
        self._touch("livrable.md")
        self._touch(".s.done", "done\n")
        ok, reason = A.wait_for_deliverable("livrable.md", ".s.done", timeout=1)
        self.assertEqual((ok, reason), (True, "ok"))
        self.assertFalse(os.path.exists(".s.done"))   # sentinelle consommée

    def test_timeout(self):
        ok, reason = A.wait_for_deliverable("jamais.md", ".s.done", timeout=0.05)
        self.assertEqual((ok, reason), (False, "timeout"))

    def test_sentinelle_sans_livrable(self):
        self._touch(".s.done", "done\n")
        ok, reason = A.wait_for_deliverable("jamais.md", ".s.done", timeout=5)
        self.assertEqual((ok, reason), (False, "sentinelle_sans_livrable"))
        self.assertFalse(os.path.exists(".s.done"))   # fautive : consommée aussi

    def test_stable_hors_format_second_palier(self):
        self._touch("livrable.md", "hors format\n")
        ok, reason = A.wait_for_deliverable("livrable.md", ".s.done", timeout=5,
                                            structural_check=lambda p: False)
        self.assertEqual((ok, reason), (False, "stable_hors_format"))

    def test_filet_sans_sentinelle_livrable_stable_conforme(self):
        self._touch("livrable.md")
        ok, reason = A.wait_for_deliverable("livrable.md", ".s.done", timeout=5,
                                            structural_check=lambda p: True)
        self.assertEqual((ok, reason), (True, "ok"))


class TestComposeRetryFeedback(unittest.TestCase):
    """A4 (L4) : le feedback cumulatif — la dernière erreur en détail, les erreurs
    antérieures DISTINCTES en résumé borné."""

    def test_premier_passage(self):
        self.assertEqual(A.compose_retry_feedback([]),
                         "Premier passage — aucun retour précédent.")

    def test_une_seule_erreur_sans_rappel(self):
        out = A.compose_retry_feedback(["critère 11.9 manquant"])
        self.assertEqual(out, "critère 11.9 manquant")

    def test_les_erreurs_anterieures_sont_rappelees(self):
        out = A.compose_retry_feedback(["critère 11.9 manquant", "Bilan incohérent"])
        self.assertTrue(out.startswith("Bilan incohérent"))
        self.assertIn("à NE PAS", out)
        self.assertIn("critère 11.9 manquant", out)

    def test_dedup_et_borne(self):
        history = ["même erreur"] * 3 + ["autre erreur", "dernière"]
        out = A.compose_retry_feedback(history)
        self.assertEqual(out.count("même erreur"), 1)          # dédupliquée
        n_errors = 12
        history = [f"erreur {i:02d}" for i in range(n_errors)] + ["dernière"]
        out = A.compose_retry_feedback(history)
        kept = sum(1 for i in range(n_errors) if f"erreur {i:02d}" in out)
        self.assertEqual(kept, A.MAX_PREVIOUS_ERRORS_IN_FEEDBACK)
        self.assertIn(f"erreur {n_errors - 1:02d}", out)       # les plus récentes gardées

    def test_troncature_des_rappels(self):
        long = "x" * 1000
        out = A.compose_retry_feedback([long, "dernière"])
        self.assertNotIn("x" * (A.MAX_PREVIOUS_ERROR_CHARS + 1), out)
        self.assertIn("x" * 50, out)


class TestPassFailureBreaker(unittest.TestCase):
    """A1 (L1) : le circuit breaker des passes — 2 consécutifs ou > 30 % des passes
    traitées, mais JAMAIS sur un échec isolé."""

    def test_echec_isole_jamais(self):
        self.assertFalse(A.pass_failure_breaker(1, 1, 1))    # 100 % mais isolé
        self.assertFalse(A.pass_failure_breaker(1, 1, 3))

    def test_deux_consecutifs(self):
        self.assertTrue(A.pass_failure_breaker(2, 2, 40))

    def test_ratio_arme_a_partir_de_deux_echecs(self):
        self.assertTrue(A.pass_failure_breaker(1, 2, 5))     # 40 % > 30 %
        self.assertFalse(A.pass_failure_breaker(1, 2, 10))   # 20 % → on continue
        self.assertTrue(A.pass_failure_breaker(1, 4, 10))    # 40 % > 30 %
        self.assertFalse(A.pass_failure_breaker(1, 3, 10))   # 30 % : non strict

    def test_nominal(self):
        self.assertFalse(A.pass_failure_breaker(0, 0, 12))


class TestSlugify(unittest.TestCase):

    def test_accents_et_kebab(self):
        self.assertEqual(A.slugify("Formulaire"), "formulaire")
        self.assertEqual(A.slugify("Écran Panier / Détail"), "ecran-panier-detail")
        self.assertEqual(A.slugify("***"), "zone")



class TestCarteDiversEtRepertoires(unittest.TestCase):
    """Validateur de carte : « Divers » facultative (le prompt demandait de ne pas y
    recopier le surplus, le validateur la rejetait vide) et entrées RÉPERTOIRE."""

    SCOPE = ["src/pages/home.tsx", "src/pages/checkout/cart.tsx", "src/pages/checkout/pay.tsx",
             "src/components/Button.tsx", "src/layout/Header.tsx"]

    def base_map(self, zones):
        return {"project": "p", "socle": {"intent": "s", "files": ["src/layout/Header.tsx"]},
                "composants": {"intent": "c", "files": ["src/components/Button.tsx"]},
                "zones": zones}

    def test_divers_vide_est_completee(self):
        a11y_map = self.base_map([
            {"id": 1, "name": "Accueil", "intent": "i", "files": ["src/pages/home.tsx"]},
            {"id": 2, "name": "Divers", "intent": "r", "files": []},
        ])
        fatal, soft = A.validate_and_normalize_a11y_map(a11y_map, self.SCOPE)
        self.assertEqual(fatal, [])
        self.assertEqual(sorted(a11y_map["zones"][1]["files"]),
                         ["src/pages/checkout/cart.tsx", "src/pages/checkout/pay.tsx"])
        self.assertTrue(any("déclarée vide" in s for s in soft))

    def test_divers_vide_sans_reste_est_retiree(self):
        a11y_map = self.base_map([
            {"id": 1, "name": "Tout", "intent": "i",
             "files": ["src/pages/home.tsx", "src/pages/checkout/cart.tsx", "src/pages/checkout/pay.tsx"]},
            {"id": 2, "name": "Divers", "intent": "r", "files": []},
        ])
        fatal, _ = A.validate_and_normalize_a11y_map(a11y_map, self.SCOPE)
        self.assertEqual(fatal, [])
        self.assertEqual([z["name"] for z in a11y_map["zones"]], ["Tout"])

    def test_zone_vide_non_divers_reste_fatale(self):
        a11y_map = self.base_map([
            {"id": 1, "name": "Accueil", "intent": "i", "files": ["src/pages/home.tsx"]},
            {"id": 2, "name": "Paiement", "intent": "i", "files": []},
        ])
        fatal, _ = A.validate_and_normalize_a11y_map(a11y_map, self.SCOPE)
        self.assertTrue(any("Paiement" in f for f in fatal))

    def test_entree_repertoire(self):
        a11y_map = self.base_map([
            {"id": 1, "name": "Paiement", "intent": "i", "files": ["src/pages/checkout/"]},
            {"id": 2, "name": "Accueil", "intent": "i", "files": ["src/pages/"]},
        ])
        fatal, _ = A.validate_and_normalize_a11y_map(a11y_map, self.SCOPE)
        self.assertEqual(fatal, [])
        self.assertEqual(sorted(a11y_map["zones"][0]["files"]),
                         ["src/pages/checkout/cart.tsx", "src/pages/checkout/pay.tsx"])
        self.assertEqual(a11y_map["zones"][1]["files"], ["src/pages/home.tsx"],
                         "le répertoire parent ne reprend pas ce qui est déjà assigné")
        self.assertEqual(A.divers_files(a11y_map), [])

    def test_echantillon_carto_privilegie_le_code(self):
        public = [f"public/dsfr/icons/i-{i:04d}.css" for i in range(700)]
        src = [f"src/pages/p{i % 15}/f-{i:04d}.tsx" for i in range(300)]
        block = A.build_carto_scope_block(sorted(public + src))
        self.assertEqual(block.count("- src/"), 300)
        self.assertIn("PAR RÉPERTOIRE", block)
        self.assertIn("public/dsfr/icons/ : 600", block, "400 = 300 src + 100 public")


if __name__ == "__main__":
    unittest.main(verbosity=2)
