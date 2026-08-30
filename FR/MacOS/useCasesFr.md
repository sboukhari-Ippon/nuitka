# Use cases — `Coding.py`

Public : développeuse ou développeur qui pilote l'usine à code (variante « verdict universel », valable pour les 6 déclinaisons FR/ENG × Ubuntu/MacOS/Windows ; ne couvre pas `Coding-Without-Tests.py`).

Ce que fait le script en une phrase : il transforme un besoin brut (`need.md`) en spec métier validée par l'humain, puis en plan d'implémentation à micro-phases bornées, puis en code produit phase par phase par un agent — chaque phase étant jugée par l'**exécution réelle** de la commande de vérification (compilation + suite complète), jamais par un LLM. Le tout est conçu pour rendre un petit modèle compétitif : contexte tranché par phase, formats stricts, décisions prises en amont par les agents qui ont le contexte.

Ce document suppose une usine déjà installée et un projet structuré : pour ça, voir `INSTALL.md` et `README.md`.

## Ce qu'il faut savoir avant tout : la reprise par fichiers

L'état du run vit dans des fichiers, pas en mémoire :

| Fichier | Rôle | Au relancement |
|---|---|---|
| `spec.md` + `.spec_approved` | Spec validée par l'humain | Étape 1 sautée (sans la sentinelle d'approbation : revalidation demandée) |
| `plan.md` | Plan de l'Architecte | Étape 2 sautée |
| `blackboard.yaml` | État des phases (`status`/`verdict`), commande de vérification, feedback | Étape 3 sautée ; phases `DONE`+`OK` sautées en production |

(« Sentinelle » : un simple fichier témoin dont la présence matérialise un signal — ici l'approbation humaine de la spec ; en production, la fin de tâche d'un agent.)

Conséquence : **relancer le script ne refait jamais ce qui est déjà validé**. Tous les cas ci-dessous reposent là-dessus. Inversement, supprimer un de ces fichiers force la régénération de l'étape correspondante.

Deux portes humaines (et seulement deux) : le y/n sur la spec (étape 1) et le y/n sur le blackboard (étape 3). Le plan n'a pas de pause dédiée : voir UC3.

---

## UC0 — Challenger le besoin (avant même la spec)

La porte la plus en amont est la moins chère de toutes : un besoin flou coûte une spec, un plan et des phases ; une question tranchée ici ne coûte rien.

