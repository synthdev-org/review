# Review Context and Input Shaping PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-11-25

Makes the review see more of what matters and less of what does not: verification
judges a finding against the real head file, hunks arrive with their enclosing
function, data and fixture noise is stubbed (and listed), batches carry a map of
the whole PR and are ordered by risk, and an optional commit-by-commit mode covers
PRs that batching cannot. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification tag
`RQPTVE`); requirements are user stories with `Given/When/Then` acceptance
criteria per `testing.md`'s mandatory rule.

**Extends [`spec-diff-batching.md`](spec-diff-batching.md), does not replace it.**
Same steps, same pipeline order, no new step, no new deployable, no database.
New code is pure domain modules under `review/`, one Protocol module under
`ingestion/`, and two adapters under `adapter/`.

## Problem & Goal

**End user: the PR author.** Dogfooding this branch (22k added lines) showed two
failures. (a) The 200 KB prompt ceiling dropped most of the diff (now mitigated by
batching). (b) Three of four spot-checked findings were false positives that the
reviewer could have refuted by reading code outside the diff: a file that is
tracked in git, a module-level `pytestmark`, a code path that only writes body
text. Today the verifier sees only the same diff slice as the generator, so it
cannot refute them.

**Secondary end user: the platform operator**, who pays per token and wants the
spend to go to code, not to cassettes.

### Measurement (this branch, `git diff main...HEAD`, 2 commits)

Patch bytes are the bytes `git diff` emits per file, a close proxy for
`render_file_block` size. Measured 2026-11-25 with `git diff main...HEAD --numstat`
plus per-file patch sizes. Categories: lockfile by basename; data/fixtures by path
segment (`testdata`, `fixtures`, `cassettes`, `snapshots`, `golden`, `baseline`) or
extension (`.json`, `.jsonl`, `.csv`, `.snap`); tests by `test_*`/`*_test.*`/
`*.test.*`/`tests/`; docs/prose by `.md`/`.rst`/`.txt`/`docs/`; source by code
extension; everything else other.

| Category | Files | Added | Deleted | Patch bytes | Share |
| --- | ---: | ---: | ---: | ---: | ---: |
| data/fixtures/cassettes/json | 12 | 6,367 | 0 | 487,263 | 30.6% |
| docs/prose | 16 | 2,529 | 3,089 | 406,218 | 25.5% |
| tests | 75 | 8,462 | 103 | 368,780 | 23.2% |
| source | 66 | 5,372 | 482 | 318,068 | 20.0% |
| other | 7 | 122 | 4 | 11,830 | 0.7% |
| lockfiles (`uv.lock`, working tree only) | 1 | 18 | 0 | 4,295 | 0.3% |
| **Total (committed range)** | **176** | **22,852** | **3,678** | **1,592,159** | 100% |

Findings that ground the rules:

- Two eval cassettes (`review/testdata/eval/cassettes/v1.json` 191,681 B and
  `v2.json` 241,289 B) are 27% of all patch bytes. All of `testdata/` is 462,459 B
  (29.0%). The review value of that data is close to zero. A size threshold of
  8,000 patch bytes inside a data path catches every one of these and leaves small
  fixtures visible.
- Three whole-file deletions (large planning documents, 3,081 deleted lines)
  are 206,057 B (12.9%). Deletion-only
  hunks carry no defect the reviewer can anchor a comment to.
- Prose that remains (about 200 KB of plans, specs, research, ADRs) is a quarter of
  what is left. Source plus tests are 43% of the bytes and are what needs the
  reviewer.
- Removing data and deletion-only bytes leaves 923,643 B (58% of the range). With
  prose trimmed the estimate is about 0.75 to 0.8 MB. At a 200 KB ceiling that is
  four to five batches instead of eight, so the default `max_diff_chunks=8` covers
  the whole branch where today it covers about 200 KB of 1.59 MB.
- Not in these numbers: untracked files in the working tree (the new modules and
  specs). The tracked working tree adds 7 files and 46 KB (`git diff main`).

**Goal (capability-based, no fabricated precision number):** (a) a finding is
judged against the head version of the file it cites; (b) a hunk is shown with its
enclosing function or class; (c) data, fixture and bulk-deletion noise costs one
line, and every file so reduced is named in the posted review; (d) every batch
sees a one-line-per-file map of the whole PR; (e) when the cap bites, the files
dropped are the least valuable ones, and they are named; (f) an optional mode
reviews a PR commit range by commit range, and a finding no longer present at head
is never posted; (g) for a small PR with nothing stubbed and every optional flag at
its default, prompts and output are byte-for-byte what they are today.

## Scope

### In scope (decided)

1. **Verification with file context.** `VerificationStep` fetches the head content
   of the files its findings cite (`DiffFetcher.fetch_content`, unchanged
   Protocol), builds a bounded text per file (whole file under a byte cap, else the
   enclosing function or class plus surrounding lines), and appends it to the
   judge prompt as inert reference data. Separate LLM call from generation: never
   self-critique.
2. **Hunk expansion.** Each hunk is rendered with the unchanged lines of its
   enclosing function or class around it, parsed from head content with
   `indexing.chunk`. Prompt-only: the pipeline `Diff` is untouched.
3. **Shrink input.** Data and fixture patches over a threshold are replaced by a
   one-line stub; deletion-only hunks over a threshold collapse to a note; oversize
   prose patches are trimmed. The stubbed file stays in the pipeline `Diff`
   (static analysis, secret scanning and inline-comment anchoring still see it)
   and is listed in the posted review.
4. **PR map.** A deterministic per-file header (path, status, counts, changed
   symbols) repeated in every batch prompt. Built from tree-sitter, no LLM call.
