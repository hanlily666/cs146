---
name: repo-onboarding
description: "Onboard a developer or non-coder onto an unfamiliar repository: set it up locally, explain its architecture, and point to where to start. Use when the user is new to a repo, wants diagrams of its architecture, data flow, or data shapes, or wants to start contributing."
---

# Repo Onboarding

## 1. Clarify first

Ask only what you can't infer, in one message:

- **Who and why.** A contributor should fork on GitHub, clone the fork, then `git remote add upstream <original-url>`. A learner or non-coder can do a plain clone, or skip setup entirely and get a plain-language overview.
- **Their task.** Ask what they want to work on. Focus the orientation on the files that task touches.
- **Where.** Confirm the clone path, then check it:
  ```bash
  git -C <path> rev-parse --show-toplevel   # prints a path → already inside a git repo
  ```
  If it is inside a repo, recommend a path outside it, or a `.gitignore` entry if the user wants it there. Never nest a repo silently.

If the repo is already cloned, skip to step 2.

## 2. Read what the repo already provides

Before installing anything, read: `CONTRIBUTING.md`, the README's development section, `AGENTS.md` / `CLAUDE.md`, `docs/`, and any planning folders (`plans/`, `ROADMAP.md`).

Also check for agent skills (`skills-lock.json`, `.claude/skills/`, `.agents/skills/`). Tell the user which ones help with onboarding. If the repo ships its own onboarding skill or guide, use it instead of duplicating it.

The repo's documented steps take priority over your defaults. Note gaps (e.g. a plan or issue saying the onboarding doc is missing); they're useful to the user.

## 3. Set up

Finish the whole setup without stopping to ask. If a step fails, fix it and keep going until the build and tests pass. Only stop for things only the user can do, like entering a password, credentials, or a paid account.

1. **Find the toolchain** from the repo, not from habit: `packageManager` and `engines` in `package.json`, the lockfile (`pnpm-lock.yaml`, `yarn.lock`, `package-lock.json`, `uv.lock`, `poetry.lock`), `.nvmrc`, `.python-version`, `pyproject.toml`.
2. **Install missing tools properly.** Install the pinned version the documented way (e.g. `npm i -g pnpm@<version>`), and tell the user what you installed. Don't use one-off workarounds like `npx pnpm@x` or `corepack pnpm`: the user would have to repeat them on every command, and they break in other terminals. If the documented install fails, explain why and suggest the next-best permanent fix.
3. **Install from the lockfile**, e.g. `pnpm install --frozen-lockfile`, `npm ci`, `uv sync`.
4. **Prove it works.** Run the repo's build and test commands (often one aggregate script, e.g. `pnpm verify`). Report pass/fail counts. If something fails, say so with the output; don't hide it.
5. **Report side effects.** Run `git status --short`. Install scripts (`prepare`, `postinstall`) can change tracked files or create folders; tell the user what changed and offer to revert.
6. **Ask before running the app** (dev server, demo). Give the exact command either way.

## 4. Explain

Write an onboarding doc to a location the user picks (default: a `research/` folder outside any assignment repo). Follow [reference/onboarding-doc.md](reference/onboarding-doc.md) for the outline and diagram types.

For a non-coder, give a short plain-language version instead: what the project does, the main parts, and how to try it. Skip internals.

## Rules

- **Cite `file:line`** and only claim what you read. If a lookup was skipped or blocked, mark that point as unverified.
- **Check starter tasks are still open** (e.g. grep for the bug a plan describes) before recommending them.
- **Stay read-only on the repo's source.** Don't commit, push, or open PRs unless asked.
