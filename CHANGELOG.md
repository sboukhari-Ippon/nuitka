# Changelog

## 3.0.0 — MAIsterMind, l'usine à code avec son app

*Première version publique unifiée : l'usine à code (12 orchestrateurs) et son cockpit
navigateur, `MAIsterMind_App`, dans un seul produit, distribué en archives prêtes à l'emploi
sur 6 variantes (FR/ENG × Ubuntu / macOS / Windows-WSL).*

### L'app : MAIsterMind_App

- **Cockpit navigateur.** L'app découvre les moteurs (`engine/` et son manifeste
  `orchestrators.json`), équipe les projets, lance les runs dans tmux, affiche leur écran en
  direct et répond aux portes de validation (y/n, choix multiples, saisie libre) depuis le
  navigateur. Le terminal reste disponible pour tout, en mode expert.
- **Bibliothèque en trois étapes.** 1 · le projet (chemin collé ou parcouru, équipement en un
  clic) ; 2 · le besoin, rédigé directement dans l'app et enregistré dans `need.md` ; 3 · le
  script. L'étape 3 affiche d'abord l'adaptation des skills, puis trois catégories à déplier :
  **Coding** (du besoin au code vérifié, planification incluse), **Design** (prototype et audits
  d'interface), **Produit** (besoin, spec, documentation). Un clic déplie les orchestrateurs de la
  catégorie, le choix est mémorisé. Badges ⭐ recommandé et 🧪 bêta portés par le manifeste.
- **Fil d'Ariane du run.** Chaque orchestrateur déclare ses étapes dans le manifeste (libellés
  fr/eng, preuve durable sur le disque, portes rattachées) ; l'app rend la timeline depuis cette
  déclaration : étape courante, porte ouverte, run terminé. Les usines de code suivent le modèle à
  cinq étapes Spécification → Plan → Blackboard → Production → Refactoring.
- **Portes riches.** Aperçu du fichier relu (spec, plan, blackboard, rapports, cartes) rendu dans
  le thème, rechargé s'il change ; édition intégrée avant de répondre ; portes à choix (triage
  r/e/o, questionnaires) et à saisie libre pilotées par le manifeste.
- **Double-clic au quotidien.** Lanceur natif par plateforme : bundle `MAIsterMind.app`
  (macOS / Finder), `MAIsterMind.bat` polyglotte (Windows → WSL), entrée de menu d'applications
  (Ubuntu). Sans terminal, l'app journalise dans `.mm-app/launcher.log` et s'éteint par le bouton ⏻
  de « Statut & réglages » ; les runs vivent dans tmux et survivent à l'extinction de l'app.
- **Réglages par projet** : timeouts (`verify`, `phase`) visibles et modifiables depuis la carte
  projet et l'en-tête du run, harness, équipement — persistés, sans fichier de config à écrire.
- **Bilingue à bord** (FR / ENG), thème clair et sombre, notifications de porte et de fin de run.

### On travaille sur le projet à distance : rien à intégrer dans le dépôt cible

