# Diff Batching for Large Pull Requests PRD

Status: Draft · Owner: Andy Beak (sole engineer/author, per every ADR's RACI) · Last updated: 2026-11-25

Lets the review cover a pull request whose diff is larger than one prompt can
hold, by splitting the diff into batches of related files, reviewing each batch
in its own LLM call, and merging the results. Built against
[`docs/rules/architecture.md`](../rules/architecture.md) (verification tag
`WKPYNQ`) and [`docs/rules/testing.md`](../rules/testing.md) (verification tag
`RQPTVE`); requirements are user stories with `Given/When/Then` acceptance
criteria per `testing.md`'s mandatory rule.

**Extends, not replaces, shipped modules.** `review_generation`
(`review/review_generation.py`) and `verification` (`review/verification.py`)
change behaviour only when the rendered diff exceeds `llm_max_diff_bytes`.
`spec_review` (`review/spec_review.py`) and `output` (`review/output.py`)
forward two new optional fields. `indexing` gains Python. No new pipeline step,
no new deployable, no Postgres access (link building parses only the changed
files).

## Problem & Goal

**End user: the PR author.** `ReviewGenerationStep` and `VerificationStep` render
the whole PR diff (`review.diff_prompt.render_and_truncate_diff`) and cut it at
`llm_max_diff_bytes` (200,000 bytes). Everything past the cut is never
reviewed, and nothing says so: a 600 KB PR gets a review of its first third
that reads like a review of the whole PR. The cut falls on diff order, so it
can also split a file from the test or caller that explains it.

**Secondary end user: the platform operator**, whose BYO-key LLM spend grows
with each extra call. Spend must be capped, and every file the cap leaves out
must be visible to the author and in logs.

**Goal (capability-based, no fabricated precision number):** (a) every changed
file the cap allows is shown to the LLM in full (an oversize single file is
still truncated alone, as today); (b) files that reference each other, and each
test file with the source it tests, are reviewed in the same call when they
fit; (c) a file the review did not look at is named in the posted review; (d)
when the whole diff fits one batch, the prompt and output are byte-for-byte
what they are today.

## Scope

### In scope (decided)

- **Batching in `review_generation`.** Only when the rendered diff exceeds
  `max_diff_bytes`: group changed files into batches of at most
  `max_diff_bytes` rendered bytes, run one completion per batch concurrently
  (bounded by a semaphore), merge the findings, then redact once.
- **Link graph over changed files only.** Parse the head content of the changed
  files (`DiffFetcher.fetch_content`), run `indexing.graph.extract_graph`, and
  link file A to file B when A imports a module B defines or calls a symbol B
  defines. Name-based, no module resolution (ADR-0007). Noisy by design, so:
  import edges weigh more than call edges; generic names and names defined in
  many changed files are ignored; hub files (very high degree) are excluded
  from component forming.
- **Packing.** Connected components are packed into batches; an oversize
  component is split along its weakest edges, falling back to directory
  boundaries; a single file over the ceiling gets its own batch and is
  truncated alone; a test file is paired with the source it tests.
- **Cost cap** `max_diff_chunks` (default 8). Batches past the cap are dropped,
  logged, and listed in the posted review summary.
- **Python in `indexing`** (`tree-sitter-python`) in both chunking and graph
  extraction, so this repo's own PRs get links. Consequence, stated plainly:
  `language_for_path` now returns `"python"` for `.py`, so the indexing
  pipeline starts indexing `.py` files for every repo that has them.
- **Verification per file.** When the diff was batched, each finding is judged
  against the diff of only the files that have findings in its batch, never the
  cut whole diff.
- **Graceful degradation.** Parse failure, unsupported language, content-fetch
  failure or link-build timeout degrades that file (or all files) to directory
  grouping. The review never fails because of linking.
- **Kill switch** `diff_batching_enabled` (default `True`). `False` restores
  today's cut exactly.

### Out of scope (deferred)

- Whole-repo call graph, Postgres graph queries, embeddings for grouping.
- Module resolution (Go modules, Composer autoload, tsconfig aliases, Python
  relative-import resolution). Edges stay name-based.
- Ranking which batches to keep under the cap by risk. Cap drops the tail in
  diff order (assumption 3).
- Cross-batch findings (a defect visible only by reading two batches). Linking
  is the mitigation; a miss is accepted.
- `intent_summary`'s own diff cut (it still reads the first
  `max_diff_bytes`; its input is a summary, not a review).