5. **Risk ordering.** Files get a risk score; when the batch cap bites, the lowest
   value groups are dropped first and listed.
6. **Commit-by-commit mode.** Off by default. When batching still drops files and
   the PR has a bounded linear commit list, review consecutive commit ranges
   instead, re-anchor each finding at head, and drop findings that are gone.
7. **Graceful degradation everywhere.** Parse failure, unsupported language, fetch
   failure, timeout or an inconsistent patch falls back to today's behaviour for
   that file (or that step). Nothing here fails a review.

### Out of scope (deferred)

- A database, embeddings or the Postgres graph for any of this.
- An extra LLM call for the PR map (deterministic extraction is enough; revisit
  only if the eval shows batches still lack cross-batch awareness).
- Reading files outside the PR diff for verification (a finding on a path the diff
  does not touch gets no file context).
- Tuning thresholds on real PRs (starting values, see Open items).
- `finding_judge`'s classifier judges: they ignore the new context.

### Priority

Must: US-X1, US-X4, US-X5, US-X7, US-X8. Should: US-X2, US-X3, US-X6, US-X9.
Could: US-X10, US-X11.

## Requirements

**US-X1: Verification judges a finding against the real file.**
As a PR author, I want a finding checked against the whole function or file it
cites, so that a claim refuted by code outside the diff is rejected.
- *Given* a finding "tracked file X is untracked" whose file X is in the head tree,
  or "missing `pytest.mark.integration`" in a file whose module-level `pytestmark`
  sits outside the diff hunk, or "body text is not escaped" where the head function
  shows the only write path is body text, *when* `verification` runs with file
  context enabled, *then* the judge prompt contains that file's head text inside a
  delimited inert-reference block and the recorded refuting verdict rejects the
  finding.
- *Given* a file over `verification_file_context_max_bytes`, *then* the prompt
  holds the enclosing function or class plus `verification_file_context_lines` lines
  either side of each cited line, with line numbers, never the whole file.
- *Given* several findings in one file, *then* that file's context appears once.

**US-X2: Verification degrades to diff-only.**
As a platform operator, I want a fetch problem never to fail verification.
- *Given* `fetch_content` raises `DiffContentUnavailableError` or
  `DiffContentMalformedError`, or exceeds `verification_file_context_timeout_seconds`,
  or a cited file is binary, a secret-shaped path (`.env*`, `*.pem`, `*.key`) or
  absent from the diff, *when* verification runs, *then* the prompt for those
  findings carries no file context, a warning is logged, and the review completes.
- *Given* `verification_file_context_enabled=False`, *then* the judge prompt is
  byte-identical to today's and `fetch_content` is never called.

**US-X3: A hunk shows its enclosing function.**
As a PR author, I want the reviewer to see the function a change sits in, so that
a change is not judged from bare `+`/`-` lines.
- *Given* `review_hunk_expansion_enabled=True` and a modified Python file whose
  hunk is inside `def save(...)`, *when* `review_generation` runs, *then* the
  rendered hunk includes the unchanged lines of `save` before and after it (bounded
  by `ExpandConfig`), marked as context lines, and the hunk header ranges stay valid.
- *Given* an unsupported language, a parse failure, a head file that does not match
  the patch, or an added or removed file, *then* that file's patch is unchanged.
- *Given* the flag is off, *then* the prompt is byte-identical to today's and
  `fetch_content` is not called for a diff that fits.

**US-X4: Data and fixtures cost one line and are listed.**
As a PR author, I want cassettes and golden files not to eat the review, and I
want to know they were not read.
- *Given* a PR containing `review/testdata/eval/cassettes/v2.json` (241 KB patch)
  and a 30-line source change, *when* `review_generation` runs, *then* the prompt
  holds a one-line stub for the cassette (path, status, line and byte counts,
  "not reviewed in detail"), the stub file is still in `ReviewGenerationOutput.diff`,
  and the posted review body lists the file in its "Not reviewed" section with the
  reason.
- *Given* a small fixture under the threshold, *then* it is rendered in full.
- *Given* a PR with nothing stubbed, *then* the prompt and the posted body are
  byte-identical to today's.
- *Given* a stubbed file, *then* static analysis still scans it (the pipeline
  `Diff` is not shrunk).

**US-X5: Bulk deletions collapse.**
As a PR author, I want a deleted file not to spend my prompt budget.
- *Given* a hunk with no added lines and at least 20 deleted lines (a whole-file
  deletion is one such hunk), *when* the prompt is rendered, *then* the hunk body is
  replaced by `(N lines deleted)` and the file is listed as summarised.
- *Given* a deletion-only hunk under 20 lines, *then* it is shown in full.
- *Given* a hunk that both adds and deletes, *then* it is never collapsed.

**US-X6: Prose is trimmed but spec review still works.**
As a PR author, I want a 70 KB plan document not to crowd out code, without breaking
spec-compliance review.
- *Given* a `.md` patch over `review_prose_max_bytes`, *when* the generation prompt
  is rendered, *then* it keeps the first `prose_keep_bytes` and a note
  "(prose trimmed: N more lines; not reviewed in detail)", and the file is listed.
- *Given* `spec_review` runs on the same PR, *then* it still resolves the spec
  document and still sees every spec path in the diff; its own code-diff prompt may
  drop data and bulk deletions but never trims prose (`prose_policy="keep"`).

**US-X7: Every batch knows the whole PR.**
As a PR author, I want a batch to know what the other batches contain, so that a
call to a symbol defined elsewhere is not reported as missing.
- *Given* an oversize diff reviewed in three batches, *when* batches run, *then*
  each prompt contains the same PR map section (one line per changed file: path,
  status, `+a/-d`, changed symbols, and the batch number or "not reviewed"), marked
  as inert reference data, within `review_pr_map_max_bytes`, and the batch's own
  files are flagged as "this batch".
