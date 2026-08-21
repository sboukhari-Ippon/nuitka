# T09: Structure of information — RGAA criteria 9.1 to 9.4

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
Structure is what a screen reader "sees": headings, regions, lists, quotations. On the SOCLE: audit the skeleton (layout landmarks). On a ZONE: audit the screen's structure (its main heading, its subheadings, its lists). The ABSENCE of structure is the most frequent finding — a file with not a single `<h*>` or landmark while it renders an entire screen is a strong signal.

## Criteria

### 9.1 — Information is structured through the appropriate use of headings — WCAG 1.3.1 (A), 2.4.1 (A), 2.4.6 (AA), 4.1.2 (A) — static
- **NC if:**
  - an entire screen without ANY heading (`<h1>`-`<h6>` or `role="heading"` + `aria-level`);
  - inconsistent hierarchy: skipped levels (h2 then h4), several competing h1 in the same document, levels chosen for their visual size;
  - text visually playing the role of a heading (div/p in large bold type via "title", "heading" classes) without a heading tag.
- **AVM:** the relevance of the headings' CONTENT is judged in context.

### 9.2 — The document structure is consistent — WCAG 1.3.1 (A) — static
- **Expected (mostly on the socle):** `<header>` (banner), `<nav>`, a SINGLE `<main>`, `<footer>` (contentinfo); the `role="banner"/"navigation"/"main"/"contentinfo"` equivalents count.
- **NC if:** no `<main>` at all; several `<main>`; main content outside any region; page `<header>`/`<footer>` missing while the layout renders them as `<div>`.

### 9.3 — Every list is correctly structured — WCAG 1.3.1 (A) — static
- **NC if:** sequences of same-nature items rendered WITHOUT a list structure: items as stacked `<div>`/`<p>` (menus, results, product cards), simulated bullets (`•`, `-`, `*` as text), `<br>` separators; incomplete ARIA lists (`role="list"` without `role="listitem"`).
- **Expected:** `<ul>`/`<ol>` + `<li>`; `<dl>`/`<dt>`/`<dd>` for term/description pairs.

### 9.4 — Every quotation is correctly indicated — WCAG 1.3.1 (A) — static
- **NC if:** identifiable quotation (testimonial, verbatim quote, "quote" classes, typographic quotation marks around an attributed passage) without `<q>` (inline) or `<blockquote>` (block).
- **NA if** no quotation in your files (common).
