#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mm_mock_runner — un harness de TEST : ni tmux, ni LLM, ni réseau
────────────────────────────────────────────────────────────────
Ce module n'est PAS distribué : il vit dans tools/, et `engine/mm_runner.py` ne le
charge que si `MM_AGENT_HARNESS=mock` (import par chaîne, cf. _load_mock_runner).
En production le nom « mock » est refusé comme n'importe quel harness inconnu.

Ce qu'il permet : dérouler un pipeline ENTIER de bout en bout, portes comprises,
en quelques secondes et sans dépenser un token. Là où check_runner_parity.py
prouve que la couche extraite est neutre, le mock prouve que le PIPELINE tourne
encore : sentinelles trouvées, reprise par fichiers, feedback retransmis, verdict
par exécution réelle, statuts écrits dans le blackboard.

Ce qu'il ne fait PAS : juger. Le `verify_cmd` des scénarios lance de VRAIES
commandes sur les fichiers que le mock a écrits — le verdict reste l'exécution.

Un scénario (JSON) est une SÉQUENCE d'étapes, une par sollicitation d'agent :

    {"steps": [
       {"expect": ["Product Owner", "spec.md"],      # attendu dans les consignes
        "write": {"spec.md": "…"},                   # fichiers écrits dans le projet
        "sentinel": ".pipeline_spec.done"},          # optionnel : sentinelle attendue
       …
    ]}

À chaque `send_task`, le mock :
  1. prend l'étape suivante (l'ORDRE des sollicitations est donc vérifié) ;
  2. exige que chaque `expect` figure dans les CONSIGNES — prompt plus contenu des
     fichiers de routage qu'il cite ('.opencode_task.md'…), car c'est là que vivent
     les consignes longues ; c'est ainsi qu'on détecte un prompt qui aurait perdu son
     contexte ou sa consigne de sentinelle ;
  3. écrit les fichiers de l'étape ;
  4. écrit la sentinelle NOMMÉE DANS LES CONSIGNES (elle l'est toujours : c'est le
     contrat existant), et vérifie qu'elle correspond à celle attendue.

Tout est journalisé dans '.mm-mock-journal.jsonl' à la racine du projet : c'est
sur ce journal que les scénarios assertent (nombre de sollicitations, ordre,
sentinelles), en plus de l'état final des fichiers.
"""

import json
import os
import re

# Le mock hérite de l'interface réelle : si `AgentRunner` gagne une méthode que le
# pipeline appelle, le mock en hérite au lieu de planter — et s'il s'agit d'une
# méthode tmux, elle est neutralisée ci-dessous.
from mm_runner import AgentRunner

# Journal des sollicitations. Il vit HORS du projet quand MM_MOCK_JOURNAL le dit —
# et le lanceur de scénarios le dit toujours. Raison : les gardes git des pipelines
# (phase 'tests', cycle TDD red) comparent le diff du projet et compteraient un
# journal qui grossit pendant la phase comme « code de production modifié ». Un
# outil de test qui fait échouer le test qu'il mesure ne mesure plus rien.
JOURNAL = os.environ.get("MM_MOCK_JOURNAL") or ".mm-mock-journal.jsonl"

# Toute sentinelle nommée dans les consignes : '.pipeline_spec.done',
# '.phase_2.attempt1.done', et les sentinelles de VERDICT des agents vérificateurs
# ('.phase_2.attempt1.verdict', '.pipeline_review.attempt1.verdict' — variantes code
# et proto). Depuis les orchestrateurs Yolo, '.triage' rejoint la liste : c'est le
# canal de l'Agent de Triage (une ligne PREVU:/IMPREVU: par fichier de test en échec).
# Le contrat est stable depuis la V2 : la sentinelle est citée entre apostrophes simples.
SENTINEL_RE = re.compile(r"'(\.[A-Za-z0-9_.\-]*\.(?:done|verdict|triage))'")

# Fichier de routage de contexte cité dans un prompt court ('.opencode_task.md',
# '.codex_refacto.md'…). Le pipeline déporte les consignes longues dans ces fichiers
# et n'envoie au TUI qu'un « lis ce fichier » : les consignes RÉELLES — sentinelle
# comprise — y sont. Le mock doit donc les lire, exactement comme l'agent.
ROUTING_RE = re.compile(r"'(\.(?:opencode|codex)_[A-Za-z0-9_.\-]*\.md)'")


class MockScenarioError(AssertionError):
    """Écart entre ce que le scénario attend et ce que l'orchestrateur a demandé."""


