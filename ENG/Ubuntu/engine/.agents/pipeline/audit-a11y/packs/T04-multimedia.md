# T04: Multimedia — RGAA criteria 4.1 to 4.13

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
Take an inventory of the time-based media (`<video>`, `<audio>`, YouTube/Vimeo/Dailymotion embeds, JS players) and non-time-based media (interactive content such as an application `<canvas>`, script-driven animations) in your files. Most RELEVANCE criteria require listening to/watching the medium: they are AVM as a matter of principle — your added value is detecting ABSENT mechanisms (transcript, captions, controls) and provable traps (audible autoplay).

## Criteria

### 4.1 — Every pre-recorded time-based medium has, where necessary, a text transcript or an audio description — WCAG 1.2.1 (A), 1.2.3 (A) — partial
- **Provable NC:** information-carrying medium without a "transcript" link, without an equivalent adjacent text block, without `aria-describedby` pointing to content, and without a detectable alternative track.
- **AVM:** medium whose informative or decorative nature cannot be inferred from the code.

### 4.2 — Transcript and audio description are relevant — WCAG 1.2.1 (A), 1.2.3 (A) — manual
- **AVM by default** (comparing the medium with its transcript requires viewing it). **Blatant NC:** empty or placeholder transcript.

### 4.3 — Every synchronised medium has, where necessary, synchronised captions — WCAG 1.2.2 (A) — partial
- **Provable NC:** `<video>` with a soundtrack, without `<track kind="captions">` and without any detectable activatable captioning. Embeds (YouTube…): AVM (the captions live on the host's side).
- **Also check:** the caption `<track>` tag does carry `kind="captions"` (not `subtitles` alone for accessibility purposes, no missing attribute).

### 4.4 — Captions are relevant — WCAG 1.2.2 (A) — manual
- **AVM by default**; **Blatant NC:** caption file visibly empty or fake in the repository.

### 4.5 — Every medium has, where necessary, a synchronised audio description — WCAG 1.2.5 (AA) — partial
- **Provable NC:** informative video without `<track kind="descriptions">` and without a detectable audio-described alternative version. Otherwise AVM.

### 4.6 — The audio description is relevant — WCAG 1.2.5 (AA) — manual
- **AVM by default.**

### 4.7 — Every time-based medium is clearly identifiable — WCAG 1.1.1 (A) — partial
- **Provable NC:** medium with no title, label or adjacent text identifying it (the reader lands on "video" without knowing which one).
- **Expected:** title or introductory text right next to the medium.

### 4.8 — Every non-time-based medium has, where necessary, an alternative — WCAG 1.1.1 (A) — partial
- **Applies to:** embedded interactive content (application `<canvas>`, non-image `<object>`/`<embed>`, third-party mini-app).
- **Provable NC:** such content without any detectable alternative (link, equivalent content). Otherwise AVM.

### 4.9 — The alternative of the non-time-based medium is relevant — WCAG 1.1.1 (A) — manual
- **AVM by default.**

### 4.10 — Every automatically triggered sound is controllable — WCAG 1.4.2 (A) — static
- **NC if:** `autoplay` without `muted` on `<video>`/`<audio>`, `play()` called on load without user action, with no obvious adjacent stop/pause button.
- **NA if** no automatic sound triggering.

### 4.11 — Playback of every time-based medium is controllable with the keyboard and the pointer — WCAG 2.1.1 (A), 2.1.2 (A) — partial
- **Provable NC:** `<video>`/`<audio>` without the `controls` attribute AND without a custom player providing controls; custom player whose controls are `<div onClick>` elements with no role and no keyboard handling (overlaps 7.3, but here for the media CONTROLS).
- **AVM:** third-party players (their controls live outside your code).

### 4.12 — Every non-time-based medium is controllable with the keyboard and the pointer — WCAG 2.1.1 (A), 2.1.2 (A) — partial
- Same logic as 4.11 for non-time-based interactive content; mouse-only `<canvas>` interactions (`mouse*`/`touch*` listeners with no keyboard equivalent) = provable NC.

### 4.13 — Every medium is compatible with assistive technologies — WCAG 4.1.2 (A) — partial
- **Provable NC:** custom player whose controls expose no accessible name, role or state (`aria-pressed`, `aria-label` absent everywhere).
- **AVM:** actual rendering by screen readers.