- Per-batch intent summaries or context bundles. Every batch gets the same
  intent summary and context bundle; static findings are filtered to the
  batch's files.
- Languages other than TypeScript/TSX, PHP, Go, Python for linking (they get
  directory grouping).

### Priority

Must: US-B1, US-B2, US-B3, US-B5, US-B6, US-B7, US-B9. Should: US-B4, US-B8,
US-B10 (B10 is a prerequisite for B3 on Python repos, so it ships in the same
phase).

## Requirements

**US-B1: A diff that fits is reviewed exactly as today.**
As a PR author, I want a normal-sized PR reviewed unchanged, so that this
feature cannot regress the common case.
- *Given* a diff whose rendered size is at most `max_diff_bytes`, *when*
  `review_generation` runs, *then* the prompt sent to the LLM Gateway is
  byte-identical to today's, exactly one completion is made, `fetch_content` is
  never called, and `ReviewGenerationOutput.review_batches` is empty.
- *Given* `diff_batching_enabled=False` and an oversize diff, *when* it runs,
  *then* the diff is cut at `max_diff_bytes` and one completion is made.

**US-B2: A large PR is reviewed past the old cut.**
As a PR author, I want every changed file reviewed, so that a defect in the
last file of a big PR is found.
- *Given* a diff of 450,000 rendered bytes across 30 files and a recorded
  response per batch that reports a defect in the file that used to fall after
  the 200,000-byte cut, *when* `review_generation` runs, *then* more than one
  completion is made, each prompt's diff text is at most `max_diff_bytes`, and
  the defect is in the step's output.
- *Given* two batches that report the same defect on the same file and line,
  *then* it appears once in the output.

**US-B3: Related files are reviewed together.**
As a PR author, I want a file reviewed together with the files it imports or
calls, so that a broken contract between them is visible in one prompt.
- *Given* an oversize diff in which `app/service.py` does
  `from app.repo import save` and `app/repo.py` defines `save`, and unrelated
  files fill the rest, *when* batches are planned, *then* both files are in the
  same batch.
- *Given* a file imported by more than the hub threshold of other changed
  files, *then* it does not pull those files into one component.
- *Given* an import link and a call link competing for the same capacity,
  *then* the import link is kept together first.

**US-B4: A test is reviewed with the source it tests.**
As a PR author, I want `test_foo.py` reviewed with `foo.py`, so that the test's
intent is judged against the code.
- *Given* an oversize diff containing `pkg/foo.py` and `pkg/test_foo.py` (or
  `foo.test.ts`/`foo.ts`, `foo_test.go`/`foo.go`) and no import link between
  them, *when* batches are planned, *then* both are in the same batch.

**US-B5: Oversize groups and files are split predictably.**
As a PR author, I want a too-big group split sensibly and a too-big file still
reviewed, so that nothing is silently skipped.
- *Given* one connected component larger than `max_diff_bytes`, *when* batches
  are planned, *then* every batch is at most `max_diff_bytes`, and the split
  falls on the lowest-weight links (a test pair is cut last); files with no
  remaining link group by directory.
- *Given* one file whose own rendered size exceeds `max_diff_bytes`, *then* it
  is alone in its batch and that batch's prompt is truncated at
  `max_diff_bytes` with the existing truncation warning.

**US-B6: A capped review says what it did not review.**
As a PR author, I want to see which files were not reviewed, so that I do not
read a partial review as a full one.
- *Given* an oversize diff needing 11 batches and `max_diff_chunks=8`, *when*
  the pipeline runs through `output`, *then* 8 completions are made, a warning
  log names the dropped files, and the posted review body contains a "Not
  reviewed" section listing them (capped at 50 paths plus "and N more").
