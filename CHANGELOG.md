# Changelog

## 3.0.0 — MAIsterMind devient une app

*Première version publique unifiée : l'usine à code (13 orchestrateurs) et son
cockpit navigateur dans un seul produit, distribué en archives prêtes à l'emploi
sur 6 variantes (FR/ENG × Ubuntu/macOS/Windows-WSL). Les itérations internes qui
y ont mené sont tracées dans l'historique git et les notes de chantier archivées
(`docs/archive/`).*

### Correctifs intégrés avant publication — Un seul Node pour l'agent et l'orchestrateur, des cartes qui découpent vraiment

*Correctifs issus des runs du 22-23 août 2026 (Advanced-ATDD sur un projet Vite/vitest,
Documentation et Audit-A11Y-RGAA sur un monorepo de 1 639 fichiers). Ces correctifs sont intégrés à la 3.0.0 avant sa publication ; il n'y a pas de 3.0.1.*

#### Pré-audit d'accessibilité : un nom et des livrables qui disent ce que c'est

- **`Audit-A11Y-RGAA` devient `Pre-Audit-A11Y-RGAA`** (id `pre-audit-a11y`, dossier de passes
  `pre_audit_a11y/`, rapport `accessibility_pre_audit_report.md`) : l'outil fait un pré-audit
  statique, son nom, ses portes, son écran, ses schémas et ses use cases le disent désormais.
- **La « déclaration d'accessibilité » disparaît.** Le squelette réglementaire pré-rempli
  (article 47, voies de recours, champs [À COMPLÉTER]) donnait l'apparence d'un document
  officiel prêt à publier. Il est remplacé par `accessibility_pre_audit_summary.md` : chiffres
  clés, non-conformités démontrées (une ligne + correction chacune), reste à vérifier par
  thématique — 100 % Python, sans cadre légal.
- Projets déjà pré-audités : `audit_a11y/`, `accessibility_audit_report.md` et
  `declaration_accessibilite.md` ne sont plus reconnus (ni repris, ni nettoyés par l'app) —
  supprime-les à la main ou relance de zéro.

#### Trois usines de code, la surcouche devient la référence

- **`Safe-Coding`, `Safe-TDD`, `Safe-ATDD` retirés.** Les variantes Advanced en étaient des
  sur-ensembles stricts (mêmes gardes, mêmes prompts, plus la revue d'impact, le vérificateur LLM
  et le triage des cassures) ; la surcouche pesait 15 % du coût d'un run mesuré le 28/08. Il ne
  reste qu'un pipeline par contrat : **`Coding`** (ex-Advanced-Coding, id `coding`), **`Test-First`**
  (ex-Advanced-TDD, id `test-first`), **`Acceptance-First`** (ex-Advanced-ATDD, id `acceptance-first`). `Coding-Without-Tests`
  et `Design-Prototype` sont inchangés : 13 orchestrateurs.
- **Bibliothèque de l'app** : plus de carte repliée « Autres pipelines de code » ni de champ
  `secondary` au manifeste — les pipelines de code sont tous visibles ; `⭐ recommandé` reste sur
  TDD et ATDD.
- **Schémas, README, INSTALL, use cases, outils** alignés ; la surcouche y est présentée comme le
  comportement de référence et non comme une option. Les scénarios mock des ex-Safe sont convertis
  au flux de référence (porte d'impact, vérificateur, triage/réparateur sur chemin rouge) ; deux
  doublons supprimés (`universel-nominal`, `atdd-lot`).
- Compatibilité : les projets équipés n'ont rien à faire (`.mm-equip.json` ne nomme aucun
  orchestrateur). Les journaux `.mm-runs/*-yolo-*` d'anciens runs restent lisibles ; les nouveaux
  s'appellent `*-coding` / `*-test-first` / `*-acceptance-first`, et les sessions tmux de l'agent
  `oc-coding-…`, `oc-test-first-…`, `oc-acceptance-first-…`.
- **Pourquoi « Test-First » et « Acceptance-First » plutôt que TDD et ATDD.** Revue du 28/08 contre Beck,
  Adzic et Gärtner : la mécanique test-first est respectée et garantie par git, mais plusieurs choix de
  sûreté propres à une usine d'agents (plan écrit d'avance, tests gelés, un cycle par comportement)
  s'écartent des méthodes canoniques. Les noms disent ce que l'usine garantit ; l'inspiration est
  déclarée dans le manifeste, les schémas et les README. Le vocabulaire interne (`tdd-red`,
  `atdd-test`, skills `plan-tdd`/`plan-atdd`) est inchangé.

