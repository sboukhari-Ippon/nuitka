# T06 : Liens — critères RGAA 6.1 à 6.2

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Inventorie les liens : `<a href>`, composants de routage (`<Link>`, `<NavLink>`, `router-link`, `routerLink`), `role="link"`. Un « lien » sans destination (`<a>` sans `href`, `<div onClick>` navigant) est aussi un problème de Scripts (7.1/7.3) : ici, concentre-toi sur l'INTITULÉ. Le nom accessible d'un lien se calcule ainsi : `aria-labelledby` > `aria-label` > contenu (texte + `alt` des images internes) > `title`.

## Critères

### 6.1 — Chaque lien est explicite — WCAG 1.1.1 (A), 2.4.4 (A), 2.5.3 (A) — partielle
- **NC démontrable :**
  - liens « voir plus », « cliquez ici », « lire la suite », « en savoir plus » répétés sans complément programmatique (`aria-label`, texte masqué accessible, `aria-labelledby`) qui précise la destination ;
  - `aria-label` qui NE CONTIENT PAS l'intitulé visible du lien (piège commande vocale, WCAG 2.5.3) ;
  - lien image dont l'`alt` ne décrit pas la destination (« logo.png » au lieu de « Accueil »).
- **AVM :** l'explicité « dans le contexte » (phrase environnante) se juge page affichée.

### 6.2 — Chaque lien a un intitulé — WCAG 1.1.1 (A), 2.4.4 (A) — statique
- **NC si :** lien au nom accessible VIDE : `<a>` contenant seulement une icône (`<svg>`/`<i>`/icon-font) sans `aria-label`, sans texte masqué ni `title` ; lien image avec `alt=""` ; `<a></a>` vide de tout contenu.
- **Regroupe :** ce défaut se répète en série (icônes sociales, pictos d'action de listes) — un constat, toutes les localisations.