- *Given* zero findings and a non-empty unreviewed list, *then* the review is
  still posted (it is not a silent clean result).
- *Given* nothing dropped, *then* the posted body is unchanged.

**US-B7: Linking failure degrades to directory grouping.**
As a platform operator, I want a parse or fetch problem never to fail a
review, so that batching is a quality feature, not a new failure mode.
- *Given* one changed file with invalid UTF-8 or an unsupported extension,
  *when* batches are planned, *then* that file is grouped by directory and the
  other files still get links.
- *Given* `fetch_content` raises `DiffContentUnavailableError`, or link
  building exceeds `diff_batch_link_timeout_seconds`, *then* all files group by
  directory, a warning is logged, and the review completes.

**US-B8: Each finding is verified against its own file's diff.**
As a PR author, I want verification to see the code a finding is about, so that
a true finding in a late file is not rejected for lack of context.
- *Given* a batched review whose finding is in a file that the old whole-diff
  cut would have dropped, *when* `verification` runs, *then* the judge prompt
  contains that file's diff, contains no file without a finding in that batch,
  and the finding is judged normally.
- *Given* `review_batches` is empty (diff fit), *then* verification is
  byte-identical to today.
- *Given* a finding whose file is in no batch (for example a spec finding on a
  non-diff path), *then* it is judged in one extra group against the cut whole
  diff, as today.

**US-B9: Concurrency is bounded and timeouts still hold.**
As a platform operator, I want batches to run concurrently without exceeding
the step timeout or hammering the endpoint.
- *Given* 8 batches and `diff_batch_concurrency=4`, *when* the step runs,
  *then* at most 4 completions are in flight at once.
- *Given* a multi-batch run in which one batch's completion fails or returns an
  unparseable response, *then* the other batches' findings are kept and the
  failed batch's files are added to `unreviewed_files`; *given* every batch
  fails, or there is exactly one batch, *then* the failure propagates exactly
  as today.
- *Given* settings where `ceil(max_diff_chunks / diff_batch_concurrency)` calls
  of `llm_timeout_seconds` plus the link timeout would not fit in
  `review_generation_step_timeout_seconds` (the same check for verification),
  *then* `WorkerSettings` construction fails.
- A redaction failure stays fatal (unchanged).

**US-B10: Python files are chunked and linked.**
As a PR author on a Python repo, I want Python files indexed and linked, so
that batching and the existing context builder understand them.
- *Given* `from app.repo import save as s` and `repo.save()` calls in a `.py`
  file, *when* the indexing pipeline processes it, *then* `language_for_path`
  returns `"python"`, chunks split at `def`/`class` boundaries, and the graph
  has edges targeting `repo` and `save`.

## Interfaces (fixed)

### (1) `indexing` Python support

`indexing/chunk.py`:

```python
ChunkLanguage = Literal["typescript", "tsx", "php", "go", "python"]
import tree_sitter_python as _tspython

_LANGUAGES["python"] = Language(_tspython.language())
_EXTENSION_LANGUAGE[".py"] = "python"
_LEAF_TYPES["python"] = frozenset({"function_definition"})
_CONTAINER_TYPES["python"] = frozenset({"class_definition"})
_IMPORT_TYPES["python"] = frozenset({"import_statement", "import_from_statement"})
```

`pyproject.toml` gains `"tree-sitter-python>=0.23,<1"` (not installed in the
venv today; add it and the lock/Docker layer). `_symbol_name` already reads the
`name` field, which Python `def`/`class` nodes carry; `decorated_definition`
needs no entry because the walk recurses into it.

`indexing/graph.py`:

