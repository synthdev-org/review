# Running review in GitHub Actions (local mode)

An **example**, shipped as documentation. It is deliberately not a `.github/workflows` file in this
repository: local mode is a pip/pipx distribution, not a deployable
([ADR-0016](../adr/0016-local-mode-composition-root.md)), and copying a workflow is the opt-in.
Exit codes, failure reasons and the run summary are in
[`operations.md`](operations.md#local-mode-review-run-exit-codes-the-run-summary-and-the-findings-file).

## What it does

On every push to a pull request the job runs `review run --base origin/<base branch>` against the
checked-out merge commit and:

- prints the findings and the run summary to the **job log**, always;
- posts **one pull-request review** whose inline comments are anchored to diff lines (US-C1);
- on the next push, does not post a finding again if it is still on the same file and line range,
  re-words its comment if the text changed, and minimises the comment of a finding that is gone as
  `OUTDATED` (US-C3);
- optionally exits `3` when a finding reaches a severity you choose (US-C2, off unless
  `REVIEW_LOCAL_FAIL_ON_SEVERITY` is set).

It is detected as a CI run from `GITHUB_ACTIONS=true`, and then needs `GITHUB_EVENT_NAME=pull_request`, `GITHUB_TOKEN`,
`GITHUB_REPOSITORY` and `GITHUB_EVENT_PATH` (all set by the runner; the token is passed by the
workflow). Outside Actions none of this applies and nothing is posted.

## The example workflow

```yaml
name: review
on:
  pull_request:            # never pull_request_target, see "Security" below

permissions:               # workflow level, least privilege
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the base branch must be present to compute the merge base
      - name: Install review and gitleaks
        run: |
          # review is not on PyPI: install the wheel you built from a checkout (ADR-0016), e.g.
          # one you publish as a release asset or keep in an internal package index.
          pipx install --python python3.12 ./review-0.1.0-py3-none-any.whl
          # gitleaks must be on PATH: it redacts secrets before anything is sent to the model.
          # Install a pinned release of it here (https://github.com/gitleaks/gitleaks).
      - name: Review
        env:
          GITHUB_TOKEN: ${{ github.token }}
          REVIEW_LOCAL_LLM_ENDPOINT: ${{ vars.REVIEW_LLM_ENDPOINT }}
          REVIEW_LOCAL_LLM_MODEL: ${{ vars.REVIEW_LLM_MODEL }}
          REVIEW_LOCAL_LLM_API_KEY: ${{ secrets.REVIEW_LLM_API_KEY }}
          # REVIEW_LOCAL_FAIL_ON_SEVERITY: high    # optional gate: exit 3 on a high or critical finding
        run: review run --base origin/${{ github.base_ref }}
```

## Behaviour you should know

| Situation | Result |
| --- | --- |
| Healthy run | Findings in the log, one review posted, exit 0 (or 3 if the gate trips) |
| Pull request from a **fork** | The token is read-only and has no secrets: no comment is posted, findings are printed to the log, and the job exits 1 with `no permission to comment`. It is never a green job with nothing posted. (A fork also has no model key, so in practice it fails earlier, at the model.) |
| Token lacks `pull-requests: write` | Same: exit 1, `no permission to comment`, findings in the log |
| GitHub rate limit (429) or 5xx while listing, updating or minimising comments | Counted in the summary (`ci comments ... dedupe failed: ...`), findings in the log, exit 1 (`the pull-request review could not be posted or refreshed`). A failed listing posts nothing, because posting blind would duplicate the earlier review |
| Event is not `pull_request` (including `pull_request_target`) | Exit 2, nothing sent to the model |
| `GITHUB_TOKEN` not passed to the step | Exit 2, nothing sent to the model |

The author login that marks a comment as ours is `github-actions[bot]` (what a `GITHUB_TOKEN`
posts as). Set `REVIEW_LOCAL_CI_BOT_LOGIN` if you post with a different identity.

## Security

- **`pull_request`, never `pull_request_target`.** `pull_request_target` gives a fork's pull request
  secrets and a write token while the job reads files the fork controls. This feature must not be
  run that way. The CLI refuses any event whose name (`GITHUB_EVENT_NAME`) is not exactly
  `pull_request`, with exit 2 before anything is sent. It checks the name, not the payload:
  a `pull_request_target` payload also contains a `pull_request` object, so the shape alone
  proves nothing.
- **Credentials stay out of child processes.** For the duration of a run every credential-named
  variable (`GITHUB_TOKEN`, `GH_TOKEN`, `ACTIONS_RUNTIME_TOKEN`, anything ending in `_TOKEN`,
  `_SECRET`, `_PASSWORD`, `_API_KEY`, ...) is removed from the environment git, gitleaks and the
  analyzers inherit. Configuration is read first, so the run itself still has what it needs.
- **`--staged` is not available in Actions** (exit 2): the index is a developer-machine notion, and
  the "base commit" it would give the memory file is the pull-request head itself.
- **Other CI systems.** Only GitHub Actions is recognised as a pull-request context. If `CI=true`
  is set without one, the memory file would be read from the checked-out working tree, which is the
  pull-request head and could dismiss its own findings; the run refuses (exit 2) unless you set
  `REVIEW_LOCAL_MEMORY_FILE_ENABLED=false` or, for a checkout you trust,
  `REVIEW_LOCAL_MEMORY_FROM_WORKING_TREE=true`.
- **MCP servers in CI are opt-in and come from your workflow only.** Relevance of MCP context is
  unproven (see `operations.md`, "MCP servers as context"). The repository's `.mcp.json` and any
  `~/.claude.json` are never read in CI. To use a server, pass `--mcp NAME` in the workflow (or put
  its name in a committed `.review/mcp.json`, which CI reads from the **base** branch, so a pull
  request cannot add one) **and** point `REVIEW_LOCAL_MCP_CONFIG` at a file the workflow writes
  **outside the checkout** (for example under `$RUNNER_TEMP`) with the server's definition. A stdio
  entry there runs a command on the runner as that definition says: giving CI a stdio command is
  your decision as the workflow author. The child gets only `PATH`, `LANG`, an empty `HOME` and the
  variables you list for that server, never `GITHUB_TOKEN`. A nomination file with `command`, `args`,
  `env`, `url` or `headers` in it is rejected.
- **Permissions are declared at workflow level**, `contents: read` and `pull-requests: write` only
  (no `checks: write`: review posts no check-run in CI).
- **A copied marker does not make a comment ours.** A comment is recognised only when its author
  login is the bot's **and** it carries the hidden marker, so a pull-request author who pastes the
  marker into their own comment cannot get it edited or hidden.
- **Posted text is sanitised**: control characters and newlines are collapsed, bare URLs are not
  auto-linked, images, HTML and `@mentions` are neutralised, and a comment is capped at 2,000
  characters of message text. This is a mitigation: a hostile diff can still steer what the model
  says, but not make it a live link.
- **The token never appears** in the log, the summary, the findings JSON or an error message
  (enforced by a canary test); error messages carry an HTTP status only.
- **The job log of a public repository is public**, and findings are printed to it. The summary
  holds names and counts only.
- The diff is redacted before it is sent to the model (Gitleaks ruleset only; see `operations.md`).

## Rollback

Delete the workflow file. Comments already posted stay on the pull request; the next run on any
pull request that still has the workflow would have minimised stale ones.

## Known limitation

A finding without a line number is shown in the review body, which is not deduplicated, so it is
posted again on every push.
