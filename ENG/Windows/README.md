# MAIsterMind (Windows via WSL Version)
*By Selim Boukhari* — [LinkedIn](https://www.linkedin.com/in/selim-boukhari-6356b949/?locale=en)

An AI-driven code factory: from the need to verified code, through structured pipelines
that YOU validate at the decisive moments — with OpenCode **or** Codex CLI, your choice.
You integrate nothing into your project: the app equips it in one click and drives
everything from the browser.

> 📄 **License**: non-commercial, personal, or educational use only — see the `LICENSE`
> file. Any commercial use requires prior written agreement from the author.

## Get started in 5 steps

1. **Install** — one command, zero `chmod`, no Python installation:
   `sh install.sh` from a WSL 2 (Ubuntu) terminal — apt prerequisites, permissions set. Missing harness? `INSTALL.md`.
2. **Open the app**: double-click `MAIsterMind.bat` — the browser opens by itself.
3. **Select your target project** (new or existing) and **equip it**: the "Equip" button
   with the harness of your choice — skills and artifacts copied, nothing else.
4. **Describe your need** in `need.md` at the project root — only to produce or frame:
   audits, documentation and repair launch WITHOUT `need.md`.
5. **Pick your script and launch.** You answer the validation gates from the browser;
   the run lives in tmux (closing the app kills nothing) and leaves a journal
   `.mm-runs/<id>/`. The AI model is set in the harness: `/model` in the TUI, or the
   project's config file (`.opencode/opencode.json` / `.codex/config.toml`).

## Which script for which need?

| Need | Script | need.md |
|---|---|---|
| Develop with tests — the most robust mode | `Coding` | required |
| Develop without tests (POC, throwaway script, glue) | `Coding-Without-Tests` | required |
| Develop test-first by red → green → refactor cycles (inspired by TDD) | `Test-First` | required |
| Develop acceptance-first by user-story batches (inspired by ATDD) | `Acceptance-First` | required |
| Clickable HTML/CSS/JS prototype (designers) | `Design-Prototype` (beta) | required |
| Brownfield/legacy: the "regression or evolution?" arbitration is built into all three (impact review, triage) | `Coding` / `Test-First` / `Acceptance-First` | required |
| Challenge the need BEFORE paying for a spec | `Challenge-Need` | required |
| Frame: the spec alone, validated with the business | `Spec` | required |
| Think with a big model, then produce with a small one | `Technical-Plan`, then a factory | required |
| Document an existing project (read-only) | `Documentation` (beta) | no |
| UX audit — Nielsen's 10 heuristics (read-only) | `Audit-Design` (beta) | no |
| RGAA 4.1.2 accessibility pre-audit (read-only) | `Pre-Audit-A11Y-RGAA` (beta) | no |
| Repair a run stopped on a red suite | `Guided-Fix` | no |
| Adapt the shipped skills to YOUR stack | `Skills-Adaptation` | no |

## To go further

- **`SCHEMAS.html`** — HOW each pipeline works: gates, verdicts, deliverables, as
  diagrams (one tab per script).
- **`useCasesEng.md`** — concrete situations: resuming after an interruption, breaking
  tests, two-stage workflow, arbitrated repair.
- **`INSTALL.md`** — prerequisites and harness installation.

## Expert mode (WSL terminal)

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
