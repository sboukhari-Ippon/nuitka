# T04 : Multimédia — critères RGAA 4.1 à 4.13

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Inventorie les médias temporels (`<video>`, `<audio>`, embeds YouTube/Vimeo/Dailymotion, players JS) et non temporels (contenus interactifs type `<canvas>` applicatif, animations pilotées) de tes fichiers. La plupart des critères de PERTINENCE exigent d'écouter/regarder le média : ils sont AVM par principe — ta valeur ajoutée est de détecter les mécanismes ABSENTS (transcription, sous-titres, contrôles) et les pièges démontrables (autoplay sonore).

## Critères

### 4.1 — Chaque média temporel pré-enregistré a, si nécessaire, une transcription textuelle ou une audiodescription — WCAG 1.2.1 (A), 1.2.3 (A) — partielle
- **NC démontrable :** média porteur d'information sans lien « transcription », sans bloc de texte équivalent adjacent, sans `aria-describedby` vers un contenu, ni piste alternative repérable.
- **AVM :** média dont le caractère informatif ou décoratif ne se déduit pas du code.

### 4.2 — Transcription et audiodescription sont pertinentes — WCAG 1.2.1 (A), 1.2.3 (A) — manuelle
- **AVM par défaut** (comparer le média et sa transcription exige de le consulter). **NC flagrant :** transcription vide ou placeholder.

### 4.3 — Chaque média synchronisé a, si nécessaire, des sous-titres synchronisés — WCAG 1.2.2 (A) — partielle
- **NC démontrable :** `<video>` avec bande-son, sans `<track kind="captions">` ni sous-titrage activable repérable. Les embeds (YouTube…) : AVM (les sous-titres vivent chez l'hébergeur).
- **Vérifie aussi :** la balise `<track>` de sous-titres porte bien `kind="captions"` (pas `subtitles` seul pour de l'accessibilité, pas d'attribut absent).

### 4.4 — Les sous-titres sont pertinents — WCAG 1.2.2 (A) — manuelle
- **AVM par défaut** ; **NC flagrant :** fichier de sous-titres visiblement vide ou factice dans le dépôt.

### 4.5 — Chaque média a, si nécessaire, une audiodescription synchronisée — WCAG 1.2.5 (AA) — partielle
- **NC démontrable :** vidéo informative sans `<track kind="descriptions">`, sans version audiodécrite alternative repérable. Sinon AVM.

### 4.6 — L'audiodescription est pertinente — WCAG 1.2.5 (AA) — manuelle
- **AVM par défaut.**

### 4.7 — Chaque média temporel est clairement identifiable — WCAG 1.1.1 (A) — partielle
- **NC démontrable :** média sans titre, intitulé ni texte adjacent qui l'identifie (le lecteur arrive sur « vidéo » sans savoir laquelle).
- **Attendu :** titre ou texte introductif accolé au média.

### 4.8 — Chaque média non temporel a, si nécessaire, une alternative — WCAG 1.1.1 (A) — partielle
- **Concerne :** contenus interactifs embarqués (`<canvas>` applicatif, `<object>`/`<embed>` non image, mini-app tierce).
- **NC démontrable :** un tel contenu sans aucune alternative repérable (lien, contenu équivalent). Sinon AVM.

### 4.9 — L'alternative du média non temporel est pertinente — WCAG 1.1.1 (A) — manuelle
- **AVM par défaut.**

### 4.10 — Chaque son déclenché automatiquement est contrôlable — WCAG 1.4.2 (A) — statique
- **NC si :** `autoplay` sans `muted` sur `<video>`/`<audio>`, `play()` appelé au chargement sans action utilisateur, sans bouton stop/pause adjacent évident.
- **NA si** aucun déclenchement sonore automatique.

### 4.11 — La consultation de chaque média temporel est contrôlable au clavier et au pointeur — WCAG 2.1.1 (A), 2.1.2 (A) — partielle
- **NC démontrable :** `<video>`/`<audio>` sans attribut `controls` ET sans player custom fournissant des commandes ; player custom dont les commandes sont des `<div onClick>` sans rôle ni gestion clavier (recoupe 7.3, mais ici pour les COMMANDES du média).
- **AVM :** players tiers (leurs commandes vivent hors de ton code).

### 4.12 — La consultation de chaque média non temporel est contrôlable au clavier et au pointeur — WCAG 2.1.1 (A), 2.1.2 (A) — partielle
- Même logique que 4.11 pour les contenus interactifs non temporels ; interactions `<canvas>` uniquement à la souris (listeners `mouse*`/`touch*` sans équivalent clavier) = NC démontrable.

### 4.13 — Chaque média est compatible avec les technologies d'assistance — WCAG 4.1.2 (A) — partielle
- **NC démontrable :** player custom dont les commandes n'exposent ni nom accessible ni rôle ni état (`aria-pressed`, `aria-label` absents partout).
- **AVM :** restitution réelle par lecteur d'écran.