```python
_DEFINITION_TYPES["python"] = frozenset({"function_definition", "class_definition"})
_CALL_TYPES["python"] = frozenset({"call"})
_IMPORT_TYPES["python"] = frozenset({"import_statement", "import_from_statement"})


def _call_target(node: Node, language: ChunkLanguage) -> str | None: ...  # + python branch
def _import_targets(node: Node, language: ChunkLanguage) -> list[str]: ...  # + python branch


def _python_call_target(node: Node) -> str | None:
    """`call` node: field `function` is an `identifier` -> its text; an
    `attribute` -> its `attribute` field's text (`a.b.save()` -> "save");
    anything else (subscript, call-of-call, lambda) -> None."""


def _python_import_targets(node: Node) -> list[str]:
    """`import a.b.c` / `import a.b as x` -> ["c"] (last dotted segment, never
    the alias). `from a.b import c, d as e` -> ["b", "c", "d"] (module's last
    segment, then each real imported name, never aliases). `from . import x`
    -> ["x"]. `from .m import *` -> ["m"] (wildcard binds no name). Edges stay
    name-based: no module resolution (ADR-0007)."""
```

Imports attach to the synthetic `<module>` node, as for every language today.
`extract_graph`'s `_DEFINITION_TYPES[language]` lookups already raise
`GraphExtractionError` for a language without a mapping, so the three maps
above are all that must be added.

### (2) `review/diff_batching.py` (pure; no infra, no `indexing` import)

```python
from collections.abc import Mapping, Sequence
from pydantic import BaseModel, ConfigDict

IMPORT_WEIGHT: float = 3.0
CALL_WEIGHT: float = 1.0
TEST_PAIR_WEIGHT: float = 100.0  # a test pair is cut last and is hub-exempt
HUB_MIN_DEGREE: int = 8  # a file linked to >= max(8, 25% of files) others is a hub
HUB_DEGREE_FRACTION: float = 0.25
SEPARATOR_BYTES: int = 2  # the "\n\n" render_diff joins files with

# path -> {other path -> weight}; either direction may be given, weights add.
FileLinks = Mapping[str, Mapping[str, float]]


class BatchPlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    batches: tuple[tuple[str, ...], ...]  # each batch in original diff order;
    # batches ordered by first file's diff index
    dropped: tuple[str, ...]  # paths in batches past max_chunks, diff order


def paired_source(test_path: str, paths: Sequence[str]) -> str | None:
    """test_foo.py / foo_test.py -> foo.py; foo.test.ts / foo.spec.ts(x) -> foo.ts(x);
    foo_test.go -> foo.go. Same directory wins; else the unique path with that
    basename; ambiguous or none -> None."""


def plan_batches(
    paths: Sequence[str],  # changed paths in diff order
    sizes: Mapping[str, int],  # rendered bytes per file (header + patch)
    links: FileLinks,
    *,
    max_bytes: int,
    max_chunks: int,
) -> BatchPlan:
    """1. If sum(sizes) + SEPARATOR_BYTES*(n-1) <= max_bytes: one batch of all
          paths in the given order (the byte-identical path), dropped=().
       2. Fold links undirected; add a TEST_PAIR_WEIGHT edge per paired test.
       3. Hub exclusion: drop a hub's non-pair edges (the file stays, linked
          only by its pair edge, else a singleton).
       4. Components by union-find, scanned in diff order (deterministic).
       5. Oversize component: merge its edges strongest-first (weight, then
          same-directory), joining two pieces only if the merged size <= max_bytes.
          The edges left unmerged are the weakest ones, so the split falls
          there; pieces with no mergeable edge fall to the directory rule in step 6.
       6. Pack pieces first-fit into bins <= max_bytes after sorting pieces by
          (directory of first file, first diff index), so unlinked files and
          leftovers group by directory. A file larger than max_bytes is a piece
          of its own and gets its own bin.
       7. Order bins by first file's diff index; bins past max_chunks go to
          `dropped`.
    Never raises on missing sizes (KeyError is a caller bug); a path with no
    links is a singleton."""
```

Generic-name filtering lives in the link builder (3), not here: this module
sees only already-filtered, weighted edges.

### (3) `review/diff_links.py` (calls `indexing`, no I/O)