- *Given* a diff that fits one prompt, *then* no map is added (prompt unchanged).
- *Given* a file that cannot be parsed, *then* its map line has no symbols and
  the review completes.

**US-X8: The cap drops the least valuable files first, and says so.**
As a PR author, I want the cap to cut cassettes and docs before source.
- *Given* an oversize diff needing more batches than `max_diff_chunks`, with source,
  tests, prose and data files, *when* batches are planned with risk ordering on,
  *then* every dropped file's risk is at most the lowest risk of any kept batch's
  files in aggregate (source and changed logic are kept before tests, tests before
  docs, docs before data; a pure move or rename ranks below a logic change), and the
  dropped paths are in `unreviewed_files` and the warning log.
- *Given* the diff fits, *then* file order is the diff order, as today.
- *Given* `review_risk_ordering_enabled=False`, *then* the plan is identical to the
  current diff-order plan.

**US-X9: A PR too big for batching is reviewed commit by commit.**
As a PR author, I want a huge PR still covered, and I do not want stale findings.
- *Given* `review_commit_mode_enabled=True`, a batching plan that drops files, and a
  PR of at most `review_commit_max_commits` linear commits, *when* `review_generation`
  runs, *then* consecutive commits are merged into slices of at most
  `max_diff_bytes`, at most `max_diff_chunks` slices are reviewed (one completion
  each), and files no slice covers are in `unreviewed_files`.
- *Given* a finding whose cited line text is no longer in the head file, *then* it
  is not posted. *Given* it is still present on a different line, *then* its line is
  rewritten to the head line. *Given* it cites a trivial line (blank, under 8
  characters) or the head content cannot be fetched, *then* it is kept as a
  file-level finding and a warning is logged.
- *Given* the commit list cannot be fetched, the PR has more commits than the cap,
  or commits are not linear, *then* the review falls back to batching.

**US-X10: Commit listing does not break existing fetchers.**
As a platform operator, I want the commit source to be additive.
- *Given* `GitHubDiffFetcher` and `LocalGitDiffFetcher`, *then* neither gains a
  method; `CommitSource` is a separate consumer-defined Protocol that
  `GitHubCommitSource` and `LocalGitCommitSource` implement.
- *Given* the GitHub commits API returns a page of commits and a compare response,
  *when* `GitHubCommitSource` runs against a recorded cassette, *then* it returns
  `CommitInfo` values oldest first and a `Diff` for the range.
- *Given* a real throwaway git repo, *then* `LocalGitCommitSource` returns the same
  shapes.

**US-X11: Settings are safe and the cost is capped.**
As a platform operator, I want a misconfiguration to fail at startup, not in a job.
- *Given* settings whose step timeouts cannot cover the commit-range fetch,
  verification context fetch and LLM waves, *then* `WorkerSettings` construction
  fails naming the setting.
- *Given* any combination of flags, *then* generation calls per review never
  exceed `max_diff_chunks` and verification calls never exceed `max_diff_chunks + 1`.

## Interfaces (fixed)

Exact signatures. Builders must not rename or reorder. `frozen=True` pydantic models
unless stated. All functions are pure CPU, no I/O, never raise on bad input (they
return the unchanged input or an empty result); the caller wraps CPU-heavy calls in
`asyncio.to_thread`.

### (P) `review/diff_classify.py` (Builder P; imports `ingestion.diff` only)

```python
FileKind = Literal["source", "test", "docs", "data", "lockfile", "generated", "other"]
ShrinkReason = Literal["data", "deletion", "prose"]


def classify_path(path: str) -> FileKind: ...
def is_move_only(patch: str | None) -> bool:
    ...
    # True when the multiset of stripped added lines equals that of removed lines.


def risk_score(file: DiffFile) -> float:
    ...
    # 0.0..11.0. base: source 10, test 6, other 5, docs 3, data 1, generated 0,
    # lockfile 0. x0.3 when is_move_only. + min(changed_lines, 200) / 200.


def risk_by_path(diff: Diff) -> dict[str, float]: ...


class ShrinkConfig(BaseModel):
    stub_data_min_bytes: int = 8_000
    collapse_deletion_min_lines: int = 20
    prose_max_bytes: int = 12_000
    prose_keep_bytes: int = 4_000
    prose_policy: Literal["trim", "keep"] = "trim"


class ShrinkNote(BaseModel):
    path: str
    reason: ShrinkReason
    original_bytes: int
    kept_bytes: int


class ShrinkResult(BaseModel):
    diff: Diff  # `is` the input object when notes is empty
    notes: tuple[ShrinkNote, ...]


def shrink_diff(diff: Diff, config: ShrinkConfig | None = None) -> ShrinkResult: ...
def render_shrink_note(note: ShrinkNote) -> str:
    ...
    # "<path> (<reason text>; not reviewed in detail)"; goes into unreviewed_files.
```

Rules: a `data` path is a file whose kind is `data` (path segment in `testdata`,
`fixtures`, `cassettes`, `__snapshots__`, `snapshots`, `golden`, `baseline(s)`; or
extension `.jsonl`, `.ndjson`, `.csv`, `.tsv`, `.snap`, `.har`; or `.json`/`.yaml`
under such a segment). A `.json`/`.yaml`/`.toml` outside those segments is `data`
only when its patch exceeds `stub_data_min_bytes` and its basename is not a known
config (`package.json`, `tsconfig*.json`, `pyproject.toml`, `*.schema.json`). A data
patch over `stub_data_min_bytes` becomes
`(data file: +A -D lines, B bytes)`. Deletion collapse is per hunk, header line
kept. Prose is `.md`/`.rst`/`.adoc`/`.txt`; trim cuts at a line boundary. A file
with `patch is None` is never changed.