class MockRunner(AgentRunner):
    """Harness de test. Aucune commande externe n'est lancée, jamais."""

    name           = "mock"
    label          = "Mock"
    tui_name       = "mock"
    binary         = "true"
    launch_cmd     = ":"
    session_prefix = "mk-"
    buffer_prefix  = "mk"
    # Les fichiers de routage gardent des noms de harness réel : les .gitignore et
    # les gardes git des scripts les testent par préfixe, on veut le même chemin
    # de code qu'en production.
    tmp_prefix     = "opencode"
    equip_dir      = ".opencode"
    config_file    = "./.mock/config.json"
    global_configs = ()
    install_hint   = "(mock)"
    auth_cmd       = ("true",)
    auth_hint      = "(mock)"
    boot_wait      = 0
    new_session_wait = 0

    def __init__(self, project_dir, role, **kwargs):
        super().__init__(project_dir, role, **kwargs)
        self.scenario_path = os.environ.get("MM_MOCK_SCENARIO")
        if not self.scenario_path:
            raise MockScenarioError("MM_MOCK_SCENARIO n'est pas posé : le runner de "
                                    "test a besoin de son scénario.")
        with open(self.scenario_path, "r", encoding="utf-8") as f:
            self.scenario = json.load(f)
        self.steps = list(self.scenario.get("steps", []))
        self.cursor = 0
        self._running = False
        self._log("init", role=role, steps=len(self.steps))

    # ─── Journal ──────────────────────────────────────────────────────────────

    def _log(self, event, **fields):
        record = dict(event=event, **fields)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ─── Couche « tmux » neutralisée ──────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self):
        self._running = True
        self._log("start", session=self.session)
        print(f"🧪 [mock] harness de test démarré (session simulée '{self.session}').")

    def new_context(self):
        self._log("new_context")

    def capture(self) -> str:
        # Jamais de '/new' littéral : la vérification warn-only du reset doit rester
        # silencieuse, comme sur un vrai reset réussi.
        return "[mock] écran simulé\n"

    def kill(self):
        self._running = False
        self._log("kill")

    def configured_model(self) -> str:
        return "mock-model"

    def preflight(self) -> list:
        return [{"ok": True, "label": "mock", "detail": "runner de test", "fix_hint": ""}]

    # ─── Le cœur : une sollicitation = une étape du scénario ───────────────────

    def _consignes(self, prompt: str) -> str:
        """Ce que l'agent LIT vraiment : le prompt, plus le contenu des fichiers de
        routage qu'il cite. Sans cela, un prompt « lis '.opencode_task.md' » paraîtrait
        vide de consignes — et c'est précisément le cas du scaffold et du refacto."""
        parts = [prompt]
        for name in ROUTING_RE.findall(prompt):
            path = os.path.join(self.project_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    parts.append(f.read())
            except OSError:
                parts.append(f"[mock] fichier de routage introuvable : {name}")
        return "\n".join(parts)

    def send_task(self, prompt: str):
        # Le tampon de prompt est écrit comme en production : les scénarios peuvent
        # vérifier qu'il est bien nettoyé en fin de run.
        with open(self.prompt_buffer, "w", encoding="utf-8") as f:
            f.write(prompt)
        consignes = self._consignes(prompt)

        if self.cursor >= len(self.steps):
            self._log("unexpected_task", index=self.cursor, prompt=prompt[:400])
            raise MockScenarioError(
                f"Sollicitation n°{self.cursor + 1} inattendue : le scénario "
                f"'{os.path.basename(self.scenario_path)}' n'en déclare que "
                f"{len(self.steps)}.\nDébut du prompt : {prompt[:300]}")
        step = self.steps[self.cursor]
        self.cursor += 1
        label = step.get("label") or f"étape {self.cursor}"

        for needle in step.get("expect", []):
            if needle not in consignes:
                self._log("expect_failed", index=self.cursor, needle=needle)
                raise MockScenarioError(
                    f"[{label}] les consignes ne contiennent pas {needle!r}.\n"
                    f"Reçu (800 premiers caractères) :\n{consignes[:800]}")
        for needle in step.get("forbid", []):
            if needle in consignes:
                self._log("forbid_failed", index=self.cursor, needle=needle)
                raise MockScenarioError(f"[{label}] les consignes contiennent {needle!r}, "
                                        f"ce que le scénario interdit.")

        for name, content in (step.get("write") or {}).items():
            path = os.path.join(self.project_dir, name)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        for name in step.get("remove") or []:
            try:
                os.remove(os.path.join(self.project_dir, name))
            except OSError:
                pass

        # La sentinelle est LUE DANS LES CONSIGNES : c'est le contrat, et le vérifier
        # ici attrape des consignes qui auraient cessé de la nommer.
        found = SENTINEL_RE.findall(consignes)
        expected = step.get("sentinel")
        if expected and expected not in found:
            self._log("sentinel_missing", index=self.cursor, expected=expected, found=found)
            raise MockScenarioError(
                f"[{label}] les consignes ne nomment pas la sentinelle attendue "
                f"{expected!r} (trouvé : {found or 'aucune'}).")
        sentinel = expected or (found[-1] if found else None)
        if sentinel and not step.get("skip_sentinel"):
            with open(os.path.join(self.project_dir, sentinel), "w", encoding="utf-8") as f:
                # Les sentinelles de phase portent la liste des fichiers touchés ; les
                # sentinelles de pipeline, le seul mot « done ».
                touched = step.get("touched")
                f.write("\n".join(touched) + "\n" if touched else "done\n")

        self._log("task", index=self.cursor, label=label, sentinel=sentinel,
                  wrote=sorted((step.get("write") or {}).keys()))
        print(f"🧪 [mock] {label} → {sentinel or '(aucune sentinelle)'}")