```python
from collections.abc import Mapping
from pydantic import BaseModel, ConfigDict
from review.diff_batching import FileLinks

# Starter list, tuned from real PRs. Names a call/import target is ignored for.
GENERIC_SYMBOL_NAMES: frozenset[str] = frozenset(
    {
        "main",
        "init",
        "__init__",
        "get",
        "set",
        "run",
        "new",
        "add",
        "open",
        "close",
        "read",
        "write",
        "load",
        "create",
        "update",
        "delete",
        "handle",
        "process",
        "execute",
        "start",
        "stop",
        "render",
        "build",
        "parse",
        "format",
        "str",
        "len",
        "print",
        "test",
        "setup",
        "teardown",
        "index",
        "default",
        "name",
        "type",
        "id",
        "Error",
        "String",
    }
)  # domain verbs like "save" are not generic; the definer cap covers ambiguity
MIN_SYMBOL_NAME_LENGTH: int = 3
MAX_DEFINING_FILES: int = 3  # a name defined in more changed files is ambiguous: ignored


class FileLinksResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    links: dict[str, dict[str, float]]  # shape of review.diff_batching.FileLinks
    unparsed: tuple[str, ...]  # unsupported language, bad UTF-8, parse/graph error


def module_aliases(path: str) -> frozenset[str]:
    """Names other files may import this file by: the stem ("repo" for
    app/repo.py); for __init__.py/index.ts/mod.go-style files also the parent
    directory name; for Go, the parent directory name (the package name)."""


def build_file_links(contents: Mapping[str, bytes]) -> FileLinksResult:
    """For each path in `contents` with language_for_path(path) not None, call
    indexing.chunk.chunk_file (for the tree) then indexing.graph.extract_graph.
    Catches ChunkParseError and GraphExtractionError per file (never a bare
    except), records the path in `unparsed`, and continues. A file's defined
    names = its non-<module> GraphNode.symbol_name values plus module_aliases.
    For each edge in file A with target t (len >= MIN_SYMBOL_NAME_LENGTH, not in
    GENERIC_SYMBOL_NAMES, defined by 1..MAX_DEFINING_FILES other files): add
    IMPORT_WEIGHT when edge.source_node.symbol_name == MODULE_SYMBOL_NAME, else
    CALL_WEIGHT, from A to each definer B != A. Weights for the same pair add.
    Sync and CPU-bound: the caller wraps it in asyncio.to_thread."""
```

`shortcut:` a module-level call (`initialize()` outside any function) is
classified as an import edge, because `GraphEdge` carries no kind. Upgrade by
adding an edge kind to `GraphEdge` if it proves noisy.

### (4) Step changes

**`review/diff_prompt.py`** (additive; existing functions untouched):

```python
def render_file_block(file: DiffFile) -> str: ...  # the per-file text render_diff joins
def file_block_size(file: DiffFile) -> int: ...  # len(render_file_block(file).encode())
def diff_subset(diff: Diff, paths: Collection[str]) -> Diff: ...  # files kept in original order
```
`render_diff` is rewritten to join `render_file_block` (same output).

**`review/review_generation.py`**

```python
@dataclass(frozen=True)
class DiffBatchingConfig:
    diff_fetcher: DiffFetcher            # ingestion.diff Protocol (existing)
    max_chunks: int                      # settings.max_diff_chunks
    concurrency: int                     # settings.diff_batch_concurrency
    link_timeout_seconds: float          # settings.diff_batch_link_timeout_seconds

class ReviewGenerationStep:
    def __init__(self, ..., batching: DiffBatchingConfig | None = None) -> None: ...
```

`ReviewGenerationOutput` gains two optional fields (default empty, so every
existing constructor and mirror still validates):

```python
review_batches: tuple[tuple[str, ...], ...] = ()  # empty when the diff fit or batching is off
unreviewed_files: tuple[str, ...] = ()
```

`run()` flow:

1. Render as today. If `batching is None` or the rendered diff
   (`len(render_diff(diff).encode())`) is at most `max_diff_bytes`: today's code
   path, untouched (US-B1).
