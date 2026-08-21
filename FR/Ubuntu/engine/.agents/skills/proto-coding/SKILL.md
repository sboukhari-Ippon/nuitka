---
name: proto-coding
description: Conventions de code pour prototypes HTML/CSS/JavaScript vanilla (zéro framework, zéro build, données mockées, BEM, tokens CSS, accessibilité) — à appliquer à chaque écran produit
---

# Rôle : Ingénieur Prototype (HTML / CSS / JS vanilla)

Tu fabriques un **prototype cliquable** destiné à valider une expérience, pas un produit de production. La contrainte fondatrice : **tout doit s'ouvrir et fonctionner en double-cliquant sur un fichier `.html`**, sans serveur, sans installation, sans étape de build. Le code reste simple, lisible et jetable, mais propre.

## 🚫 RÈGLES CRITIQUES (NON NÉGOCIABLES)
| ❌ INTERDIT | ✅ CORRECT |
| :--- | :--- |
| Framework (React, Vue, Angular…) | HTML5 + CSS + JavaScript natif uniquement |
| Étape de build, bundler, `npm install` | Le `.html` s'ouvre directement dans le navigateur |
| Dépendance backend / appel réseau réel | Données **mockées en dur** dans un objet JS |
| Styles inline `style="..."` | Classes CSS **BEM** + variables CSS |
| Valeurs magiques répétées (couleurs, espacements) | **Tokens** via `:root { --... }` |
| `<div>` à tout faire | HTML **sémantique** (`header`, `nav`, `main`, `section`, `button`…) |
| Code mort, écran à moitié branché | Chaque écran livré est complet et navigable |

> Exception dépendance : une ressource servie par CDN (police, jeu d'icônes) est tolérée UNIQUEMENT si la spec l'exige, et via une simple balise `<link>` ou `<script>` ; par défaut, aucune dépendance.

## 📂 Structure de fichiers recommandée
```text
index.html                 # Point d'entrée : écran d'accueil + liens vers les écrans
screens/
  ecran-x.html             # Un fichier par écran principal
assets/
  css/
    tokens.css             # Variables : couleurs, typo, espacements, rayons
    base.css               # Reset léger + styles globaux (typo, body)
    components.css         # Composants réutilisables (boutons, cartes, champs)
  js/
    data.js                # Données mockées (objets JS exportés ou globaux)
    app.js                 # Interactions (navigation, états, rendu des listes)
```
Les écrans partagent les MÊMES fichiers CSS/JS : on ne duplique pas un bouton d'un écran à l'autre.

## 🎨 CSS : tokens + BEM
```css
/* tokens.css */
:root {
  --color-primary: #2563eb;
  --color-text: #1f2937;
  --color-muted: #6b7280;
  --color-bg: #ffffff;
  --space-1: 4px; --space-2: 8px; --space-3: 16px; --space-4: 24px;
  --radius: 8px;
  --font-sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}

/* components.css — BEM, sélecteurs plats */
.btn { /* bloc */
  font: inherit; padding: var(--space-2) var(--space-3);
  border-radius: var(--radius); border: 1px solid transparent; cursor: pointer;
}
.btn--primary { background: var(--color-primary); color: #fff; }      /* modificateur */
.btn:hover:not(:disabled) { filter: brightness(0.95); }
.btn:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
```
- Mobile-first : styles de base pour petit écran, puis `@media (min-width: 768px)` pour enrichir.
- Le scroll horizontal de PAGE est un défaut (le vertical est normal) : la page ne déborde jamais en largeur — médias en `max-width: 100%`, unités relatives, `flex-wrap`/`grid` qui replient. Un contenu intrinsèquement large (tableau, diagramme) défile dans SON conteneur (`overflow-x: auto` sur le composant), jamais la page.
- Jamais d'`outline: none` sans focus de remplacement visible.

## 🧱 HTML sémantique et accessible
```html
<main class="screen">
  <h1 class="screen__title">Titre de l'écran</h1>
  <form class="form">
    <label class="form__label" for="email">Adresse e-mail</label>
    <input class="form__input" id="email" name="email" type="email" required />
    <button class="btn btn--primary" type="submit">Enregistrer le profil</button>
  </form>
  <ul class="list" id="results" aria-live="polite"><!-- rendu par JS --></ul>
</main>
```
- Un seul `h1` par écran, titres hiérarchisés ensuite.
- `label` associé à chaque champ ; `aria-live` sur les zones mises à jour dynamiquement.

## ⚙️ JavaScript vanilla
```js
// data.js — données mockées, aucune source externe
const MOCK_USERS = [
  { id: 1, name: "Alex Martin", role: "Designer" },
  { id: 2, name: "Sam Diop", role: "Développeur" },
];

// app.js — rendu et interactions simples
function renderUsers(users) {
  const list = document.getElementById("results");
  if (users.length === 0) {
    list.innerHTML = `<li class="list__empty">Aucun résultat. Modifie ta recherche.</li>`;
    return;
  }
  list.innerHTML = users
    .map((u) => `<li class="list__item">${u.name} — <span class="list__muted">${u.role}</span></li>`)
    .join("");
}
document.addEventListener("DOMContentLoaded", () => renderUsers(MOCK_USERS));
```
- Pas de transformation de données dans un timer ou un effet : on calcule au moment du rendu.
- Gère explicitement les états vide et erreur dans le rendu.
- Échappe ou maîtrise les données injectées dans le DOM (les mocks sont contrôlés ; reste prudent).

## ✅ CHECKLIST FINALE (par écran)
1. [ ] Le `.html` s'ouvre seul dans un navigateur, sans serveur ni build.
2. [ ] Aucun framework ni dépendance non justifiée par la spec.
3. [ ] HTML sémantique, un seul `h1`, labels présents.
4. [ ] CSS en BEM + tokens, focus visible, mobile-first, aucun scroll horizontal de page.
5. [ ] Données mockées en dur, états vide et erreur traités.
6. [ ] Aucun style inline, aucune valeur magique dupliquée.
7. [ ] Navigation entre écrans fonctionnelle (liens / JS).
