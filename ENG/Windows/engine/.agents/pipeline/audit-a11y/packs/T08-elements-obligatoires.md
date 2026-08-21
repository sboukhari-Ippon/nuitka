# T08: Mandatory elements — RGAA criteria 8.1 to 8.10

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
On the SOCLE: audit the root document (doctype, `<html lang>`, `<title>`). On a ZONE of a single-page application: the root document is outside your files — focus on PER-SCREEN title management (8.5/8.6: `document.title`, Helmet, `useTitle`…), changes of language (8.7/8.8) and misused tags (8.9). Declare NA whatever belongs to the root document absent from your pass.

## Criteria

### 8.1 — Every page is defined by a document type — WCAG 4.1.1 (A) — static
- **NC if:** `<!DOCTYPE html>` missing, invalid, or placed AFTER the `<html>` tag in the root document. **NA** outside the socle.

### 8.2 — The generated source code is valid — WCAG 4.1.1 (A), 4.1.2 (A) — partial
- **Provable NC:** blatant invalidities readable in the source: duplicated `id` within the same document/component, forbidden nestings (`<button>` inside `<a>`, `<div>` inside `<span>`, `<li>` outside a list), non-existent or misspelled ARIA attributes (`aria-lable`), unclosed tags.
- **AVM:** full W3C validation requires the generated HTML.

### 8.3 — The default language is present — WCAG 3.1.1 (A) — static
- **NC if:** `<html>` without a `lang` attribute (nor `xml:lang`) in the root document. **NA** outside the socle (but flag a dynamically set `lang` if one exists: it is the authoritative one).

### 8.4 — The default language code is relevant — WCAG 3.1.1 (A) — static
- **NC if:** invalid `lang` (`lang="french"`, `lang=""`) or plainly inconsistent with the language of the content (interface entirely in French with `lang="en"` — very common on templates).

### 8.5 — Every page has a page title — WCAG 2.4.2 (A) — static
- **NC if:** `<title>` missing from the root document; in a single-page application: NO per-screen title mechanism (no `document.title`, no Helmet/`useTitle`/router `meta.title`) anywhere in the project — the title then stays frozen for every page.

### 8.6 — The page title is relevant — WCAG 2.4.2 (A) — partial
- **Provable NC:** generic scaffolding `<title>` ("React App", "Vite App", "Document", "Untitled", framework name); multi-screen application whose title is never updated (every screen bears the same title).
- **AVM:** fine-grained relevance (does the title reflect the content AND the site?) is judged page by page.

### 8.7 — Every change of language is indicated — WCAG 3.1.2 (AA) — partial
- **Provable NC:** entire passages in a language other than the default language (slogans, quotations, English labels in a French interface) without a `lang` attribute on their container. Special cases aside: proper nouns, terms in common usage.
- **AVM:** an exhaustive inventory requires reading all the rendered content.

### 8.8 — The language code of every change of language is valid and relevant — WCAG 3.1.2 (AA) — static
- **NC if:** invalid inner `lang` attributes (`lang="anglais"`) or inconsistent with the text they cover. **NA if** no change of language is marked up.

### 8.9 — Tags are not used solely for presentation purposes — WCAG 1.3.1 (A) — static
- **NC if:** semantic tags misused for their rendering: `<h*>` to enlarge text that is not a heading, `<blockquote>` to indent, `<fieldset>` to draw a border outside a form, series of `<br>` for spacing, layout `<table>` (overlaps with 5.8: mention it, do not duplicate the detailed finding).

### 8.10 — Changes in reading direction are indicated — WCAG 1.3.2 (A) — static
- **NC if:** text in a right-to-left language (Arabic, Hebrew…) without a `dir="rtl"` attribute on its container (with a consistent `lang`). **NA if** no bidirectional content (the most common case).
