#!/usr/bin/env python3
"""Send exactly one request through the real transport, and write down what came back.

WHY THIS EXISTS. Every test in this repository drives a deterministic fake
through an injected transport. That is the right default -- `make test` passes
with no key and no network -- but it left AnthropicTransport as documented design
that had never run. The module docstrings said so honestly and the honesty did
not make the code work: a parameter name invented from memory, a response field
that is not where the reader thought, an endpoint that rejects the combination
outright, all pass a fake and all fail once.

This script is the smallest thing that answers the question. It builds the REAL
transport with the REAL client on the model the application pins, sends one tiny
request, and prints the model id the endpoint reported, the token counts, the
exact request the transport built and the exact text it returned.

WHAT IT DELIBERATELY DOES NOT DO. It does not loop, batch, sweep parameters or
retry beyond the single retry the client is bounded to. One call costs real
money on somebody's account, so the default invocation SENDS NOTHING: it prints
the request it would make and stops. Passing --send is the deliberate act.

THE TRANSPORT IS NOT MODIFIED TO BE OBSERVED. AnthropicTransport.complete()
returns a string, and a transcript needs the model id, the stop reason and the
usage block as well. Rather than widen the return type of production code for a
script, the real SDK client is wrapped in a recorder that delegates every call
and keeps the response object. What runs is the shipped class, building the
shipped request, against the shipped model id.

WHICH TRANSPORT. app/interpretation/propose.py's, because it is the one the
citation gate sits on: no tools, structured output constrained to the
judgement schema, adaptive thinking. app/chat/agent.py declares a second
AnthropicTransport with a different parameter set (tools, effort inside
output_config) and this script does NOT exercise it. One call, one claim.

Run:  .venv/bin/python scripts/probe_live_transport.py           # prints, sends nothing
      .venv/bin/python scripts/probe_live_transport.py --send    # spends money

--send IS THE ONLY SAFETY, AND `env -u ANTHROPIC_API_KEY` IS NOT A SECOND ONE.
load_env() puts any name from .env into the environment that is not already set,
which is exactly the case unsetting it creates -- so clearing the variable does
not disarm this script, it just hides from you that a key is present. Somebody
has already made that mistake and paid for a call they did not mean to make. To
run it with no key at all, move .env aside.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402
from app.interpretation.propose import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    AnthropicTransport,
    _client,
)

#: One retry at most, set explicitly rather than left at the SDK default of two.
#: The default is right for an application and wrong for an instrument whose
#: whole claim is "this ran once".
MAX_RETRIES = 1

#: The smallest prompt that can produce the shape the transport's schema
#: requires: a verdict, a sentence, and a citation with offsets into a named
#: version. Tiny on purpose -- this is a wire test, not an eval. The schema
#: itself is the application's, not this script's.
SYSTEM = (
    "You judge whether one change to a regulatory filing matters to the utility "
    "that has to live with it. Answer with one object: material, true or false; "
    "why, in one sentence; and a citation naming the version, the 0-based "
    "character offsets of the exact span, and the text at that span copied "
    "character for character."
)

USER = """\
VERSION v-probe-1, whole text:
Utilities shall file the annual reliability report by March 1.

CHANGE: the deadline moved from April 1 to March 1.\
"""


class _Recording:
    """Delegates to the real client and keeps what came back.

    Exposes just enough of the SDK's surface for the transport to use it --
    ``client.messages.create(...)`` -- which is why ``messages`` returns self.
    Nothing is altered on the way through in either direction.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.request: dict[str, Any] | None = None
        self.response: Any = None

    @property
    def messages(self) -> "_Recording":
        return self

    def create(self, **kwargs: Any) -> Any:
        self.request = kwargs
        self.response = self._inner.messages.create(**kwargs)
        return self.response


class _NoSend(Exception):
    """Raised by the refusing client once the request has been recorded."""


class _Refusing:
    """A client that records a request by refusing to make it.

    ``_Recording`` stores the keyword arguments BEFORE it delegates, so a client
    that raises still leaves the exact request behind. The alternative -- return
    a stub response -- would put a fabricated response object into a script whose
    only job is to report what really happened, and the first person to read the
    output would not know which half was real.
    """

    @property
    def messages(self) -> "_Refusing":
        return self

    def create(self, **kwargs: Any) -> Any:
        raise _NoSend


def request_the_transport_would_send() -> dict[str, Any]:
    """The exact keyword arguments ``complete()`` hands to ``messages.create``.

    Built by driving the SHIPPED transport against the refusing client above, so
    what comes back is the request the product makes rather than this script's
    idea of it. Nothing is sent, no key is needed and nothing is charged, which
    is what lets the request block in docs/.ai/live-transport-probe.html be
    regenerated on any machine -- and held to this code by a test -- rather than
    being a paragraph a reader has to take on trust.
    """
    client = _Recording(_Refusing())
    try:
        AnthropicTransport(client).complete(system=SYSTEM, user=USER)
    except _NoSend:
        pass
    if client.request is None:
        raise RuntimeError(
            "the transport returned without calling messages.create, so there "
            "is no request to report. It is not the request this script prints "
            "on a good day; there is nothing to print."
        )
    return client.request


def _usage(response: Any) -> dict[str, Any]:
    """Token counts, named one by one rather than dumped.

    A missing field is reported as missing. A usage block this script could not
    read is not a usage block of zeros.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"problem": "the response carried no usage block"}
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    return {name: getattr(usage, name, "not reported") for name in fields}


def main(argv: list[str]) -> int:
    load_env()

    import os

    send = "--send" in argv
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if send and not key:
        # Absence is denial. A probe asked to call with no key does not report a
        # healthy nothing; it says which name is unset and stops.
        print(
            "REFUSED: ANTHROPIC_API_KEY is not set in the environment or .env, "
            "so no call was made and nothing is proven.",
            file=sys.stderr,
        )
        return 2
    # The key itself is never printed. Its length is enough to tell an empty
    # string from a real one without putting a secret in a terminal or a log.
    # The dry run needs no key and says so rather than implying one it has not
    # got -- it is the path a reviewer runs to regenerate the recorded request,
    # and refusing there would put a key between them and the check.
    print(f"key present: {f'yes (length {len(key)})' if key else 'no'}")

    print(f"model: {MODEL_ID}")
    print(f"max_tokens: {MAX_OUTPUT_TOKENS}")
    print(f"max_retries: {MAX_RETRIES}")
    print("\n--- system ---")
    print(SYSTEM)
    print("\n--- user ---")
    print(USER)

    if not send:
        print("\n--- request the transport would build ---")
        print(json.dumps(request_the_transport_would_send(), indent=2, default=str))
        print(
            "\nNOTHING SENT. This call costs real money. Re-run with --send to "
            "make it."
        )
        return 0

    client = _Recording(_client(key).with_options(max_retries=MAX_RETRIES))
    transport = AnthropicTransport(client)

    try:
        text = transport.complete(system=SYSTEM, user=USER)
    except Exception as error:  # noqa: BLE001 -- the failure IS the result
        print(f"\nCALL FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    response = client.response
    print("\n--- request the transport built ---")
    print(json.dumps(client.request, indent=2, default=str))
    print("\n--- response ---")
    print(f"model reported by the endpoint: {getattr(response, 'model', 'absent')}")
    print(f"stop_reason: {getattr(response, 'stop_reason', 'absent')}")
    print(f"id: {getattr(response, 'id', 'absent')}")
    print(f"usage: {json.dumps(_usage(response))}")
    print(f"content block types: {[getattr(b, 'type', '?') for b in response.content]}")
    print("\n--- text returned by AnthropicTransport.complete() ---")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
