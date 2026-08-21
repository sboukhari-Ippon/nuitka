---
name: proto-coding
description: Coding conventions for vanilla HTML/CSS/JavaScript prototypes (zero framework, zero build, mock data, BEM, CSS tokens, accessibility) — apply to every product screen
---

# Role: Prototype Engineer (HTML / CSS / vanilla JS)

You build a **clickable prototype** meant to validate an experience, not a production product. The founding constraint: **everything must open and work by double-clicking an `.html` file**, with no server, no installation, no build step. The code stays simple, readable and disposable, but clean.

## 🚫 CRITICAL RULES (NON-NEGOTIABLE)
| ❌ FORBIDDEN | ✅ CORRECT |
| :--- | :--- |
| Framework (React, Vue, Angular…) | HTML5 + CSS + native JavaScript only |
| Build step, bundler, `npm install` | The `.html` opens directly in the browser |
| Backend dependency / real network call | **Hardcoded mock data** in a JS object |
| Inline styles `style="..."` | **BEM** CSS classes + CSS variables |
| Repeated magic values (colors, spacings) | **Tokens** via `:root { --... }` |
| Catch-all `<div>` | **Semantic** HTML (`header`, `nav`, `main`, `section`, `button`…) |
| Dead code, half-wired screen | Every delivered screen is complete and navigable |

> Dependency exception: a CDN-served resource (font, icon set) is tolerated ONLY if the spec requires it, and via a simple `<link>` or `<script>` tag; by default, no dependency.

## 📂 Recommended file structure
```text
index.html                 # Entry point: home screen + links to the screens
screens/
  screen-x.html            # One file per main screen
assets/
  css/
    tokens.css             # Variables: colors, typography, spacings, radii
    base.css               # Light reset + global styles (typography, body)
    components.css         # Reusable components (buttons, cards, fields)
  js/
    data.js                # Mock data (exported or global JS objects)
    app.js                 # Interactions (navigation, states, list rendering)
```
Screens share the SAME CSS/JS files: a button is not duplicated from one screen to another.

## 🎨 CSS: tokens + BEM
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

/* components.css — BEM, flat selectors */
.btn { /* block */
  font: inherit; padding: var(--space-2) var(--space-3);
  border-radius: var(--radius); border: 1px solid transparent; cursor: pointer;
}
.btn--primary { background: var(--color-primary); color: #fff; }      /* modifier */
.btn:hover:not(:disabled) { filter: brightness(0.95); }
.btn:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
```
- Mobile-first: base styles for small screens, then `@media (min-width: 768px)` to enrich.
- Horizontal PAGE scrolling is a defect (vertical is normal): the page never overflows in width — media with `max-width: 100%`, relative units, `flex-wrap`/`grid` that wrap. Intrinsically wide content (table, diagram) scrolls inside ITS container (`overflow-x: auto` on the component), never the page.
- Never `outline: none` without a visible replacement focus.

## 🧱 Semantic and accessible HTML
```html
<main class="screen">
  <h1 class="screen__title">Screen title</h1>
  <form class="form">
    <label class="form__label" for="email">Email address</label>
    <input class="form__input" id="email" name="email" type="email" required />
    <button class="btn btn--primary" type="submit">Save profile</button>
  </form>
  <ul class="list" id="results" aria-live="polite"><!-- rendered by JS --></ul>
</main>
```
- A single `h1` per screen, then hierarchical headings.
- A `label` associated with each field; `aria-live` on dynamically updated areas.

## ⚙️ Vanilla JavaScript
```js
// data.js — mock data, no external source
const MOCK_USERS = [
  { id: 1, name: "Alex Martin", role: "Designer" },
  { id: 2, name: "Sam Diop", role: "Developer" },
];

// app.js — rendering and simple interactions
function renderUsers(users) {
  const list = document.getElementById("results");
  if (users.length === 0) {
    list.innerHTML = `<li class="list__empty">No results. Adjust your search.</li>`;
    return;
  }
  list.innerHTML = users
    .map((u) => `<li class="list__item">${u.name} — <span class="list__muted">${u.role}</span></li>`)
    .join("");
}
document.addEventListener("DOMContentLoaded", () => renderUsers(MOCK_USERS));
```
- No data transformation in a timer or an effect: compute at render time.
- Explicitly handle the empty and error states in the render.
- Escape or control data injected into the DOM (the mocks are controlled; stay cautious).

## ✅ FINAL CHECKLIST (per screen)
1. [ ] The `.html` opens on its own in a browser, with no server or build.
2. [ ] No framework or dependency unjustified by the spec.
3. [ ] Semantic HTML, a single `h1`, labels present.
4. [ ] CSS in BEM + tokens, visible focus, mobile-first, no horizontal page scrolling.
5. [ ] Hardcoded mock data, empty and error states handled.
6. [ ] No inline style, no duplicated magic value.
7. [ ] Working navigation between screens (links / JS).
