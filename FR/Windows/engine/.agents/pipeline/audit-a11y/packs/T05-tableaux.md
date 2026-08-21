# T05 : Tableaux — critères RGAA 5.1 à 5.8

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Classe chaque `<table>` (et `role="table"/"grid"`) de tes fichiers : tableau de DONNÉES (l'information dépend du croisement ligne/colonne) ou tableau de MISE EN FORME (layout, rare aujourd'hui). Les composants de type DataGrid/DataTable comptent : audite le HTML qu'ils émettent quand il est visible dans le code, sinon l'usage (props d'en-têtes, caption).

## Critères

### 5.1 — Chaque tableau de données complexe a un résumé — WCAG 1.3.1 (A) — partielle
- **Complexe =** en-têtes sur plusieurs niveaux (`colspan`/`rowspan` dans les en-têtes, doubles entrées imbriquées).
- **NC démontrable :** tableau complexe sans résumé (contenu de `<caption>` détaillé ou `aria-describedby` vers un texte).
- **NA si** aucun tableau complexe.

### 5.2 — Le résumé du tableau complexe est pertinent — WCAG 1.3.1 (A) — manuelle
- **AVM par défaut** ; **NC flagrant :** résumé placeholder ou sans rapport.

### 5.3 — Le contenu linéarisé d'un tableau de mise en forme reste compréhensible — WCAG 1.3.2 (A), 4.1.2 (A) — partielle
- **NC démontrable :** table de layout dont l'ordre des cellules casse manifestement l'ordre de lecture (contenus coupés en colonnes) ; table de layout sans `role="presentation"`.
- **NA si** aucun tableau de mise en forme.

### 5.4 — Le titre d'un tableau de données est correctement associé — WCAG 1.3.1 (A) — statique
- **NC si :** tableau de données précédé d'un titre visible (`<h*>`, `<p>` en gras, div) NON associé : attendu `<caption>` (ou `aria-labelledby` pointant le titre).
- **NA si** tableaux sans titre visible (alors vérifie 5.5 en NA aussi).

### 5.5 — Le titre du tableau de données est pertinent — WCAG 1.3.1 (A) — partielle
- **NC démontrable :** `<caption>` générique (« Tableau », « Données », identifiant technique). Sinon AVM.

### 5.6 — Chaque en-tête de colonne et de ligne est correctement déclaré — WCAG 1.3.1 (A) — statique
- **NC si :** première ligne/colonne manifestement d'en-têtes codée en `<td>` (ou divs stylées) au lieu de `<th>` (ou `role="columnheader"/"rowheader"`).
- **Vérifie :** cellules d'en-têtes partiels (`<th>` en milieu de tableau) et cellules multi-en-têtes structurées en `<td>`/`<th>` correctement.

### 5.7 — La technique d'association cellules/en-têtes appropriée est utilisée — WCAG 1.3.1 (A) — statique
- **NC si :** tableau à double entrée dont les `<th>` n'ont pas de `scope` (`col`/`row`) ; tableau complexe sans mécanisme `headers`/`id` cohérent (ids référencés inexistants = NC franc) ; `scope` posé sur des `<td>`.
- **Règle simple :** tableau simple → `scope` ; tableau complexe → `headers`/`id`.

### 5.8 — Un tableau de mise en forme n'utilise aucun élément propre aux tableaux de données — WCAG 1.3.1 (A) — statique
- **NC si :** table de layout contenant `<th>`, `<caption>`, `summary`, `scope`, `headers`, ou sans `role="presentation"` alors qu'elle est purement présentationnelle.
- **NA si** aucun tableau de mise en forme.