#### Bibliothèque : « recommandé » sur Test-First et Acceptance-First, « bêta » sur quatre pipelines

- Badge **⭐ recommandé** porté par `Test-First` et `Acceptance-First` ; badge **🧪 bêta** (nouveau champ
  `beta` du manifeste, v1.3) sur `Design-Prototype`, `Documentation`, `Audit-Design` et
  `Pre-Audit-A11Y-RGAA` : fonctionnels, moins éprouvés que les usines de code. README alignés.
- `Audit-Design` gagne son scénario mock de bout en bout (`design-nominal` : périmètre → porte →
  10 passes Nielsen → synthèse → `design_audit_report.md`), le seul orchestrateur qui n'en avait pas.
  32 scénarios sous `--golden check`.

#### Fil d'Ariane du run : une timeline par orchestrateur

- **La timeline déclarée au manifeste.** Chaque orchestrateur hors production déclare ses étapes
  dans `orchestrators.json` (`steps` : libellés fr/eng + preuve durable — fichier livré, dossier
  apparu, motif — et portes rattachées). L'app rend la timeline depuis cette déclaration
  (`infer_declared_step`) : étape courante = première sans preuve, porte ouverte = son étape en
  cours, run mort code 0 = tout terminé. Les 8 pipelines concernés : Spec, Challenge-Need,
  Technical-Plan, Documentation, Audit-Design, Pre-Audit-A11Y-RGAA, Skills-Adaptation, Guided-Fix.
- Le modèle usine à 5 étapes (Spécification → … → Refactoring) reste le repli des pipelines de
  production, inchangé. Fini le pré-audit terminé code 0 affiché bloqué sur « Spécification (PO) »
  (constat du 28/08).

#### Documentation : la garde des sources ne rejette plus les chemins d'exécution

- **Faux positifs supprimés.** La garde « chemin cité inexistant » tenait pour source tout
  token entre backticks contenant un `/`. Une zone de scripts d'orchestration parle de
  branches (`origin/epic/<KEY>`), de globs (`docs/*.md`), de motifs (`tick_*_agent_<TICKET>.json`)
  et de dossiers créés à l'exécution (`docs/`) : 8 écarts sur 11 étaient de cette nature, trois
  tentatives brûlées (28/08). Désormais un motif (`<>*?{}$|→`) n'est jamais une citation, et un
  chemin avec `/` n'est vérifié que si son premier segment existe à la racine du projet
  (`scripts/…`, `src/…`) — les fautes réelles (`agent_drilldown.h`, `orchestrationdispatch_plan.sh`)
  restent rejetées.
- **Basename nu : le chemin exact en retour.** `dispatch_plan.sh` cité seul, qui correspond à un
  unique fichier de la zone, renvoie « cite `scripts/orchestration/dispatch_plan.sh` » au lieu de
  « n'existe pas ».
- **Bilan : « Tests d'acceptation » accepté** au même titre que « Tests d'acceptance » (le modèle
  écrivait le français correct et se faisait rejeter au caractère près).
- Skill `doc-zone` : les backticks sont réservés aux fichiers du projet, recopiés depuis la racine.

#### Environnement d'outillage (P1-P3)

- **Même Node pour le verdict et pour l'agent.** L'agent tourne dans un pane tmux ouvert
  sans commande (shell de login interactif : nvm/fnm/volta chargés) ; l'orchestrateur
  héritait du PATH du processus ayant créé le serveur tmux — l'app, lancée sans terminal,
  voyait le Node système. Verdict `npx tsc && vitest` rendu sous Node 18 pendant que
  l'agent voyait 12/12 tests verts sous Node 22. Désormais `mm_core` sonde UNE fois le
  PATH du shell de login (`$SHELL -lic`) et le place en tête avant tout `run_verify` /
  `run_mutation` ; l'app fait de même (`enrich_path`) et passe son PATH explicitement à
  la session tmux du run. Désactivable : `MM_TOOLCHAIN_PROBE=0`.
