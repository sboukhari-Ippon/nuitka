# MAIsterMind — App + Usine (dépôt unifié V3.0)
*By Selim Boukhari* — [LinkedIn](https://www.linkedin.com/in/selim-boukhari-6356b949/)

MAIsterMind est une usine à code automatisée qui pilote un agent IA en ligne de commande — **OpenCode ou Codex CLI, au choix, projet par projet** — à travers des pipelines structurés et validés par l'humain. Ce dépôt unifie **les 13 orchestrateurs** (les scripts de l'usine, ex-dépôt privé) et **l'app cockpit** (ex-App_v3) : l'app est le point d'entrée grand public — elle découvre les binaires du moteur (`engine/`), équipe les projets, lance les runs dans tmux, affiche leur écran et répond aux portes depuis le navigateur.

> **Requiert UN harness d'agent** : [OpenCode](https://opencode.ai) ou [Codex CLI](https://github.com/openai/codex).
> Un seul suffit ; les deux peuvent cohabiter. Voir l'`INSTALL.md` de chaque variante.

> 📄 **Licence** : usage non commercial, personnel ou éducatif uniquement — voir [LICENSE](LICENSE).
> Tout usage commercial requiert un accord écrit préalable de l'auteur.

### Le harness est un choix, pas un fork

Historiquement, changer d'agent IA voulait dire forker le produit : deux dépôts complets, chaque
correctif appliqué douze fois (6 variantes × 2 forks). Désormais, le harness est une
**abstraction** — `engine/mm_runner.py`, une classe par agent — et le choix se fait **au runtime** :

| Où | Comment |
|---|---|
| Dans l'app | bouton « Équiper » par harness : les artefacts du harness choisi sont copiés dans le projet et le harness est inscrit dans son `.mm-equip.json` |
| En terminal | `MM_AGENT_HARNESS=opencode\|codex python3 <orchestrateur>.py` |
| Sans rien préciser | déduit des artefacts présents dans le projet, puis du seul binaire installé ; si rien ne tranche, arrêt propre avec un message actionnable |

Les binaires embarquent les DEUX implémentations : aucune matrice de build supplémentaire, et
un même projet bascule d'un agent à l'autre sans rien changer d'autre. Ajouter un 3ᵉ harness =
écrire une classe dans `mm_runner.py` et une entrée dans son registre.

## Les deux promesses de distribution

1. **Zéro chmod, jamais.** `install.sh` pose les droits une première fois (et lève la quarantaine Gatekeeper sur macOS) ; ensuite **l'app remet elle-même son moteur en état** à chaque démarrage et avant chaque lancement (`heal_engine_binaries` : bit exécutable + quarantaine). Un zip qui écrase les permissions, une copie via l'explorateur, une clé USB : l'app répare, l'utilisateur ne voit rien.
2. **Double-clic pour ouvrir l'app.** Chaque plateforme a son lanceur natif, livré dans l'archive : bundle `MAIsterMind.app` (macOS, Finder), `MAIsterMind.bat` (Windows → WSL), entrée de menu d'applications (Ubuntu, posée par `install.sh`). Sans terminal, l'app journalise dans `.mm-app/launcher.log` et s'éteint par le bouton ⏻ de « Statut & réglages » (`POST /api/quit`). Les runs vivent dans tmux : éteindre l'app n'en tue aucun.

## Choisir sa variante

| Langue | Plateforme | Dossier source | Archives de release |
|---|---|---|---|
| Français | Ubuntu / Debian | `FR/Ubuntu` | `MAIsterMind-fr-linux-x64.tar.gz` |
| Français | Windows (WSL 2) | `FR/Windows` | `MAIsterMind-fr-linux-x64-wsl.tar.gz` |
| Français | macOS | `FR/MacOS` | `MAIsterMind-fr-macos-arm64.tar.gz` · `-x64` |
| English | Ubuntu / Debian | `ENG/Ubuntu` | `MAIsterMind-eng-linux-x64.tar.gz` |
| English | Windows (WSL 2) | `ENG/Windows` | `MAIsterMind-eng-linux-x64-wsl.tar.gz` |
| English | macOS | `ENG/MacOS` | `MAIsterMind-eng-macos-arm64.tar.gz` · `-x64` |

