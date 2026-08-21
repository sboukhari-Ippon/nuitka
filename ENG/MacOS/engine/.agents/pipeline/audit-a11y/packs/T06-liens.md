# T06: Links — RGAA criteria 6.1 to 6.2

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
Take an inventory of the links: `<a href>`, routing components (`<Link>`, `<NavLink>`, `router-link`, `routerLink`), `role="link"`. A "link" with no destination (`<a>` without `href`, a navigating `<div onClick>`) is also a Scripts issue (7.1/7.3): here, focus on the LABEL. The accessible name of a link is computed as follows: `aria-labelledby` > `aria-label` > content (text + `alt` of inner images) > `title`.

## Criteria

### 6.1 — Every link is explicit — WCAG 1.1.1 (A), 2.4.4 (A), 2.5.3 (A) — partial
- **Provable NC:**
  - "see more", "click here", "read more", "learn more" links repeated with no programmatic complement (`aria-label`, accessible hidden text, `aria-labelledby`) specifying the destination;
  - `aria-label` that does NOT CONTAIN the link's visible label (voice control trap, WCAG 2.5.3);
  - image link whose `alt` does not describe the destination ("logo.png" instead of "Home").
- **AVM:** explicitness "in context" (the surrounding sentence) is judged with the page displayed.

### 6.2 — Every link has a label — WCAG 1.1.1 (A), 2.4.4 (A) — static
- **NC if:** link with an EMPTY accessible name: `<a>` containing only an icon (`<svg>`/`<i>`/icon font) with no `aria-label`, no hidden text and no `title`; image link with `alt=""`; `<a></a>` with no content at all.
- **Group:** this defect repeats in series (social icons, action pictograms in lists) — one finding, all locations.
