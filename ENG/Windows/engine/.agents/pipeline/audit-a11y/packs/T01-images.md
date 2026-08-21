# T01: Images — RGAA criteria 1.1 to 1.9

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
Start by taking an inventory of the images in your files: `<img>`, `<svg>`, `<canvas>`, `<area>`, `<input type="image">`, image-type `<object>`/`<embed>`, `role="img"`, library icons (`<Icon>`, `<FontAwesomeIcon>`…), CSS images (`background-image`). Classify each one: conveying information (it adds something the surrounding text does not say) or decorative. This classification drives 1.1 vs 1.2.

## Criteria

### 1.1 — Every image conveying information has a text alternative — WCAG 1.1.1 (A) — static
- **Where:** `alt` on `<img>`/`<area>`/`<input type="image">`; `aria-label`/`aria-labelledby` on `role="img"`; informative `<svg>`: referenced `<title>` or `aria-label` + `role="img"`; `<canvas>`: fallback content between the tags; `<object>`/`<embed>`: internal or adjacent alternative.
- **NC if:** informative image with NONE of these mechanisms; in JSX/Vue: missing `alt` prop, `alt={undefined}`, icon button without an accessible name.
- **Default:** an image with no alternative AND no decorative markup is NC here; nature (informative/decorative) undecidable → AVM.

### 1.2 — Every decorative image is properly ignored by assistive technologies — WCAG 1.1.1 (A), 4.1.2 (A) — static
- **Where:** decorative image: `alt=""` (and neither `title` nor `aria-label*`), or `aria-hidden="true"`, or `role="presentation"/"none"`; decorative `<svg>`: `aria-hidden="true"`.
- **NC if:** obviously decorative image (pictogram redundant with the adjacent text, background image placed as an `<img>`) with a verbose `alt`, a `title`, or no neutralisation; decorative library icons without `aria-hidden`.

### 1.3 — The text alternative of images conveying information is relevant — WCAG 1.1.1 (A), 4.1.2 (A) — partial
- **Provable NC:** `alt` equal to the file name (`alt="IMG_0123.png"`), generic (`alt="image"`, `alt="photo"`, `alt="icon"`), identical across different images, or obvious placeholder (`alt="TODO"`, lorem ipsum).
- **AVM otherwise:** actual relevance (does the alternative convey the image's information?) is judged with the image displayed. Flag `alt` values > ~80 characters (an alternative must stay short, see 1.6 for the detailed description).

### 1.4 — The alternative of a CAPTCHA or test image identifies its nature and function — WCAG 1.1.1 (A) — partial
- **NA if** no CAPTCHA/test image in your files (the most common case; patterns: `captcha`, `recaptcha`, `hcaptcha`, challenge images).
- **NC if:** image CAPTCHA whose alternative describes the CONTENT (the solution!) or does not say it is a test; expected: "verification code" or equivalent.

### 1.5 — Every image CAPTCHA offers an alternative access solution — WCAG 1.1.1 (A) — partial
- **NA if** no CAPTCHA. **NC if:** visual CAPTCHA with no non-visual variant detectable in the code (audio version, logic question, alternative mechanism).

### 1.6 — Every image conveying information has, where necessary, a detailed description — WCAG 1.1.1 (A) — partial
- **Applies to:** complex images (charts, diagrams, schematics, infographics) whose meaning does not fit in a short `alt`.
- **Provable NC:** identifiable chart/diagram (charting library, dataviz `<canvas>`, file name "chart/graph/diagram") without `aria-describedby`, without a "detailed description" link, without equivalent adjacent text.
- **AVM:** "where necessary" is judged by seeing the image; flag the candidates.

### 1.7 — The detailed description is relevant — WCAG 1.1.1 (A) — manual
- **AVM by default** (judging a description requires seeing the image). **Blatant NC:** detailed description that is empty, a placeholder, or unrelated (copy-pasted from another image).

### 1.8 — Every text image is replaced with styled text where possible — WCAG 1.4.5 (AA) — partial
- **Provable NC:** images visibly containing text that could be rebuilt in CSS: heading banners as PNG, image buttons with a label, file name "title/btn/text/banner" + context. Special cases aside: logos are exempt.
- **AVM:** the actual content of an image is not visible in the code; flag the suspects.

### 1.9 — Every image caption is properly linked to its image — WCAG 1.1.1 (A), 4.1.2 (A) — static
- **Where:** image + adjacent caption: expected `<figure>` + `<figcaption>` (with `role="figure"`/`aria-label` where needed to link them).
- **NC if:** visual caption pattern (credit/explanatory text right under an image, `caption`, `legend` classes) without a `<figure>/<figcaption>` structure.
- **NA if** no captioned image.