`review/diff_batching.py` change (Builder P): `plan_batches` gains one keyword,
`risk: Mapping[str, float] | None = None`. `None` keeps today's behaviour exactly.
When given: `ordered` groups sort by `(-group_risk, g[0])` instead of by directory
(group risk is the sum of its files' scores), and the final batches sort by
`(-batch_risk, first_index)` before the `max_chunks` cut. Within a batch, files keep
diff order. The single-batch short circuit (diff fits) is untouched.
`BatchPlan` is unchanged; `dropped` lists the cut files in diff order.

`review/prefilter.py` is **deliberately unchanged**: stubbed files must stay in the
pipeline `Diff` so gitleaks and Semgrep still scan them and inline comments can
anchor. Builder P owns the file only to confirm this with a regression test (a
cassette path survives prefilter).

### (X) `review/diff_expand.py` (Builder X; imports `ingestion.diff`, `indexing.chunk`)

```python
class ExpandConfig(BaseModel):
    context_before: int = 40  # max unchanged lines added above a hunk
    context_after: int = 20  # max below
    max_bytes_per_file: int = 16_000  # expanded patch above this: original returned


def expandable_paths(diff: Diff) -> list[str]:
    ...
    # status modified/renamed, patch present, language_for_path(path) is not None.


def expand_patch(
    path: str, patch: str, head_content: bytes, config: ExpandConfig | None = None
) -> str: ...
def expand_diff(
    diff: Diff, contents: Mapping[str, bytes], config: ExpandConfig | None = None
) -> Diff: ...
```

Contract: for each hunk, find the smallest `Chunk` (from `chunk_file`) with a
`symbol_name` whose `line_range` contains the hunk's new-side start; add context
lines (prefixed with a single space) from `head_content` between
`max(symbol_start, hunk_start - context_before)` and the hunk, and after it up to
`min(symbol_end, hunk_end + context_after)`; clamp so no head line appears twice
across neighbouring hunks; rewrite the `@@` header counts so they stay valid, and
append the symbol name to the header section text when it is empty. Before
expanding, check that the first context or added line of the hunk equals head line
`new_start`; on mismatch, parse error or decode error return the original patch.
`expand_diff` replaces only patches that changed; with none changed it returns the
input object.

### (X) `review/pr_map.py` (Builder X; imports `ingestion.diff`, `indexing.chunk`)

```python
class PrMapEntry(BaseModel):
    path: str
    status: str
    added: int
    deleted: int
    symbols: tuple[str, ...]  # chunk symbols whose head line range holds an added line


def build_pr_map(
    diff: Diff, contents: Mapping[str, bytes], *, max_symbols_per_file: int = 8
) -> tuple[PrMapEntry, ...]: ...
def render_pr_map(
    entries: Sequence[PrMapEntry],
    batches: Sequence[Sequence[str]],
    *,
    current: int,  # 0-based batch index this prompt is for
    unreviewed: Collection[str] = (),
    max_bytes: int,
) -> str: ...
```

`render_pr_map` returns the full prompt section including a leading `"\n\n"` and
the framing `INERT_REFERENCE_DATA_FRAMING` (imported from `review.diff_prompt`), one
line per file `path (status +a/-d) [batch k | this batch | not reviewed]: sym, sym`,
truncated at `max_bytes` with `... and N more files`. It returns `""` for no entries.
Extraction is deterministic from `indexing`: `GraphNode` carries no line numbers, so
changed symbols come from `chunk_file`'s `Chunk.symbol_name` and `line_range`
(same tree-sitter parse that `extract_graph` uses). No LLM call: the map is a
lookup, a call would add cost and a failure mode with no accuracy gain. Removed
files and unparseable files get `symbols=()`.

Size accounting (integrator): reserve `review_pr_map_max_bytes` from
`max_diff_bytes` when planning (`plan_batches(max_bytes=max_diff_bytes -
pr_map_max_bytes)`), build the map once, render per batch after the plan is known.

### (V) `review/verification_context.py` (Builder V; imports `findings.models`, `indexing.chunk`)

```python
class FileContextConfig(BaseModel):
    max_bytes_per_file: int = 24_000  # whole file when it fits, else windows
    surrounding_lines: int = 40
    max_total_bytes: int = 80_000


def finding_context_paths(findings: Sequence[Finding]) -> tuple[str, ...]:
    ...
    # Unique cited paths, first-appearance order, secret-shaped paths excluded
    # (.env*, *.pem, *.key, id_rsa*), data-kind paths excluded.


def build_file_contexts(
    findings: Sequence[Finding],
    head_files: Mapping[str, bytes],
    config: FileContextConfig | None = None,
) -> dict[str, str]:
    ...
    # path -> numbered text ("  42| line"). Files with NUL bytes or undecodable
    # content are skipped. Windows = enclosing symbol (smallest Chunk with a
    # symbol_name whose line_range holds the finding line) union +-surrounding_lines,
    # merged, gaps shown as "...". A finding with no line gets the file head.
    # Stops adding files at max_total_bytes; omitted paths get no entry.


def render_file_context_section(contexts: Mapping[str, str]) -> str:
    ...
    # "" when empty. Otherwise "\n\nCurrent (head) version of the files the candidate
    # findings cite (" + INERT_REFERENCE_DATA_FRAMING + "). A finding is invalid if
    # this code refutes it:\n" followed by "--- <path>\n<text>" blocks.
```

How the step obtains head contents without changing `DiffFetcher`: `VerificationStep`
receives a new optional `file_context: VerificationFileContext | None` (dataclass in
`review/verification.py`: `diff_fetcher: DiffFetcher`, `config: FileContextConfig`,
`timeout_seconds: float`). In `run()`, after memory suppression and before judging:
`paths = [p for p in finding_context_paths(llm_findings) if p is a non-removed file in
parsed.diff]`; `contents = await asyncio.timeout(...)(diff_fetcher.fetch_content(
installation_id=..., repo_full_name=..., diff=parsed.diff, paths=paths))`;
`contexts = await asyncio.to_thread(build_file_contexts, llm_findings, contents,
config)`. `parsed.diff` is the pipeline head `Diff` (it carries the blob SHAs, so
this is the head version). Catch `DiffContentUnavailableError`,
`DiffContentMalformedError`, `TimeoutError` and fall back to no context. The context
reaches the judge as a new field `JudgeInput.file_context: str = ""`; per batch group
the step renders the section from the group's own findings' paths. `JudgeInput` needs
`org`/`repo`-style job fields the step already has; `installation_id` and
`repo_full_name` come from `_VerificationInput` (add both, defaulted via
`JobIdentity`, already present).

### (C) Commits (Builder C)

`ingestion/commits.py` (domain, consumer-defined, no infra imports):

```python
class CommitInfo(BaseModel):
    sha: str
    parent_shas: tuple[str, ...]
    title: str = ""


class CommitsUnavailableError(Exception):
    def __init__(self, *, repo_full_name: str, detail: str) -> None: ...


class CommitSource(Protocol):
    async def list_commits(
        self, *, installation_id: int, repo_full_name: str, pr_number: int, head_sha: str
    ) -> tuple[CommitInfo, ...]: ...  # oldest first
    async def fetch_range_diff(
        self, *, installation_id: int, repo_full_name: str, base_sha: str, head_sha: str
    ) -> Diff: ...  # base_sha..head_sha, patches only
```

`adapter/github/commits.py`:

```python
class GitHubCommitSource:
    def __init__(
        self,
        *,
        token_provider: GitHubInstallationTokenProvider,
        github_api_base_url: str = _DEFAULT_GITHUB_API_BASE_URL,
        timeout: float = 10.0,
        max_retry_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None: ...
```

`list_commits` pages `GET /repos/{o}/{r}/pulls/{n}/commits?per_page=100`;
`fetch_range_diff` pages `GET /repos/{o}/{r}/compare/{base}...{head}?per_page=100`
and maps `files[]` to `DiffFile` (`mode`, `blob_sha`, `blob_size_bytes` stay `None`;
they are not needed). Reuses `adapter/github/http.request_with_retry`; translates
every adapter error to `CommitsUnavailableError`.

`adapter/localgit/commits.py`:

```python
class LocalGitCommitSource:
    def __init__(self, repo: Path, base_ref: str, head_ref: str) -> None: ...
```

`list_commits` runs `git log --reverse --topo-order --end-of-options base..head`
(`--end-of-options` so a ref beginning with `-` is never read as an option);
`fetch_range_diff` returns `LocalGitDiffFetcher(repo, base_sha, head_sha).fetch(...)`
(composition, no duplicated diff code). Errors become `CommitsUnavailableError`.

`review/commit_slices.py` (pure):

```python
class CommitSlice(BaseModel):
    base_sha: str  # first commit's first parent
    head_sha: str  # last commit's sha
    shas: tuple[str, ...]
    estimated_bytes: int  # sum of member commit sizes (an over-estimate)


class CommitPlan(BaseModel):
    slices: tuple[CommitSlice, ...]
    skipped_commits: tuple[str, ...]  # merge or root commits (parents != 1)
    dropped_commits: tuple[str, ...]  # beyond max_slices


def plan_commit_slices(
    commits: Sequence[CommitInfo],
    sizes: Mapping[str, int],  # sha -> rendered bytes of that commit alone
    *,
    max_bytes: int,
    max_slices: int,
) -> CommitPlan: ...


class HeadMatch(BaseModel):
    present: bool
    line: int | None  # head line number; None = keep as file-level


def locate_at_head(
    *, line: int | None, slice_patch: str | None, head_content: bytes | None
) -> HeadMatch: ...
```

`plan_commit_slices`: greedy, consecutive; a slice breaks when the next commit does
not have the previous commit as `parent_shas[0]` (non-linear) or adding it would
exceed `max_bytes`; a commit alone over `max_bytes` is its own slice. Merge/root
commits are skipped, never sized. `locate_at_head`: `head_content is None` -> `present
False`; `line is None` -> `present True, line None`; take the stripped text of
new-side line `line` from `slice_patch` (added or context line); if not found, blank
or under 8 characters -> `present True, line None`; else search head lines with equal
stripped text, nearest to `line` wins -> `present True, line=<that>`; none ->
`present False`.

### (W) Integrator wiring (Integrator W)

New config dataclass in `review/review_generation.py`:

```python
@dataclass(frozen=True)
class ReviewContextConfig:
    shrink: ShrinkConfig | None  # None = shrink off
    expand: ExpandConfig | None  # None = hunk expansion off
    pr_map_max_bytes: int  # 0 = PR map off
    risk_ordering: bool
    commits: CommitSource | None  # None = commit mode off
    commit_max_commits: int
    commit_fetch_timeout_seconds: float
```

`ReviewGenerationStep.__init__` gains `context: ReviewContextConfig | None = None`
(None = today's behaviour). `ReviewGenerationOutput` gains nothing: stubbed files
and summarised files are appended to the existing `unreviewed_files` as
`render_shrink_note(...)` strings, so `verification` forwards them and
`output`/`adapter/github/posting.py` list them with the existing 50-path cap and
escaping. `output.py` change is limited to the section heading wording ("Not
reviewed" -> "Not reviewed in detail") only if the existing test pins allow; otherwise
unchanged.

Generation flow (only when `context` is set; each stage is skipped when its field is
off):

1. `shrunk = shrink_diff(parsed.diff, context.shrink)`; `prompt_diff = shrunk.diff`.
   Notes -> `unreviewed_files`. The pipeline `Diff` forwarded in the output is still
   `parsed.diff`.
2. If expansion is on: fetch head contents of `expandable_paths(prompt_diff)` once
   (`fetch_content`, under `batching.link_timeout_seconds` or a 15 s default), then
   `prompt_diff = await to_thread(expand_diff, prompt_diff, contents)`. On fetch
   failure keep the unexpanded diff. The same contents feed link building and the PR
   map (one fetch, not three).
3. Fit test on `render_diff(prompt_diff)` against `max_diff_bytes`. Fits -> one
   completion, byte-identical prompt when nothing changed, no map.
4. Does not fit -> `_plan_batches` with `sizes` from `prompt_diff`, `risk=risk_by_path`
   when on, `max_bytes = max_diff_bytes - pr_map_max_bytes`. Build the map once
   (`build_pr_map`), render per batch and pass as an extra section to
   `_build_user_message(..., pr_map: str = "")` (appended after the diff block, before
   the context bundle).
5. If `plan.dropped` and `context.commits` set: try commit mode
   (`_review_in_commit_slices`): `list_commits`; bail to step 4's plan if more than
   `commit_max_commits`, any error, or no slices; size each commit with
   `fetch_range_diff(parent, sha)` under `commit_fetch_timeout_seconds` (bounded by
   `diff_batch_concurrency`); `plan_commit_slices`; per slice fetch the range diff,
   restrict to paths in `parsed.diff`, shrink it, one completion; merge with
   `merge_findings`; for each finding fetch head content once, apply `locate_at_head`
   with the slice patch, drop `present False`, rewrite `line`. `review_batches` is the
   last-slice-wins partition of files; `unreviewed_files` is every head-diff path no
   slice covered. Total completions stay at most `max_diff_chunks`.

`review/spec_review.py`: `render_and_truncate_diff(shrink_diff(diff,
ShrinkConfig(prose_policy="keep")).diff, ...)` when shrink is on; spec resolution
still reads `parsed.diff` untouched. `review/diff_prompt.py`: no change expected
(shrink and expand return `Diff`s, so `render_diff` is reused), W may add a
`render_prompt_diff` helper only if duplication appears. `review/finding_judge.py`
(W also owns this file for this change, it was missing from the original ownership
list): `JudgeInput.file_context: str = ""`, `_build_user_message(..., file_context: str
= "")` appends it after the diff and before the context bundle; empty string keeps the
prompt byte-identical.

Worker and local wiring: `apps/worker/main.py` builds `ReviewContextConfig` and
`VerificationFileContext` from settings, constructing `GitHubCommitSource` only when
`review_commit_mode_enabled`; `apps/local_review/__main__.py` mirrors it with
`LocalGitCommitSource` and `REVIEW_LOCAL_*` names.

## Settings (`apps/worker/settings.py`, env var = upper-case name)

| Setting | Default | Meaning |
| --- | --- | --- |
| `review_shrink_enabled` | `True` | Stub data, collapse bulk deletions, trim prose. Inert when nothing matches. |
| `review_stub_data_min_bytes` | `8000` (ge=1) | Data patch size that gets stubbed. |
| `review_prose_max_bytes` | `12000` (ge=1) | Prose patch size that gets trimmed (keeps 4000). |
| `review_risk_ordering_enabled` | `True` | Risk-ordered batches. Affects only oversize diffs. |
| `review_pr_map_enabled` | `True` | Map header in batched prompts. Affects only oversize diffs. |
| `review_pr_map_max_bytes` | `8000` (ge=0) | Map size and the budget reserved from `llm_max_diff_bytes`. |
| `review_hunk_expansion_enabled` | `False` | Enclosing-function context in hunks. |
| `verification_file_context_enabled` | `False` | Head-file context for the verifier. |
| `verification_file_context_max_bytes` | `24000` | Whole-file cap per file; windows above it. |
| `verification_file_context_total_bytes` | `80000` | Cap across all files in one judge call. |
| `verification_file_context_timeout_seconds` | `10` | Head-content fetch bound; falls back to diff-only. |
| `review_commit_mode_enabled` | `False` | Commit-range review when batching drops files. |
| `review_commit_max_commits` | `60` (ge=1) | More commits than this: batching only. |
| `review_commit_fetch_timeout_seconds` | `30` (ge=1) | Bound on commit listing plus per-commit sizing. |

`review_hunk_expansion_enabled` and `verification_file_context_enabled` default to
`False` because they change the prompt of a PR that fits one batch, and the contract
for this feature is that a small PR is byte-for-byte unchanged. Flip both to `True`
after the eval comparison in Open items; no code change needed. Shrink, ordering
and the map default on because they do nothing for a PR with no stubbed file that
fits one batch.

Validator additions (extend `_batched_steps_fit_their_timeouts`):
`review_generation_step_timeout_seconds >= generation_needed +
(review_commit_fetch_timeout_seconds if commit mode else 0)` and
`verification_step_timeout_seconds >= verification_needed +
(verification_file_context_timeout_seconds if file context else 0)`. Local settings:
`REVIEW_LOCAL_` variants of the same names. `.env.example` documents each.

## Non-Functional Requirements

- **Cost (decided):** no new LLM call anywhere. Generation completions per review
  at most `max_diff_chunks` (commit mode shares the cap), verification at most
  `max_diff_chunks + 1`. Verification prompt grows by at most
  `verification_file_context_total_bytes` (80 KB, about 20k tokens) per call, only
  when findings exist and the flag is on; with the feature off, token spend falls
  because data and bulk deletions leave the prompt (about 42% of this branch's
  bytes). Commit mode adds GitHub API calls (at most `review_commit_max_commits + 2`
  plus one per slice), no LLM spend beyond the cap.
- **Timeouts:** each LLM call keeps `llm_timeout_seconds`. Head-content fetches are
  bounded (`link_timeout_seconds`, `verification_file_context_timeout_seconds`,
  `review_commit_fetch_timeout_seconds`) and fall back, never block. The settings
  validator rejects timeouts that cannot cover the worst case.
- **CPU and memory:** parsing runs in `asyncio.to_thread`; only changed or cited
  files are fetched; `prefilter`'s file and byte ceilings still bound the set;
  `ExpandConfig.max_bytes_per_file` and `FileContextConfig` caps bound output.
- **Security:** file content is parsed, never executed or logged; logs and metrics
  carry paths and counts only; paths in the posted review are escaped (existing
  `escape_markdown`). Whole-file verification context is new exposure of unchanged
  lines to the BYO LLM endpoint, so secret-shaped paths (`.env*`, `*.pem`, `*.key`,
  `id_rsa*`) and data files are never sent, and the prompt marks the block as inert
  reference data (`INERT_REFERENCE_DATA_FRAMING`). The PR map and file context are
  PR-author-controlled text and use the same framing. Fork PRs cannot raise cost past
  the caps.
- **Reliability / degradation:** every stage has a defined fallback (US-X2, US-X3,
  US-X7, US-X9); a failure of any new stage logs a warning and yields today's
  behaviour for that file or step. Redaction stays fatal-on-failure (unchanged).
- **Byte-for-byte invariant:** with every flag at its default and a PR that fits one
  batch with no stubbed or summarised file, prompts, `ReviewGenerationOutput`,
  `VerificationOutput` and the posted body are identical to today's. Covered by
  explicit tests (US-X3/X4/X7/X2 first scenarios).
- **Observability:** one `info` line per feature use (counts only: stubbed,
  collapsed, trimmed, expanded files; map bytes; file-context files and bytes;
  commit slices, skipped, dropped, filtered-at-head). Counters, closed label sets:
  `review.input_shrink.files_total{reason=data|deletion|prose}`,
  `review.verification.file_context_total{outcome=used|fallback|skipped}`,
  `review.commit_review.runs_total{outcome=used|fallback}`,
  `review.commit_review.findings_filtered_total`. No paths in labels.

## Phases / Units

Four builders on disjoint files, then the integrator. No builder touches another's
files.

| Unit | Owner | Files (+ co-located `test_*.py`) | Depends on |
| --- | --- | --- | --- |
| P | Builder P | `review/prefilter.py` (regression test only), new `review/diff_classify.py`, `review/diff_batching.py` (risk param only) | none |
| X | Builder X | new `review/diff_expand.py`, new `review/pr_map.py` | none (imports `indexing`, `review.diff_prompt` constant) |
| V | Builder V | new `review/verification_context.py` | none |
| C | Builder C | new `ingestion/commits.py`, `adapter/github/commits.py`, `adapter/localgit/commits.py`, new `review/commit_slices.py` | none |
| W | Integrator | `review/review_generation.py`, `review/verification.py`, `review/finding_judge.py`, `review/output.py`, `review/spec_review.py`, `review/diff_prompt.py`, `apps/worker/settings.py`, `apps/worker/main.py`, `apps/local_review/__main__.py`, `.env.example`, step-level integration tests | P, X, V, C |

Builders import each other's types only through the signatures above (P and X are
independent: X's `render_pr_map` takes plain `risk`-free inputs; V does not import P,
it uses its own path rules; C does not import P, `plan_commit_slices` takes plain
mappings). Shared constant `INERT_REFERENCE_DATA_FRAMING` already lives in
`review/diff_prompt.py` (unchanged).

## Testing / Key Scenarios

Integration tests use the recorded-response fake `LLMClient` and a fake `DiffFetcher`
/ `CommitSource` (fakes at Protocol boundaries, real tree-sitter parsing), per
`testing.md`; the GitHub adapter uses `respx`, the local adapter a real temp git repo.
All integration tests carry `pytest.mark.integration` and `pytest.mark.asyncio`.
Unit tests are co-located with each new module.

| Story | Scenario (G/W/T summary) | Level | Test path |
| --- | --- | --- | --- |
| US-X1 | Tracked-file / module `pytestmark` / body-only-path findings rejected with head context; large file gets windows; one block per file | Integration | `review/test_review_context_steps.py` (verification half) |
| US-X1 | Window selection, whole-file cap, line numbers, total cap, secret-path exclusion | Unit | `review/test_verification_context.py` |
| US-X2 | Fetch error, timeout, binary, secret-shaped, off-diff path: no context, review completes; flag off byte-identical, no fetch | Integration | `review/test_review_context_steps.py` |
| US-X3 | Expanded hunk shows enclosing function; unsupported/parse-failure/mismatch unchanged; flag off identical | Integration | `review/test_review_context_steps.py` |
| US-X3 | Header rewrite, clamping across neighbouring hunks, mismatch fallback, added/removed skip | Unit | `review/test_diff_expand.py` |
| US-X4 | Cassette stubbed in prompt, still in output diff, listed in posted body; small fixture kept; nothing stubbed = identical; gitleaks input still includes it | Integration | `review/test_review_context_steps.py` (through `FindingsOutputStep`), `adapter/github/test_posting_unreviewed.py` (reason text, escaping, 50 cap) |
| US-X4 | Classification table, threshold edges, `diff is input` when no notes, config-file exemptions | Unit | `review/test_diff_classify.py` |
| US-X4 | Cassette path survives prefilter | Unit | `review/test_prefilter.py` |
| US-X5 | Whole-file deletion collapses and is listed; 19-line deletion kept; mixed hunk kept | Integration + Unit | `review/test_review_context_steps.py`, `review/test_diff_classify.py` |
| US-X6 | 70 KB plan trimmed and listed; `spec_review` still resolves spec and sees spec paths, prose kept | Integration | `review/test_review_context_steps.py` (with `review/test_spec_review_step.py` unchanged) |
| US-X7 | Three batches each carry the same map with correct batch labels and "this batch"; fits = no map; unparseable file has no symbols | Integration | `review/test_review_context_steps.py` |
| US-X7 | Changed-symbol extraction, truncation, empty input, determinism | Unit | `review/test_pr_map.py` |
| US-X8 | Mixed source/test/prose/data oversize diff, cap 2: dropped set is the lowest-risk files and is listed; fits keeps diff order; flag off equals old plan | Integration | `review/test_review_context_steps.py` |
| US-X8 | Risk table, move-only detection, ordering with `risk`, determinism, `risk=None` equals old | Unit | `review/test_diff_classify.py`, `review/test_diff_batching.py` |
| US-X9 | Oversize PR, commit mode: slices within cap, gone finding dropped, moved finding re-anchored, trivial line kept file-level, fetch failure / too many commits / non-linear falls back | Integration | `review/test_review_context_steps.py` (commit half) |
| US-X9 | Slice greedy packing, linear-break, merge-commit skip, oversize single commit, `locate_at_head` cases | Unit | `review/test_commit_slices.py` |
| US-X10 | GitHub commits and compare pages against a recorded cassette; auth, 404, retry exhausted map to `CommitsUnavailableError`; existing `GitHubDiffFetcher` tests untouched | Integration | `adapter/github/test_commits.py` (`respx`) |
| US-X10 | Real temp repo: commit list oldest first, range diff equals `LocalGitDiffFetcher` | Integration | `adapter/localgit/test_commits.py` |
| US-X11 | Validator rejects short step timeouts with each new term; defaults load | Unit | `apps/worker/test_settings.py` |
| US-X11 | `main.py` builds the config and `GitHubCommitSource` only when flags on; local runner mirrors | Unit | `apps/worker/test_main.py`, `apps/local_review/test_main.py` |
| All | Compose stack: PR with a cassette and a large source change posts a review listing the cassette, covering all source files | E2E | **Deferred** (no `tests/e2e/test_review_context.py` yet; needs a runnable Compose stack to write and verify, which the delivery sandbox lacks). Covered meanwhile through the real `ReviewGenerationStep` -> `FindingsOutputStep` chain in `review/test_review_context_steps.py` and the posting adapter in `adapter/github/test_posting_unreviewed.py`. |

Failure-mode E2E addition (per `testing.md`): head-content fetch times out during
verification and the review still posts with diff-only verification. **Deferred** with the happy-path E2E above;
the same case runs at integration level (`test_us_x2_every_fetch_problem_degrades_to_diff_only`).

## Rollback

Every behaviour has an env switch and no schema, queue or persisted shape changes:

- `review_shrink_enabled=False`, `review_risk_ordering_enabled=False`,
  `review_pr_map_enabled=False` restore today's prompts and plan exactly (stubs,
  ordering and map are the only default-on parts).
- `review_hunk_expansion_enabled`, `verification_file_context_enabled` and
  `review_commit_mode_enabled` are off by default; turning them off is the rollback.
- `diff_batching_enabled=False` (existing) disables batching and with it the map,
  ordering and commit mode; the single cut diff is back.
- No flag is read by the output contract: `unreviewed_files` is an existing field of
  strings, so old and new workers interoperate during a rolling deploy.
- Code rollback: revert the new modules; `plan_batches`'s `risk` keyword defaults to
  `None`, `JudgeInput.file_context` defaults to `""`, `ReviewGenerationStep(context=
  None)` is today's constructor.

## Open items (assumptions pending validation)

1. Thresholds (8,000 B data, 20 lines deletion, 12,000 / 4,000 B prose, risk
   weights 10/6/5/3/1/0, expansion 40/20 lines, context 24 KB / 80 KB) are starting
   values grounded in one branch, not tuned. Re-measure on three more PRs.
2. Before flipping `review_hunk_expansion_enabled` and
   `verification_file_context_enabled` to `True`, rerun the eval corpus
   (`review/testdata/eval`) for precision and cost against the current baseline, and
   re-run the dogfood PR to confirm the three known false positives are rejected.
3. `prefilter` silently drops lockfiles, vendored and binary files today (counted,
   not listed). Listing them is outside this change; revisit if authors ask.
4. Commit mode sizes each commit with one range-diff call; a 60-commit PR costs 60
   GitHub calls. If rate limits bite, size by commit stats instead.
5. `locate_at_head` is line-text matching, not a semantic check; a trivial or
   duplicated line may re-anchor to the wrong place. The verifier with head file
   context (US-X1) is the backstop, which is why commit mode and
   `verification_file_context_enabled` should be enabled together.
6. Whole-file verification context sends unchanged code to the LLM endpoint. The
   exclusion list is path-based; a secret in an ordinary source file is not caught
   (the diff path has the same property for changed lines).
7. `output.py` heading "Not reviewed" for stubbed files is slightly strong; if the
   existing posting tests allow, rename to "Not reviewed in detail".