- **L'équipement.** L'app copie dans le projet les skills `.agents/` et les artefacts du harness
  choisi, pose le marqueur `.mm-equip.json`, et c'est tout : les binaires restent dans `engine/`, à
  côté de l'app. Un correctif de l'usine profite immédiatement à tous les projets équipés
  (« Mettre à jour l'équipement », avec sauvegarde `.agents.bak-*`).
- **Le harness d'agent est un choix, pas un fork.** OpenCode ou Codex CLI, décidé projet par
  projet à l'équipement (ou `MM_AGENT_HARNESS=` en terminal), changeable à tout moment. Les
  binaires embarquent les deux implémentations (`mm_runner.py` : une interface, une classe par
  agent — en ajouter un troisième = une classe + une entrée de registre). **Codex CLI est en
  bêta** : moins éprouvé qu'OpenCode sur des runs réels, il demande des retours utilisateurs
  (portes, permissions, modèles) ; OpenCode reste le choix de référence. Codex n'ayant pas de
  réglage « ne pose pas de question », la consigne est portée par `AGENTS.md` et rappelée en tête de
  chaque tâche envoyée à l'agent.
- **Reprise par fichiers.** Tout l'état d'un run vit dans le projet (`spec.md` + `.spec_approved`,
  `plan.md`, `impact.md` + `.impact_approved`, `blackboard.yaml`, fichiers de verdicts, cartes).
  Relancer ne refait jamais ce qui est validé ; supprimer un fichier force la régénération de la
  seule étape correspondante. Le workflow deux temps (gros modèle pour penser, petit pour
  produire) tient en un `n` à la porte blackboard, un changement de modèle et une relance.
- **Boîte noire.** Chaque run laisse un journal local `.mm-runs/<id>/` : chronologie
  `events.jsonl` crash-safe, artefacts figés aux transitions, copie de l'écran de l'orchestrateur,
  `run.json` (version de distro, code de sortie, modèle observé), `summary.md`. Rétention 20 runs,
  auto-gitignoré, `MM_AUDIT=0` pour désactiver. Ctrl-C, `SIGTERM` et `SIGHUP` clôturent le journal
  proprement et éteignent la session d'agent ; le bouton d'arrêt de l'app fait de même.

### Installation

- **Une commande, puis plus rien** : `sh install.sh` (détection d'OS, prérequis apt/brew, droits
  posés, quarantaine macOS levée, vérification de Node et de sa cohérence avec le shell de login),
  puis double-clic au quotidien.
- **Binaires autonomes** (Nuitka onefile) : aucune installation de Python demandée ; en dev, le
  dépôt s'utilise tel quel, le binaire absent retombe sur la source `.py`.
- **Zéro `chmod`, jamais** : l'app remet elle-même son moteur en état à chaque démarrage et avant
  chaque lancement (bit exécutable, quarantaine Gatekeeper). Zip qui écrase les permissions, copie
  par l'explorateur, clé USB : elle répare.
- **Archives complètes** `tar.gz` par variante (app + lanceur + moteur + docs + `LICENSE`),
  construites par la CI au tag. Licence non commerciale personnalisée.

### Douze orchestrateurs, quatre catégories

Tous déclarés dans `engine/orchestrators.json` (catégorie, portes, étapes, badges) et pilotables
depuis l'app comme en terminal. Philosophie constante : **le verdict est l'exécution réelle**
(compilation + suite complète, jamais un LLM qui se note lui-même), des portes humaines aux
moments à fort levier, un contexte tranché par phase qui rend les petits modèles compétitifs.

**Adaptation des skills** — à lancer d'abord si la stack n'est pas Java/Spring + React/TS.

- `Skills-Adaptation` (WIP) réécrit les skills techniques **du moteur** (`engine/.agents/skills` :
  codage et tests, back et front) pour ta stack, questionnaire court, garde-fous Python (limite de
  lignes, frontmatter, tableau ❌/✅, checklist), revue qualité indépendante, écrasement skill par
  skill après ta validation (`.bak`), miroir dans le projet courant. Les autres projets équipés
  les reçoivent via « Mettre à jour l'équipement ». Un moteur = une stack : pour des projets aux
  stacks distinctes, dupliquer le dossier de l'outil. À lancer avec un bon modèle, de préférence
  frontier : ces skills conditionnent tous les runs suivants.

**Coding** — du besoin au code vérifié, planification (spec, plan, blackboard) incluse.

- `Acceptance-First` ⭐ : acceptance-first par lots de user story (inspiré de l'ATDD), suite
  d'acceptance en boîte noire, étapes intermédiaires compilées seules, clôture à la suite complète.
- `Test-First` ⭐ : test-first par cycles red → green → refactor (inspiré du TDD), verdicts
  interprétés par nature, refactor de cycle annulé mécaniquement s'il casse.
- `Coding` : verdict universel à chaque phase.
- `Coding-Without-Tests` : vérificateur LLM indépendant quand une suite n'a pas de sens (POC,
  script jetable, glue).
- Les trois usines testées embarquent la **surcouche** : revue d'impact validée par l'humain avant
  production, vérificateur LLM sur chemin vert, et sur chemin rouge un triage par fichier de test
  (prévu / imprévu), un réparateur, et une porte d'arbitrage mid-run « régression subie ou
  évolution voulue ? ». Le plan peut déclarer des tests obsolètes (`tests_to_remove`, supprimés par
  l'orchestrateur) ou à faire évoluer (`tests_to_update`). Sous git : commit par phase verte,
  tests protégés restaurés, gardes anti-codeur fantôme et de non-décroissance du compte de tests,
  rollback d'un polish final qui casse.
- `Guided-Fix` (bêta) : réparation arbitrée d'un run arrêté sur suite rouge — diagnostic des
  comportements cassés, triage humain comportement par comportement, marqueur `FIXED` revalidé par
  l'usine à la relance sans re-payer de codeur.

**Design** — prototype et audits d'interface.

- `Design-Prototype` (bêta) : prototypes cliquables HTML/CSS/JS vanilla pour les designers, porte
  design system en tout premier, gardes mécaniques de tokens à chaque phase, review UX +
  blackboard + design system en fin de run.
- `Audit-Design` (bêta) : audit UX d'une interface existante contre les 10 heuristiques de
  Nielsen, une passe par heuristique, synthèse priorisée par sévérité, lecture seule garantie par
  git.
- `Pre-Audit-A11Y-RGAA` (bêta) : pré-audit d'accessibilité RGAA 4.1.2 (106 critères, 13
  thématiques) — packs routés par déclencheurs déterministes, tranches de 40 fichiers ou 80 Ko,
  tranches sans motif classées NA sans agent, chaque constat porteur d'un extrait recherché dans
  le fichier cité, agrégation Python avant la synthèse, taux en fourchette, rapport PARTIEL qui
  survit aux passes échouées, reprise `--rejouer-modifiees [ref]` après remédiation, fiche de
  résultats 100 % Python.

**Produit** — besoin, spec, documentation.

- `Challenge-Need` : challenger le besoin avant de payer une spec — ambiguïtés, contradictions,
  présupposés, citations vérifiées dans `need.md` ; opt-in, aucun couplage aval.
- `Spec` : la spécification seule, validée avec le métier, reprise telle quelle par toute usine.
- `Documentation` (bêta) : documentation comportementale d'un projet existant — carte fonctionnelle
  validée, une passe par zone sous gardes de contenu, features sourcées `fichier:ligne`, tests
  d'acceptance Couvert / Proposé, assemblage Python.

### Environnement d'outillage

- **Même Node pour le verdict et pour l'agent.** L'orchestrateur sonde une fois le PATH du shell
  de login (`$SHELL -lic`) et le place en tête avant tout verdict ; l'app fait de même et passe son
  PATH à la session tmux du run. Désactivable : `MM_TOOLCHAIN_PROBE=0`.
- **Pré-vol toolchain** : au premier verdict JS/TS, une ligne dit quel Node l'orchestrateur exécute
  et ce que le projet attend (`.nvmrc`, `.node-version`, `engines.node`) ; le préflight de l'app
  affiche version et chemin, avec une alerte sous Node 20 ou en cas de divergence avec le shell de
  login.
- **Rapport d'échec « environnement »** : une incompatibilité de runtime détectée au scaffold
  produit un `failReport.md` qui désigne l'environnement (Node vu, Node attendu, cause probable)
  avant tout appel d'agent.

### Documentation du produit

- **`SCHEMAS.html`** (FR / ENG) : un onglet par orchestrateur — pipeline, portes, verdicts,
  livrables, boucles d'échec, branches « n » et reprise par fichiers, relus contre le code. Le rail
  suit les catégories de la Bibliothèque ; un bloc commun sous chaque onglet rappelle les règles
  partagées (boucles bornées, « n », reprise, garde git, contexte neuf) et donne un glossaire.
- **README** (racine et par variante), `INSTALL.md`, `useCases*.md` : quel script pour quel besoin,
  par catégorie ; mode expert ; règles du jeu.

### Sous le capot (pour qui développe MAIsterMind)

- **6 variantes synchronisées mécaniquement** : identité octet par octet intra-langue, AST FR = ENG
  modulo chaînes, couche app unifiée — checkers refusés en CI s'ils sont rouges.
- **Schémas verrouillés sur le manifeste** : `tools/check_schemas.py` vérifie le JavaScript
  embarqué, la correspondance onglets ↔ binaires déclarés et l'identité des copies par variante.
- **Caractérisation exécutable** : 32 scénarios mock (pipelines entiers, zéro token, harness de
  test isolé jusqu'au moteur jetable) comparés à des transcripts goldens versionnés, plus les tests
  unitaires des fonctions pures, sur chaque push/PR ; `build.yml` rejoue tout avant de compiler.
- **Moteur factorisé** : le socle commun des orchestrateurs vit dans `mm_core.py`, le journal de
  run dans `mm_audit.py`, le harness dans `mm_runner.py` — modules embarqués dans chaque binaire,
  jamais livrés en `.py`. `MM_ENGINE_HOME`, posé par l'app au lancement d'un run, désigne le moteur
  aux orchestrateurs qui en ont besoin.
