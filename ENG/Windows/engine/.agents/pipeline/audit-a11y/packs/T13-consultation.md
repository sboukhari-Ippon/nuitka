# T13: Consultation — RGAA criteria 13.1 to 13.12

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
The pack for "time-based and touch-based" behaviours: time limits, motion, gestures, downloadable documents. Plenty of legitimate NA here: force nothing, but hunt down the provable classics — meta refresh, auto-playing carousel with no pause, action on `mousedown`, delayed redirect.

## Criteria

### 13.1 — The user controls every time limit that changes the content — WCAG 2.2.1 (A), 2.2.2 (A) — partial
- **Provable NC:** `<meta http-equiv="refresh" content="N">` with N > 0 (delayed refresh or redirect: outright NC); session expiry/logout on `setTimeout`/`setInterval` with no warning and no way to extend; content that updates automatically with no control (auto-refreshing news feed).
- **NA if** no time limit.

### 13.2 — No window opens without user action — WCAG 3.2.1 (A) — static
- **NC if:** `window.open()` (or equivalent) triggered on load (mount `useEffect`, `mounted`, `DOMContentLoaded`, inline script) and not within a user-action handler.
- **NA if** no programmatic opening. (A `target="_blank"` on a clicked link is a user action: out of scope here.)

### 13.3 — Every downloadable office document has, where necessary, an accessible version — WCAG 1.1.1 (A), 1.3.1 (A), 1.3.2 (A), 2.4.1 (A), 2.4.3 (A), 3.1.1 (A), 4.1.2 (A) — partial
- **Look for:** links to `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.odt`…
- **Provable NC:** a document carrying essential information with no detectable alternative (HTML version, equivalent page).
- **AVM:** the accessibility of the document itself cannot be read from the site's code.

### 13.4 — The accessible version offers the same information — WCAG 1.1.1 (A), 1.3.1 (A), 1.3.2 (A), 2.4.1 (A), 2.4.3 (A), 3.1.1 (A), 4.1.2 (A) — manual
- **AVM by default**; **NA if** no office document.

### 13.5 — Every cryptic content (ASCII art, emoticon, cryptic syntax) has an alternative — WCAG 1.1.1 (A) — static
- **NC if:** text emoticons (`:-)`), ASCII art or cryptic syntax carrying meaning with no alternative (`title`, `aria-label`, adjacent text). Isolated information-bearing Unicode emojis are best wrapped (`role="img"` + `aria-label`).
- **NA if** no cryptic content (common).

### 13.6 — The alternative for cryptic content is relevant — WCAG 1.1.1 (A) — partial
- **Provable NC:** an unrelated alternative or a placeholder. **NA if** 13.5 is NA.

### 13.7 — Sudden changes of luminosity and flash effects are used correctly — WCAG 2.3.1 (A) — partial
- **Provable NC:** an identifiable fast-blinking CSS/JS animation (visible alternation > 3 times per second: very short looping `@keyframes` on solid fills).
- **AVM:** video and GIF content (their content cannot be read from the code). **NA if** no effect of this kind.

### 13.8 — Every moving or blinking content is controllable — WCAG 2.2.1 (A), 2.2.2 (A) — static
- **NC if:** an AUTOMATICALLY scrolling carousel/slideshow with no pause/stop button (`autoplay`/`interval` props of carousel libs, slide `setInterval`); an infinite animation (`animation: … infinite`) on meaningful content with no respect for `prefers-reduced-motion` and no control; `<marquee>`/`<blink>` (outright NC).
- **NA if** nothing moves automatically for more than 5 seconds.

### 13.9 — Content is consultable regardless of screen orientation — WCAG 1.3.4 (AA) — partial
- **Provable NC:** orientation lock (`screen.orientation.lock(…)`, manifest `"orientation": "portrait"`); media queries that HIDE content in one orientation ("rotate your device").
- **NA/AVM otherwise.**

### 13.10 — Complex gestures have a simple alternative — WCAG 2.5.1 (A) — partial
- **Provable NC:** functionality accessible ONLY by multipoint gesture or path (custom pinch-to-zoom, mandatory swipe on a carousel with no previous/next buttons, drag-and-drop with no button equivalent).
- **NA if** no gesture interaction.

### 13.11 — Pointer actions can be cancelled — WCAG 2.5.2 (A) — static
- **NC if:** a final action triggered on the DOWN event (`mousedown`, `pointerdown`, `touchstart`) instead of the up event (`click`, `mouseup`, `pointerup`) — the user can no longer drag off the control to cancel. (Legitimate exceptions: continuous press such as piano/slider.)
- **NA if** no down-event listener that triggers an action.

### 13.12 — Functionality triggered by device motion has an alternative — WCAG 2.5.4 (A) — partial
- **Provable NC:** `devicemotion`/`deviceorientation` (shake, tilt) triggering a function WITH no equivalent button and no disable setting.
- **NA if** no motion detection (the most common case).
