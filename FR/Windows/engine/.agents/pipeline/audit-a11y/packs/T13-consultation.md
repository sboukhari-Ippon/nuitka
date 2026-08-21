# T13 : Consultation — critères RGAA 13.1 à 13.12

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Le pack des comportements « dans le temps et au doigt » : limites de temps, mouvements, gestes, documents téléchargeables. Beaucoup de NA légitimes ici : ne force rien, mais traque les classiques démontrables — meta refresh, carrousel auto sans pause, action sur `mousedown`, redirection différée.

## Critères

### 13.1 — L'utilisateur contrôle chaque limite de temps modifiant le contenu — WCAG 2.2.1 (A), 2.2.2 (A) — partielle
- **NC démontrable :** `<meta http-equiv="refresh" content="N">` avec N > 0 (rafraîchissement ou redirection différés : NC franc) ; expiration de session/déconnexion sur `setTimeout`/`setInterval` sans avertissement ni possibilité de prolonger ; contenus qui se mettent à jour automatiquement sans contrôle (fil d'actualité auto-rafraîchi).
- **NA si** aucune limite de temps.

### 13.2 — Aucune ouverture de fenêtre sans action de l'utilisateur — WCAG 3.2.1 (A) — statique
- **NC si :** `window.open()` (ou équivalent) déclenché au chargement (`useEffect` de montage, `mounted`, `DOMContentLoaded`, script inline) et non dans un gestionnaire d'action utilisateur.
- **NA si** aucune ouverture programmée. (Un `target="_blank"` sur un lien cliqué est une action utilisateur : hors sujet ici.)

### 13.3 — Chaque document bureautique en téléchargement a, si nécessaire, une version accessible — WCAG 1.1.1 (A), 1.3.1 (A), 1.3.2 (A), 2.4.1 (A), 2.4.3 (A), 3.1.1 (A), 4.1.2 (A) — partielle
- **Repère :** liens vers `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.odt`…
- **NC démontrable :** document porteur d'information essentielle sans alternative repérable (version HTML, page équivalente).
- **AVM :** l'accessibilité du document lui-même ne se lit pas depuis le code du site.

### 13.4 — La version accessible offre la même information — WCAG 1.1.1 (A), 1.3.1 (A), 1.3.2 (A), 2.4.1 (A), 2.4.3 (A), 3.1.1 (A), 4.1.2 (A) — manuelle
- **AVM par défaut** ; **NA si** aucun document bureautique.

### 13.5 — Chaque contenu cryptique (art ASCII, émoticône, syntaxe cryptique) a une alternative — WCAG 1.1.1 (A) — statique
- **NC si :** émoticônes texte (`:-)`), art ASCII ou syntaxe cryptique porteurs de sens sans alternative (`title`, `aria-label`, texte adjacent). Les emojis Unicode porteurs d'information isolés gagnent à être enveloppés (`role="img"` + `aria-label`).
- **NA si** aucun contenu cryptique (fréquent).

### 13.6 — L'alternative du contenu cryptique est pertinente — WCAG 1.1.1 (A) — partielle
- **NC démontrable :** alternative sans rapport ou placeholder. **NA si** 13.5 est NA.

### 13.7 — Les changements brusques de luminosité et effets de flash sont correctement utilisés — WCAG 2.3.1 (A) — partielle
- **NC démontrable :** animation CSS/JS de clignotement rapide identifiable (alternance visible > 3 fois par seconde : `@keyframes` très courts en boucle sur des aplats).
- **AVM :** contenus vidéo et GIF (leur contenu ne se lit pas dans le code). **NA si** aucun effet de ce type.

### 13.8 — Chaque contenu en mouvement ou clignotant est contrôlable — WCAG 2.2.1 (A), 2.2.2 (A) — statique
- **NC si :** carrousel/diaporama à défilement AUTOMATIQUE sans bouton pause/stop (props `autoplay`/`interval` des libs de carrousel, `setInterval` de slide) ; animation infinie (`animation: … infinite`) sur du contenu porteur sans respect de `prefers-reduced-motion` ni contrôle ; `<marquee>`/`<blink>` (NC franc).
- **NA si** rien ne bouge automatiquement plus de 5 secondes.

### 13.9 — Le contenu est consultable quelle que soit l'orientation de l'écran — WCAG 1.3.4 (AA) — partielle
- **NC démontrable :** verrouillage d'orientation (`screen.orientation.lock(…)`, manifest `"orientation": "portrait"`) ; media queries qui MASQUENT le contenu dans une orientation (« tournez votre appareil »).
- **NA/AVM sinon.**

### 13.10 — Les gestes complexes ont une alternative simple — WCAG 2.5.1 (A) — partielle
- **NC démontrable :** fonctionnalités accessibles UNIQUEMENT par geste multipoint ou tracé (pinch-to-zoom custom, swipe obligatoire d'un carrousel sans boutons précédent/suivant, glisser-déposer sans équivalent bouton).
- **NA si** aucune interaction gestuelle.

### 13.11 — Les actions au pointeur peuvent être annulées — WCAG 2.5.2 (A) — statique
- **NC si :** action définitive déclenchée sur l'événement DESCENDANT (`mousedown`, `pointerdown`, `touchstart`) au lieu de l'événement remontant (`click`, `mouseup`, `pointerup`) — l'utilisateur ne peut plus glisser hors du contrôle pour annuler. (Exceptions légitimes : pressage continu type piano/curseur.)
- **NA si** aucun écouteur descendant déclencheur d'action.

### 13.12 — Les fonctionnalités déclenchées par le mouvement de l'appareil ont une alternative — WCAG 2.5.4 (A) — partielle
- **NC démontrable :** `devicemotion`/`deviceorientation` (secouer, incliner) déclenchant une fonction SANS bouton équivalent ni réglage de désactivation.
- **NA si** aucune détection de mouvement (cas le plus fréquent).
