# T11: Forms — RGAA criteria 11.1 to 11.13

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
The most rewarding pack: real blockers cluster in forms. Take an inventory of every field (`<input>`, `<select>`, `<textarea>`, custom controls `role="textbox|combobox|checkbox|radio|switch|slider"`) and every button. For a field: what names it, how the error comes back, is the required state indicated? In React/Vue/Angular, follow the design system's field component: if it is in your files, audit it; otherwise audit its USAGES (are label props passed?).

## Criteria

### 11.1 — Every form field has a label — WCAG 1.3.1 (A), 2.4.6 (AA), 3.3.2 (A), 4.1.2 (A) — static
- **NC if:** a field with no `<label for>` matching its `id`, no `aria-label`, no `aria-labelledby`, no `title`; an orphan `label for` (id nonexistent or duplicated); `placeholder` used as the SOLE label (it disappears on input: NC).
- **Frameworks:** `htmlFor` (React); custom field with no label prop passed.

### 11.2 — Every label is relevant — WCAG 2.4.6 (AA), 2.5.3 (A), 3.3.2 (A) — partial
- **Provable NC:** generic labels ("field", "value", "input"), identical labels for different fields, `aria-label` NOT CONTAINING the adjacent visible label (WCAG 2.5.3).
- **AVM:** fine-grained relevance is judged in context.

### 11.3 — Labels with the same function are consistent across pages — WCAG 3.2.4 (AA) — partial
- **Provable NC:** in YOUR files, the same functional field labelled differently ("E-mail" here, "Email" there, "Email address" elsewhere).
- **AVM:** flag as AVM — cross-zone verification is NOT automated (no cross-compartment check exists): a documented limit of this audit, to be covered manually.

### 11.4 — Each label is adjacent to its field — WCAG 3.3.2 (A) — partial
- **Provable NC:** label and field in DOM containers far apart (separate columns); order reversed for no reason: the label goes BEFORE the field (above or to the left), AFTER it for checkboxes and radio buttons.
- **AVM:** actual VISUAL adjacency depends on the CSS.

### 11.5 — Fields of the same nature are grouped, where necessary — WCAG 1.3.1 (A), 3.3.2 (A) — partial
- **Provable NC:** obvious groups not grouped: a series of radio buttons for the same question with no `<fieldset>` and no `role="radiogroup"`; an address/identity block with no `<fieldset>` and no `role="group"` when several blocks look alike.
- **NA if** short, unambiguous forms (a single natural group).

### 11.6 — Every field grouping has a legend — WCAG 1.3.1 (A), 3.3.2 (A) — static
- **NC if:** `<fieldset>` with no `<legend>`; `role="group"/"radiogroup"` with no `aria-label`/`aria-labelledby`.
- **NA if** no grouping (but check 11.5 first).

### 11.7 — Every grouping legend is relevant — WCAG 1.3.1 (A), 3.3.2 (A) — partial
- **Provable NC:** generic legends or placeholders. **Otherwise AVM**; **NA if** no grouping.

### 11.8 — Same-nature items in a choice list are grouped — WCAG 1.3.1 (A) — static
- **NC if:** a long `<select>` mixing families of options with no `<optgroup>`; `<optgroup>` with no `label` attribute; irrelevant optgroup `label`.
- **NA if** short, homogeneous selects.

### 11.9 — Every button label is relevant — WCAG 2.5.3 (A), 4.1.2 (A) — static
- **NC if:** a button WITH no accessible name: a `<button>` containing only an icon with no `aria-label`; `<input type="submit">` with no `value`; a custom button with no name; `aria-label` not containing the visible label.
- **Partial:** vague labels ("OK", "Submit" ambiguous when there are several forms) → flag it, fine-grained relevance is AVM.

### 11.10 — Input control is used relevantly — WCAG 3.3.1 (A), 3.3.2 (A) — static
- **NC if:**
  - required fields with no indication: neither a visible mention (asterisk + legend, "required") nor `required`/`aria-required="true"` — or the reverse: visual markers with no attributes;
  - error messages NOT tied to the field: no `aria-describedby` pointing to the message, no `aria-invalid="true"` on the field in error;
  - no expected-format hint BEFORE input when a format is required (date, password with constraints).

### 11.11 — Input control comes with correction suggestions — WCAG 3.3.3 (AA) — partial
- **Provable NC:** terse, uninformative error messages in the code ("invalid field", "error") with no expected type/format and no example, when correction is possible.
- **NA if** no validation with messages in your files.

### 11.12 — Financial, legal or test data can be modified, checked or recovered — WCAG 3.3.4 (AA) — partial
- **Provable NC:** an irreversible action (payment, data deletion, final submission) triggered by direct submission with no verification/summary step, NO confirmation and NO detectable way to cancel in the code.
- **NA if** no form with this kind of stake.

### 11.13 — The purpose of fields can be inferred (autofill) — WCAG 1.3.5 (AA) — static
- **NC if:** fields relating to the USER (surname, first name, e-mail, phone, address, country, postal code, date of birth, card number) WITH no appropriate `autocomplete` attribute (`given-name`, `family-name`, `email`, `tel`, `street-address`, `postal-code`, `bday`, `cc-number`…).
- **NA if** no personal-data field.
