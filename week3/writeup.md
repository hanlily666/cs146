# Week 3 Write-up

**Skill name**, and the repo you targeted:
> `repo-onboarding` (in `week3/repo-onboarding/`), targeting [slidevjs/slidev](https://github.com/slidevjs/slidev).


## Part I: The Workflow

**What the workflow is**, and why it's worth encoding:
> Getting a new person productive in an unfamiliar repo: clone it in the right place, install everything, find the docs the repo already has, then explain the architecture and data shapes and point to a first task. I'll do this every time I pick up a new codebase, and the steps are the same each time, but the details (package manager, install scripts, where docs live) change per repo. Without a skill I'd re-explain the same checklist in chat each time.

**The decision point** in it (what the agent has to judge, not just execute):
> - **Who the user is.** A contributor should fork and add `upstream`; a learner can do a plain clone; a non-coder may only need a plain-language overview with no setup.
> - **Where to clone.** If the path is inside an existing git repo (my `cs146/weekly_assignment` repo), the agent has to notice and pick a path outside it.
> - **How to fix a missing tool.** Install the pinned version permanently, rather than a one-off workaround that works once.
> - **Whether the repo already covers onboarding.** Follow `CONTRIBUTING.md`, `AGENTS.md`, or a bundled skill instead of duplicating it.
> - **Which starter tasks are still real.** Planning docs go stale, so each suggested task has to be checked against the current code.

**What you learned running it manually** that you would not have guessed:
> - **Workarounds stick.** pnpm wasn't installed, and `corepack` failed with an outdated signing-key error. The agent switched to `npx pnpm@12.15.0`, which worked, but I'd have had to type that prefix on every command forever. Installing pnpm globally was the right fix, so the skill now says to install tools permanently and explains why.
> - **Install changes the repo.** slidev's `prepare` script syncs AI-agent skills on `pnpm install`. It rewrote a hash in `skills-lock.json` and created an untracked `.continue/` folder. I only found out by running `git status`, so the skill now reports install side effects.
> - **The repo documents its own gap.** `CONTRIBUTING.md` is short, and `plans/005-agents-md.md` says outright that there's no `AGENTS.md`/`CLAUDE.md` for new contributors. Reading the planning docs turned up more onboarding information than the official guide.
> - **Planning docs go stale.** `plans/README.md` lists TODOs written months earlier. I had to grep for each bug (for example the missing `await` at `loaders.ts:237`) before recommending it.
> - **Tests need a build first.** slidev's packages import each other's built `dist/`, so `pnpm test` fails on a fresh clone without `pnpm build`.
> - **The agent stops too often.** In a test session it paused to ask before finishing the build. Setup has one clear goal (build and tests pass), so I added a rule to finish setup without asking, except for things only the user can do.


## Part II: The Skill

**Your description**, verbatim:
```
Onboard a developer or non-coder onto an unfamiliar repository: set it up locally, explain its architecture, and point to where to start. Use when the user is new to a repo, wants diagrams of its architecture, data flow, or data shapes, or wants to start contributing.
```

**Why it's worded that way** (what a user would type to trigger it):
> The first sentence says what the skill does; the second says when. I describe situations ("new to a repo", "wants to start contributing") instead of listing exact phrases, because agents match descriptions by meaning, so "I want to contribute to this repo" and "what's the best way to understand this repo" both land without being quoted. I wrote "a repo" rather than "this repo" because the description is read before the agent knows which repo the user means. The riskiest part is "wants diagrams of its architecture, data flow, or data shapes", which could catch a single-diagram request on a repo the user already knows. I used that as my near-miss.

**Judgment encoded in the body** (what it says to do when things are ambiguous, and what not to do):
> - **Clarify first, in one message, and only what can't be inferred:** contributor vs. learner vs. non-coder, the user's task (so orientation focuses on the files it touches), and the clone path. The path is checked with `git -C <path> rev-parse --show-toplevel`, and the skill says never to nest a repo silently.
> - **The repo's docs take priority** over the agent's defaults. If the repo ships its own onboarding skill or guide, use it instead of duplicating it.
> - **Find the toolchain from the repo, not from habit:** the `packageManager`/`engines` fields, lockfiles, `.nvmrc`.
> - **Don't use workarounds** like `npx pnpm@x` or `corepack pnpm`, with the reason: the user would repeat them on every command.
> - **Finish setup without stopping to ask**, until build and tests pass. Stop only for passwords, credentials, or paid accounts.
> - **Report honestly:** pass/fail counts, failures with their output, and files the install scripts changed (`git status --short`), with an offer to revert.
> - **Ask before running the app**, since a dev server is optional and not part of setup.
> - **Non-coders get a short plain-language version** with no internals.
> - **Cite `file:line`, and only claim what was read.** Unverified points are marked. Starter tasks are confirmed to still apply.
> - **Stay read-only on the repo's source:** no commits, pushes, or PRs unless asked.

**Supporting files**, if any, and why they aren't inline:

| File | Contents | Why it's separate |
|---|---|---|
| `repo-onboarding/reference/onboarding-doc.md` | Outline of the onboarding doc (8 sections), which Mermaid diagram type to use for which content, the input → output table format for data shapes, and Mermaid rendering pitfalls | Only needed at the last step, when the doc is written. Keeping it out of `SKILL.md` keeps the body short, since the body loads every time the skill fires, even for a non-coder who never gets a doc. |


## Part III: Testing

**Triggering:**

| Prompt | Should fire? | Did it? |
|---|---|---|
| "i want to contribute to this repo" | Yes | Yes |
| "i want a architecture rundown of this repo" | Yes | Yes |
| "let me know the best way to understand this repo" | Yes | Yes |
| "Draw a sequence diagram of how hot reload works in slidev's loaders.ts." | No (near-miss) | No. The agent drew the diagram directly without starting onboarding (no clone or setup questions). |

**End-to-end run** on your repo, and the result:
```
TODO
```


## Submission
1. `Command (⌘) + F` for `TODO`. No results means you're done.
2. Confirm the skill directory itself is committed under `week3/`.
3. Push all changes to your remote repository and submit via Gradescope.
