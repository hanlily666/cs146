# Week 4 Write-up

**Repo chosen** and link to your fork:
> [iamkun/dayjs](https://github.com/iamkun/dayjs). Fork: https://github.com/hanlily666/dayjs (branch `dev`, based on `436bde0`)

**File you wrote** (`AGENTS.md` or `CLAUDE.md`):
> `CLAUDE.md` (copy in this folder, and at the root of the fork)


## Part I: Exploration

| | Finding |
|---|---|
| Build / run | `npm run build` (Rollup builds each locale and plugin into its own UMD bundle, plus `dayjs.min.js`, then runs a `size-limit` check against 2.99 KB). On Node 23 this fails with `ERR_OSSL_EVP_UNSUPPORTED`; `NODE_OPTIONS=--openssl-legacy-provider npm run build` works (2.79 KB) and matches `release.yml`. `npm run babel` builds `esm/`. It's a library, so there is no app to run. |
| Test | `npm test` runs the timezone tests in 8 fixed TZs, then the full Jest suite in the **machine's** TZ with a 100% line-coverage gate. On my machine (PDT): 788 pass, 6 fail (`localizedFormat`, `dayOfYear`, 4 DST cases). `npx cross-env TZ=UTC jest` (UTC like CI): 794/794 pass, 93 suites. One file: `npx jest <file> --coverage=false`. |
| Lint / format | `npm run lint` (ESLint 4, airbnb-base, no semicolons, no trailing commas, no import extensions). It's also a `pre-commit` hook. Prettier is only used for `docs/**/*.md`. |
| Key directories and entry points | `src/index.js` (core `Dayjs` class, `dayjs()` factory, `extend`, `locale`), `src/plugin/<name>/index.js` (37 plugins with the signature `(option, DayjsClass, dayjsFactory)`), `src/locale/<code>.js` (143 locales), `types/` (hand-written `.d.ts`), `build/` (Rollup and ESM scripts), `test/` (Jest). |
| Unstated conventions | Every plugin needs `types/plugin/<name>.d.ts` (enforced by `test/plugin/declarations.test.js`). Every locale file must start with `// Language Name [code]` (parsed by `build/index.js` and `test/locale/keys.test.js`). Tests import `../../src`, compare against `moment@2.29.2`, and freeze time with `MockDate`. Private fields use a `$` prefix (`$d`, `$y`, `$L`). |
| Off-limits (generated, vendored) | Build output, all gitignored: `/plugin`, `/locale`, `/esm`, `/dayjs.min.js`, `/index.d.ts`, `/locale.json`. `CHANGELOG.md` is written by semantic-release. `patches/` patches a release dependency. Default branch is `dev`; pushing to `master` triggers a release. |

**How you figured it out**: what you asked the agent, what you had to read yourself:
> I used Claude Code to check which sample repos still had no agent config (TheAlgorithms/Python already has `AGENTS.md` and React has `CLAUDE.md`), fork dayjs, and read `package.json`, the CI workflows, `build/`, `.gitignore`, and sample plugins, locales and tests. Then I had it run every command instead of trusting `package.json`. Running them is how the two most useful findings came up: `npm test` fails outside UTC, and the build needs the OpenSSL flag on new Node. Neither is documented anywhere. It also showed that `npm install` rewrites `package-lock.json`, so `npm ci` is the right install. I read the README and `CONTRIBUTING.md` myself to decide what the file shouldn't repeat.


## Part II: The File

**Anything you deliberately left out**, and why:
> Anything the README or `CONTRIBUTING.md` already says (API usage, how to load plugins and locales, "run `npm run lint` / `npm test`", semantic commits, 100% coverage). I point to `CONTRIBUTING.md` instead. I also left out an architecture section, because the agent can find `src/plugin` and `src/locale` with one `ls`, and a list of plugins it can look up itself. I cut the "no semicolons" line too: `npm run lint` already enforces it, so the cost of leaving it out is one lint fix.

**The hardest call** on what to include:
> Setup. My first instinct was "follow the README," since people also install dayjs to use it in their own projects. But `CLAUDE.md` only loads for an agent working inside this repo, which means a contributor. The README's only setup step is `npm install dayjs --save`, which installs the published package and doesn't set up the source. So I wrote `npm ci` and explained why `npm install` is wrong here.


## Part III: Trying It Out

| # | Task | Did the agent use your commands and conventions? |
|---|---|---|
| 1 | Fresh session: "Run the full test suite and tell me the results." | TODO |
| 2 | Fresh session: "Add an `isWeekend` plugin that returns true if the date is a Saturday or Sunday." | TODO |

**One thing you'd change** about the file after watching it get used:
> TODO


## Submission
1. `Command (⌘) + F` for `TODO`. No results means you're done.
2. Push all changes to your remote repository and submit via Gradescope.
