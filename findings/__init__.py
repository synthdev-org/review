"""findings — domain model and merge/dedupe logic for review findings.

Also home to the pure text-transformation logic findings-adjacent surfaces
need (``findings/redact.py``'s ``TextRedactor`` Protocol, per
docs/adr/0006-protocol-defined-at-data-model.md; ``findings/markdown.py``'s
Markdown-escaping helper for the GitHub posting surface) — this package's
own scope grew from "independent of how findings are posted or stored" to
include the posting-adjacent pure logic those findings need before they
reach that surface, once a real posting client existed to need it."""