1. Rédige `need.md`, puis lance `python3 Challenge-Need.py` (opt-in : aucun autre pipeline n'en dépend).
2. Un agent au contexte neuf produit `need_review.md` : ambiguïtés, contradictions, zones d'ombre, présupposés, questions à trancher — chaque point marqué `[BLOQUANT]` ou `[MINEUR]`, chaque citation du besoin vérifiée mécaniquement (une citation inventée est rejetée).
3. **Porte unique** : entérine la revue (`y`). Elle ne modifie rien : tranche les questions `[BLOQUANT]`, mets à jour `need.md` TOI-MÊME, puis relance le pipeline de ton choix (`Spec.py`, `Coding.py`…).

## UC1 — Usage normal : du besoin au code livré

1. Rédige `need.md` à la racine du projet cible (sois précis, mais c'est la validation de la spec qui verrouille le périmètre).
2. Lance `python3 Coding.py` (venv activé).
3. **Porte 1** : relis `spec.md` (sections « Hypothèses & Questions » et « Hors périmètre » en priorité), tape `y`.
4. **Porte 2** : relis le résumé du blackboard (phases, `verify_cmd`, couverture des US — user stories), tape `y`.
5. Le script enchaîne seul : scaffold exécutable (étape 0), production phase par phase (3 tentatives max chacune, verdict = code de sortie de `verify_cmd`), refactoring final re-vérifié.
6. À la fin : relis le code (jamais de boîte noire) et `refactoring_report.md`.

Suivi en direct, optionnel : `tmux attach -t <nom-de-session>` dans un autre terminal (nom affiché au lancement ; `Ctrl+B` puis `D` pour sortir sans couper l'IA). Deux projets peuvent tourner en parallèle : chaque projet a sa session tmux propre.

## UC2 — Challenger la spec

C'est **l'endroit le moins cher** pour corriger : une exigence mal comprise rejetée ici évite de payer plan + blackboard + production. Au y/n de l'étape 1 :

- **Ajustement** : édite `spec.md` directement dans un autre terminal (reformuler une US, ajouter un critère d'acceptation, durcir le « Hors périmètre »), puis tape `y`.
- **Désaccord de fond** : tape `n` (arrêt propre), précise `need.md`, supprime `spec.md`, relance. L'Agent PO régénère une spec depuis le besoin enrichi.

## UC3 — Challenger le blackboard (et le plan)

Le blackboard est une **copie mécanique** du plan : le challenger, c'est challenger les décisions de l'Architecte. C'est pourquoi le plan n'a pas de pause dédiée — le y/n blackboard couvre les deux. Deux niveaux d'intervention :

- **Petit ajustement** (corriger `verify_cmd`, retoucher une checklist, retirer un skill halluciné signalé par le script) : édite `blackboard.yaml` pendant que le prompt attend. Le script détecte l'édition, recharge, revalide la structure et redemande confirmation — tape `y` une fois satisfait·e.
- **Refonte profonde** (redécouper les phases, changer la stratégie de tests) : tape `n`, édite `plan.md` (Markdown, plus confortable que le YAML), **supprime `blackboard.yaml`**, relance. Le compilateur le régénère depuis ton plan édité ; spec et plan existants sont repris tels quels.

Aides à la décision affichées avant le y/n : anomalies structurelles (bloquantes), champs manquants non critiques, traçabilité spec → phases (US couverte par aucune phase = exigence peut-être oubliée par l'Architecte ; US référencée mais absente de la spec = hallucination probable du compilateur).

## UC4 — Des tests cassent pendant la production (régression)

Grâce au verdict universel (chaque phase = compilation + suite complète), une régression est détectée **à la phase qui l'introduit**, pas en fin de run. Ce qui se passe alors, dans l'ordre :

1. Le script **ne s'arrête pas tout de suite** : la sortie réelle du runner (tronquée début + fin) est renvoyée à l'agent codeur comme feedback, et il retente — jusqu'à **3 tentatives** par phase. La plupart des régressions se résorbent ici sans toi.
2. Après 3 échecs seulement : arrêt propre. La phase est marquée `REJECTED` dans `blackboard.yaml` avec le dernier feedback, la session tmux est tuée, et le message d'échec rappelle que les phases déjà vertes seront reprises automatiquement.

À toi de jouer — diagnostique d'abord (le `critic_feedback` dans `blackboard.yaml` contient la dernière sortie), puis choisis :

- **Le plus simple : `python3 Guided-Fix.py`** (UC11) — il fait tout ce qui suit pour toi : diagnostic IA des comportements cassés, arbitrage guidé régression/évolution, réparation sous gardes git, marqueur `FIXED` revalidé à la relance.
- **Vraie régression, le modèle bloque** : repasse sur un modèle un cran au-dessus (`/model` dans le TUI, ou le fichier de config du harness) et relance. Reprise automatique à la phase fautive, 3 tentatives fraîches.
- **Tu corriges le code toi-même** : finis la phase à la main, puis lance toi-même, depuis la racine du projet, la commande inscrite dans le champ `verify_cmd` de `blackboard.yaml` (code de sortie 0 = vert). Marque alors la phase `status: FIXED` (plus sûr que de tamponner `DONE`/`OK` à la main : MAIsterMind la revalidera par exécution à la relance, sans re-payer de codeur) — ou lance `Guided-Fix.py`, qui constate le vert, pose le marqueur et committe pour toi.
- **Le test lui-même est mauvais** (assertion fausse, test fragile) : corrige ou supprime le test toi-même — les agents ont l'interdiction d'affaiblir un test, pas toi. Si tu en supprimes, ajuste (ou retire) `last_test_count` dans `blackboard.yaml`, sinon la garde « compteur de tests non décroissant » rejettera la phase suivante à tort.

Repères pour ces éditions manuelles — voici où vivent les champs cités dans `blackboard.yaml` :

```yaml
verify_cmd: "npm test"         # commande du verdict universel (niveau racine)
last_test_count: 42            # garde « compteur de tests non décroissant »
protected_test_files:          # produits par les phases tests vertes (cf. UC8)
  - src/__tests__/cart.test.ts
phases:
  - id: 3
    name: "Calcul du panier"
    status: DONE               # TODO / IN_PROGRESS / DONE / FIXED (réparé, à revalider)
    verdict: OK                # PENDING / OK / REJECTED / PENDING_RECHECK
    critic_feedback: ""        # dernière sortie de vérification en cas d'échec
```

Cas voisins, même logique d'arrêt différé : un timeout de vérification est traité comme un incident d'infrastructure (tentative **non** consommée, re-vérification immédiate, abandon seulement après 3 timeouts persistants) ; une régression introduite par le refactoring final déclenche une boucle de correction dédiée (3 tentatives), puis un **rollback git automatique** vers l'état toutes-phases-vertes si elle échoue.

## UC5 — Reprise après interruption (Ctrl-C, crash, coupure)

Relance simplement le script : tout reprend par fichiers (voir tableau d'intro). Deux protections à connaître :

- Une spec présente mais **jamais approuvée** (run interrompu pendant le y/n) repasse par la validation humaine au lieu d'être prise pour acquise.
- Un `blackboard.yaml` corrompu (kill pendant une écriture, cas rendu rare par l'écriture atomique) provoque un arrêt propre avec consigne : le corriger ou le supprimer (il sera régénéré depuis `plan.md`).

## UC6 — Workflow deux temps : gros modèle pour penser, petit modèle pour produire

Les étapes 1 à 3 (spec, plan, blackboard) sont des one-shots à fort levier ; la production est itérative et tolère un petit modèle. La reprise par fichiers rend la bascule triviale :

1. Configure un **gros modèle** (`/model` dans le TUI, ou le fichier de config du harness), lance, valide la spec… et réponds `n` au y/n blackboard pour t'arrêter là proprement.
2. Bascule sur le **petit modèle**, relance : spec, plan et blackboard sont repris tels quels, la production démarre après ton `y`.

Pour ne payer que la spec avec le gros modèle : `Spec.py` s'arrête dès la spec approuvée — mêmes fichiers, même reprise.

## UC7 — Le scaffold (étape 0) n'aboutit pas : suspecte le modèle, pas le code

Le scaffold est la requête la plus simple du run (2-3 fichiers + une sentinelle) : c'est le **smoke test** du modèle. S'il échoue, le problème est presque toujours le tool calling (fréquent sur les petits modèles locaux : l'appel d'outil est imprimé en texte au lieu d'être exécuté). Le script affiche le dernier écran du TUI pour diagnostiquer sans t'attacher à la session ; confirme avec `tmux attach` si besoin, change de modèle, relance.

## UC8 — Arbitrer un faux positif des gardes mécaniques (git)

Les fichiers produits par une phase `tests` verte deviennent protégés : une phase `feature` qui en modifie un est rejetée et les fichiers restaurés. Faux positif connu : un helper de test légitimement partagé. Le feedback nomme les fichiers — c'est toi qui arbitres : retire le fichier de `protected_test_files` dans `blackboard.yaml` (au besoin pendant un arrêt), puis laisse le run continuer ou relance.

## UC9 — Documenter un existant avant de le faire évoluer

Tu reprends un projet (legacy, prototype validé, code hérité d'une autre équipe) et tu veux savoir ce qu'il FAIT avant d'y toucher. Lance `python3 Documentation.py` depuis sa racine (pas de `need.md` requis) :

1. Le périmètre (fichiers de code + tests) est découvert par l'orchestrateur et confirmé par un y/n avant de payer le moindre agent.
2. Un cartographe propose un découpage en zones fonctionnelles (`doc_map.yaml`) — vérifié par le script (couverture totale, zone « Divers » pour le résiduel), puis validé par toi (le YAML est éditable avant le `y` : renomme, redécoupe, réordonne — l'ordre des zones devient l'ordre de lecture).
3. Une passe de documentation par zone (contexte réinitialisé entre chaque), puis un assemblage 100 % Python produisent **`documentation.md`** à la racine : features sourcées `fichier:ligne`, tests d'acceptance Étant donné/Quand/Alors avec statut **Couvert** (un test existant les vérifie, cité) ou **Proposé** (à écrire — l'annexe de couverture en donne le décompte : c'est ton backlog de tests).

Chaînage naturel : la documentation en main, décris l'évolution dans `need.md` et enchaîne avec `Coding.py` : gros modèle jusqu'au blackboard (`n` à sa porte), petit modèle pour produire — cf. UC6. Après l'évolution, supprime les fichiers des zones touchées dans `doc_zones/` et relance : seules ces zones sont re-documentées, l'assemblage est refait.

## UC10 — Pré-auditer l'accessibilité d'une interface existante (RGAA / EAA)

Un client doit se mettre en conformité (obligation légale étendue au privé depuis juin 2025 par l'European Accessibility Act), ou tu veux chiffrer la dette d'accessibilité d'un front avant un devis de remédiation. Lance `python3 Pre-Audit-A11Y-RGAA.py` depuis la racine du projet (pas de `need.md` requis) :

1. Le périmètre UI ET le routage des 13 thématiques RGAA sont calculés par l'orchestrateur (déclencheurs regex : pas de vidéo dans le code → le pack Multimédia n'est jamais payé), puis confirmés par un y/n avant le moindre agent.
2. Un cartographe répartit les fichiers en socle / composants partagés / zones d'écrans (`a11y_map.yaml`, éditable) — le y/n de la carte affiche le décompte EXACT des passes avant de payer.
3. Une passe d'audit par (thématique × compartiment), chacune contrôlée par un parseur mécanique (verdict C/NC/NA/AVM pour CHAQUE critère du pack, constats localisés `fichier:ligne` avec **extrait exact vérifié dans les fichiers** — badge ✓ vérifié / ⚠️ à vérifier sur chaque constat), puis une agrégation 100 % Python produisent **`accessibility_pre_audit_report.md`** : taux de conformité en fourchette, non-conformités par impact (1 à 4) avec corrections, checklist des vérifications manuelles restantes — plus **`accessibility_pre_audit_summary.md`**, synthèse courte des résultats (chiffres clés, non-conformités, reste à vérifier).
4. Une passe qui n'aboutit pas après 3 tentatives ne tue pas le run : ses critères sortent en AVM prudent, le rapport porte un bandeau « Rapport PARTIEL » avec l'annexe des passes à rejouer, et la relance ne rejoue que le manquant (2 échecs consécutifs ou > 30 % d'échecs = arrêt, le modèle cale).

À savoir : c'est un PRÉ-audit statique — les critères indécidables depuis le code sont marqués AVM (à vérifier manuellement : clavier, lecteur d'écran, zoom), jamais devinés ; le rapport liste précisément cette dette de vérification. Après remédiation, `python3 Pre-Audit-A11Y-RGAA.py --rejouer-modifiees <ref>` invalide les seules passes dont un fichier apparaît dans `git diff --name-only <ref>` (sans git : supprime les fichiers des passes concernées dans `pre_audit_a11y/`) et relance : seules elles sont rejouées, l'agrégation est refaite.

## UC11 — Réparer un arrêt sur suite rouge avec arbitrage guidé (`Guided-Fix.py`)

Le run s'est arrêté (phase `REJECTED` après 3 tentatives, run tué en pleine phase, régression post-refacto non résorbée). Plutôt que la chirurgie manuelle d'UC4, lance `python3 Guided-Fix.py` depuis la racine :

1. **Verdict d'entrée puis diagnostic** : Python re-exécute `verify_cmd` — déjà vert (tu as réparé à la main ?) → il propose simplement de poser le marqueur `FIXED` sans payer d'agent ; timeout → incident d'infra signalé, aucun arbitrage à rendre. Sur rouge confirmé, l'état à l'arrêt est committé (`wip(fix)`), puis un agent écrit **`fix_report-<uid>.md`** : les échecs regroupés par **comportement métier cassé**, chacun avec ses tests rouges, le critère de spec concerné, le changement suspect (diff exact de la phase fautive) et une lecture IA.
2. **Triage** : pour chaque comportement, tu réponds dans la console — `r` (régression NON souhaitée : les tests ont raison, le code sera corrigé), `e` (évolution souhaitée : le code a raison, spec puis tests seront alignés), `o` (afficher le détail ici même). La question à te poser à chaque fois : « le critère de la spec a-t-il encore raison ? ». Un récapitulatif du plan d'action est confirmé avant de payer le moindre agent (`n` refait le triage, `q` abandonne sans rien modifier).
3. **Réparation encadrée** : évolutions d'abord — mise à jour de `spec.md` proposée par un agent et validée par toi (diff affiché, fichier éditable avant le `y` ; `n` restaure), puis adaptation des tests avec la production GELÉE par git — régressions ensuite : correction du code avec TOUS les fichiers de test GELÉS. Le verdict reste l'exécution de `verify_cmd` par Python (3 rounds max).
4. **Handshake** : au vert, la phase fautive est marquée `FIXED` — jamais `DONE` : c'est une réclamation, pas un verdict — et tout est committé, rapport compris (piste d'audit). Relance toi-même `python3 Coding.py` : il REVALIDE la phase par exécution (sans re-payer de codeur) puis poursuit le run à la phase suivante.

À savoir : chaque session produit un rapport à nom unique — l'historique de tes arbitrages survit aux relances, contrairement à `failReport.md` que MAIsterMind purge au démarrage. Si la réparation ne converge pas, le rapport documente l'échec et tes décisions : monte le modèle d'un cran et relance `Guided-Fix.py` (nouveau triage), ou corrige à la main puis relance-le (il constatera le vert et posera le marqueur sans payer d'agent).

---

## Récapitulatif : qui s'arrête, qui retente, qui reprend

| Événement | Comportement du script | Ton levier |
|---|---|---|
| Spec ou blackboard à valider | Pause y/n | Éditer le fichier avant `y`, ou `n` pour reprendre en amont |
| Vérification rouge (régression incluse) | 3 tentatives avec feedback réel, puis arrêt propre | `Guided-Fix.py` (UC11 : arbitrage guidé) ou correction manuelle (UC4), puis relance : reprise à la phase fautive |
| Timeout de vérification | Re-vérification (tentative non consommée), abandon après 3 timeouts persistants | Vérifier machine/commande, relancer |
| Scaffold muet | Arrêt + diagnostic tool calling affiché | Changer de modèle, relancer |
| Refacto qui casse la suite | Boucle de correction, puis rollback git automatique | Inspecter `refactoring_report.md` |
| Interruption (Ctrl-C, crash) | Arrêt propre (session tmux tuée) | Relancer : reprise par fichiers |
