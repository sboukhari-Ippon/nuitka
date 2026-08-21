# Use cases — `Safe-Coding.py`

Audience: a developer driving the code factory ("universal verdict" variant, valid for the 6 flavors FR/ENG × Ubuntu/MacOS/Windows; does not cover `Coding-Without-Tests.py`).

What the script does in one sentence: it turns a raw need (`need.md`) into a human-validated business spec, then into an implementation plan made of bounded micro-phases, then into code produced phase by phase by an agent — each phase being judged by the **actual execution** of the verification command (compilation + full suite), never by an LLM. The whole thing is designed to make a small model competitive: context sliced per phase, strict formats, decisions made upstream by the agents that have the context.

This document assumes an already-installed factory and a structured project: for that, see `INSTALL.md` and `README.md`.

## What to know before anything else: file-based resume

The run's state lives in files, not in memory:

| File | Role | On relaunch |
|---|---|---|
| `spec.md` + `.spec_approved` | Spec validated by the human | Step 1 skipped (without the approval sentinel: re-validation requested) |
| `plan.md` | Architect's plan | Step 2 skipped |
| `blackboard.yaml` | Phase state (`status`/`verdict`), verification command, feedback | Step 3 skipped; `DONE`+`OK` phases skipped in production |

("Sentinel": a simple witness file whose presence materializes a signal — here the human approval of the spec; in production, the end of an agent's task.)

Consequence: **relaunching the script never redoes what is already validated**. Every case below relies on this. Conversely, deleting one of these files forces the corresponding step to be regenerated.

Two human gates (and only two): the y/n on the spec (step 1) and the y/n on the blackboard (step 3). The plan has no dedicated pause: see UC3.

---

## UC0 — Challenge the need (before even the spec)

The most upstream gate is the cheapest of all: a vague need costs a spec, a plan and phases; a question settled here costs nothing.

1. Write `need.md`, then run `python3 Challenge-Need.py` (opt-in: no other pipeline depends on it).
2. An agent with a fresh context produces `need_review.md`: ambiguities, contradictions, grey areas, assumptions, questions to settle — each point marked `[BLOCKING]` or `[MINOR]`, every quote of the need checked mechanically (an invented quote is rejected).
3. **Single gate**: endorse the review (`y`). It changes nothing: settle the `[BLOCKING]` questions, update `need.md` YOURSELF, then relaunch the pipeline of your choice (`Spec.py`, `Safe-Coding.py`…).

## UC1 — Normal usage: from need to delivered code

1. Write `need.md` at the root of the target project (be precise, but it is the spec validation that locks the scope).
2. Run `python3 Safe-Coding.py` (venv activated).
3. **Gate 1**: review `spec.md` ("Assumptions & Questions" and "Out of scope" sections first), type `y`.
4. **Gate 2**: review the blackboard summary (phases, `verify_cmd`, US — user stories — coverage), type `y`.
5. The script carries on by itself: executable scaffold (step 0), phase-by-phase production (3 attempts max each, verdict = exit code of `verify_cmd`), final refactoring re-verified.
6. At the end: review the code (never a black box) and `refactoring_report.md`.

Optional live monitoring: `tmux attach -t <session-name>` in another terminal (name printed at launch; `Ctrl+B` then `D` to leave the display without killing the AI). Two projects can run in parallel: each project gets its own tmux session.

## UC2 — Challenging the spec

This is **the cheapest place** to fix things: a misunderstood requirement rejected here avoids paying for plan + blackboard + production. At the step 1 y/n:

- **Adjustment**: edit `spec.md` directly in another terminal (rephrase a US, add an acceptance criterion, harden the "Out of scope"), then type `y`.
- **Fundamental disagreement**: type `n` (clean stop), refine `need.md`, delete `spec.md`, relaunch. The PO Agent regenerates a spec from the enriched need.

## UC3 — Challenging the blackboard (and the plan)

The blackboard is a **mechanical copy** of the plan: challenging it means challenging the Architect's decisions. That is why the plan has no dedicated pause — the blackboard y/n covers both. Two levels of intervention:

- **Small adjustment** (fix `verify_cmd`, touch up a checklist, remove a hallucinated skill flagged by the script): edit `blackboard.yaml` while the prompt waits. The script detects the edit, reloads, re-validates the structure and asks for confirmation again — type `y` once satisfied.
- **Deep rework** (re-slice the phases, change the test strategy): type `n`, edit `plan.md` (Markdown, more comfortable than YAML), **delete `blackboard.yaml`**, relaunch. The compiler regenerates it from your edited plan; the existing spec and plan are reused as is.

Decision aids printed before the y/n: structural anomalies (blocking), missing non-critical fields, spec → phases traceability (a US covered by no phase = requirement possibly forgotten by the Architect; a US referenced but absent from the spec = probable compiler hallucination).

## UC4 — Tests break during production (regression)

Thanks to the universal verdict (every phase = compilation + full suite), a regression is detected **at the phase that introduces it**, not at the end of the run. What happens then, in order:

1. The script **does not stop right away**: the runner's real output (truncated head + tail) is sent back to the coder agent as feedback, and it retries — up to **3 attempts** per phase. Most regressions get absorbed here without you.
2. Only after 3 failures: clean stop. The phase is marked `REJECTED` in `blackboard.yaml` with the last feedback, the tmux session is killed, and the failure message reminds you that the already-green phases will be resumed automatically.

Your turn — diagnose first (the `critic_feedback` in `blackboard.yaml` holds the last output), then choose:

- **The simplest: `python3 Guided-Fix.py`** (UC11) — it does everything below for you: AI diagnosis of the broken behaviors, guided regression/evolution arbitration, repair under git guards, `FIXED` marker revalidated on relaunch.
- **Genuine regression, the model is stuck**: switch to a model one notch above (`/model` in the TUI, or the harness config file) and relaunch. Automatic resume at the faulty phase, 3 fresh attempts.
- **You fix the code yourself**: finish the phase by hand, then run yourself, from the project root, the command stored in the `verify_cmd` field of `blackboard.yaml` (exit code 0 = green). Then mark the phase `status: FIXED` (safer than hand-stamping `DONE`/`OK`: MAIsterMind will revalidate it by execution on relaunch, without re-paying a coder) — or launch `Guided-Fix.py`, which observes the green, sets the marker and commits for you.
- **The test itself is bad** (false assertion, brittle test): fix or delete the test yourself — agents are forbidden from weakening a test, you are not. If you delete some, adjust (or remove) `last_test_count` in `blackboard.yaml`, otherwise the "non-decreasing test count" guard will wrongly reject the next phase.

Landmarks for these manual edits — here is where the fields above live in `blackboard.yaml`:

```yaml
verify_cmd: "npm test"         # universal verdict command (root level)
last_test_count: 42            # "non-decreasing test count" guard
protected_test_files:          # produced by green tests phases (see UC8)
  - src/__tests__/cart.test.ts
phases:
  - id: 3
    name: "Cart computation"
    status: DONE               # TODO / IN_PROGRESS / DONE / FIXED (repaired, to revalidate)
    verdict: OK                # PENDING / OK / REJECTED / PENDING_RECHECK
    critic_feedback: ""        # last verification output on failure
```

Neighboring cases, same deferred-stop logic: a verification timeout is treated as an infrastructure incident (attempt **not** consumed, immediate re-verification, giving up only after 3 persistent timeouts); a regression introduced by the final refactoring triggers a dedicated correction loop (3 attempts), then an **automatic git rollback** to the all-phases-green state if it fails.

## UC5 — Resuming after an interruption (Ctrl-C, crash, power loss)

Simply relaunch the script: everything resumes by files (see the intro table). Two protections to know about:

- A spec that is present but **never approved** (run interrupted during the y/n) goes through human validation again instead of being taken for granted.
- A corrupt `blackboard.yaml` (kill during a write, made rare by the atomic write) causes a clean stop with instructions: fix it or delete it (it will be regenerated from `plan.md`).

## UC6 — Two-step workflow: big model to think, small model to produce

Steps 1 to 3 (spec, plan, blackboard) are high-leverage one-shots; production is iterative and tolerates a small model. File-based resume makes the switch trivial:

1. Configure a **big model** (`/model` in the TUI, or the harness config file), launch, validate the spec… and answer `n` at the blackboard y/n to stop cleanly there.
2. Switch to the **small model**, relaunch: spec, plan and blackboard are reused as is, production starts after your `y`.

Variant with no `n` to type: `Technical-Plan.py` runs steps 1 to 3 then stops on its own at the validated blackboard (and `Spec.py` stops as soon as the spec is approved) — same files, same resume.

## UC7 — The scaffold (step 0) never completes: suspect the model, not the code

The scaffold is the simplest request of the run (2-3 files + a sentinel): it is the model's **smoke test**. If it fails, the problem is almost always tool calling (frequent on small local models: the tool call is printed as text instead of being executed). The script prints the last TUI screen to diagnose without attaching to the session; confirm with `tmux attach` if needed, change model, relaunch.

## UC8 — Arbitrating a false positive of the mechanical guards (git)

Files produced by a green `tests` phase become protected: a `feature` phase that modifies one is rejected and the files are restored. Known false positive: a legitimately shared test helper. The feedback names the files — you arbitrate: remove the file from `protected_test_files` in `blackboard.yaml` (during a stop if needed), then let the run continue or relaunch.

## UC9 — Documenting an existing project before evolving it

You are picking up a project (legacy, a validated prototype, code inherited from another team) and you want to know what it DOES before touching it. Launch `python3 Documentation.py` from its root (no `need.md` required):

1. The scope (code files + tests) is discovered by the orchestrator and confirmed with a y/n before paying for a single agent.
2. A mapper proposes a breakdown into functional zones (`doc_map.yaml`) — checked by the script (full coverage, a "Miscellaneous" zone for the remainder), then validated by you (the YAML is editable before the `y`: rename, re-split, reorder — the order of the zones becomes the reading order).
3. One documentation pass per zone (context reset between each), then a 100% Python assembly produce **`documentation.md`** at the root: features sourced as `file:line`, acceptance tests in Given/When/Then form with a **Covered** status (an existing test verifies them, cited) or **Proposed** (to be written — the coverage appendix gives the count: that is your test backlog).

Natural chaining: with the documentation in hand, describe the evolution in `need.md` and chain with `Technical-Plan.py` (big model) then `Safe-Coding.py` (small model) — see UC6. After the evolution, delete the files of the touched zones in `doc_zones/` and relaunch: only those zones are re-documented, and the assembly is redone.

---

## UC10 — Pre-auditing the accessibility of an existing interface (RGAA / EAA)

A client must reach compliance (a legal obligation extended to the private sector since June 2025 by the European Accessibility Act), or you want to quantify the accessibility debt of a front-end before a remediation quote. Launch `python3 Audit-A11Y-RGAA.py` from the project root (no `need.md` required):

1. The UI scope AND the routing of the 13 RGAA topics are computed by the orchestrator (regex triggers: no video in the code → the Multimedia pack is never paid for), then confirmed with a y/n before any agent is paid.
2. A cartographer splits the files into base layer / shared components / screen zones (`a11y_map.yaml`, editable) — the map's y/n displays the EXACT count of passes before paying.
3. One audit pass per (topic × compartment), each checked by a mechanical parser (a C/NC/NA/AVM verdict for EVERY criterion of the pack, findings located `file:line` with an **exact excerpt verified in the files** — badge ✓ verified / ⚠️ to verify on every finding), then a 100% Python aggregation produce **`accessibility_audit_report.md`**: compliance rate as a range, non-compliances by impact (1 to 4) with fixes, checklist of the remaining manual checks — plus **`declaration_accessibilite.md`**, a pre-filled skeleton to complete.
4. A pass that does not succeed after 3 attempts does not kill the run: its criteria come out as cautious AVM, the report carries a "PARTIAL report" banner with the appendix of passes to replay, and a relaunch replays only what is missing (2 consecutive failures or > 30% failures = stop, the model is stalling).

Worth knowing: this is a static PRE-audit — criteria that cannot be decided from the code alone are marked AVM (requires manual verification: keyboard, screen reader, 200% zoom), never guessed; the report lists precisely this verification debt. This variant audits against the FRENCH legal framework RGAA 4.1.2, in English (the per-criterion WCAG 2.1 mapping is quoted by reference); a native WCAG pack set may arrive later as a separate `-WCAG` entry point. After remediation, `python3 Audit-A11Y-RGAA.py --rejouer-modifiees <ref>` invalidates only the passes where a file appears in `git diff --name-only <ref>` (without git: delete the files of the affected passes in `audit_a11y/`) and relaunch: only those passes are replayed, the aggregation is redone.

## UC11 — Repairing a red-suite halt with guided arbitration (`Guided-Fix.py`)

The run halted (phase `REJECTED` after 3 attempts, run killed mid-phase, unresolved post-refactoring regression). Instead of UC4's manual surgery, launch `python3 Guided-Fix.py` from the root:

1. **Entry verdict then diagnosis**: Python re-runs `verify_cmd` — already green (you repaired by hand?) → it simply offers to set the `FIXED` marker without paying for an agent; timeout → infra incident reported, no arbitration to render. On confirmed red, the state at halt is committed (`wip(fix)`), then an agent writes **`fix_report-<uid>.md`**: the failures grouped by **broken business behavior**, each with its red tests, the spec criterion concerned, the suspect change (exact diff of the faulty phase) and an AI reading.
2. **Triage**: for each behavior, you answer in the console — `r` (UNWANTED regression: the tests are right, the code will be fixed), `e` (desired evolution: the code is right, spec then tests will be aligned), `o` (display the detail right here). The question to ask yourself each time: "is the spec's criterion still right?". A recap of the action plan is confirmed before paying for any agent (`n` redoes the triage, `q` abandons without changing anything).
3. **Guarded repair**: evolutions first — update of `spec.md` proposed by an agent and validated by you (diff displayed, file editable before the `y`; `n` restores), then adaptation of the tests with the production FROZEN by git — regressions next: code fix with ALL test files FROZEN. The verdict remains Python's execution of `verify_cmd` (3 rounds max).
4. **Handshake**: on green, the faulty phase is marked `FIXED` — never `DONE`: it is a claim, not a verdict — and everything is committed, report included (audit trail). Relaunch `python3 Safe-Coding.py` yourself: it REVALIDATES the phase by execution (without re-paying a coder) then continues the run at the next phase.

Worth knowing: each session produces a uniquely-named report — the history of your arbitrations survives relaunches, unlike `failReport.md` which MAIsterMind purges at startup. If the repair does not converge, the report documents the failure and your decisions: bump the model one notch and relaunch `Guided-Fix.py` (new triage), or fix by hand then relaunch it (it will observe the green and set the marker without paying for an agent).

---

## Recap: who stops, who retries, who resumes

| Event | Script behavior | Your lever |
|---|---|---|
| Spec or blackboard to validate | y/n pause | Edit the file before `y`, or `n` to go back upstream |
| Red verification (regression included) | 3 attempts with real feedback, then clean stop | `Guided-Fix.py` (UC11: guided arbitration) or manual fix (UC4), then relaunch: resume at the faulty phase |
| Verification timeout | Re-verification (attempt not consumed), giving up after 3 persistent timeouts | Check machine/command, relaunch |
| Silent scaffold | Stop + tool-calling diagnosis printed | Change model, relaunch |
| Refacto breaking the suite | Correction loop, then automatic git rollback | Inspect `refactoring_report.md` |
| Interruption (Ctrl-C, crash) | Clean stop (tmux session killed) | Relaunch: file-based resume |
