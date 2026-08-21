# T07 : Scripts — critères RGAA 7.1 à 7.5

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Le pack décisif pour les applications JS (React, Vue, Angular, vanilla). Inventorie les composants d'interface PILOTÉS par script : modales, menus, onglets, accordéons, autocomplétions, carrousels, toasts, tout élément avec gestionnaire d'événement. Pour chacun, trois questions : a-t-il un nom, un rôle et ses états exposés (7.1) ? Est-il utilisable au clavier (7.3) ? Ses messages sont-ils annoncés (7.5) ?

## Critères

### 7.1 — Chaque script est, si nécessaire, compatible avec les technologies d'assistance — WCAG 2.5.3 (A), 4.1.2 (A) — statique
- **NC si :** composant interactif custom SANS le motif ARIA attendu :
  - contrôle cliquable en `<div>`/`<span>` sans `role` ni nom accessible ;
  - modale sans `role="dialog"` + `aria-modal="true"` + titre lié ;
  - accordéon/menu déroulant sans `aria-expanded` sur son déclencheur ;
  - onglets sans `role="tablist"/"tab"/"tabpanel"` + `aria-selected` ;
  - case/interrupteur custom sans `role="checkbox"/"switch"` + `aria-checked` ;
  - état visuel (actif, sélectionné, désactivé) rendu uniquement par une classe CSS sans propriété ARIA.
- **Privilégie le natif :** `<button>`, `<details>`, `<dialog>` bien employés valent C.

### 7.2 — Pour chaque script ayant une alternative, cette alternative est pertinente — WCAG 1.1.1 (A), 4.1.2 (A) — partielle
- **Concerne :** `<noscript>` et contenus de repli. **NC démontrable :** `<noscript>` vide ou « activez JavaScript » sans accès alternatif au contenu essentiel. **NA si** aucune alternative fournie (fréquent et toléré : c'est 7.1/7.3 qui portent l'exigence).

### 7.3 — Chaque script est contrôlable par le clavier et par tout dispositif de pointage — WCAG 1.3.1 (A), 2.1.1 (A), 2.4.7 (AA) — statique
- **NC si :**
  - élément NON nativement focusable (`<div>`, `<span>`, `<li>`, `<svg>`…) avec `onClick`/`@click` mais SANS `tabindex="0"` NI gestion `keydown/keyup` (Entrée + Espace) ;
  - interactions uniquement à la souris : `onMouseOver`/`onDoubleClick`/drag sans équivalent clavier ou point d'entrée alternatif ;
  - `tabindex` positif (> 0) qui casse l'ordre naturel (recoupe 12.8 : signale-le ici seulement si c'est le script qui l'injecte) ;
  - appel `blur()` ou suppression du focus d'un élément qui vient de le recevoir.
- **Bon réflexe framework :** un `onClick` sur `<button>`/`<a href>` natif est C pour la focusabilité.

### 7.4 — Chaque changement de contexte initié par script est annoncé ou contrôlé par l'utilisateur — WCAG 3.2.1 (A), 3.2.2 (A) — statique
- **NC si :** navigation ou soumission déclenchée par un simple `onChange`/`onInput` (select « aller à », auto-submit du dernier champ, redirection à la saisie) sans bouton de validation ni avertissement préalable.
- **NA si** aucun changement de contexte scripté.

### 7.5 — Les messages de statut sont correctement restitués — WCAG 4.1.3 (AA) — statique
- **NC si :** contenu inséré dynamiquement pour informer SANS attribut d'annonce :
  - confirmation/succès (toast « enregistré ») sans `role="status"` ou `aria-live="polite"` ;
  - erreur/avertissement sans `role="alert"` (ou `aria-live="assertive"`) ;
  - progression (« chargement », compteur de résultats, spinner) sans `role="status"/"progressbar"/"log"`.
- **NA si** aucun message de statut dynamique dans tes fichiers.
