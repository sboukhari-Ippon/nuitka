# T09 : Structuration de l'information — critères RGAA 9.1 à 9.4

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
La structure est ce que « voit » un lecteur d'écran : titres, régions, listes, citations. Sur le SOCLE : audite l'ossature (landmarks du layout). Sur une ZONE : audite la structure de l'écran (son titre principal, ses sous-titres, ses listes). L'ABSENCE de structure est le constat le plus fréquent — un fichier sans le moindre `<h*>` ni landmark alors qu'il rend un écran entier est un signal fort.

## Critères

### 9.1 — L'information est structurée par l'utilisation appropriée de titres — WCAG 1.3.1 (A), 2.4.1 (A), 2.4.6 (AA), 4.1.2 (A) — statique
- **NC si :**
  - un écran entier sans AUCUN titre (`<h1>`-`<h6>` ou `role="heading"` + `aria-level`) ;
  - hiérarchie incohérente : sauts de niveaux (h2 puis h4), plusieurs h1 concurrents dans un même document, niveaux choisis pour la taille visuelle ;
  - texte jouant visuellement le rôle de titre (div/p en gros + gras via classes « title », « heading ») sans balise de titre.
- **AVM :** la pertinence du CONTENU des titres se juge en contexte.

### 9.2 — La structure du document est cohérente — WCAG 1.3.1 (A) — statique
- **Attendu (socle surtout) :** `<header>` (banner), `<nav>`, `<main>` UNIQUE, `<footer>` (contentinfo) ; les équivalents `role="banner"/"navigation"/"main"/"contentinfo"` valent.
- **NC si :** pas de `<main>` du tout ; plusieurs `<main>` ; contenu principal hors de toute région ; `<header>`/`<footer>` de page absents alors que le layout les rend en `<div>`.

### 9.3 — Chaque liste est correctement structurée — WCAG 1.3.1 (A) — statique
- **NC si :** suites d'éléments de même nature rendues SANS structure de liste : items en `<div>`/`<p>` empilés (menus, résultats, cartes produits), puces simulées (`•`, `-`, `*` en texte), `<br>` séparateurs ; listes ARIA incomplètes (`role="list"` sans `role="listitem"`).
- **Attendu :** `<ul>`/`<ol>` + `<li>` ; `<dl>`/`<dt>`/`<dd>` pour les paires terme/description.

### 9.4 — Chaque citation est correctement indiquée — WCAG 1.3.1 (A) — statique
- **NC si :** citation identifiable (témoignage, verbatim, classes « quote », guillemets typographiques encadrant un passage attribué) sans `<q>` (en ligne) ni `<blockquote>` (bloc).
- **NA si** aucune citation dans tes fichiers (fréquent).
