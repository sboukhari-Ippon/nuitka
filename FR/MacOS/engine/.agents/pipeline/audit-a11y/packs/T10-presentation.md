# T10 : Présentation de l'information — critères RGAA 10.1 à 10.14

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Le pack CSS. Beaucoup de ses critères se jugent au rendu (zoom, reflow, désactivation des styles) : ton rôle est d'attraper les violations DÉMONTRABLES dans les feuilles de style et les styles inline — `outline: none`, viewport verrouillé, contenus masqués incohérents, menus au survol pur — et de baliser précisément le reste en AVM.

## Critères

### 10.1 — Les feuilles de styles contrôlent la présentation — WCAG 1.3.1 (A), 1.3.2 (A) — statique
- **NC si :** balisage de présentation dans le HTML : attributs `align`, `bgcolor`, `border` (hors tableaux de données), `width`/`height` de mise en page sur des éléments non remplacés ; balises `<font>`, `<center>`, `<big>`, `<u>` ; espaces détournés (mots espacés lettre à lettre « T I T R E », colonnes simulées par `&nbsp;`).

### 10.2 — Le contenu visible reste présent quand les styles sont désactivés — WCAG 1.1.1 (A), 1.3.1 (A) — partielle
- **NC démontrable :** information portée UNIQUEMENT par la CSS : `content:` (pseudo-éléments) véhiculant du texte informatif, information donnée par une image de fond CSS sans équivalent texte.
- **AVM :** vérification complète = page affichée styles coupés.

### 10.3 — L'information reste compréhensible quand les styles sont désactivés — WCAG 1.3.2 (A), 2.4.3 (A) — partielle
- **NC démontrable :** ordre du DOM manifestement différent de l'ordre de lecture visuel : usages massifs de `order:` (flex/grid) ou `position: absolute` pour REPLACER des blocs de sens (le DOM dit A-C-B, l'écran montre A-B-C).
- **AVM sinon.**

### 10.4 — Le texte reste lisible à 200 % de zoom — WCAG 1.4.4 (AA) — partielle
- **NC démontrable :** `<meta name="viewport">` avec `user-scalable=no` ou `maximum-scale=1` (interdit d'agrandir : NC franc) ; conteneurs de texte à hauteur FIXE en px avec `overflow: hidden` (texte coupé au zoom).
- **AVM :** le comportement réel au zoom se constate au rendu.

### 10.5 — Les déclarations CSS de couleur de police et de fond vont par paires — WCAG 1.4.3 (AA) — partielle
- **NC démontrable :** règle qui pose `color` sur un composant de texte sans AUCUN `background`/`background-color` déclaré dans le composant ou son parent direct visible dans tes fichiers (le texte hérite alors d'un fond imprévisible), et réciproquement ; image de fond (`background-image`) sous du texte sans couleur de fond de secours.
- **AVM :** l'héritage réel à travers l'arbre complet.

### 10.6 — Chaque lien dont la nature n'est pas évidente est visible autrement que par la couleur — WCAG 1.4.1 (A) — statique
- **NC si :** liens dans du texte courant avec `text-decoration: none` sans AUCUN autre différenciateur permanent (soulignement, graisse, bordure, icône) — la couleur seule ne suffit pas, même contrastée, sans indication complémentaire au survol ET au focus.
- **NA si** tous les liens sont hors texte courant (menus, boutons : nature évidente par la position).

### 10.7 — La prise de focus est visible pour chaque élément qui le reçoit — WCAG 1.4.1 (A), 2.4.7 (AA) — statique
- **NC si :** `outline: none`/`outline: 0` (ou `:focus { outline: ... }` supprimé) SANS style `:focus`/`:focus-visible` de remplacement au moins équivalent. Cherche les resets globaux (`*:focus`, `button:focus`) : c'est LE grand classique.
- **C possible :** reset accompagné d'un `:focus-visible` net (bordure, ombre, contour).

### 10.8 — Les contenus cachés ont vocation à être ignorés par les technologies d'assistance — WCAG 1.3.2 (A), 4.1.2 (A) — statique
- **NC si :** contenu PORTEUR d'information visible masqué aux TA : `aria-hidden="true"` sur des blocs visibles (icônes porteuses, textes) ; ou l'inverse : contenu masqué à tort restitué (menus fermés en simple `left: -9999px` restant lisibles quand ils ne devraient pas… juge à l'intention).
- **Motifs légitimes :** classes `sr-only`/`visually-hidden` POUR les TA ; `display:none` d'un contenu réellement inactif.

### 10.9 — L'information n'est pas donnée uniquement par la forme, la taille ou la position — WCAG 1.3.3 (A), 1.4.1 (A) — partielle
- **NC démontrable :** consignes textuelles s'appuyant sur la seule position ou forme : « cliquez sur le bouton de droite », « voir l'encadré ci-dessous » (sans lien), « le bouton rond ».
- **AVM :** signalétique visuelle réelle (juger au rendu).

### 10.10 — La règle 10.9 est implémentée de façon pertinente — WCAG 1.3.3 (A), 1.4.1 (A) — manuelle
- **AVM par défaut** ; **NA si** 10.9 est NA.

### 10.11 — Le contenu se présente sans défilement horizontal à 320 px de large — WCAG 1.4.10 (AA) — partielle
- **NC démontrable :** largeurs FIXES > 320 px sur des conteneurs de contenu (`width: 960px` sans media query ni `max-width`), viewport figé (`content="width=1024"`), tableaux de mise en page larges.
- **AVM :** le reflow réel se constate fenêtre réduite.

### 10.12 — Les propriétés d'espacement du texte peuvent être redéfinies sans perte — WCAG 1.4.12 (AA) — partielle
- **NC démontrable :** `line-height`, `letter-spacing` ou `word-spacing` verrouillés avec `!important` dans des valeurs inférieures aux minima utilisateur ; conteneurs de texte à hauteur fixe + `overflow: hidden` (le texte espacé déborde et se coupe).
- **AVM sinon.**

### 10.13 — Les contenus additionnels au survol ou au focus sont contrôlables — WCAG 1.4.13 (AA) — partielle
- **NC démontrable :** tooltip/popover custom apparaissant au survol SANS possibilité de le fermer au clavier (Échap) ni de le survoler sans qu'il disparaisse (contenu détaché du déclencheur).
- **NA si** aucun contenu additionnel au survol/focus ; l'attribut natif `title` est un cas particulier accepté.

### 10.14 — Les contenus additionnels via CSS sont atteignables au clavier — WCAG 2.1.1 (A) — statique
- **NC si :** menu ou contenu révélé UNIQUEMENT par `:hover` en CSS, sans équivalent `:focus`/`:focus-within` ni prise en charge JS clavier — l'utilisateur clavier ne peut JAMAIS l'ouvrir (grand classique des menus déroulants CSS).
- **NA si** aucun contenu révélé par CSS seule.
