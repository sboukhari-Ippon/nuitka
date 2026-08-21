# T12: Navigation — RGAA criteria 12.1 to 12.11

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
On the SOCLE: the global mechanisms (menu, search, site map, skip link, stable layout). On a ZONE: the screen's tab order, the keyboard traps in its widgets, its shortcuts. The "set of pages" criteria (12.1 to 12.5) are judged mainly at the socle level: declare them NA in the zones if the mechanism is carried by the global layout.

## Criteria

### 12.1 — Every set of pages provides at least two navigation systems — WCAG 2.4.5 (AA) — partial
- **Expected (2 of 3):** navigation menu, search engine, site map.
- **Provable NC (socle):** a single detectable system across the whole project (menu only, with no search and no map).
- **AVM:** a mechanism may live outside the code provided (search hosted elsewhere).

### 12.2 — The menu and navigation bars are always in the same place — WCAG 3.2.3 (AA) — partial
- **C likely (socle):** navigation rendered by a single SHARED layout.
- **Provable NC:** navigations redefined screen by screen with different structures/orders.

### 12.3 — The "site map" page is relevant — WCAG 2.4.5 (AA) — partial
- **Provable NC:** a site map with dead links (routes that do not exist in the code) or plainly unrepresentative. **NA if** no site map (see 12.1).

### 12.4 — The site map is reachable in a consistent way — WCAG 2.4.5 (AA), 3.2.3 (AA) — partial
- **C likely:** a "Site map" link in the footer of the shared layout. **NA if** no site map.

### 12.5 — The search engine is reachable in a consistent way — WCAG 3.2.3 (AA) — partial
- **C likely:** search in the header of the shared layout. **NA if** no search engine.

### 12.6 — Grouping areas can be reached or skipped — WCAG 1.3.1 (A), 2.4.1 (A), 4.1.2 (A) — static
- **Expected:** header, main navigation, main content, footer (and search) marked up as landmarks (`<header>`, `<nav>`, `<main>`, `<footer>`, `role="search"`) AND/OR reachable via skip links AND/OR titled.
- **NC if:** none of these three techniques for an area that is present (layout all in `<div>` with no roles and no skip links). Cross-check 9.2: here we judge REACHABILITY, not just semantics.

### 12.7 — A skip link or quick access to the main content is present — WCAG 2.4.1 (A), 2.4.3 (A), 3.2.3 (AA) — static
- **NC if (socle):** no "Skip to content" link at the very start of the document; a link present but with a nonexistent `#` target; a hidden link that does NOT reappear on focus (`sr-only` with no `:focus` variant).
- **Expected:** the first focusable element, visible on focus, pointing to the main container.

### 12.8 — The tab order is consistent — WCAG 2.4.3 (A) — partial
- **Provable NC:** POSITIVE `tabindex` (> 0) that short-circuit the natural order; DOM reordered visually (cf. 10.3) with interactive elements; `autofocus` placed on an element late in the path.
- **AVM:** the effective order is verified with the keyboard on the rendered page.

### 12.9 — Navigation contains no keyboard trap — WCAG 2.1.1 (A), 2.1.2 (A) — partial
- **Provable NC:** an overlay (modal, panel) that captures focus with NO keyboard way out: no Esc listener, no focusable close button; `keydown` that `preventDefault()`s the Tab key with no cycle handling.
- **AVM:** embedded third-party widgets (their keyboard behaviour is not in your code).

### 12.10 — Single-key keyboard shortcuts are controllable — WCAG 2.1.4 (A) — static
- **NC if:** a global listener triggering an action on a SINGLE KEY (letter, digit, punctuation) with no modifier, active outside input fields, with no mechanism to disable or remap it.
- **NA if** no single-key shortcut.

### 12.11 — Additional content on hover/focus/activation is reachable with the keyboard — WCAG 2.1.1 (A) — static
- **NC if:** content revealed by mouse events only (`mouseover`/`mouseenter` with no `focus`, JS tooltips with no keyboard trigger). The scripting counterpart of 10.14 (which covers pure CSS): do not duplicate a finding already made there if you audit both.
- **NA if** no scripted additional content.
