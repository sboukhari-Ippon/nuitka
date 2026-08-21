# T11 : Formulaires — critères RGAA 11.1 à 11.13

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Le pack le plus rentable : c'est dans les formulaires que se concentrent les blocages réels. Inventorie chaque champ (`<input>`, `<select>`, `<textarea>`, contrôles custom `role="textbox|combobox|checkbox|radio|switch|slider"`) et chaque bouton. Pour un champ : qui le nomme, comment l'erreur revient, l'obligatoire est-il indiqué ? En React/Vue/Angular, suis le composant de champ du design system : s'il est dans tes fichiers, audite-le ; sinon audite ses USAGES (props d'étiquette transmises ?).

## Critères

### 11.1 — Chaque champ de formulaire a une étiquette — WCAG 1.3.1 (A), 2.4.6 (AA), 3.3.2 (A), 4.1.2 (A) — statique
- **NC si :** champ sans `<label for>` correspondant à son `id`, ni `aria-label`, ni `aria-labelledby`, ni `title` ; `label for` orphelin (id inexistant ou dupliqué) ; `placeholder` utilisé comme SEULE étiquette (il disparaît à la saisie : NC).
- **Frameworks :** `htmlFor` (React) ; champ custom sans prop de label transmise.

### 11.2 — Chaque étiquette est pertinente — WCAG 2.4.6 (AA), 2.5.3 (A), 3.3.2 (A) — partielle
- **NC démontrable :** étiquettes génériques (« champ », « valeur », « input »), identiques pour des champs différents, `aria-label` ne CONTENANT PAS l'intitulé visible accolé (WCAG 2.5.3).
- **AVM :** la pertinence fine se juge en contexte.

### 11.3 — Les étiquettes de même fonction sont cohérentes entre pages — WCAG 3.2.4 (AA) — partielle
- **NC démontrable :** dans TES fichiers, un même champ fonctionnel étiqueté différemment (« E-mail » ici, « Courriel » là, « Adresse électronique » ailleurs).
- **AVM :** signale en AVM — la vérification inter-zones N'EST PAS automatisée (aucun contrôle croisé entre compartiments) : c'est une limite documentée de l'audit, à couvrir manuellement.

### 11.4 — Chaque étiquette et son champ sont accolés — WCAG 3.3.2 (A) — partielle
- **NC démontrable :** étiquette et champ dans des conteneurs éloignés du DOM (colonnes séparées) ; ordre inversé sans raison : l'étiquette se place AVANT le champ (au-dessus ou à gauche), APRÈS pour les cases à cocher et boutons radio.
- **AVM :** l'accolement VISUEL réel dépend de la CSS.

### 11.5 — Les champs de même nature sont regroupés, si nécessaire — WCAG 1.3.1 (A), 3.3.2 (A) — partielle
- **NC démontrable :** groupes évidents non regroupés : série de boutons radio d'une même question sans `<fieldset>` ni `role="radiogroup"` ; bloc adresse/identité sans `<fieldset>` ni `role="group"` quand plusieurs blocs se ressemblent.
- **NA si** formulaires courts sans ambiguïté (un seul groupe naturel).

### 11.6 — Chaque regroupement de champs a une légende — WCAG 1.3.1 (A), 3.3.2 (A) — statique
- **NC si :** `<fieldset>` sans `<legend>` ; `role="group"/"radiogroup"` sans `aria-label`/`aria-labelledby`.
- **NA si** aucun regroupement (mais vérifie 11.5 d'abord).

### 11.7 — Chaque légende de regroupement est pertinente — WCAG 1.3.1 (A), 3.3.2 (A) — partielle
- **NC démontrable :** légendes génériques ou placeholders. **AVM sinon** ; **NA si** aucun regroupement.

### 11.8 — Les items de même nature d'une liste de choix sont regroupés — WCAG 1.3.1 (A) — statique
- **NC si :** `<select>` long mélangeant des familles d'options sans `<optgroup>` ; `<optgroup>` sans attribut `label` ; `label` d'optgroup non pertinent.
- **NA si** selects courts et homogènes.

### 11.9 — L'intitulé de chaque bouton est pertinent — WCAG 2.5.3 (A), 4.1.2 (A) — statique
- **NC si :** bouton SANS nom accessible : `<button>` contenant seulement une icône sans `aria-label` ; `<input type="submit">` sans `value` ; bouton custom sans nom ; `aria-label` ne contenant pas l'intitulé visible.
- **Partielle :** intitulés vagues (« OK », « Envoyer » ambigu quand plusieurs formulaires) → signale, la pertinence fine est AVM.

### 11.10 — Le contrôle de saisie est utilisé de manière pertinente — WCAG 3.3.1 (A), 3.3.2 (A) — statique
- **NC si :**
  - champs obligatoires sans indication : ni mention visible (astérisque + légende, « obligatoire ») ni `required`/`aria-required="true"` — ou l'inverse : marqueurs visuels sans attributs ;
  - messages d'erreur NON reliés au champ : pas d'`aria-describedby` vers le message, pas d'`aria-invalid="true"` sur le champ en erreur ;
  - indication de format attendue absente AVANT la saisie quand un format est exigé (date, mot de passe à contraintes).

### 11.11 — Le contrôle de saisie est accompagné de suggestions de correction — WCAG 3.3.3 (AA) — partielle
- **NC démontrable :** messages d'erreur du code secs et non informatifs (« champ invalide », « erreur ») sans type/format attendu ni exemple, alors qu'une correction est possible.
- **NA si** aucune validation avec messages dans tes fichiers.

### 11.12 — Les données financières, juridiques ou d'examen sont modifiables, vérifiables ou récupérables — WCAG 3.3.4 (AA) — partielle
- **NC démontrable :** action irréversible (paiement, suppression de données, envoi définitif) déclenchée par soumission directe sans étape de vérification/récapitulatif NI confirmation NI possibilité d'annulation repérable dans le code.
- **NA si** aucun formulaire à enjeu de ce type.

### 11.13 — La finalité des champs peut être déduite (remplissage automatique) — WCAG 1.3.5 (AA) — statique
- **NC si :** champs se rapportant à l'UTILISATEUR (nom, prénom, e-mail, téléphone, adresse, pays, code postal, date de naissance, numéro de carte) SANS attribut `autocomplete` approprié (`given-name`, `family-name`, `email`, `tel`, `street-address`, `postal-code`, `bday`, `cc-number`…).
- **NA si** aucun champ de données personnelles.