- **Pré-vol toolchain** : au premier verdict JS/TS d'un run, une ligne dit quel Node
  l'orchestrateur exécute (chemin + version) et ce que le projet attend (`.nvmrc`,
  `.node-version`, `engines.node`) ; journalisé (`toolchain` dans events.jsonl). Le
  préflight de l'app affiche la version, le chemin et une pastille orange sous Node 20
  ou si un shell de login résoudrait un autre `node`. `install.sh` vérifie la version et
  la cohérence avec le shell de login.
- **Rapport d'échec « environnement »** : quand le pré-contrôle du scaffold échoue sur
  une signature d'incompatibilité de runtime (`does not provide an export named`,
  `EBADENGINE`, `ERR_REQUIRE_ESM`…), l'orchestrateur s'arrête net avec un `failReport.md`
  qui désigne l'environnement (Node vu, Node attendu, cause probable) au lieu de
  solliciter un agent scaffold puis d'accuser le modèle (« monte le modèle d'un cran »).
- **Lanceur Ubuntu / WSL en `bash -lic`** : un shell de login non interactif s'arrêtait à
  la garde `case $- in *i*)` du `~/.bashrc` standard avant de charger nvm.

#### Cartographie (Documentation, Audit-A11Y-RGAA) (P4)

- **« Divers » facultative** : le prompt demandait de ne PAS y recopier le surplus, le
  validateur rejetait une « Divers » vide — contradiction levée (complétée par la
  couverture, retirée si rien ne reste).
- **Entrées RÉPERTOIRE** : une entrée de la carte terminée par `/` assigne tous les
  fichiers du périmètre qu'elle contient (récursivement). Un monorepo se cartographie sans
  recopier des milliers de chemins — et sans que tout tombe en « Divers ».
- **Échantillon représentatif** : les 400 fichiers listés au cartographe sont tirés de
  tous les répertoires (code applicatif d'abord, assets/migrations/outillage en dernier)
  au lieu des 400 premiers par ordre alphabétique (311 feuilles de style d'icônes, zéro
  fichier de `src/`).
- **Résiduel borné** : plus de 100 fichiers en « Divers » = carte rejouée tant qu'il
  reste des tentatives, avec les répertoires à assigner en retour. (697 fichiers en
  « Divers », c'était 364 passes d'audit sur du « non classé ».)
- **Attente adaptative** : le budget d'une passe se prolonge tant que l'écran de l'agent
  change (il travaille), jusqu'à 3 × le budget ; un agent figé sur une demande de
  permission de sa TUI arrête l'attente immédiatement au lieu de consommer 3 × 600 s.
- **Agent d'usine** : `external_directory: allow` (un `cd` hors projet ouvrait un
  dialogue de permission que personne ne validait). **OpenCode** : `autoupdate: false`
  dans la config équipée — l'auto-mise à jour au premier boot avalait le prompt collé.
  `mm_runner.start()` attend en plus que la TUI ait pris l'écran (readiness ≤ 45 s).

#### Production : le plan peut déclarer des tests obsolètes ou à faire évoluer

