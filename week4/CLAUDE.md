# CLAUDE.md

A ~2 KB immutable date library that mirrors Moment.js; extra features are opt-in plugins.

## Setup and conventions

- First-time setup: `npm ci` (not `npm install`, which rewrites `package-lock.json`).
- Code style, commit messages, and test expectations: follow `CONTRIBUTING.md`. Also:
  - A new plugin needs `src/plugin/<name>/index.js`, `types/plugin/<name>.d.ts`, and `test/plugin/<name>.test.js`; a missing `.d.ts` fails `test/plugin/declarations.test.js`.
  - Each locale file must start with `// Language Name [code]`; the build and `test/locale/keys.test.js` parse it.
  - Tests import from `../../src` (not `dayjs`), check results against `moment`, and freeze time with `MockDate.set` / `MockDate.reset`.

## Commands

- Full test suite: `npx cross-env TZ=UTC jest`
- One test file: `npx jest <file> --coverage=false`
- Build on new Node versions: `NODE_OPTIONS=--openssl-legacy-provider npm run build`

## Guardrails

- Root `/plugin`, `/locale`, `/esm`, and `dayjs.min.js` are build output. Edit `src/` instead.
- Open PRs against `dev`. Never push to `master`, because that triggers a release.
