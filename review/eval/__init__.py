"""review.eval — offline evaluation harness for review quality.

Corpus (``review/testdata/eval/corpus.jsonl``), metrics, a respx cassette
helper for the real ``LiteLLMClient`` wire path, and ``python -m
review.eval.record``. Dev/CI tooling only: nothing in the runtime pipeline
imports it, so ``respx`` (a dev dependency) is imported here by design.
Spec: docs/specs/spec-review-quality.md.
"""
