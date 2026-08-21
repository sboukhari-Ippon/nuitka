# T03: Colours — RGAA criteria 3.1 to 3.3

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
This pack is normally judged on the rendered output; on code, you can still settle two things: (1) uses of colour as the ONLY information vector visible in the logic (conditional classes, text that mentions a colour); (2) the contrast of text/background pairs hard-coded in the CSS. The orchestrator may supply you with a "CONTRAST MEASUREMENTS" block computed mechanically on the safe pairs from your files: rely on it, it is authoritative for those pairs. Everything else is AVM, not C.

## Criteria

### 3.1 — Information is never conveyed by colour alone — WCAG 1.3.1 (A), 1.4.1 (A) — partial
- **Provable NC:** state signalled by a colour class/style alone with no text, icon or conveying attribute (e.g. a field in error turning red with no message and no `aria-invalid`; an "active/inactive" status rendered as a coloured dot with no label; a link within text identifiable by its colour alone, see 10.6); text that refers to a colour ("click the green button", "fields in red are required").
- **AVM:** charts and visualisations (legend by colour alone?), maps, badges whose actual content is not readable in the code.

### 3.2 — Text/background contrast is sufficient — WCAG 1.4.3 (AA) — partial
- **Thresholds:** 4.5:1 for regular text; 3:1 for text ≥ 24px without bold weight or ≥ 18.5px bold.
- **Provable NC:** literal `color`/`background(-color)` pair from the same CSS block (or from the supplied MEASUREMENTS block) below the threshold; risk patterns: light greys on white (`#aaa` and lighter on `#fff`), lightened placeholders, text over an image with no overlay.
- **AVM:** colours coming from variables/dynamic themes, text over gradients or images, anything not measured. NEVER declare C from reading variable names alone.

### 3.3 — The colours of interface components and information-carrying graphical elements are sufficiently contrasted — WCAG 1.4.11 (AA) — partial
- **Threshold:** 3:1 against adjacent colours, for components (field borders, buttons, focus, checkboxes) and graphical elements required for understanding (icons, curves, segments).
- **Provable NC:** measurable literal values below 3:1 (field border `#ddd` on a white background, light grey action icon); states (hover, focus, checked) defined with colours nearly identical to the resting state.
- **AVM:** any component whose effective colours depend on the theme or on states computed at render time.
