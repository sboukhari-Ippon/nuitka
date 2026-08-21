# T05: Tables — RGAA criteria 5.1 to 5.8

Grid adapted from RGAA 4.1.2 (DINUM, accessibilite.numerique.gouv.fr, Open Licence 2.0). WCAG 2.1 equivalence and static testability indicated for each criterion.

## Pack method
Classify every `<table>` (and `role="table"/"grid"`) in your files: DATA table (the information depends on the row/column intersection) or LAYOUT table (rare these days). DataGrid/DataTable-style components count: audit the HTML they emit when it is visible in the code, otherwise the usage (header props, caption).

## Criteria

### 5.1 — Every complex data table has a summary — WCAG 1.3.1 (A) — partial
- **Complex =** headers on several levels (`colspan`/`rowspan` in the headers, nested two-way entries).
- **Provable NC:** complex table without a summary (detailed `<caption>` content or `aria-describedby` pointing to a text).
- **NA if** no complex table.

### 5.2 — The summary of the complex table is relevant — WCAG 1.3.1 (A) — manual
- **AVM by default**; **Blatant NC:** placeholder or unrelated summary.

### 5.3 — The linearised content of a layout table remains understandable — WCAG 1.3.2 (A), 4.1.2 (A) — partial
- **Provable NC:** layout table whose cell order obviously breaks the reading order (content split across columns); layout table without `role="presentation"`.
- **NA if** no layout table.

### 5.4 — The title of a data table is properly associated — WCAG 1.3.1 (A) — static
- **NC if:** data table preceded by a visible title (`<h*>`, bold `<p>`, div) NOT associated with it: expected `<caption>` (or `aria-labelledby` pointing to the title).
- **NA if** the tables have no visible title (then mark 5.5 NA as well).

### 5.5 — The title of the data table is relevant — WCAG 1.3.1 (A) — partial
- **Provable NC:** generic `<caption>` ("Table", "Data", technical identifier). Otherwise AVM.

### 5.6 — Every column and row header is properly declared — WCAG 1.3.1 (A) — static
- **NC if:** first row/column that is obviously a header row/column coded as `<td>` (or styled divs) instead of `<th>` (or `role="columnheader"/"rowheader"`).
- **Check:** partial header cells (`<th>` in the middle of the table) and multi-header cells are properly structured as `<td>`/`<th>`.

### 5.7 — The appropriate cell/header association technique is used — WCAG 1.3.1 (A) — static
- **NC if:** two-way table whose `<th>` elements have no `scope` (`col`/`row`); complex table without a consistent `headers`/`id` mechanism (referenced ids that do not exist = outright NC); `scope` set on `<td>` elements.
- **Simple rule:** simple table → `scope`; complex table → `headers`/`id`.

### 5.8 — A layout table uses no element specific to data tables — WCAG 1.3.1 (A) — static
- **NC if:** layout table containing `<th>`, `<caption>`, `summary`, `scope`, `headers`, or without `role="presentation"` although it is purely presentational.
- **NA if** no layout table.
