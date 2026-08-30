# MAIsterMind (Ubuntu / Debian Version)
*By Selim Boukhari* — [LinkedIn](https://www.linkedin.com/in/selim-boukhari-6356b949/?locale=en)

An AI-driven code factory: from the need to verified code, through structured pipelines
that YOU validate at the decisive moments — with OpenCode **or** Codex CLI (beta, see below).
You integrate nothing into your project: the app equips it in one click and drives
everything from the browser.

> 📄 **License**: non-commercial, personal, or educational use only — see the `LICENSE`
> file. Any commercial use requires prior written agreement from the author.

## Get started in 5 steps

1. **Install** — one command, zero `chmod`, no Python installation:
   `sh install.sh` (apt prerequisites, permissions, applications-menu entry). Missing harness? `INSTALL.md`.
2. **Open the app**: "MAIsterMind" in the applications menu — the browser opens by itself.
3. **Select your target project** (new or existing) and **equip it**: the "Equip" button
   with the harness of your choice — skills and artifacts copied, nothing else.
   OpenCode is the reference choice; **Codex CLI is in beta**: less proven on real runs, it needs
   user feedback (gates, permissions, models) — report what you observe.
4. **Describe your need** in `need.md` at the project root — only to produce or frame:
   audits, documentation and repair launch WITHOUT `need.md`.
5. **Adapt the engine's skills if your stack is not Java/Spring + React/TS** (`Skills-Adaptation`, at
   the top of the Library), then **pick a category (Coding, Design, Product), your script, and launch.** You answer the validation gates from the browser;
   the run lives in tmux (closing the app kills nothing) and leaves a journal
   `.mm-runs/<id>/`. The AI model is set in the harness: `/model` in the TUI, or the
   project's config file (`.opencode/opencode.json` / `.codex/config.toml`).

## Which script for which need?

In the app, step 3 of the Library shows skills adaptation first, then the categories: click
**Coding**, **Design** or **Product** to see its scripts.

### Skills adaptation — do this first if your stack is not Java/Spring + React/TS (WIP)

| Need | Script | need.md |
|---|---|---|
| Adapt the **engine's** technical skills (coding and testing, back and front) to YOUR stack: they apply to this project and to every project equipped from now on ("Update equipment" for the others) | `Skills-Adaptation` | no |

> **One engine = one stack.** For projects on distinct stacks, duplicate the tool folder (the
> extracted archive) and adapt each copy.
>
> **A good model to adapt**, preferably frontier: these skills shape every later run. The
> questionnaire's "target model" is the one that will consume them, not the one writing them.

### Coding — from need to verified code (planning included: spec, plan, blackboard)

| Need | Script | need.md |
|---|---|---|
| Develop acceptance-first by user-story batches (inspired by ATDD) ⭐ | `Acceptance-First` | required |
| Develop test-first by red → green → refactor cycles (inspired by TDD) ⭐ | `Test-First` | required |
| Develop with tests, universal verdict | `Coding` | required |
| Develop without tests (POC, throwaway script, glue) | `Coding-Without-Tests` | required |
| Brownfield/legacy: the "regression or evolution?" arbitration is built into all three (impact review, triage) | `Coding` / `Test-First` / `Acceptance-First` | required |
| Think with a big model, then produce with a small one | a factory, `n` at the blackboard gate, model switch, relaunch (see `useCasesEng.md`, UC6) | required |
| Repair a run stopped on a red suite (after the fact; in-run arbitration is already built into the three factories) | `Guided-Fix` (beta) | no |

### Design — prototype and interface audits

| Need | Script | need.md |
|---|---|---|
| Clickable HTML/CSS/JS prototype (designers) | `Design-Prototype` (beta) | required |
| UX audit — Nielsen's 10 heuristics (read-only) | `Audit-Design` (beta) | no |
| RGAA 4.1.2 accessibility pre-audit (read-only) | `Pre-Audit-A11Y-RGAA` (beta) | no |

### Product — need, spec, documentation

| Need | Script | need.md |
|---|---|---|
| Challenge the need BEFORE paying for a spec | `Challenge-Need` | required |
| Frame: the spec alone, validated with the business | `Spec` | required |
| Document an existing project (read-only) | `Documentation` (beta) | no |

## To go further

- **`SCHEMAS.html`** — HOW each pipeline works: gates, verdicts, deliverables, as
  diagrams (one tab per script).
- **`useCasesEng.md`** — concrete situations: resuming after an interruption, breaking
  tests, two-stage workflow, arbitrated repair.
- **`INSTALL.md`** — prerequisites and harness installation.

## Expert mode (terminal)

The binaries are usable directly, without Python or a venv, from the root of YOUR
equipped project:

```bash
cd /path/to/your/project
/path/to/MAIsterMind/engine/Coding
```

`MM_AGENT_HARNESS=opencode|codex` forces the harness for one launch; otherwise it is
inferred from the equipment. Follow a run live: `tmux attach -t <session>` (name shown at
launch; `Ctrl+B` then `D` to leave without stopping the AI). In dev, from the source
repository: `python3 engine/Coding.py`.

Quick troubleshooting:
- **Force-stop a run**: `tmux kill-session -t <session>`.
- **Resume after a crash**: just relaunch — everything resumes from files, nothing
  validated is redone.
- **The scaffold (step 0) never completes**: it is the model's smoke test — if it writes
  its tool calls as text instead of executing them, switch models.
- **Understand a run after the fact**: journal `.mm-runs/<id>/` (chronology, frozen
  artifacts, summary) — `MM_AUDIT=0` to disable.

## The rules of the game

- **The verdict is real execution** (compilation + full suite) — never an LLM grading
  itself.
- **You validate at the high-leverage moments**: spec, blackboard, audit maps — every
  file is editable before your `y`.
- **Everything resumes from files**: relaunching never redoes what is validated; deleting
  a deliverable regenerates only the corresponding step.
- **Under git, the run is guarded**: a commit per green phase, protected test files,
  automatic rollback of a refactor that breaks the suite — without git, everything works,
  without these nets.
- **Read the produced code** — never a black box.