> Les builds Windows sont des binaires Linux : ils tournent dans WSL 2 (Ubuntu), pas sous Windows natif (tmux n'y existe pas, et un onefile Nuitka `.exe` est régulièrement classé trojan par les antivirus). Les binaires macOS ne sont pas signés : `install.sh` lève la quarantaine, sinon un unique passage par Réglages > « Ouvrir quand même » suffit.

## Anatomie d'une variante

```
MAIsterMind/                     ← l'archive extraite (ou un dossier source LANG/OS)
├── MAIsterMind_App(.py)         ← l'app cockpit (binaire en release, source en dev)
├── install.sh                   ← LA commande d'installation (unique geste technique)
├── MAIsterMind.bat / .app       ← lanceur double-clic (selon plateforme)
├── INSTALL.md · README.md · useCases*.md · LICENSE
└── engine/                      ← le moteur : ce que l'app découvre et lance
    ├── orchestrators.json       ← manifeste des orchestrateurs et de leurs portes
    ├── Coding, Spec… (× 13)      ← binaires en release, sources .py en dev
    ├── need.md                  ← gabarit (mode expert)
    ├── mm_runner.py             ← l'abstraction du harness (module, jamais un binaire)
    ├── mm_core.py               ← fonctions partagées des orchestrateurs (module)
    ├── mm_audit.py              ← journal de run `.mm-runs/` (module, stdlib pure)
    ├── .agents/                 ← skills copiés dans les projets équipés (communs)
    ├── .opencode/               ← artefacts d'équipement OpenCode
    ├── .codex/ + AGENTS.md      ← artefacts d'équipement Codex
    └── ...
```

L'app trouve ses moteurs par la présence du manifeste (à côté d'elle, ou dans ses sous-dossiers immédiats). En dev, le binaire absent retombe sur la source `.py` du même nom : le dépôt s'utilise tel quel, sans compilation.

## Les 13 orchestrateurs

