# MAIsterMind (Version Ubuntu / Debian)
*Par Selim Boukhari* — [LinkedIn](https://www.linkedin.com/in/selim-boukhari-6356b949/?locale=en)

Une usine à code pilotée par IA : du besoin au code vérifié, par des pipelines structurés
que TU valides aux moments décisifs — avec OpenCode **ou** Codex CLI, au choix. Tu
n'intègres rien dans ton projet : l'app l'équipe en un clic et pilote tout depuis le
navigateur.

> 📄 **Licence** : usage non commercial, personnel ou éducatif uniquement — voir le fichier
> `LICENSE`. Tout usage commercial requiert un accord écrit préalable de l'auteur.

## Démarrer en 5 étapes

1. **Installe** — une commande, zéro `chmod`, aucune installation de Python :
   `sh install.sh` (prérequis apt, droits, entrée de menu). Harness manquant ? `INSTALL.md`.
2. **Ouvre l'app** : « MAIsterMind » dans le menu d'applications — le navigateur s'ouvre seul.
3. **Sélectionne ton projet** cible (nouveau ou existant) et **équipe-le** : bouton
   « Équiper » avec le harness de ton choix — skills et artefacts copiés, rien d'autre.
4. **Décris ton besoin** dans `need.md` à la racine du projet — uniquement pour produire ou
   cadrer : les audits, la documentation et la réparation se lancent SANS `need.md`.
5. **Choisis ton script et lance.** Tu réponds aux portes de validation depuis le navigateur ;
   le run vit dans tmux (fermer l'app ne tue rien) et laisse un journal `.mm-runs/<id>/`.
   Le modèle IA se règle dans le harness : `/model` dans le TUI, ou le fichier de config du
   projet (`.opencode/opencode.json` / `.codex/config.toml`).

## Quel script pour quel besoin ?

| Besoin | Script | need.md |
|---|---|---|
| Développer avec tests — le mode le plus robuste | `Coding` | requis |
| Développer sans tests (POC, script jetable, glue) | `Coding-Without-Tests` | requis |
| Développer test-first par cycles red → green → refactor (inspiré du TDD) | `Test-First` | requis |
| Développer acceptance-first par lots de user story (inspiré de l'ATDD) | `Acceptance-First` | requis |
| Prototype cliquable HTML/CSS/JS (designers) | `Design-Prototype` (bêta) | requis |
| Brownfield/legacy : l'arbitrage « régression ou évolution ? » est intégré aux trois (revue d'impact, triage) | `Coding` / `Test-First` / `Acceptance-First` | requis |
| Challenger le besoin AVANT de payer une spec | `Challenge-Need` | requis |
| Cadrer : la spec seule, validée avec le métier | `Spec` | requis |
| Penser avec un gros modèle, produire ensuite avec un petit | `Technical-Plan`, puis une usine | requis |
| Documenter un projet existant (lecture seule) | `Documentation` (bêta) | non |
| Audit UX — 10 heuristiques de Nielsen (lecture seule) | `Audit-Design` (bêta) | non |
| Pré-audit d'accessibilité RGAA 4.1.2 (lecture seule) | `Pre-Audit-A11Y-RGAA` (bêta) | non |
| Réparer un run arrêté sur suite rouge | `Guided-Fix` | non |
| Adapter les skills livrés à TA stack | `Skills-Adaptation` | non |

## Pour aller plus loin

- **`SCHEMAS.html`** — COMMENT chaque pipeline fonctionne : portes, verdicts, livrables,
  en schémas (un onglet par script).
- **`useCasesFr.md`** — les situations concrètes : reprise après interruption, tests qui
  cassent, workflow deux temps, réparation arbitrée.
- **`INSTALL.md`** — prérequis et installation des harness.

## Mode expert (terminal)

Les binaires s'utilisent en direct, sans Python ni venv, depuis la racine de TON projet
équipé :

```bash
cd /chemin/vers/ton/projet
/chemin/vers/MAIsterMind/engine/Coding
```

`MM_AGENT_HARNESS=opencode|codex` impose le harness pour un lancement ; sinon il est déduit
de l'équipement. Suivre un run en direct : `tmux attach -t <session>` (nom affiché au
lancement ; `Ctrl+B` puis `D` pour sortir sans couper l'IA). En dev, depuis le dépôt source :
`python3 engine/Coding.py`.

Dépannage express :
- **Forcer l'arrêt d'un run** : `tmux kill-session -t <session>`.
- **Reprendre après un crash** : relance simplement — tout reprend par fichiers, rien de
  validé n'est refait.
- **Le scaffold (étape 0) n'aboutit jamais** : c'est le smoke test du modèle — s'il écrit
  ses appels d'outils en texte au lieu de les exécuter, change de modèle.
- **Comprendre un run a posteriori** : journal `.mm-runs/<id>/` (chronologie, artefacts
  figés, résumé) — `MM_AUDIT=0` pour désactiver.

## Les règles du jeu

- **Le verdict est l'exécution réelle** (compilation + suite complète) — jamais un LLM qui
  se note lui-même.
- **Tu valides aux moments à fort levier** : spec, blackboard, cartes d'audit — chaque
  fichier est éditable avant ton `y`.
- **Tout reprend par fichiers** : relancer ne refait jamais ce qui est validé ; supprimer
  un livrable force la régénération de la seule étape correspondante.
- **Sous git, le run est gardé** : commit par phase verte, fichiers de tests protégés,
  rollback automatique d'une refacto qui casse — sans git, tout fonctionne, sans ces filets.
- **Relis le code produit** — jamais de boîte noire.
