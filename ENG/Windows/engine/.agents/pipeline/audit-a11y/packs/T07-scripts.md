# T07: Scripts — RGAA criteria 7.1 to 7.5

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
The decisive pack for JS applications (React, Vue, Angular, vanilla). Take an inventory of the script-DRIVEN interface components: modals, menus, tabs, accordions, autocompletes, carousels, toasts, any element with an event handler. For each one, three questions: does it expose a name, a role and its states (7.1)? Is it usable with the keyboard (7.3)? Are its messages announced (7.5)?

## Criteria

### 7.1 — Every script is, where necessary, compatible with assistive technologies — WCAG 2.5.3 (A), 4.1.2 (A) — static
- **NC if:** custom interactive component WITHOUT the expected ARIA pattern:
  - clickable control built as a `<div>`/`<span>` with no `role` and no accessible name;
  - modal without `role="dialog"` + `aria-modal="true"` + linked title;
  - accordion/dropdown menu without `aria-expanded` on its trigger;
  - tabs without `role="tablist"/"tab"/"tabpanel"` + `aria-selected`;
  - custom checkbox/switch without `role="checkbox"/"switch"` + `aria-checked`;
  - visual state (active, selected, disabled) conveyed only by a CSS class with no ARIA property.
- **Favour native elements:** properly used `<button>`, `<details>`, `<dialog>` count as C.

### 7.2 — For every script that has an alternative, that alternative is relevant — WCAG 1.1.1 (A), 4.1.2 (A) — partial
- **Applies to:** `<noscript>` and fallback content. **Provable NC:** empty `<noscript>`, or "enable JavaScript" with no alternative access to the essential content. **NA if** no alternative is provided (common and tolerated: 7.1/7.3 carry the requirement).

### 7.3 — Every script is controllable with the keyboard and with any pointing device — WCAG 1.3.1 (A), 2.1.1 (A), 2.4.7 (AA) — static
- **NC if:**
  - element NOT natively focusable (`<div>`, `<span>`, `<li>`, `<svg>`…) with `onClick`/`@click` but with NEITHER `tabindex="0"` NOR `keydown/keyup` handling (Enter + Space);
  - mouse-only interactions: `onMouseOver`/`onDoubleClick`/drag with no keyboard equivalent or alternative entry point;
  - positive `tabindex` (> 0) breaking the natural order (overlaps 12.8: report it here only if the script injects it);
  - `blur()` call or focus removal on an element that has just received it.
- **Good framework reflex:** an `onClick` on a native `<button>`/`<a href>` is C for focusability.

### 7.4 — Every script-initiated change of context is announced or controlled by the user — WCAG 3.2.1 (A), 3.2.2 (A) — static
- **NC if:** navigation or submission triggered by a mere `onChange`/`onInput` ("go to" select, auto-submit on the last field, redirect on input) with no submit button and no prior warning.
- **NA if** no scripted change of context.

### 7.5 — Status messages are correctly rendered — WCAG 4.1.3 (AA) — static
- **NC if:** content inserted dynamically to inform the user WITHOUT an announcement attribute:
  - confirmation/success ("saved" toast) without `role="status"` or `aria-live="polite"`;
  - error/warning without `role="alert"` (or `aria-live="assertive"`);
  - progress ("loading", results counter, spinner) without `role="status"/"progressbar"/"log"`.
- **NA if** no dynamic status message in your files.