| Binaire | Famille | Portes (validation humaine) | need.md |
|---|---|---|---|
| `Coding-Without-Tests` | production (sans tests, vérif LLM) | spec → blackboard | requis |
| `Design-Prototype` (bêta) | production (prototypes HTML) | design system → spec → blackboard | requis |
| `Coding` | production (verdict universel ; revue d'impact, vérificateur LLM, triage) | spec → impact → blackboard (+ arbitrage mid-run) | requis |
| `Test-First` | production (test-first par cycles red/green/refactor, inspiré du TDD ; surcouche sur les phases green) | spec → impact → blackboard (+ arbitrage mid-run) | requis |
| `Acceptance-First` | production (acceptance-first par lots de user story, inspiré de l'ATDD ; surcouche sur les clôtures de lot) | spec → impact → blackboard (+ arbitrage mid-run) | requis |
| `Challenge-Need` | cadrage (opt-in, aucun couplage aval) | revue du besoin (unique) | requis |
| `Spec` | cadrage | spec (unique) | requis |
| `Technical-Plan` | cadrage | spec (puis plan + blackboard sans production) | requis |
| `Documentation` (bêta) | lecture seule | périmètre → carte des zones | optionnel |
| `Audit-Design` (bêta) | lecture seule | périmètre (unique) | optionnel |
| `Pre-Audit-A11Y-RGAA` (bêta) | lecture seule | périmètre → carte d'interface | optionnel |
| `Skills-Adaptation` | outillage | questionnaire guidé → écrasement validé | non |
| `Guided-Fix` | maintenance | triage régression/évolution + réparation validée | non |

Les 13 sont déclarés dans `engine/orchestrators.json` et pilotables depuis l'app (portes y/n, à choix et à saisie libre). Chaque run laisse un journal local `.mm-runs/<id>/` (chronologie `events.jsonl`, artefacts figés aux transitions, `summary.md` — rétention 20 runs, opt-out `MM_AUDIT=0`). Le détail de chaque pipeline : `SCHEMAS.html` (un onglet par script), `README.md` et `useCases*.md` de la variante.

## Développer et publier

- **Synchronisation des 6 variantes** : `python3 tools/check_variants_sync.py` — scripts identiques octet par octet par langue (`mm_runner.py` compris), AST FR = AST ENG modulo chaînes, couche app (app, manifeste, install.sh) identique sur les 6, artefacts des DEUX harness présents partout, lanceurs identiques par OS. La CI refuse de compiler des variantes désynchronisées.
- **Contrôles du harness** (tous sans tmux, sans LLM, sans réseau) :
  - `python3 tools/check_runner_parity.py` — rejoue la couche harness de l'ancien fork et celle du dépôt migré avec `subprocess`/`time` bouchonnés, et exige la MÊME sortie console et la MÊME séquence de commandes tmux, à l'octet. Attend les deux forks d'origine à côté du dépôt (`--base`, `--ref`) ; absents → SKIP explicite.
  - `python3 tools/check_message_parity.py` — normalise les deux côtés avec la même table de valeurs et vérifie qu'aucune chaîne visible n'a changé de VALEUR (c'est lui qui a attrapé un `./` parasite dans un message).
  - `python3 tools/check_gate_labels.py` — les prompts y/n et `orchestrators.json` se correspondent dans les DEUX sens : porte déclarée sans prompt, et prompt y/n sans porte déclarée (celui que l'app ne saurait pas voir).
  - `python3 tools/check_unused_imports.py` — imports morts ET noms inconnus (le second ferme le trou de `py_compile`, qui compile sans broncher un module dont un import manque).
  - `python3 tools/run_mock_scenarios.py` — déroule les pipelines ENTIERS avec un harness de test (`tools/mm_mock_runner.py`, jamais distribué) sur 32 scénarios (nominal, échecs à 3 tentatives, reprises, cycles TDD/ATDD, triages d'impact, audits, adaptation de skills…). Quelques minutes, zéro token — le `verify_cmd` de chaque scénario lance de VRAIES commandes sur les fichiers écrits : le verdict reste l'exécution. `--golden record|check` compare le stdout normalisé aux transcripts versionnés (`tools/goldens/`) : la caractérisation fait loi.
  - `python3 tools/test_audit_units.py` — tests unitaires (stdlib) des fonctions pures de l'audit RGAA et du journal de run : parseur de verdicts, extraits de matérialité, sondes, breaker, split de compartiments…
  - `python3 engine/mm_runner.py` — diagnostic : harness retenu pour ce dossier, origine de la décision, préflight des deux (binaire, authentification, modèle).
- **CI** : `.github/workflows/checks.yml` rejoue tout ça (5 checkers, tests unitaires, 32 scénarios sous `--golden check`) sur chaque push/PR ; `build.yml` l'exécute aussi avant de compiler.
- **Release** : `git tag v3.0.0 && git push --tags` — `.github/workflows/build.yml` compile (Nuitka onefile, versions épinglées) l'app une fois par job + les 13 orchestrateurs par langue (les modules `mm_runner`/`mm_core`/`mm_audit` sont EMBARQUÉS dans chaque binaire, jamais livrés en `.py`), assemble les archives complètes (lanceurs, skills et LICENSE compris, bits exécutables posés) et les publie en artifacts `tar.gz` (jamais de zip : il écrase les permissions). Attacher les tar.gz à une Release GitHub pour la distribution pérenne.
- **Workflow de correctif** : éditer la variante `Ubuntu` d'une langue, recopier tel quel vers `MacOS`/`Windows`, porter la traduction dans l'autre langue, `check_variants_sync.py` jusqu'au vert.

> ⚠️ Ne modifie pas les skills d'orchestration (`.agents/pipeline/`, `refacto`) : c'est le moteur du pipeline. Et ne laisse jamais le code produit devenir une boîte noire — relis-le.
