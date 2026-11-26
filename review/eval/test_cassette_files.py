"""Unit tests for the cassette file helpers and the recorder's small pure
functions (no network: the respx replay path is covered by
``test_eval_cassettes.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from review.eval.cassette import Cassette, Interaction, canonical, load_cassette, save_cassette
from review.eval.metrics import Metrics
from review.eval.record import _completion, baseline_dict, cassette_path, main


def test_canonical_is_key_order_and_whitespace_independent() -> None:
    assert canonical({"b": [1, 2], "a": {"y": 1, "x": 2}}) == '{"a":{"x":2,"y":1},"b":[1,2]}'
    assert canonical({"a": 1, "b": 2}) == canonical({"b": 2, "a": 1})


def test_cassette_round_trips_through_a_file_and_is_written_deterministically(
    tmp_path: Path,
) -> None:
    cassette = Cassette(
        synthetic=True,
        model="m",
        note="n",
        rerecord="cmd",
        interactions=[Interaction(request={"z": 1, "a": 2}, status=200, response={"ok": True})],
    )
    path = tmp_path / "nested" / "v1.json"

    save_cassette(path, cassette)

    assert load_cassette(path) == cassette
    text = path.read_text()
    assert text.endswith("}\n") and text.index('"a"') < text.index('"z"')  # sorted keys


def test_cassette_path_is_per_version() -> None:
    assert cassette_path("v2").name == "v2.json"
    assert cassette_path("v1").parent == cassette_path("v2").parent


def test_completion_wraps_content_as_an_openai_chat_completion() -> None:
    body = _completion({"findings": []}, "the-model")

    assert body["model"] == "the-model"
    message = body["choices"][0]["message"]
    assert message["role"] == "assistant" and json.loads(message["content"]) == {"findings": []}


def test_baseline_dict_carries_version_synthetic_flag_and_metrics() -> None:
    metrics = Metrics(
        precision=0.5,
        recall=1.0,
        findings_per_pr=2.0,
        keep_rate_by_category={"style": 0.25},
        kept_ids=["a"],
    )

    baseline = baseline_dict(metrics, version="v1", synthetic=True)

    assert baseline == {"version": "v1", "synthetic": True, "metrics": metrics.model_dump()}


def test_real_recording_requires_an_endpoint_model_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVAL_LLM_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main(["--endpoint", "http://llm", "--model", "m"])

    assert exit_info.value.code == 2