2. Else: `sizes = {f.path: file_block_size(f)}`. Under
   `asyncio.timeout(link_timeout_seconds)`: `fetch_content` for non-removed
   files whose `language_for_path` is not `None`, then
   `await asyncio.to_thread(build_file_links, contents)`. `TimeoutError`,
   `DiffContentUnavailableError` and `DiffContentMalformedError` give
   `links = {}` and a warning (directory grouping for all). Then
   `plan = plan_batches(paths, sizes, links, max_bytes=self._max_diff_bytes,
   max_chunks=batching.max_chunks)`. Log `plan.dropped` at `warning` (count and
   paths, never content).
3. For each batch: `diff_subset(parsed.diff, batch)` rendered through
   `render_and_truncate_diff` (so a lone oversize file is truncated exactly as
   today); static findings filtered to files in the batch plus findings whose
   file is in no batch; the same system prompt, intent summary and context
   bundle. Run `_review_one_batch` (build request, `complete`, record usage,
   `_parse_llm_findings`) under `asyncio.Semaphore(batching.concurrency)` with
   `asyncio.gather(..., return_exceptions=True)`. With exactly one batch, or
   when every batch raised, re-raise the first exception unchanged. Otherwise a
   failed batch's files join `unreviewed_files` (with the dropped paths) and the
   failure is logged.
4. Merge: `findings.merge.merge_findings(all_llm_findings,
   tiered=context.review_quality_v2).findings`, then the existing severity sort,
   `apply_redaction_ceiling` and `_redact_all` once over the merged list. Static
   findings are forwarded unchanged.
5. Return `ReviewGenerationOutput(..., review_batches=plan.batches,
   unreviewed_files=...)`.

**`review/spec_review.py`**: `SpecReviewOutput` and its input mirror gain the same
two fields and forward them (like `context_bundle`).

**`review/verification.py`**

```python
class VerificationStep:
    def __init__(self, ..., batch_concurrency: int = 1) -> None: ...
```

`_VerificationInput` gains `review_batches` and `unreviewed_files`;
`VerificationOutput` forwards `unreviewed_files` (and `review_batches` is not
forwarded). `run()`:

- `review_batches` empty: today's path, one judge call over the whole diff.
- Else: after memory suppression, group `llm_findings` by the batch containing
  `finding.file` (findings in no batch form one extra group). For each group the
  `JudgeInput` carries `diff_subset(parsed.diff, files-with-findings-in-group)`.
  Run `judge.judge(group_findings, group_job)` per group under
  `asyncio.Semaphore(batch_concurrency)` with `asyncio.gather` (a judge
  exception stays fatal, unchanged), then reassemble judgements into
  `llm_findings` order, so the existing counting and `zip(strict=True)` loop is
  untouched. `select_judge` is still called once per job. Group count is at most
  `max_diff_chunks + 1` by construction.

**`review/output.py` and `adapter/github/posting.py`** (surfacing):
`FindingsOutputInput.unreviewed_files: tuple[str, ...] = ()`;
`FindingsPublisher.post_review(..., unreviewed_files: tuple[str, ...] = ())`,
passed as a kwarg only when non-empty (the v1 precedent in `output.py`), and
`post_review` is also called when there are no findings but `unreviewed_files`
is non-empty. `_review_body` appends one section
`Not reviewed (diff too large for one review, or a review call failed):` with
escaped paths, capped at 50 plus "and N more", inside the existing body budget
and hard limit. Dry-run logs include the list. The check-run finding count is
unchanged.

**`apps/worker/settings.py`** (new `WorkerSettings` fields):

```python
diff_batching_enabled: bool = True
max_diff_chunks: int = 8  # ge=1
diff_batch_concurrency: int = 4  # ge=1
diff_batch_link_timeout_seconds: int = 15  # ge=1
verification_step_timeout_seconds: int = 120  # was 60
```

Validator (same pure cross-field shape as the existing ones), with
`waves(n) = ceil(n / diff_batch_concurrency)`:
`review_generation_step_timeout_seconds >= diff_batch_link_timeout_seconds +
waves(max_diff_chunks) * llm_timeout_seconds` and
`verification_step_timeout_seconds >= waves(max_diff_chunks + 1) *
llm_timeout_seconds`. Defaults check: 15 + 2*30 = 75 <= 90; 3*30 = 90 <= 120.
The existing stuck-job sum goes 575 to 635 s, still under 660.

