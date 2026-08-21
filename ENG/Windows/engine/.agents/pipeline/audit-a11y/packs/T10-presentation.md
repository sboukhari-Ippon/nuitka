# T10: Presentation of information — RGAA criteria 10.1 to 10.14

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
The CSS pack. Many of its criteria are judged at render time (zoom, reflow, disabling styles): your job is to catch the PROVABLE violations in the style sheets and inline styles — `outline: none`, a locked viewport, inconsistent hidden content, hover-only menus — and to flag the rest precisely as AVM.

## Criteria

### 10.1 — Style sheets control the presentation — WCAG 1.3.1 (A), 1.3.2 (A) — static
- **NC if:** presentation markup in the HTML: `align`, `bgcolor`, `border` attributes (outside data tables), layout `width`/`height` on non-replaced elements; `<font>`, `<center>`, `<big>`, `<u>` tags; misused spaces (words spaced letter by letter "T I T L E", columns simulated with `&nbsp;`).

### 10.2 — Visible content remains present when styles are disabled — WCAG 1.1.1 (A), 1.3.1 (A) — partial
- **Provable NC:** information carried ONLY by the CSS: `content:` (pseudo-elements) conveying informative text, information given by a CSS background image with no text equivalent.
- **AVM:** full verification = page displayed with styles turned off.

### 10.3 — Information remains understandable when styles are disabled — WCAG 1.3.2 (A), 2.4.3 (A) — partial
- **Provable NC:** DOM order plainly different from the visual reading order: heavy use of `order:` (flex/grid) or `position: absolute` to REPOSITION meaningful blocks (the DOM says A-C-B, the screen shows A-B-C).
- **Otherwise AVM.**

### 10.4 — Text remains legible at 200% zoom — WCAG 1.4.4 (AA) — partial
- **Provable NC:** `<meta name="viewport">` with `user-scalable=no` or `maximum-scale=1` (zooming forbidden: outright NC); text containers with a FIXED height in px plus `overflow: hidden` (text cut off on zoom).
- **AVM:** actual zoom behaviour is observed at render time.

### 10.5 — Font and background colour CSS declarations come in pairs — WCAG 1.4.3 (AA) — partial
- **Provable NC:** a rule that sets `color` on a text component with NO `background`/`background-color` declared in the component or its direct parent visible in your files (the text then inherits an unpredictable background), and vice versa; a background image (`background-image`) under text with no fallback background colour.
- **AVM:** actual inheritance across the full tree.

### 10.6 — Every link whose nature is not obvious is distinguished by more than colour — WCAG 1.4.1 (A) — static
- **NC if:** links in body text with `text-decoration: none` and NO other permanent differentiator (underline, bold, border, icon) — colour alone is not enough, even when contrasted, without an additional cue on hover AND on focus.
- **NA if** all links are outside body text (menus, buttons: nature obvious from position).

### 10.7 — Focus is visible on every element that receives it — WCAG 1.4.1 (A), 2.4.7 (AA) — static
- **NC if:** `outline: none`/`outline: 0` (or `:focus { outline: ... }` removed) WITHOUT an at-least-equivalent replacement `:focus`/`:focus-visible` style. Look for global resets (`*:focus`, `button:focus`): that is THE classic offender.
- **C possible:** a reset accompanied by a clear `:focus-visible` (border, shadow, outline).

### 10.8 — Hidden content is meant to be ignored by assistive technologies — WCAG 1.3.2 (A), 4.1.2 (A) — static
- **NC if:** visible information-BEARING content hidden from assistive technologies: `aria-hidden="true"` on visible blocks (meaningful icons, text); or the reverse: wrongly hidden content still exposed (menus closed with a mere `left: -9999px` staying readable when they should not… judge by intent).
- **Legitimate patterns:** `sr-only`/`visually-hidden` classes FOR assistive technologies; `display:none` on genuinely inactive content.

### 10.9 — Information is not conveyed by shape, size or position alone — WCAG 1.3.3 (A), 1.4.1 (A) — partial
- **Provable NC:** textual instructions relying on position or shape alone: "click the button on the right", "see the box below" (with no link), "the round button".
- **AVM:** actual visual signposting (judge at render time).

### 10.10 — Criterion 10.9 is implemented in a relevant way — WCAG 1.3.3 (A), 1.4.1 (A) — manual
- **AVM by default**; **NA if** 10.9 is NA.

### 10.11 — Content is presented without horizontal scrolling at 320px wide — WCAG 1.4.10 (AA) — partial
- **Provable NC:** FIXED widths > 320px on content containers (`width: 960px` with no media query and no `max-width`), a frozen viewport (`content="width=1024"`), wide layout tables.
- **AVM:** actual reflow is observed with the window narrowed.

### 10.12 — Text spacing properties can be overridden without loss — WCAG 1.4.12 (AA) — partial
- **Provable NC:** `line-height`, `letter-spacing` or `word-spacing` locked with `!important` at values below the user minima; fixed-height text containers + `overflow: hidden` (the spaced-out text overflows and is cut off).
- **Otherwise AVM.**

### 10.13 — Additional content shown on hover or focus is controllable — WCAG 1.4.13 (AA) — partial
- **Provable NC:** a custom tooltip/popover appearing on hover WITHOUT any way to dismiss it with the keyboard (Esc) or to hover over it without it disappearing (content detached from its trigger).
- **NA if** no additional content on hover/focus; the native `title` attribute is an accepted special case.

### 10.14 — Additional content shown via CSS is reachable with the keyboard — WCAG 2.1.1 (A) — static
- **NC if:** a menu or content revealed ONLY by `:hover` in CSS, with no `:focus`/`:focus-within` equivalent and no keyboard JS support — the keyboard user can NEVER open it (the classic CSS dropdown-menu offender).
- **NA if** no content revealed by CSS alone.
