# Onboarding doc template

File name: `YYYY-MM-DD-<repo>-architecture.md`. At the top, record the repo URL, the commit hash, and the version, so readers know when the doc may be out of date.

Use Mermaid for diagrams (renders on GitHub, VS Code, Obsidian). Pick the diagram type by what it shows:

| Content | Diagram |
|---|---|
| Big picture / packages / modules | `flowchart` with `subgraph` per layer (e.g. server vs. browser) |
| Package or module dependencies | `flowchart TD` |
| Data shapes and their relations | `classDiagram` (fields, inheritance, composition) |
| How data transforms step by step | `flowchart LR` with function names on the arrows |
| Runtime behavior (requests, hot reload, sync) | `sequenceDiagram` |
| Lifecycle / modes | `stateDiagram-v2` (only if the repo has real states) |

Keep Mermaid safe to render: avoid `"`, `|`, and `{}` inside class members; write unions as `a / b / c`; use `~T~` for generics.

## Sections

1. **Big picture.** One diagram plus 2–3 sentences: what goes in, what comes out.
2. **Packages / modules.** Dependency graph plus a one-line purpose for each.
3. **Data shapes.**
   - A class diagram of the core types, with file paths.
   - An **input → output table**, one row per shape: what it's built from, the function that builds it (`file`), and who uses it next.
   - A data-flow diagram of the main chain, including any write-back path.
4. **Pipelines and runtime flows.** One diagram for each flow the user's task touches (e.g. request handling, build, hot reload, sync).
5. **Commands.** What each CLI command or script calls.
6. **Where to look first.** A table of "to understand X → start at `file`".
7. **Starter tasks.** 3–5 items tied to the user's goal: good-first issues, open plans or TODOs, missing tests or docs. Give the location and a size for each, and confirm each one still applies.
8. **Gotchas.** Non-obvious conventions from the docs (e.g. tests need a build first, generated folders must not be edited by hand).
