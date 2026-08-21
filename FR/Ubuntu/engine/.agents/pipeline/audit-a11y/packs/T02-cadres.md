# T02 : Cadres — critères RGAA 2.1 à 2.2

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Inventorie chaque `<iframe>` et `<frame>` de tes fichiers, y compris ceux générés par des composants (players vidéo intégrés, cartes, widgets de paiement, contenus tiers). Deux questions seulement : le cadre a-t-il un `title` ? Ce `title` dit-il ce qu'on trouve dans le cadre ?

## Critères

### 2.1 — Chaque cadre a un titre de cadre — WCAG 4.1.2 (A) — statique
- **Où :** attribut `title` sur chaque `<iframe>`/`<frame>` ; en JSX : prop `title` (souvent oubliée sur les embeds YouTube/Maps/Stripe).
- **NC si :** au moins un cadre sans `title`. Regroupe les occurrences (un constat, toutes les localisations).
- **NA si** aucun cadre dans tes fichiers.

### 2.2 — Chaque titre de cadre est pertinent — WCAG 4.1.2 (A) — partielle
- **NC démontrable :** `title` générique ou technique : `title="iframe"`, `title="frame"`, `title="embed"`, identifiant brut, URL recopiée, `title` identique pour des cadres au contenu différent.
- **AVM sinon :** la pertinence (le titre annonce-t-il vraiment le contenu du cadre ?) se confirme en consultant la page ; attendu : « Vidéo de présentation », « Carte d'accès », « Module de paiement ».
