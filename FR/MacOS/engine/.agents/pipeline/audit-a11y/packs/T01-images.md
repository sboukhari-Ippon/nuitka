# T01 : Images — critères RGAA 1.1 à 1.9

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Commence par inventorier les images de tes fichiers : `<img>`, `<svg>`, `<canvas>`, `<area>`, `<input type="image">`, `<object>`/`<embed>` de type image, `role="img"`, icônes de bibliothèques (`<Icon>`, `<FontAwesomeIcon>`…), images CSS (`background-image`). Classe chacune : porteuse d'information (elle apporte quelque chose que le texte voisin ne dit pas) ou décorative. Cette classification pilote 1.1 vs 1.2.

## Critères

### 1.1 — Chaque image porteuse d'information a une alternative textuelle — WCAG 1.1.1 (A) — statique
- **Où :** `alt` sur `<img>`/`<area>`/`<input type="image">` ; `aria-label`/`aria-labelledby` sur `role="img"` ; `<svg>` informatif : `<title>` référencé ou `aria-label` + `role="img"` ; `<canvas>` : contenu alternatif entre les balises ; `<object>`/`<embed>` : alternative interne ou adjacente.
- **NC si :** image informative sans AUCUN de ces mécanismes ; en JSX/Vue : prop `alt` absente, `alt={undefined}`, icône-bouton sans nom accessible.
- **Défaut :** une image sans alternative NI marquage décoratif est NC ici ; nature (info/déco) indécidable → AVM.

### 1.2 — Chaque image de décoration est correctement ignorée par les technologies d'assistance — WCAG 1.1.1 (A), 4.1.2 (A) — statique
- **Où :** image décorative : `alt=""` (et ni `title` ni `aria-label*`), ou `aria-hidden="true"`, ou `role="presentation"/"none"` ; `<svg>` décoratif : `aria-hidden="true"`.
- **NC si :** image manifestement décorative (pictogramme redondant avec le texte accolé, image de fond posée en `<img>`) avec un `alt` verbeux, un `title`, ou sans neutralisation ; icônes décoratives de bibliothèques sans `aria-hidden`.

### 1.3 — L'alternative textuelle des images porteuses d'information est pertinente — WCAG 1.1.1 (A), 4.1.2 (A) — partielle
- **NC démontrable :** `alt` égal au nom de fichier (`alt="IMG_0123.png"`), générique (`alt="image"`, `alt="photo"`, `alt="icon"`), identique pour des images différentes, ou visiblement du placeholder (`alt="TODO"`, lorem ipsum).
- **AVM sinon :** la pertinence réelle (l'alternative rend-elle l'information de l'image ?) se juge image affichée sous les yeux. Signale les `alt` > ~80 caractères (une alternative doit rester courte, cf. 1.6 pour la description longue).

### 1.4 — L'alternative d'une image CAPTCHA ou image-test identifie sa nature et sa fonction — WCAG 1.1.1 (A) — partielle
- **NA si** aucun CAPTCHA/image-test dans tes fichiers (cas le plus fréquent, motifs : `captcha`, `recaptcha`, `hcaptcha`, images-défis).
- **NC si :** CAPTCHA image dont l'alternative décrit le CONTENU (la solution !) ou ne dit pas que c'est un test ; attendu : « code de vérification » ou équivalent.

### 1.5 — Chaque CAPTCHA image propose une solution d'accès alternative — WCAG 1.1.1 (A) — partielle
- **NA si** aucun CAPTCHA. **NC si :** CAPTCHA visuel sans variante non visuelle repérable dans le code (version audio, question logique, mécanisme alternatif).

### 1.6 — Chaque image porteuse d'information a, si nécessaire, une description détaillée — WCAG 1.1.1 (A) — partielle
- **Concerne :** images complexes (graphiques, diagrammes, schémas, infographies) dont le sens ne tient pas dans un `alt` court.
- **NC démontrable :** graphique/diagramme identifiable (bibliothèque de charts, `<canvas>` de dataviz, nom de fichier « chart/graph/diagram ») sans `aria-describedby`, sans lien « description détaillée », sans texte adjacent équivalent.
- **AVM :** « si nécessaire » se juge en voyant l'image ; signale les candidats.

### 1.7 — La description détaillée est pertinente — WCAG 1.1.1 (A) — manuelle
- **AVM par défaut** (juger une description exige de voir l'image). **NC flagrant :** description détaillée vide, placeholder, ou sans rapport (copiée-collée d'une autre image).

### 1.8 — Chaque image texte est remplacée par du texte stylé quand c'est possible — WCAG 1.4.5 (AA) — partielle
- **NC démontrable :** images contenant visiblement du texte reconstructible en CSS : bannières-titres en PNG, boutons-images avec libellé, nom de fichier « title/btn/text/banner » + contexte. Hors cas particuliers : logos exemptés.
- **AVM :** le contenu réel d'une image ne se voit pas dans le code ; signale les suspects.

### 1.9 — Chaque légende d'image est correctement reliée à son image — WCAG 1.1.1 (A), 4.1.2 (A) — statique
- **Où :** image + légende adjacente : attendu `<figure>` + `<figcaption>` (avec `role="figure"`/`aria-label` si nécessaire pour lier).
- **NC si :** motif visuel de légende (texte de crédit/explication accolé sous une image, classes `caption`, `legend`) sans structure `<figure>/<figcaption>`.
- **NA si** aucune image légendée.
