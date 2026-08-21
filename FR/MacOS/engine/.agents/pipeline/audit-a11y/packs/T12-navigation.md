# T12 : Navigation — critères RGAA 12.1 à 12.11

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Sur le SOCLE : les dispositifs globaux (menu, recherche, plan du site, lien d'évitement, layout stable). Sur une ZONE : l'ordre de tabulation de l'écran, les pièges clavier de ses widgets, ses raccourcis. Les critères « ensemble de pages » (12.1 à 12.5) se jugent surtout au niveau du socle : déclare-les NA dans les zones si le dispositif est porté par le layout global.

## Critères

### 12.1 — Chaque ensemble de pages dispose d'au moins deux systèmes de navigation — WCAG 2.4.5 (AA) — partielle
- **Attendus (2 parmi 3) :** menu de navigation, moteur de recherche, plan du site.
- **NC démontrable (socle) :** un seul système repérable dans tout le projet (menu seul, sans recherche ni plan).
- **AVM :** un dispositif peut vivre hors du code fourni (recherche déportée).

### 12.2 — Le menu et les barres de navigation sont toujours à la même place — WCAG 3.2.3 (AA) — partielle
- **C probable (socle) :** navigation rendue par un layout PARTAGÉ unique.
- **NC démontrable :** navigations redéfinies écran par écran avec structures/ordres différents.

### 12.3 — La page « plan du site » est pertinente — WCAG 2.4.5 (AA) — partielle
- **NC démontrable :** plan du site avec liens morts (routes inexistantes dans le code) ou manifestement non représentatif. **NA si** aucun plan du site (voir 12.1).

### 12.4 — Le plan du site est atteignable de manière identique — WCAG 2.4.5 (AA), 3.2.3 (AA) — partielle
- **C probable :** lien « Plan du site » dans le pied de page du layout partagé. **NA si** aucun plan du site.

### 12.5 — Le moteur de recherche est atteignable de manière identique — WCAG 3.2.3 (AA) — partielle
- **C probable :** recherche dans l'en-tête du layout partagé. **NA si** aucun moteur de recherche.

### 12.6 — Les zones de regroupement peuvent être atteintes ou évitées — WCAG 1.3.1 (A), 2.4.1 (A), 4.1.2 (A) — statique
- **Attendu :** en-tête, navigation principale, contenu principal, pied de page (et recherche) balisés en landmarks (`<header>`, `<nav>`, `<main>`, `<footer>`, `role="search"`) ET/OU accessibles par liens d'évitement ET/OU titrés.
- **NC si :** aucune de ces trois techniques pour une zone présente (layout tout en `<div>` sans roles ni skip links). Recoupe 9.2 : ici on juge l'ATTEIGNABILITÉ, pas seulement la sémantique.

### 12.7 — Un lien d'évitement ou d'accès rapide au contenu principal est présent — WCAG 2.4.1 (A), 2.4.3 (A), 3.2.3 (AA) — statique
- **NC si (socle) :** aucun lien « Aller au contenu » en tout début de document ; lien présent mais cible `#` inexistante ; lien masqué qui ne réapparaît PAS au focus (`sr-only` sans variante `:focus`).
- **Attendu :** premier élément focusable, visible au focus, pointant vers le conteneur principal.

### 12.8 — L'ordre de tabulation est cohérent — WCAG 2.4.3 (A) — partielle
- **NC démontrable :** `tabindex` POSITIFS (> 0) qui court-circuitent l'ordre naturel ; DOM réordonné visuellement (cf. 10.3) avec éléments interactifs ; `autofocus` placé sur un élément en fin de parcours.
- **AVM :** l'ordre effectif se vérifie au clavier sur la page rendue.

### 12.9 — La navigation ne contient pas de piège au clavier — WCAG 2.1.1 (A), 2.1.2 (A) — partielle
- **NC démontrable :** surcouche (modale, panneau) qui capte le focus SANS issue clavier : pas d'écouteur Échap, pas de bouton de fermeture focusable ; `keydown` qui `preventDefault()` la touche Tab sans gestion de cycle.
- **AVM :** widgets tiers embarqués (leur comportement clavier n'est pas dans ton code).

### 12.10 — Les raccourcis clavier à touche unique sont contrôlables — WCAG 2.1.4 (A) — statique
- **NC si :** écouteur global déclenchant une action sur une TOUCHE SEULE (lettre, chiffre, ponctuation) sans modificateur, actif hors champs de saisie, sans mécanisme pour le désactiver ou le remapper.
- **NA si** aucun raccourci à touche unique.

### 12.11 — Les contenus additionnels au survol/focus/activation sont atteignables au clavier — WCAG 2.1.1 (A) — statique
- **NC si :** contenu révélé par des événements souris uniquement (`mouseover`/`mouseenter` sans `focus`, tooltips JS sans déclenchement clavier). Versant script de 10.14 (qui couvre la CSS pure) : ne double pas un constat déjà porté là-bas si tu audites les deux.
- **NA si** aucun contenu additionnel scripté.
