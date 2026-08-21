# T08 : Éléments obligatoires — critères RGAA 8.1 à 8.10

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Sur le SOCLE : audite le document racine (doctype, `<html lang>`, `<title>`). Sur une ZONE d'application monopage : le document racine est hors de tes fichiers — concentre-toi sur la gestion du titre PAR ÉCRAN (8.5/8.6 : `document.title`, Helmet, `useTitle`…), les changements de langue (8.7/8.8) et les balises détournées (8.9). Déclare NA ce qui relève du document racine absent de ta passe.

## Critères

### 8.1 — Chaque page est définie par un type de document — WCAG 4.1.1 (A) — statique
- **NC si :** `<!DOCTYPE html>` absent, invalide, ou placé APRÈS la balise `<html>` dans le document racine. **NA** hors socle.

### 8.2 — Le code source généré est valide — WCAG 4.1.1 (A), 4.1.2 (A) — partielle
- **NC démontrable :** invalidités flagrantes lisibles : `id` dupliqués dans un même document/composant, imbrications interdites (`<button>` dans `<a>`, `<div>` dans `<span>`, `<li>` hors liste), attributs ARIA inexistants ou mal orthographiés (`aria-lable`), balises non fermées.
- **AVM :** la validation W3C complète exige le HTML généré.

### 8.3 — La langue par défaut est présente — WCAG 3.1.1 (A) — statique
- **NC si :** `<html>` sans attribut `lang` (ni `xml:lang`) dans le document racine. **NA** hors socle (mais signale un `lang` posé dynamiquement s'il existe : c'est lui qui fait foi).

### 8.4 — Le code de langue par défaut est pertinent — WCAG 3.1.1 (A) — statique
- **NC si :** `lang` invalide (`lang="french"`, `lang=""`) ou manifestement incohérent avec la langue du contenu (interface entièrement en français avec `lang="en"` — cas très courant sur les templates).

### 8.5 — Chaque page a un titre de page — WCAG 2.4.2 (A) — statique
- **NC si :** `<title>` absent du document racine ; en application monopage : AUCUN mécanisme de titre par écran (ni `document.title`, ni Helmet/`useTitle`/router `meta.title`) dans tout le projet — le titre reste alors figé pour toutes les pages.

### 8.6 — Le titre de page est pertinent — WCAG 2.4.2 (A) — partielle
- **NC démontrable :** `<title>` générique d'échafaudage (« React App », « Vite App », « Document », « Untitled », nom du framework) ; application multi-écrans dont le titre n'est jamais mis à jour (tous les écrans portent le même titre).
- **AVM :** la pertinence fine (le titre reflète-t-il le contenu ET le site ?) se juge par page.

### 8.7 — Chaque changement de langue est indiqué — WCAG 3.1.2 (AA) — partielle
- **NC démontrable :** passages entiers dans une autre langue que la langue par défaut (slogans, citations, libellés anglais dans une interface française) sans attribut `lang` sur leur conteneur. Hors cas particuliers : noms propres, termes passés dans l'usage.
- **AVM :** l'inventaire exhaustif exige de lire tout le contenu rendu.

### 8.8 — Le code de langue de chaque changement de langue est valide et pertinent — WCAG 3.1.2 (AA) — statique
- **NC si :** attributs `lang` internes invalides (`lang="anglais"`) ou incohérents avec le texte qu'ils couvrent. **NA si** aucun changement de langue balisé.

### 8.9 — Les balises ne sont pas utilisées uniquement à des fins de présentation — WCAG 1.3.1 (A) — statique
- **NC si :** balises sémantiques détournées pour leur rendu : `<h*>` pour grossir un texte qui n'est pas un titre, `<blockquote>` pour indenter, `<fieldset>` pour encadrer hors formulaire, `<br>` en série pour espacer, `<table>` de mise en page (recoupe 5.8 : mentionne, ne double pas le constat détaillé).

### 8.10 — Les changements du sens de lecture sont signalés — WCAG 1.3.2 (A) — statique
- **NC si :** texte en langue à lecture droite-vers-gauche (arabe, hébreu…) sans attribut `dir="rtl"` sur son conteneur (avec un `lang` cohérent). **NA si** aucun contenu bidirectionnel (cas le plus fréquent).
