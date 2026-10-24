# Contributing to cs2-analyzer

These rules apply to every contributor and every coding agent (Claude Code, Codex, or
anything else that reads this file). CI enforces the ticket rules on every pull request
(`.github/workflows/pr-conventions.yml`), so a PR that skips them cannot merge.

## Every change has a DEV ticket

Work is tracked in Jira, project `DEV` on `midgard-home.atlassian.net`.

- Start from an existing ticket, or create one (type `Task`, a short markdown
  description) before writing code.
- **Never invent or guess a key.** Before citing `DEV-<n>` anywhere, confirm it exists
  and is this work (`project = DEV ORDER BY created DESC`). A wrong key is worse than
  none: it survives in history and sends the next reader to unrelated work.
- No Jira access? Ask the repo owner for a ticket and wait for the key. Don't open the
  PR without one.

## Branch, commits, PR

- **Branch:** `DEV-<n>-<short-description>`, cut from `main`, e.g.
  `DEV-62-trade-window`. One ticket per branch. Never push to `main`.
- **Commits:** subject `<type>: <what changed>` (`feat`, `fix`, `chore`, `docs`,
  `refactor`, `test`, `ci`, `perf`, `build`), a body that says *why*, and the bare
  ticket key on its own line as the footer:

  ```
  fix: count a trade only inside the trade window

  Kills five seconds apart were scored as trades, inflating every entry's numbers.

  DEV-62
  ```

- **PR title:** `<type>: <summary>`, the same shape as a commit subject. PRs are
  squash-merged, so the title becomes the commit on `main`. Append the new version for
  a release, e.g. `feat: time to contact per round (0.22.0)`.
- **PR body:** what changed and why, how it was tested, and the ticket key on its own
  line.
- Before opening the PR, run what CI runs: `pytest`.

## Releasing

`VERSION` is the published image tag. Every push to `main` builds
`ghcr.io/mano1233/cs2-analyzer:<VERSION>`. A newer VERSION also tags `v<VERSION>`,
publishes a GitHub Release, and makes midgard open the PR that deploys it. Bump it in
any PR that should reach production: minor for a feature, patch for a fix, one bump per
PR. A PR that leaves VERSION alone ships nothing.

The release notes are the merged PR titles, grouped by the title's type: `feat` under
Features, `fix` under Fixes, `perf` under Performance, the rest under Other changes,
and `ci` / `chore` left out. CI labels each PR `type:<type>` from its title, so the
type is how a change shows up to whoever reads the release. Pick it for that reader,
and write the summary for them too.

## Keep out of git

Demos (`*.dem`, `*.dem.zst`, `*.dem.bz2`), parsed output (`results.json`, `trend.csv`,
`processed.json`), and every credential: R2 / AWS keys, `FACEIT_API_KEY`,
`FACEIT_DOWNLOADS_TOKEN`. Configuration comes from the environment; see the README.
