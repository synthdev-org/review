# Deliberately corrupted local embedding model fixture

Used by `adapter/embedding/test_local_onnx.py` and
`adapter/embedding/test_startup_check.py` to exercise the real
"`LocalONNXEmbeddingClient`/`fastembed`/ONNX runtime call genuinely fails"
path: a real failure through the real code path, never a mock, per
`docs/rules/testing.md`'s
"fakes over mocks for adapters" convention.

Every file here except `model.onnx` is a byte-identical copy of the real,
checksum-verified `Qdrant/all-MiniLM-L6-v2-onnx` files this repo's
Dockerfile bakes in (see `adapter/embedding/model.sha256`) — so a test
constructed against this directory fails specifically because the *model
weights* are unusable, not because a tokenizer/config file is missing or
malformed. `model.onnx` itself is the first 4096 bytes of that same real
file — a genuinely truncated, invalid-protobuf ONNX artifact (verified to
raise `onnxruntime.InvalidProtobuf` when loaded), not a synthetic/empty
placeholder.
