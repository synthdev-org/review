# Local mode quickstart

Run an AI code review of your branch from your own terminal, with your own model, and nothing else
(no server, database or container). Reference detail lives in
[`operations.md`](../reference/operations.md) ("Local mode"); this guide is the path through it.

**How to read the checks.** Every command below was run against the built wheel installed into a
clean `python:3.12-slim` container (nothing from the source checkout on the path, `git` and
`gitleaks` installed, a local stub standing in for the model that returns one canned finding) unless
it carries a tag:

- **[test]** not run by hand here, but covered by an automated test (named).
- **[UNVERIFIED]** not run and not covered end to end. Treat it as a lead, not a promise.

## 1. Install

`review` is **not published to PyPI** (the name `review` there belongs to someone else). Today you
build a wheel from a checkout of this repository and install that.

Requirements:

- **Python 3.12 exactly.** The package declares `>=3.12,<3.13`. On a machine whose default Python is
  3.13, `pipx install` fails with `This package needs a Python matching '<3.13,>=3.12' and none of the
  interpreters on this system does` (pipx 1.18; older pip-based installs say "requires a different
  Python"); pass `--python python3.12`. That message needs a 3.13 interpreter present; on a machine
  with no `python3.12` on `PATH` at all, pipx instead says the interpreter was not found. Either way:
  install Python 3.12 first.
- **`pipx`** (recommended install route; see <https://pipx.pypa.io>). Without it use the virtualenv
  route below.
- **`git`** on `PATH`.
- **`gitleaks`** on `PATH`. Input redaction needs it and cannot be turned off; without it the run
  exits 2 before anything is sent. Install it from <https://github.com/gitleaks/gitleaks>
  (the checks here used v8.21.2). Other analyzers (semgrep, ruff, eslint, phpstan, golangci-lint,
  trivy) are optional and shown as `skipped (not installed)`.

From a checkout of this repository (building leaves `build/` and `dist/` in it; both are
git-ignored, and container builds may leave them root-owned):

```sh
pip wheel --no-deps . -w dist
pipx install --python python3.12 dist/review-0.1.0-py3-none-any.whl     # recommended
# or, into a virtualenv you manage:
python3.12 -m venv ~/.venvs/review && ~/.venvs/review/bin/pip install dist/review-0.1.0-py3-none-any.whl
review --version        # prints: review 0.1.0
```

Both the `pip` and the `pipx` install (and the 3.13 failure above) are exercised by
`tests/e2e/test_local_distribution.py` (the 3.13 case there runs with a 3.13 interpreter present). The wheel pulls in the same dependency set as the
server (the one source tree is shared by design), so it is a large install.

Upgrade or roll back by installing another wheel (`pipx install --force ...`).

## 2. Give it a model

Nothing is sent anywhere until you choose a model; there is no default. Without one:

```
$ review run --base main
error: no model is configured. Set REVIEW_LOCAL_LLM_MODEL and REVIEW_LOCAL_LLM_ENDPOINT ...
$ echo $?
2
```

### a. Any OpenAI-compatible endpoint (the default backend)

```sh
export REVIEW_LOCAL_LLM_ENDPOINT=http://localhost:1234/v1     # base URL, ends in /v1
export REVIEW_LOCAL_LLM_MODEL=<model name the endpoint knows>
export REVIEW_LOCAL_LLM_API_KEY=<key>                         # only if the endpoint needs one
```

Examples **[UNVERIFIED against the real servers]** (no LM Studio or Ollama was available; the run
used a stub speaking the same `/v1/chat/completions` protocol, and the client is the same one the server mode uses).

| Server | `REVIEW_LOCAL_LLM_ENDPOINT` | `REVIEW_LOCAL_LLM_MODEL` |
| --- | --- | --- |
| LM Studio (local server running) | `http://localhost:1234/v1` | the model identifier LM Studio shows |
| Ollama | `http://localhost:11434/v1` | a pulled model, e.g. `qwen2.5-coder:14b` |
| A hosted provider | its OpenAI-compatible base URL | its model name, plus `REVIEW_LOCAL_LLM_API_KEY` |

Optional: `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS` (default 30, per call; raise it for a slow local
model) and `REVIEW_LOCAL_LLM_MAX_DIFF_BYTES` (default 200000, the diff size cut per request).
A malformed endpoint is caught up front: `error: invalid configuration: llm_endpoint: Input should
be a valid URL ...` (exit 2). An unreachable endpoint fails fast: `error: a review stage failed:
step 'intent_summary' (LLMUnreachableError) via http://127.0.0.1:1/v1` (exit 1).

**Trying it without a real model.** [`stub_model.py`](stub_model.py) (stdlib only) is the stand-in
the checks in this guide used. It lives in the source checkout only (`docs/guides/`), not in the
installed wheel: run it from a checkout, or skip it and use a real endpoint. A review makes three kinds of call, and a stub that answers them all
alike fails (e.g. `step 'verification' (VerificationResponseParseError)`): the intent summary wants
plain text, the review pass wants `{"findings": [{file, line, severity, message}]}`, and verification
wants `{"verdicts": [{index, valid, actionable, specific, valuable}]}`. The script answers each
correctly (one canned finding in `app.py` line 2, confirmed by verification):

```sh
python3 docs/guides/stub_model.py 8099 &       # prints http://127.0.0.1:8099/v1
export REVIEW_LOCAL_LLM_ENDPOINT=http://127.0.0.1:8099/v1 REVIEW_LOCAL_LLM_MODEL=m
```

It is exercised by `apps/local_cli/test_guide_stub_model.py`.

### b. The opt-in Claude CLI backend: Anthropic terms compliance is UNVERIFIED

> **Whether Anthropic's terms allow driving a Claude subscription through the `claude` CLI this way
> has not been checked** (plan blocker B4). The backend is off by default and nothing here says it
> is compliant. Read Anthropic's current terms before relying on it.

```sh
export REVIEW_LOCAL_BACKEND=claude_cli        # no endpoint needed
export REVIEW_LOCAL_LLM_MODEL=<alias>         # optional: passed as the CLI's --model
review run --base main
```

It uses only the CLI's own stored login (sign in by running `claude` yourself); no API key is read.
**CI refusal** (run): with `CI=true` and this backend, the run exits 2 with `error: the Claude CLI
backend is refused because CI was detected ...` and starts nothing. `--allow-claude-cli-in-ci`
overrides it. With the CLI absent, the run exits 1 with `error: the model backend is unavailable:
... `claude` was not found on PATH` (run), before any content is sent. A real call on your
subscription was **not** made here **[UNVERIFIED]**; the owner can check it with
`REVIEW_LIVE_CLAUDE_CLI=1 pytest -m integration adapter/llm/test_claude_cli_live.py`.

## 3. First run

From inside a git repository, on a branch with commits that are not on `main`:

```sh
review run --base main
```

`--base` takes a branch, tag or commit. It reviews the merge base to `HEAD`, which is what a pull
request shows. Output: findings on **stdout**; progress, the **run summary** and errors on
**stderr**. Real output from the check run (summary shortened):

```
Run summary
  backend: openai_compatible, model m
  stages: prefilter ... output: success (stages that need a database are "disabled")
  analyzers: used gitleaks; skipped semgrep (not installed); ...
  token usage: 6 tokens over 3 request(s)
  redaction: applied (0 redacted)
  mcp servers: none consulted
  memory file: not found
app.py:2: high: canned finding
1 finding(s).
```

Each finding is `file:line: severity: message`, with the rule id in parentheses for analyzer
findings. The summary holds names, statuses and counts only (never code), and says
`not reported` / `not available` rather than showing a misleading zero. The run summary is printed
on stderr on every run, including `Nothing to review`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, including `Nothing to review` (no model call). Findings do **not** fail a run by default. |
| 1 | Runtime failure: `error: <reason>: <detail>` (see troubleshooting). Never printed as a clean review. |
| 2 | Usage or configuration error; nothing was sent to the model. |
| 3 | The severity gate tripped: a finding at or above `REVIEW_LOCAL_FAIL_ON_SEVERITY` (`critical`, `high`, `medium`, `low`, `info`). Off unless set. |

Run: no model gives 2; `--base nope` gives `error: git ref 'nope' does not exist (give --base a
branch, tag or commit that exists locally, e.g. --base main)` and 2; outside a repository `error:
/tmp is not inside a git repository` and 2; `--base` with `--staged` or neither is a usage error
(2); `REVIEW_LOCAL_FAIL_ON_SEVERITY=high` with a high finding gives `gate: 1 finding(s) at or above
severity high` and 3.

`review --help` and `review run --help` list every option, the exit codes and every environment
variable; `review --version` prints the version.

## 4. `--staged`, and hooks

```sh
git add -p
review run --staged
```

Reviews only what is staged; unstaged edits are never read and findings are anchored to the staged
line numbers. With nothing staged: `Nothing to review: no staged changes.` and exit 0 (run).

A **pre-push hook** (`.git/hooks/pre-push`, `chmod +x`) that reviews the branch against the
remote's main and blocks the push when the gate trips (run: a push without the gate variable
succeeded; with `REVIEW_LOCAL_FAIL_ON_SEVERITY=high` the push was rejected with exit 1 and `gate:`
printed):

```sh
#!/bin/sh
exec review run --base origin/main
```

Set `REVIEW_LOCAL_FAIL_ON_SEVERITY=high` in your shell profile to make the hook block. A
**pre-commit** hook is the same with `exec review run --staged` (run, same result). Model calls take
seconds to minutes, so many people prefer pre-push to pre-commit. `git push --no-verify` skips it.

## 5. Writing the findings to a file

```sh
review run --base main --output review.md      # markdown
review run --base main --output review.json   # JSON (schema_version "1.0")
```

Findings still print to stdout. The extension decides the format; any other extension exits 2
(`error: --output: x.txt must end in .md, .markdown or .json`). The path is checked **before** any
model call: it may not be a symlink, a file git tracks (`error: --output: review.md is tracked by
git; refusing to overwrite it`), or in a missing directory. An untracked previous output is
replaced, and a failed run writes no file. `--output` cannot be combined with `--dry-run`. The JSON
shape is described by [`review-report.schema.json`](../reference/review-report.schema.json); check
`schema_version` before reading it.

## 6. The memory file and dismissing a finding

To stop a finding from coming back, add one line to **`.review/memory.jsonl`** in the repository
and commit it. Copy `file`, `rule_id` (or `null`) and `message` exactly as printed (matching is
exact). It dismisses model findings only: static-analysis findings always pass through unchanged
(US-V6), so configure the analyzer to silence those:

```
# false positive: totals are always ints here
{"file": "app.py", "rule_id": null, "message": "canned finding", "note": "optional"}
```

Run: the next review printed `Suppressed by .review/memory.jsonl (1):` with the finding, and the
summary showed `suppressed by memory: 1`, `memory file: loaded with 1 entries`. A dismissal is
always visible, never silent. `file` may be `*` for any file. Static-analysis findings are not
suppressed this way, only model findings. A malformed file stops the run before any model call:
`error: memory file .review/memory.jsonl:1: not valid JSON (Expecting value)`, exit 2.
`REVIEW_LOCAL_MEMORY_FILE_ENABLED=false` turns it off. In CI the file is read from the **base**
commit, so a pull request cannot dismiss its own findings.

## 7. MCP servers as background context (opt-in, relevance UNPROVEN)

Local mode has no code index; it can instead ask MCP servers you already run (docs, a wiki, an
internal knowledge base) for background. **Whether this improves reviews has not been shown**
(ADR-0017 is PROPOSED); treat it as an experiment.

- **Nothing is started or contacted unless you name the server**, by name only:
  `review run --base main --mcp docs` or `--mcp kb:search_kb` (`NAME[:TOOL]`, repeatable), or
  commit `.review/mcp.json`: `{"mcp": {"servers": ["docs", {"name": "kb", "tool": "search_kb"}]}}`
  (max 3). That file may not contain `command`, `args`, `env`, `url`, `headers` or `cwd`: run, it
  exits 2 with `the key 'command' is not allowed; nominations are names only`.
- **Where servers are defined** is your own MCP configuration (`~/.claude.json`, the Claude Desktop
  file, then the repository's `.mcp.json`), or the one file in `REVIEW_LOCAL_MCP_CONFIG`
  (`{"mcpServers": {"docs": {"command": "...", "args": [...]}}}`), which replaces them. An unknown
  name exits 2: `error: MCP server 'nosuch' was nominated but is not in your MCP configuration`.
- **Read-only tools only.** The server's one search-like tool (name contains `search`, `query` or
  `lookup`) is called, and only if it declares `readOnlyHint` true and neither `destructiveHint`
  nor `openWorldHint`. The query goes only into a parameter named `query`, `q`, `search`, `text`,
  `keywords` or `prompt`; a tool needing any other parameter is skipped (`no usable tool`).
  Naming the tool with `--mcp NAME:TOOL` skips the read-only check for that tool only.
- **Run** against a real stdio MCP server built with the official SDK and a read-only
  `search_docs(query)` tool: the summary showed `mcp servers: used docs`, both with `--mcp docs`
  and with `.review/mcp.json` naming `docs`.
- **What leaves your machine:** the query is built, never model-written: changed files as
  `parent/basename` plus up to 10 symbol names, at most 500 characters, after secret redaction.
  What the server returns is redacted again, cut to size limits and placed in the prompt as inert
  reference data; nothing it says is ever acted on (one call per server). Preview it with
  `--dry-run`: `MCP server docs (stdio): the tool is chosen from the server's tool list at run
  time; query 'app.py total'` (run). A failing server never blocks the review; the summary says
  which failed and why.
- Limits (environment only): `REVIEW_LOCAL_MCP_DEADLINE_SECONDS` (10), `..._MAX_CALLS` (3),
  `..._MAX_PROCESSES` (3) and the byte caps; see `review run --help`. POSIX only.
- Contacting real third-party MCP servers was **not** done in this check **[UNVERIFIED]**. The
  owner's manual go/no-go run is `REVIEW_LIVE_MCP=1 pytest -m integration
  apps/local_cli/test_mcp_live.py` (procedure in ADR-0017).

## 8. GitHub Actions

Inside Actions the job also posts one pull-request review and refreshes it on each push. The full
reference and security reasoning are in [`ci-github-actions.md`](../reference/ci-github-actions.md);
the posting behaviour is covered by `apps/local_cli/test_ci.py` against a stubbed GitHub API
**[test]**, and the workflow itself was **not** run on GitHub **[UNVERIFIED]**.

```yaml
name: review
on:
  pull_request:            # never pull_request_target: the CLI refuses it (exit 2)
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the base branch must be present
      - name: Install review and gitleaks
        run: |
          pipx install --python python3.12 ./review-0.1.0-py3-none-any.whl   # your built wheel
          # install a pinned gitleaks onto PATH here
      - name: Review
        env:
          GITHUB_TOKEN: ${{ github.token }}
          REVIEW_LOCAL_LLM_ENDPOINT: ${{ vars.REVIEW_LLM_ENDPOINT }}
          REVIEW_LOCAL_LLM_MODEL: ${{ vars.REVIEW_LLM_MODEL }}
          REVIEW_LOCAL_LLM_API_KEY: ${{ secrets.REVIEW_LLM_API_KEY }}
          # REVIEW_LOCAL_FAIL_ON_SEVERITY: high
        run: review run --base origin/${{ github.base_ref }}
```

Run, the guards: `GITHUB_ACTIONS=true` without an event gives `error: the workflow event is
'unknown', not 'pull_request' ...` (2); event `push` gives the same naming `push` (2).

- **Fork pull requests:** the token is read-only and has no secrets, so nothing can be posted (a
  fork also has no model key). The job exits 1 with `no permission to comment`, findings in the
  log; never a green job with nothing posted **[test]**.
- **`--staged` is refused in Actions** (exit 2). Public job logs are public: findings are printed
  there.
- **Other CI systems (GitLab, Jenkins, self-hosted):** only Actions is recognised as a pull-request
  context. Set **`CI=true`** yourself so the CI protections apply (memory file and nominations read
  from the base commit, no personal MCP configuration, Claude CLI refusal). Run: `CI=true` outside
  Actions with the memory file enabled exits 2 (`CI was detected (CI=true) without a recognised
  pull-request context, so the memory file would be read from the pull-request head`); set
  `REVIEW_LOCAL_MEMORY_FILE_ENABLED=false` (run, works) or, for a checkout you trust,
  `REVIEW_LOCAL_MEMORY_FROM_WORKING_TREE=true`. This is a guard, not a security control.

## 9. `--dry-run`: preview what would leave your machine

```sh
review run --base main --dry-run
```

Runs the real pipeline against a recorder and sends **nothing**. It prints the destination
(credentials in the URL and query values masked), the files and byte counts that would be sent, the
size ceiling and any truncation, and each MCP server with its exact query. Run (stdout):

```
Dry run: nothing was sent. This is what a real run would send.
Model endpoint http://127.0.0.1:8099/v1 (model m): 2 request(s), 2719 bytes in total
  app.py: 178 bytes
Size ceiling: 200000 bytes of diff per request
Truncation: none
MCP servers: none consulted
Not previewed: requests that depend on the model's findings (verification) ...
```

It does not preview verification requests (they depend on findings) or what an MCP server would
answer, and local analyzers still run. A diff containing a GitHub token was run through it: the
token did not appear in the preview.

## 10. Privacy: what is sent where

- **Sent to your chosen model endpoint:** the diff (after redaction, cut to
  `REVIEW_LOCAL_LLM_MAX_DIFF_BYTES`) and prompts built from it, nothing else. With the Claude CLI
  backend, the same prompt goes to the `claude` process, hence to Anthropic under your login.
- **Sent to nominated MCP servers only:** the short query in section 7.
- **Sent to GitHub, in Actions only:** the review comments, using the workflow's token.
- **Never sent:** content of credential-named files (`.env*`, `*.pem`, `*.key`, `id_rsa*`,
  `.netrc`, `.npmrc`, `*.tfvars`, ...): the entry is kept but the content is withheld and the
  summary names them. Credential-named environment variables are stripped from child processes.
  Run: a diff adding a `ghp_...` token was sent with that line replaced by `[REDACTED]` (`redaction:
  applied (1 redacted)`), and the same token raised a `critical` gitleaks finding.
- **Redaction limits:** coverage is the **Gitleaks ruleset only**. A bespoke secret format, a
  password in prose, or proprietary code that is not a secret all pass through. If the scan fails,
  times out or the diff exceeds the file or byte caps, nothing is sent (exit 1, `redaction failed,
  nothing sent`). Gitleaks' own allowlist skips well-known placeholders (the AWS documentation key
  `AKIAIOSFODNN7EXAMPLE` was not flagged). Use `--dry-run` if in doubt, and point at a local model
  for code that must not leave the machine.
- **State on disk:** none beyond what you ask for: a `--output` file and a memory file you commit.
  No `.env` file is read.

## 11. Troubleshooting

Exit 2 (nothing was sent): `no model is configured` (set the two variables); `invalid
configuration: <field>: <reason>` (values never echoed); `git ref '<x>' does not exist` (give an
existing branch or commit; in a shallow clone fetch the base first, e.g. `git fetch --depth=50
origin main`); `<dir> is not inside a git repository`; `gitleaks was not found on PATH`; `--output:
...` (extension, symlink, tracked file, missing directory); `memory file <path>:<line>: ...`;
`MCP server '<x>' was nominated but is not in your MCP configuration`; the Claude CLI refused in
CI; the CI event/token/`--staged`/memory messages in section 8.

Exit 1, always `error: <reason>: <detail>`:

| Reason | What to do |
| --- | --- |
| `git could not read the repository` | git failed, timed out or produced too much output; the detail names the subcommand. Check the repository is healthy and `git` is on `PATH`. |
| `a review stage failed` | A stage failed or timed out; the detail names it and, for the model call, the endpoint (an unreachable endpoint failed in the check run with `LLMUnreachableError`). Check the endpoint is up; raise `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS` for a slow model. |
| `redaction failed, nothing sent` | The secret scan failed or the diff is over the caps. Re-run, review a smaller range, or raise `REVIEW_LOCAL_REDACTION_MAX_FILES` / `_MAX_BYTES`. |
| `no permission to comment` | CI only: the token lacks `pull-requests: write`, or it is a fork. Findings are in the log. |
| `the pull-request review could not be posted or refreshed` | CI only: GitHub rate limit, 5xx or no response. Findings are in the log; re-run. |
| `the output file could not be written` | The review completed but the file could not be written (disk full, path swapped for a symlink). Findings were printed. |
| `the model backend is unavailable` | Claude CLI backend only: not installed, not logged in, hung or crashed. Run `claude` once and sign in. |

Exit 3 is the severity gate, not an error. The `Run summary` above any error shows which stages
finished. Add `-v` for info logs (`-vv` for debug); logs never contain code.

## 12. Uninstall

```sh
pipx uninstall review          # run: removes the app and the review command
# or, in a virtualenv:
pip uninstall review
```

The tool keeps no state outside your repositories. Delete any `--output` files, and
`.review/memory.jsonl` / `.review/mcp.json` if you committed them, to remove everything.
