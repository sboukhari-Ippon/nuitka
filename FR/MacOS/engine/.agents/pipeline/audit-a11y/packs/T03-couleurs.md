# T03 : Couleurs — critères RGAA 3.1 à 3.3

Grille adaptée du RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Licence Ouverte 2.0). Équivalence WCAG 2.1 et testabilité statique indiquées par critère.

## Méthode du pack
Ce pack se juge normalement sur le rendu ; sur du code, tu peux quand même trancher deux choses : (1) les usages de couleur comme SEUL vecteur d'information visibles dans la logique (classes conditionnelles, textes qui mentionnent une couleur) ; (2) les contrastes des paires texte/fond déclarées en dur dans le CSS. L'orchestrateur peut te fournir un bloc « MESURES DE CONTRASTE » calculé mécaniquement sur les paires sûres de tes fichiers : appuie-toi dessus, il fait foi pour ces paires-là. Tout le reste est AVM, pas C.

## Critères

### 3.1 — L'information n'est jamais donnée uniquement par la couleur — WCAG 1.3.1 (A), 1.4.1 (A) — partielle
- **NC démontrable :** état signalé par la seule classe/style de couleur sans texte, icône ni attribut porteur (ex. champ en erreur devenant rouge sans message ni `aria-invalid` ; statut « actif/inactif » rendu par un point coloré sans libellé ; lien du texte identifiable à sa seule couleur, cf. 10.6) ; texte qui référence une couleur (« cliquez sur le bouton vert », « les champs en rouge sont obligatoires »).
- **AVM :** graphiques et visualisations (légende par couleur seule ?), cartes, badges dont le contenu réel n'est pas lisible dans le code.

### 3.2 — Le contraste texte/arrière-plan est suffisant — WCAG 1.4.3 (AA) — partielle
- **Seuils :** 4,5:1 pour le texte courant ; 3:1 pour le texte ≥ 24px sans graisse ou ≥ 18,5px en gras.
- **NC démontrable :** paire `color`/`background(-color)` littérale d'un même bloc CSS (ou du bloc MESURES fourni) sous le seuil ; motifs à risque : gris clairs sur blanc (`#aaa` et plus clair sur `#fff`), placeholders éclaircis, texte sur image sans voile.
- **AVM :** couleurs issues de variables/thèmes dynamiques, texte sur dégradés ou images, tout ce qui n'est pas mesuré. Ne déclare JAMAIS C sur la seule lecture de noms de variables.

### 3.3 — Les couleurs des composants d'interface et éléments graphiques porteurs d'information sont suffisamment contrastées — WCAG 1.4.11 (AA) — partielle
- **Seuil :** 3:1 contre les couleurs adjacentes, pour les composants (bordures de champs, boutons, focus, cases) et les éléments graphiques nécessaires à la compréhension (icônes, courbes, segments).
- **NC démontrable :** valeurs littérales mesurables sous 3:1 (bordure de champ `#ddd` sur fond blanc, icône-action gris clair) ; états (survol, focus, coché) définis par des couleurs quasi identiques au repos.
- **AVM :** tout composant dont les couleurs effectives dépendent du thème ou d'états calculés au rendu.
