"""A stand-in model for trying `review` without a real one (stdlib only, Python 3.8+).

    python3 stub_model.py [PORT]        # default 8099; PORT 0 picks a free one; prints the base URL

Serves POST /v1/chat/completions. A review makes three kinds of call and each needs a different
reply, which this tells apart by the system prompt:

* intent summary  -> plain text
* verification    -> {"verdicts": [{index, valid, actionable, specific, valuable}]}
* anything else   -> {"findings": [{file, line, severity, message}]}  (one canned finding)
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

FINDING = {"file": "app.py", "line": 2, "severity": "high", "message": "canned finding"}


def reply_for(system: str, user: str) -> str:
    if "summary (2-4 sentences)" in system:
        return "Stub summary of the change."
    if "verification assistant" in system:
        # Confirm every finding; the user text is a header line, then a JSON array of candidates.
        indexes = [c["index"] for c in json.loads(user.split("\n", 2)[1])]
        return json.dumps(
            {
                "verdicts": [
                    {
                        "index": i,
                        "valid": True,
                        "actionable": True,
                        "specific": True,
                        "valuable": True,
                    }
                    for i in indexes
                ]
            }
        )
    return json.dumps({"findings": [FINDING]})


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
        payload = json.dumps(
            {
                "id": "stub",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "stub"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply_for(system, user)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8099), Handler)
    print(f"http://127.0.0.1:{server.server_address[1]}/v1", flush=True)
    server.serve_forever()
