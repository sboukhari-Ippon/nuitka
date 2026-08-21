# Changelog

## 3.0.0 — MAIsterMind devient une app

*Première version publique unifiée : l'usine à code (16 orchestrateurs) et son
cockpit navigateur dans un seul produit, distribué en archives prêtes à l'emploi
sur 6 variantes (FR/ENG × Ubuntu/macOS/Windows-WSL). Les itérations internes qui
y ont mené sont tracées dans l'historique git et les notes de chantier archivées
(`docs/archive/`).*

### C'est une app

- **Cockpit navigateur** : l'app découvre les moteurs (`engine/`), équipe les
  projets, lance les runs dans tmux, affiche leur écran en direct et répond aux
  portes de validation (y/n, choix multiples, saisie libre) depuis le
  navigateur. Le terminal reste disponible pour tout, en mode expert.
- **Double-clic au quotidien** : lanceur natif par plateforme — bundle
  `MAIsterMind.app` (macOS/Finder), `MAIsterMind.bat` polyglotte (Windows →
  WSL), entrée de menu d'applications (Ubuntu). Sans terminal, l'app journalise
  dans `.mm-app/launcher.log` et s'éteint par le bouton ⏻ de « Statut &
  réglages » ; les runs vivent dans tmux et survivent à l'extinction de l'app.
- **Boîte noire** : chaque run laisse un journal local `.mm-runs/<id>/`
  (chronologie `events.jsonl` crash-safe, artefacts figés aux transitions,
  `run.json`, `summary.md`) — rétention 20 runs, auto-gitignoré, désactivable
  via `MM_AUDIT=0`. Une seule ligne visible au bilan : « 📁 Journal du run ».
- **Réglages par projet** dans l'app : timeouts (`verify`, `phase`), harness,
  équipement — persistés, sans fichier de config à écrire.

### On travaille sur le projet à distance — plus rien à intégrer dans le dépôt cible

- **L'équipement remplace l'intégration** : plus aucun script MAIsterMind à
  copier ni à maintenir dans le projet cible. L'app équipe le projet en un clic
  (copie des skills `.agents/` et des artefacts du harness, marqueur
  `.mm-equip.json`) et les binaires restent dans `engine/`, à côté de l'app —
  un correctif de l'usine profite immédiatement à tous les projets équipés.
- **Le harness d'agent est un choix, pas un fork** : OpenCode ou Codex CLI,
  décidé projet par projet à l'équipement (ou par `MM_AGENT_HARNESS=` en
  terminal), changeable à tout moment. Les binaires embarquent les deux
  implémentations (`mm_runner.py` : une interface, une classe par agent —
  en ajouter un troisième = une classe + une entrée de registre).
- **Reprise par fichiers** : tout l'état d'un run vit dans le projet
  (`spec.md`, `plan.md`, `blackboard.yaml`, sentinelles, fichiers de verdicts).
  Relancer ne refait jamais ce qui est validé ; supprimer un fichier force la
  régénération de la seule étape correspondante. La bascule gros modèle
  (penser) / petit modèle (produire) est triviale, sans configuration.

### Installation facilitée

- **Une commande, puis plus rien** : `sh install.sh` (détection d'OS, prérequis
  apt/brew, droits posés, quarantaine macOS levée), puis double-clic au
  quotidien.
- **Binaires autonomes** (Nuitka onefile) : aucune installation de Python
  demandée à l'utilisateur ; en dev, le dépôt s'utilise tel quel (le binaire
  absent retombe sur la source `.py`).
- **Zéro `chmod`, jamais** : l'app remet elle-même son moteur en état à chaque
  démarrage et avant chaque lancement (bit exécutable, quarantaine Gatekeeper) —
  zip qui écrase les permissions, copie par l'explorateur, clé USB : elle répare.
- **Archives complètes** `tar.gz` par variante (app + lanceur + moteur + docs +
  `LICENSE`), construites par la CI au tag. Licence non commerciale
  personnalisée (`LICENSE`), mentionnée dans toutes les docs.

### Couverture élargie : quasi tous les besoins

Seize orchestrateurs, tous déclarés dans `engine/orchestrators.json` et
pilotables depuis l'app. Philosophie constante : **le verdict est l'exécution
réelle** (jamais un LLM qui se note lui-même), des portes humaines aux moments
à fort levier, un contexte tranché par phase qui rend les petits modèles
compétitifs.

- **Production** : `Safe-Coding` (verdict universel : compilation + suite
  complète à chaque phase), `Coding-Without-Tests` (vérificateur LLM
  indépendant quand une suite n'a pas de sens), `Safe-TDD` (cycles
  red → green → refactor, verdicts interprétés par nature, gardes git),
  `Safe-ATDD` (lots par user story, suite d'acceptance en boîte noire),
  `Design-Prototype` (prototypes cliquables, design system transporté sous
  gardes mécaniques, review UX finale).
- **Surcouche Yolo** : `Advanced-Coding`, `Advanced-TDD`, `Advanced-ATDD` —
  revue d'impact validée par l'humain avant production, puis arbitrage
  « régression subie ou évolution voulue ? » PENDANT le run (triage sur suite
  rouge, porte d'arbitrage mid-run). Pour le brownfield ; les bases restent le
  choix robuste du greenfield.
- **Cadrage** : `Challenge-Need` (challenger le besoin AVANT de payer une spec :
  ambiguïtés, contradictions, présupposés — citations vérifiées mot pour mot,
  opt-in, aucun couplage aval), `Spec` (la spec seule), `Technical-Plan`
  (spec + plan + blackboard sans production — le workflow deux temps).
- **Lecture seule sur l'existant** : `Documentation` (cartographie validée,
  une passe par zone, features sourcées `fichier:ligne`, tests d'acceptance
  Couvert/Proposé), `Audit-Design` (10 heuristiques de Nielsen, rapport
  priorisé par sévérité), `Audit-A11Y-RGAA` (pré-audit RGAA 4.1.2, 106
  critères : packs routés par déclencheurs déterministes, chaque constat
  PROUVÉ par un extrait retrouvé mécaniquement dans les fichiers, taux en
  fourchette honnête, rapport PARTIEL qui survit aux passes échouées, reprise
  `--rejouer-modifiees <ref>` après remédiation).
- **Maintenance & outillage** : `Guided-Fix` (réparation arbitrée d'un arrêt
  sur suite rouge, triage humain comportement par comportement, gardes git),
  `Skills-Adaptation` (réécrit les skills livrés pour TA stack, sous
  garde-fous et revue qualité indépendante).

### Sous le capot (pour qui développe MAIsterMind)

- **6 variantes synchronisées mécaniquement** : identité octet par octet
  intra-langue, AST FR = ENG modulo chaînes, couche app unifiée — 5 checkers,
  refusés en CI s'ils sont rouges.
- **Caractérisation exécutable** : 32 scénarios mock (pipelines entiers, zéro
  token) comparés à des transcripts goldens versionnés + tests unitaires des
  fonctions pures, sur chaque push/PR ; `build.yml` rejoue tout avant de
  compiler.
- **Moteur factorisé** : le socle commun des orchestrateurs vit dans
  `mm_core.py`, le journal de run dans `mm_audit.py`, le harness dans
  `mm_runner.py` — modules embarqués dans chaque binaire, jamais livrés en
  `.py`.