- Nouveaux champs de phase, optionnels, transportés du plan au blackboard par les
  compilateurs : **`tests_to_remove`** (tests existants rendus obsolètes par la spec :
  l'orchestrateur les supprime LUI-MÊME au début de la phase, `git rm` + commit, retrait
  des protections, re-baseline de la garde de non-décroissance) et **`tests_to_update`**
  (tests que la phase d'implémentation a le droit de modifier). Les gardes de gel
  (implémentation ATDD/TDD, tests protégés) les exemptent ; le prompt codeur porte
  l'exception planifiée ; le schéma refuse tout chemin qui n'est pas un fichier de test.
  Grilles `plan`, `plan-tdd`, `plan-atdd` (champs « Tests à supprimer » / « Tests à
  modifier ») et `plan-to-blackboard*` mises à jour. Le 23/08/2026, un plan qui déclarait
  noir sur blanc la suppression de `Counter.test.tsx` était restauré trois fois par la
  garde : la phase échouait sans issue.

#### Audit RGAA : 4 fois moins de passes, même couverture

- **Tranches sans motif = NA mécanique** : une tranche dont aucun fichier ne porte de
  déclencheur du pack n'est plus envoyée à l'agent (173 des 509 passes d'egapro) ; son
  fichier de verdicts est écrit au format des passes (parseur, consolidation, reprise
  inchangés) et tracé en annexe. Exception conservée : packs « toujours » sur le socle.
- **Assets tiers hors périmètre** (`public/`, `static/`, `assets/`, `dsfr/`, bundles
  legacy) : la bibliothèque n'est pas le projet ; ses surcharges dans `src/` restent
  auditées. **Logique pure sans signal d'interface** (`.ts/.js` sans balise, composant,
  ARIA ni DOM) hors périmètre ; les extensions porteuses de balisage (`.tsx`, `.vue`,
  `.html`…) restent toujours dedans. Les deux listes sont affichées à l'écran É0 et
  détaillées par répertoire dans l'annexe « Périmètre et routage ».
- **Tranches par budget d'octets** (80 Ko, plafond 40 fichiers) au lieu de 25 fichiers
  quelle que soit leur taille. Sur egapro : 509 passes → ~130.

#### Arrêts propres et observabilité (P5-P6)

- Ctrl-C, `SIGTERM` et `SIGHUP` (kill de la session tmux) clôturent le journal en
  `interrupted` et tuent la session d'agent ; filet `atexit` (`aborted`). Le bouton
  d'arrêt de l'app envoie Ctrl-C, attend, puis tue la session de run ET celle de l'agent
  — plus d'agent orphelin qui écrit une carte résiduelle après la mort de l'orchestrateur.
- Une carte reprise dont le mtime est postérieur à la dernière trace d'un run resté sans
  clôture est signalée avant le y/n.
- `.mm-runs/<run>/orchestrator.log` : copie de tout ce que l'orchestrateur affiche ;
  `run.json` porte `distro_version` (lue dans `.mm-equip.json` à défaut), le code de
  sortie du run (`exit_code`, recopié par l'app), et le modèle réellement observé dans le
  journal d'OpenCode quand aucune config ne le fixe.

#### App

- **Bibliothèque recentrée** : Advanced-ATDD et Advanced-TDD sont les deux pipelines de
  code mis en avant (⭐ recommandés) ; Safe-Coding, Coding-Without-Tests, Safe-TDD,
  Safe-ATDD et Advanced-Coding sont repliés dans une carte « Autres pipelines de code »
  qui se déploie au clic (champ `secondary` du manifeste, choix mémorisé). Rien n'est
  retiré du moteur.
- **Timeouts visibles** : le bouton « ⏱ Timeouts » de la carte projet (Bibliothèque)
  n'est plus un bouton fantôme et affiche les valeurs en vigueur ; il est aussi présent
  dans l'en-tête de la vue Run, à côté du binaire, de la commande de vérification et du
  harness.
- **Aperçu des documents lisible en thème clair** : les blocs de code des specs, plans,
  blackboard et rapports (`.gate-doc pre`) reprenaient les couleurs du terminal (fond
  sombre, texte clair) et la règle globale `code {}` posait un fond clair sous le texte
  clair — illisible en Aurore. Ils suivent désormais le thème (fond panneau, encre du
  thème, bordure), en clair comme en sombre.

#### Schémas

- `SCHEMAS_FR.html` / `SCHEMAS_ENG.html` (et leurs copies par variante) mis à jour : gel des
  tests avec exception planifiée, tranches et NA mécanique de l'audit RGAA, périmètre exclu,
  cartographie par répertoires, verdict avec la toolchain du shell de login.

#### Tests

- `tools/test_toolchain_env.py` (sonde, fusion PATH, contrainte Node, échantillon,
  répertoires, attente adaptative, livrable résiduel), `tools/test_documentation_units.py`
  (validateur de carte), cas ajoutés à `test_audit_units.py` et `test_mm_audit.py`.

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