**`apps/worker/main.py` `_build_review_registry`**: when
`settings.diff_batching_enabled`, pass `batching=DiffBatchingConfig(diff_fetcher,
settings.max_diff_chunks, settings.diff_batch_concurrency,
float(settings.diff_batch_link_timeout_seconds))` to `ReviewGenerationStep` and
`batch_concurrency=settings.diff_batch_concurrency` to `VerificationStep`;
otherwise neither (today's construction).

## Non-Functional Requirements

### Performance, Security, Reliability, Scale

- **Cost (decided):** at most `max_diff_chunks` generation calls per review
  (default 8, so at most 8x today's generation spend, only on PRs that today
  are cut), at most `max_diff_chunks + 1` verification calls, zero extra calls
  for a diff that fits. The cap is the ceiling; the posted list of unreviewed
  files is how an author recovers (split the PR).
- **Timeouts:** each call keeps its own `llm_timeout_seconds`; the semaphore
  makes the worst case `waves * llm_timeout`, and the settings validator
  enforces it fits the step timeout. No new unbounded wait: link building has
  its own timeout.
- **Memory/CPU:** only changed files are fetched and parsed, only those with a
  supported extension, and parsing runs off the event loop. `prefilter`'s
  existing file-count and byte ceilings already bound the set.
- **Security:** file content is parsed, never executed or logged; only paths
  and counts appear in logs and metrics. Dropped/unreviewed paths are
  PR-author-controlled text: escape them in the posted body (existing
  `escape_markdown`). A fork PR cannot raise cost past `max_diff_chunks`.
- **Redaction unchanged:** merged findings are redacted once after the
  existing ceiling; a redaction failure remains fatal.

### Observability

- Log (`info`) one line per batched run: `batch_count`, `dropped_count`,
  `link_edge_count`, `unparsed_count`, `link_build_ms`, `fallback`
  (`none`/`fetch`/`timeout`). Log (`warning`) the dropped and failed-batch
  paths.
- Counter `review.diff_batching.runs_total{outcome}` where outcome is the
  closed set `fit | batched | capped | link_fallback` (cardinality-bounded per
  `spec-observability.md`); histogram `review.diff_batching.batches` (batch
  count per run). No paths in labels.

### Rollout & Release Safety

- `diff_batching_enabled=False` restores today's behaviour; flag-on with a diff
  that fits is byte-identical (US-B1). Both are covered by tests.
- Python indexing is not behind the flag. Rolling it back means reverting the
  `.py` mapping (assumption 4).

## Phases / Units

- **Unit A: Python in `indexing`.** Depends on: nothing. Risk: low-medium
  (starts indexing `.py` repo-wide). **Acceptance:** US-B10; unit tests in
  `indexing/test_chunk.py` and `indexing/test_graph.py`.
- **Unit B: `review/diff_batching.py` (pure).** Depends on: nothing. Risk:
  medium (heuristic). **Acceptance:** exhaustive unit tests in
  `review/test_diff_batching.py` (fit, components, hub, split, directory
  fallback, oversize file, pairing, cap, determinism); US-B3/B4/B5 via the
  integration file below.
- **Unit C: `review/diff_links.py` and `diff_prompt` helpers.** Depends on:
  A, B. Risk: medium. **Acceptance:** `review/test_diff_links.py` (generic
  names, definer cap, per-file degrade), US-B7.
- **Unit D: step, settings and wiring.** Depends on: B, C. Risk:
  medium-high (concurrency, timeouts, output surfacing across four modules).
  **Acceptance:** US-B1, B2, B6, B8, B9 integration tests, plus
  `apps/worker/test_settings.py` validator cases and a `test_main.py` wiring
  case for flag on and off.

## Testing / Key Scenarios

Integration tests use a recorded-response fake `LLMClient` and a fake
`DiffFetcher` (fakes at Protocol boundaries, real tree-sitter parsing, per
`testing.md`); the GitHub adapter case uses `respx`.

| Story | Scenario (G/W/T summary) | Level | Test path (proposed) |
| --- | --- | --- | --- |
| US-B1 | Fits: prompt byte-identical, one call, no `fetch_content`; flag off cuts as today | Integration (+ unit on prompt builder) | `review/test_diff_batching_steps.py` (`@integration`) |
| US-B2 | 450 KB / 30 files: >1 call, each diff <= ceiling, late-file defect surfaces, cross-batch duplicate merged | Integration | `review/test_diff_batching_steps.py` |
| US-B3 | Importer and definer in one batch; hub does not bridge; import beats call | Integration | `review/test_diff_batching_steps.py` |
| US-B3 | Pure component/hub/weight cases | Unit | `review/test_diff_batching.py` |
| US-B4 | `test_foo.py` with `foo.py` (and ts/go variants) in one batch | Integration | `review/test_diff_batching_steps.py` |
| US-B4 | `paired_source` cases (same dir, unique, ambiguous) | Unit | `review/test_diff_batching.py` |
| US-B5 | Oversize component split on weakest edge; lone oversize file own batch, truncated | Integration | `review/test_diff_batching_steps.py` |
| US-B5 | Split, directory fallback, first-fit packing | Unit | `review/test_diff_batching.py` |
| US-B6 | 11 batches, cap 8: 8 calls, warning names dropped, body lists them; clean + dropped still posts | Integration | `review/test_diff_batching_steps.py` (through `FindingsOutputStep`), `adapter/github/test_posting.py` (`respx`: body section, 50-path cap, escaping) |
| US-B7 | Bad UTF-8 / unsupported file groups by directory; fetch error and link timeout degrade all; review completes | Integration | `review/test_diff_batching_steps.py` |
| US-B7 | `build_file_links` per-file isolation, generic-name and definer-cap filtering | Unit | `review/test_diff_links.py` |
| US-B8 | Late-file finding judged against its file's diff only; empty `review_batches` byte-identical; no-batch finding in extra group | Integration | `review/test_diff_batching_steps.py` (verification half), `review/test_verification_step.py` |
| US-B9 | Max 4 in flight; one failed batch keeps others and lists its files; all failed or one batch re-raises | Integration | `review/test_diff_batching_steps.py` |
| US-B9 | Settings validator rejects a timeout that cannot fit the waves | Unit | `apps/worker/test_settings.py` |
| US-B10 | `.py` file chunked at `def`/`class`, graph edges to `repo` and `save` | Integration | `indexing/test_pipeline.py` (real Postgres), unit: `indexing/test_chunk.py`, `indexing/test_graph.py` |
| All | Compose stack: oversize PR posts a review covering a late file; capped PR lists unreviewed files | E2E | `tests/e2e/test_diff_batching.py` |

Failure-mode E2E addition (per `testing.md`): one batch's LLM call times out
and the review still posts with that batch's files listed as not reviewed.

## Open items (assumptions pending validation)

1. Edge weights (3.0 / 1.0), hub threshold (>= max(8, 25% of files)),
   `MAX_DEFINING_FILES=3`, minimum name length 3 and the generic-name list are
   starting values, not tuned on real PRs.
2. `max_diff_chunks=8`, `diff_batch_concurrency=4`,
   `diff_batch_link_timeout_seconds=15`; verification step timeout raised from
   60 to 120 seconds.
3. Under the cap, the diff-order tail is dropped. Ranking by risk (source before
   docs, tests last) is a likely follow-up.
4. Indexing `.py` for every customer repo is acceptable (it adds embedding and
   storage work for Python repos); if not, gate `.py` behind a setting or give
   the batching a private language map.
5. Module-level calls count as import edges (`GraphEdge` has no kind).
6. The semaphore is per job. If one worker ever runs several review jobs at
   once, move it to the composition root, as the classifier semaphores are.
7. `tree-sitter-python` must be added to `pyproject.toml`, the lock file and the
   worker image; it is not in the venv today.
