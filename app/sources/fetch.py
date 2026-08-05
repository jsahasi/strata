"""Retrieve one public filing, hash it, and store it only if it changed.

THIS IS THE FIRST CODE IN THIS PRODUCT THAT OPENS A SOCKET AT ALL, AND AN
ADMINISTRATOR CHOOSES THE ADDRESS. That sentence is the whole threat model.
Server-side request forgery is not a subtlety here: this process runs on a
droplet that shares a kernel with another project, it can reach a cloud
metadata service at 169.254.169.254, it can reach every neighbour on the
private network, and it can reach itself on the loopback interface. A registry
screen that lets somebody type a URL and then fetches it has handed all of that
to whoever can reach the screen. So the guard below is not a validation step in
front of the interesting code. It IS the interesting code.

WHAT THE GUARD DOES, IN ORDER, AND WHY EACH PART EXISTS.

1.  THE SCHEME. http and https, nothing else. file:// reads the disk,
    gopher:// and friends will happily be spoken by some other service on a
    port that was never meant to hear them, and data:/javascript: are not
    addresses at all. Refused before a name is resolved, so a bad scheme costs
    no lookup of an attacker's choosing.

2.  NO CREDENTIALS IN THE URL. A password in an address travels into logs, into
    proxies, and onto this screen. The refusal never repeats what it refused.

3.  THE ADDRESS, NOT THE NAME. The host is resolved here and every address it
    answers with is checked -- loopback, private, link-local, reserved,
    multicast, unspecified, and anything the ipaddress module will not call
    global. ANY bad address refuses the whole name, not just the one that would
    have been used: a name with one public A record and one private A record is
    the cheap version of DNS rebinding, and a guard that checked only the
    address it picked would let it through on the second attempt.

    Four literals a guard written from memory misses, and all four are in the
    tests: ::ffff:127.0.0.1 and ::ffff:169.254.169.254 (loopback and the
    metadata service wearing an IPv6 hat), 2002:7f00:1::1 (the same thing
    wrapped as 6to4) and 64:ff9b::7f00:1 (NAT64 carrying 127.0.0.1 in its low
    bits).

    WHAT THE UNWRAPPING IN address_problem() IS ACTUALLY WORTH, STATED HONESTLY
    RATHER THAN IMPLIED. Python 3.12's ipaddress module already reports
    is_private for all four, so on this interpreter the unwrapping is not what
    refuses them -- deleting it turns no test red except the one that pins the
    REASON for 2002:7f00:1::1, which goes from "a private range" to "the
    loopback interface". It is kept for that specificity and because
    is_private's treatment of embedded IPv4 has changed between releases and
    this product must not depend on which one it is installed under.

4.  THE ADDRESS IS CARRIED FORWARD AND CONNECTED TO. The transport is given the
    address the guard approved and connects to THAT, with the hostname kept for
    TLS and for the Host header. A fetcher that resolved a name, approved it,
    and then let the HTTP client resolve the name again has checked one address
    and connected to another, and the window between the two lookups is exactly
    what DNS rebinding aims at. There is no window here, because there is one
    lookup.

5.  EVERY HOP. A public host that answers 302 Location: http://169.254.169.254/
    defeats any check that only reads the URL an administrator typed. So the
    transport does ONE hop and never follows a redirect itself; this module
    reads the Location, resolves it against the hop it came from, and runs the
    whole guard again from the top. Five hops, then it stops.

6.  A CEILING AND A DEADLINE. A 40 GB response and a server that dribbles one
    byte a minute are both denial of service against this host, and a socket
    timeout catches only the second kind badly -- a slow loris keeps every
    individual read inside the timeout. So there is a byte ceiling AND a wall
    clock deadline across all hops. A body over the ceiling is REFUSED, never
    truncated: a short read hashes differently, and a different hash reads as a
    changed filing, so truncating would invent a diff.

    THE DEADLINE IS ENFORCED IN TWO PLACES BECAUSE ONE WOULD NOT COVER IT.
    HttpTransport watches the clock while it reads, which answers the dribbling
    server. _walk() checks it before opening each hop, which answers the other
    half: a slow CONNECT is time nothing has read yet, and MAX_REDIRECTS limits
    how many hops a hostile server gets without limiting how long each one
    takes. Without the second check a redirect chain costs up to
    HOP_TIMEOUT_SECONDS * (MAX_REDIRECTS + 1) whatever the deadline says. The
    walk-level check is also the only one that holds for an injected transport,
    which need watch no clock at all.

WHAT A FETCH DOES, ONCE ALL THAT PASSES. Retrieve the bytes, decode them as
UTF-8 or refuse, hash the text, and compare that hash with what is stored.
Unchanged means say so and write no version. Changed means hand the text to
app/pipeline.py::ingest_and_diff, which already ingests, hashes, and diffs
against the predecessor -- this module does not reimplement any of it and must
not. Either way the attempt is recorded on the registration, so the registry
can print last_scanned_at and last_result truthfully rather than leaving a
blank cell that reads as a quiet week.

THE VERSION ID IS DERIVED FROM THE BYTES. SRC-xxxx-<first 16 of the digest>.
The pipeline is idempotent on version id, so fetching the same changed document
twice writes one version and one set of changes. A generated id would mint a
second copy of the same filing on every retry.

OFFLINE BY DEFAULT, AND MEASURED. Two dependencies are injected, because both
of them reach out: the transport, and the resolver. A guard that called the
real getaddrinfo would need DNS to run its own tests. tests/test_fetch.py hands
in a resolver that answers from a dict and a transport that answers from a
dict, exactly as tests/test_propose.py fakes the model.

WITH NO TRANSPORT THE PATH IS OFF AND SAYS SO. transport_from_environment()
returns a transport only when STRATA_FETCH_ENABLED is set, and fetch_source()
refuses with ANNOUNCEMENT_NO_TRANSPORT rather than quietly doing nothing.
best-practices.html section 26: a fallback announces itself.

WHAT IS NOT PROVEN, STATED PLAINLY. HttpTransport has never spoken to a state
commission from this repository. What the suite proves is that it speaks HTTP,
returns the bytes, stops at the ceiling, refuses a host that is not listening,
and does not follow a redirect -- against a server on 127.0.0.1, which it can
only be pointed at by calling it underneath the guard, because the guard
refuses loopback. Everything a real endpoint could add -- a TLS chain this
process does not trust, a server that answers HTTP/2 only, a commission behind
a bot wall, chunked encoding oddities -- is unexercised. Treat the transport as
unexercised code and the guard above it as tested code, because that is what
they are.

WHAT THIS GUARD DOES NOT DO, so nobody has to find out by reading the code.

* IT DOES NOT RESTRICT THE PORT. https://some.public.host:25/ is permitted, and
  speaking HTTP at somebody else's SMTP port is a real trick. It is a much
  smaller one here, because the address has already been proved to be on the
  public internet, so the reach is a stranger's mail server and never this
  network. An allow-list of ports would also refuse the 8443s that real
  commissions publish on. Named rather than silently accepted.
* IT DOES NOT LOOK AT robots.txt, DOES NOT RATE LIMIT AND DOES NOT WAIT BETWEEN
  FETCHES. One fetch happens when a person asks for one. A scheduler in front
  of this would need all three before it pointed at a real commission, and
  USER_AGENT is set so that a commission that wants to refuse us can.
* IT IGNORES HTTP_PROXY AND HTTPS_PROXY. http.client does not read them, which
  is what this module wants: a proxy would resolve the name itself and connect
  wherever it liked, and the pinned address -- the whole point of the guard --
  would stop meaning anything. A deployment that must fetch through a proxy
  needs this rewritten, not configured.
* THE PEER CHECK AFTER connect() IS BELT AND BRACES, NOT THE DEFENCE. The
  defence is that the socket is opened to the address that was checked. The
  read-back exists because it costs one line and the failure it would catch is
  somebody reading this host's cloud credentials.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Callable, Mapping, NamedTuple, Protocol
from urllib.parse import urljoin, urlsplit

from sqlalchemy.orm import Session

from app.auth import policy
from app.pipeline import ingest_and_diff
from app.sources import (
    CONFIG_DOCUMENT_STATUS,
    CONFIG_ENDPOINT,
    CONFIG_PROCEEDING,
    DOCUMENT_STATUSES,
    FETCHER_KINDS,
    fetch_problem,
)
from app.state import sources
from app.state.claims import proceedings_for_company
from app.state.models import (
    SOURCE_REGISTRY_PERMISSION,
    SOURCE_STATUS_CONNECTED,
    SOURCE_STATUS_UNREACHABLE,
    DocumentVersion,
)
from app.state.queries import _require_scope, row_for_company

# ---------------------------------------------------------------------------
# The limits. Named, because every one of them is a decision somebody will want
# to argue with, and a literal buried in a loop cannot be argued with.
# ---------------------------------------------------------------------------

#: The most a single filing may weigh. The largest document in data/real is
#: about a megabyte, so this is a wide margin and still nowhere near enough
#: memory to hurt the host. A body over it is refused, not truncated.
MAX_BODY_BYTES = 25_000_000

#: Per-socket timeout: connect, and each read. Not sufficient on its own -- see
#: TOTAL_DEADLINE_SECONDS.
HOP_TIMEOUT_SECONDS = 20.0

#: Wall clock across the whole fetch, every hop included. This is the answer to
#: a slow loris, which keeps each individual read inside HOP_TIMEOUT_SECONDS
#: and would otherwise hold this process open for as long as it likes.
TOTAL_DEADLINE_SECONDS = 90.0

#: Redirects followed before giving up. Each one is re-checked from the top.
MAX_REDIRECTS = 5

#: What this product calls itself when it knocks. A commission that wants to
#: block us must be able to, and an anonymous agent string is how a small
#: product ends up looking like a scraper.
USER_AGENT = "strata-source-fetcher/1.0 (regulatory change monitoring)"

#: The statuses that mean "look somewhere else". 303 is included and behaves
#: like the rest here because every request this module makes is a GET.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

#: The environment switch. Absent means the product opens no sockets, which is
#: the state a reviewer's checkout should start in.
FETCH_ENABLED_ENV = "STRATA_FETCH_ENABLED"
_TRUE_WORDS = ("1", "true", "yes", "on")

#: Named states, in the shape app/interpretation/propose.py uses for the model
#: path. Each is a thing the product must be able to say out loud.
FALLBACK_NO_TRANSPORT = "FETCH_PATH_OFF_NO_TRANSPORT"
ANNOUNCEMENT_NO_TRANSPORT = (
    "The fetch path is off: no transport was supplied, so nothing was "
    f"retrieved and nothing was recorded. Set {FETCH_ENABLED_ENV} to switch it "
    "on. Nothing on screen came from a fetch."
)


class FetchFailed(RuntimeError):
    """The transport could not complete one hop. Carries words, not a code.

    Every reason a socket gives up -- refused, reset, timed out, a certificate
    that does not verify -- arrives here as a sentence, because that sentence
    goes into last_result and a person reads it. There is nothing to branch on.
    """


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Names that resolve inside a network rather than on the public one. Checked
#: as well as the resolved address, not instead of it: a resolver under an
#: attacker's control answers whatever it likes for any name, and a resolver
#: this host trusts still answers 127.0.0.1 for "localhost".
PRIVATE_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")


class Target(NamedTuple):
    """One address the guard approved, in the form the transport needs.

    `address` is the IP literal to connect to. `host` is the name to present in
    the Host header and to TLS. They are two fields on purpose: connecting to
    the name would resolve it a second time, and one lookup is the difference
    between a checked address and a checked guess.
    """

    url: str
    scheme: str
    host: str
    port: int
    address: str
    path: str


class Verdict(NamedTuple):
    """Whether this address may be fetched, why not, and what to connect to."""

    allowed: bool
    reason: str
    target: Target | None


def system_resolve(host: str) -> tuple[str, ...]:
    """Every address this host resolves to, as literals. The only DNS call here.

    Both families, because a name with a harmless A record and a loopback AAAA
    record is checked on one and reached on the other. Deduplicated so a name
    with four identical answers is not reported four times.
    """
    found: list[str] = []
    for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
        host, None, proto=socket.IPPROTO_TCP
    ):
        literal = sockaddr[0]
        if literal not in found:
            found.append(literal)
    return tuple(found)


def address_problem(literal: str) -> str:
    """Why this address must not be reached, or "" when it may be.

    THE UNWRAPPING. An IPv4 address travelling inside an IPv6 one -- IPv4-mapped
    (::ffff:127.0.0.1) or 6to4 (2002:7f00:1::1) -- reaches the same interface as
    the IPv4 address it carries, so it is unwrapped and the inner address is
    what gets checked. On Python 3.12 this is belt and braces rather than the
    thing that saves us: is_private already answers True for both forms. What it
    buys is a reason that names the loopback interface instead of "a private
    range", and independence from a property whose treatment of embedded IPv4
    has moved between releases. The module docstring says the same thing at
    greater length, including which test would catch its removal.

    THE ORDER OF THE CHECKS IS THE ORDER OF THE SENTENCES, not a hierarchy.
    is_reserved is asked before is_global because 64:ff9b::/96 -- NAT64, which
    can carry any IPv4 address including 127.0.0.1 -- is reported as reserved
    AND as global by the same module, and only the first of those two answers
    is any use.
    """
    try:
        address = ip_address(literal)
    except ValueError:
        return (
            f"{literal!r} is not an address this product can check, and an "
            "address it cannot check is one it will not open."
        )

    # An IPv4 address travelling inside an IPv6 one is still an IPv4 address.
    for attribute in ("ipv4_mapped", "sixtofour"):
        inner = getattr(address, attribute, None)
        if inner is not None:
            address = inner

    if address.is_unspecified:
        return "the unspecified address, which reaches every interface on this host"
    if address.is_loopback:
        return "the loopback interface, which is this process talking to itself"
    if address.is_link_local:
        return (
            "the link-local range, which is where a cloud metadata service "
            "hands out this host's own credentials"
        )
    if address.is_private:
        return "a private range, which is this host's own network"
    if address.is_reserved:
        return "a reserved range, which is not a place a commission publishes from"
    if address.is_multicast:
        return "a multicast address, which is not one endpoint but many"
    if not address.is_global:
        return "an address that is not on the public internet"
    return ""


def check_url(url: str, *, resolve: Callable[[str], tuple[str, ...]] | None = None) -> Verdict:
    """Read an address, resolve it, and say whether a fetch may proceed.

    Returns on the FIRST problem rather than collecting every one, which is the
    opposite of endpoint_preflight() in app/state/sources.py, and deliberately:
    that one talks to a person filling in a form and should list everything
    wrong at once; this one is a gate, and refusing a bad scheme before
    resolving anything means a rejected URL costs no lookup of a name somebody
    else chose.
    """
    lookup = resolve or system_resolve
    candidate = (url or "").strip()
    if not candidate:
        return Verdict(False, "There is no address here to fetch.", None)

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return Verdict(False, "That cannot be read as a URL, so it was not opened.", None)

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return Verdict(
            False,
            (
                f"Only {' and '.join(ALLOWED_SCHEMES)} are fetched, and that "
                f"address names {scheme or 'no scheme at all'}. Any other "
                "scheme either reads this host's own disk or speaks to a "
                "service that was never meant to hear an HTTP request."
            ),
            None,
        )

    if parts.username or parts.password:
        return Verdict(
            False,
            (
                "That address carries a username or a password in it, and a "
                "credential in a URL travels into logs, proxies and screens. "
                "What was submitted is not repeated here. Name an environment "
                "variable in the credential field instead."
            ),
            None,
        )

    try:
        host = (parts.hostname or "").strip().lower()
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError:
        return Verdict(False, "The host or port in that address cannot be read.", None)

    if not host:
        return Verdict(False, "That address names no host, so there is nothing to reach.", None)

    if host == "localhost" or host.endswith(PRIVATE_NAME_SUFFIXES):
        return Verdict(
            False,
            (
                f"{host} resolves inside this network rather than on the public "
                "one, so fetching it would be this server reading itself or its "
                "neighbours."
            ),
            None,
        )

    # A literal needs no lookup, and must not get one: resolving it would make
    # this guard depend on DNS to answer a question DNS was not asked.
    try:
        ip_address(host)
    except ValueError:
        literal_host = False
    else:
        literal_host = True

    if literal_host:
        addresses: tuple[str, ...] = (host,)
    else:
        try:
            addresses = tuple(lookup(host))
        except Exception as failure:  # noqa: BLE001 -- any resolver failure is a refusal
            return Verdict(
                False,
                (
                    f"{host} could not be resolved, so there is no address to "
                    f"check and nothing was opened. The lookup failed with: "
                    f"{failure.__class__.__name__}."
                ),
                None,
            )
        if not addresses:
            return Verdict(
                False,
                (
                    f"{host} resolves to no address at all. That is not "
                    "permission to try anyway: an address that cannot be "
                    "established cannot be checked."
                ),
                None,
            )

    # EVERY address, not the one that would have been used. See the module note.
    for literal in addresses:
        problem = address_problem(literal)
        if problem:
            return Verdict(
                False,
                (
                    f"{host} resolves to {literal}, which is {problem}. "
                    "Refusing every address a name answers with, rather than "
                    "only the one about to be used, is what stops a name with "
                    "one public answer and one private answer from reaching "
                    "this network on the second attempt."
                ),
                None,
            )

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    return Verdict(
        True,
        (
            f"{host} resolves only to public addresses, and {addresses[0]} is "
            "the one this fetch will connect to."
        ),
        Target(
            url=candidate,
            scheme=scheme,
            host=host,
            port=port,
            address=str(ip_address(addresses[0])),
            path=path,
        ),
    )


# ---------------------------------------------------------------------------
# The transport: one hop, no redirects, injected
# ---------------------------------------------------------------------------


class HopRequest(NamedTuple):
    """One approved hop. Everything the socket needs and nothing it must decide.

    The transport does not parse the URL, does not resolve the host and does not
    choose whether to follow a redirect. All three decisions are the guard's,
    and a transport that made any of them would be a second path around it.
    """

    url: str
    scheme: str
    host: str
    port: int
    address: str
    path: str
    timeout: float
    max_bytes: int
    deadline: float | None


class Hop(NamedTuple):
    """What one hop answered.

    `oversize` rather than a truncated body silently returned: the caller has to
    be able to tell "here is the document" from "here is as much of it as we
    were willing to read", because hashing the second is how a fetcher reports
    a change that never happened.
    """

    status: int
    location: str | None
    body: bytes
    oversize: bool


class Transport(Protocol):
    """One HTTP hop: an approved request in, a Hop out, FetchFailed on trouble.

    Narrow on purpose, like the model transport in
    app/interpretation/propose.py. Everything above it -- the scheme check, the
    resolution, the address rules, the redirect walk, the ceiling, the hashing,
    the decision to store -- is pure and runs offline. Everything the network
    touches is on the far side of this one method, which is what makes the fake
    in tests/test_fetch.py a fair stand-in rather than a mock of something more
    interesting.
    """

    def open(self, request: HopRequest) -> Hop: ...


class HttpTransport:
    """The real one. UNEXERCISED against a commission: see the module docstring.

    IT CONNECTS TO AN ADDRESS AND SPEAKS TO A NAME. http.client resolves
    whatever host it is given, so handing it the hostname would be a second
    lookup and a second answer. Instead the connection is built for the
    hostname -- which is what TLS verifies and what the Host header carries --
    and its socket factory is replaced so the actual connect() goes to the
    address the guard approved. Then the peer is read back off the socket and
    checked again, because a belt costs one line here and the failure it catches
    is somebody reading this host's cloud credentials.

    REDIRECTS ARE NOT FOLLOWED. That is not an omission. Following one inside
    the transport would put the second address beyond the guard, which is the
    entire attack this module exists to stop.
    """

    def open(self, request: HopRequest) -> Hop:
        connection = self._connect(request)
        try:
            connection.request(
                "GET",
                request.path,
                headers={
                    "Host": self._host_header(request),
                    "User-Agent": USER_AGENT,
                    # identity, so the byte ceiling counts the document rather
                    # than a compressed stream that expands past it.
                    "Accept-Encoding": "identity",
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            answer = connection.getresponse()
            body, oversize = self._read(answer, request)
            return Hop(
                status=answer.status,
                location=answer.getheader("Location"),
                body=body,
                oversize=oversize,
            )
        except FetchFailed:
            raise
        except (OSError, http.client.HTTPException) as failure:
            raise FetchFailed(
                f"{request.host} stopped answering: "
                f"{failure.__class__.__name__}: {failure}"
            ) from failure
        finally:
            connection.close()

    def _connect(self, request: HopRequest):
        address = request.address
        if request.scheme == "https":
            connection = http.client.HTTPSConnection(
                request.host,
                request.port,
                timeout=request.timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                request.host, request.port, timeout=request.timeout
            )

        # The one line that makes the checked address the connected address.
        def to_the_checked_address(target, timeout, source_address=None):
            return socket.create_connection((address, target[1]), timeout, source_address)

        connection._create_connection = to_the_checked_address  # noqa: SLF001

        try:
            connection.connect()
        except (OSError, ssl.SSLError) as failure:
            connection.close()
            raise FetchFailed(
                f"{request.host} at {address} could not be reached: "
                f"{failure.__class__.__name__}: {failure}"
            ) from failure

        peer = self._peer(connection)
        if peer is not None and peer != request.address:
            connection.close()
            raise FetchFailed(
                f"the connection to {request.host} landed on {peer}, which is "
                f"not the {request.address} that was checked. Nothing was read."
            )
        return connection

    @staticmethod
    def _peer(connection) -> str | None:
        try:
            return str(ip_address(connection.sock.getpeername()[0]))
        except (AttributeError, OSError, ValueError, IndexError):
            # Nothing to compare against is not a reason to refuse: the guard
            # already checked the address this socket was told to use.
            return None

    @staticmethod
    def _host_header(request: HopRequest) -> str:
        if request.port == DEFAULT_PORTS[request.scheme]:
            return request.host
        return f"{request.host}:{request.port}"

    @staticmethod
    def _read(answer, request: HopRequest) -> tuple[bytes, bool]:
        """Read up to the ceiling plus one byte, checking the clock as it goes.

        The extra byte is how oversize is detected without reading the rest: if
        one more byte than the ceiling arrives, the document is too big and
        nothing more needs to be known about it.
        """
        limit = request.max_bytes
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            if request.deadline is not None and time.monotonic() > request.deadline:
                raise FetchFailed(
                    f"{request.host} was still sending after "
                    f"{TOTAL_DEADLINE_SECONDS:.0f} seconds, so the read was "
                    "abandoned. A server that dribbles bytes holds this host "
                    "open for as long as it likes."
                )
            chunk = answer.read(min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), total > limit


def transport_from_environment(env: Mapping[str, str] | None = None) -> Transport | None:
    """A real transport when the switch is on, None when it is not.

    OFF IS THE DEFAULT AND THAT IS THE POINT. A reviewer's fresh checkout, and
    the test suite, open no sockets because nothing hands fetch_source() a
    transport. Switching it on is a deliberate act by whoever deployed this,
    recorded in the environment where a deployment's decisions belong.
    """
    values = os.environ if env is None else env
    if str(values.get(FETCH_ENABLED_ENV, "")).strip().lower() not in _TRUE_WORDS:
        return None
    return HttpTransport()


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What one accepted fetch did. Returned; never raised.

    `changed` is THREE-VALUED and that is the honest shape: True stored a new
    version, False compared equal and wrote nothing, and None means nothing was
    retrieved to compare, so no statement about the document is available. A
    boolean here would make "we could not reach it" render as "it has not
    changed", which is the failure this whole product is built to refuse.
    """

    source_id: str
    #: The SOURCE_STATUS_ written to the registration.
    status: str
    #: True when nothing was retrieved: refused before the request, refused
    #: between hops, or the transport gave up.
    refused: bool
    #: The sentence written to last_result, in plain words.
    result: str
    changed: bool | None
    version_id: str | None
    #: How many Change rows the pipeline recorded. Zero is a real answer for a
    #: first version, which has nothing to be compared against.
    changes: int
    #: SHA-256 of the retrieved text. None when nothing was retrieved, and never
    #: computed from a partial body.
    digest: str | None
    #: The address the bytes actually came from, after any redirects.
    url: str | None


def fetch_source(
    session: Session,
    *,
    company_id: str,
    source_id: str,
    user_id: str,
    transport: Transport | None,
    now=None,
    resolve: Callable[[str], tuple[str, ...]] | None = None,
) -> FetchOutcome:
    """Fetch the document a registered public docket names. Audited either way.

    TWO KINDS OF ANSWER, AND THE LINE BETWEEN THEM IS "DID THE FETCHER TAKE THE
    JOB".

    It RAISES, and writes nothing at all, for a job it will not accept: a source
    that is not this company's, one an administrator has disabled, a kind no
    fetcher serves, a registration that does not name a document, a proceeding
    that is not in this workspace, and a call with no transport. None of those
    are facts about reachability, and recording last_scanned_at for any of them
    would say the product scanned when it refused to try.

    It RETURNS a FetchOutcome, and records the attempt, for every job it accepts
    -- including the ones that fail. A URL refused by the address rules, a
    redirect into this network, a host that will not answer and a body over the
    ceiling all land on the registration as a status and a sentence, because
    "we tried at 14:02 and here is what happened" is exactly what the registry
    exists to be able to say.

    THE GATE COMES FIRST, before the row is even read, so a person who may not
    administer sources learns nothing about whether one exists.
    """
    _require_scope(company_id)
    policy.require(session, company_id, user_id, SOURCE_REGISTRY_PERMISSION)

    row = sources.source_registration_for_company(
        session, company_id=company_id, source_id=source_id
    )
    if row is None:
        # Same answer for "not here" and "not yours", as everywhere else in this
        # registry: telling them apart tells this caller about another tenant.
        raise LookupError(f"no source {source_id!r} for this company")

    if not row.enabled:
        raise sources.SourceRefused(
            f"{row.name} is disabled, so nothing was fetched from it. Disabled "
            "is what this company wants of the source, and a fetcher that "
            "ignored it would make the switch on the registry screen decorative."
        )

    problem = fetch_problem(row.kind, row.config or {})
    if problem:
        raise sources.SourceRefused(problem)

    settings = row.config or {}
    proceeding_id = str(settings[CONFIG_PROCEEDING]).strip()
    document_status = str(settings[CONFIG_DOCUMENT_STATUS]).strip()
    endpoint = str(settings[CONFIG_ENDPOINT]).strip()

    # Before any socket. A proceeding that is not here means the text has
    # nowhere to land, and finding that out after the download is finding it
    # out too late to say anything useful.
    if not any(
        found.id == proceeding_id for found in proceedings_for_company(session, company_id)
    ):
        raise sources.SourceRefused(
            f"{row.name} names proceeding {proceeding_id}, which is not in this "
            "workspace. A filing has to land in a docket that exists; nothing "
            "was fetched."
        )
    # document_status is NOT re-checked here. fetch_problem() above refuses a
    # registration that does not name one of DOCUMENT_STATUSES, and a second
    # copy of that rule is a second place for it to drift.

    if transport is None:
        raise sources.SourceRefused(ANNOUNCEMENT_NO_TRANSPORT)

    stamp = now or datetime.now(timezone.utc)
    deadline = time.monotonic() + TOTAL_DEADLINE_SECONDS

    walk = _walk(endpoint, transport, resolve=resolve, deadline=deadline)
    if walk.reason:
        return _refuse(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            row=row,
            result=walk.reason,
            now=stamp,
        )

    if not walk.body:
        # A commission that answers 200 with nothing has not published an empty
        # order. Ingesting it would replace a filing with a blank version and
        # report every paragraph as removed, which is a confident wrong answer
        # about a document that is probably still there.
        return _refuse(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            row=row,
            result=(
                f"{walk.url} answered 200 with no body at all. An empty document "
                "is treated as a failure rather than as a filing that lost its "
                "text, so nothing was stored."
            ),
            now=stamp,
        )

    try:
        text = walk.body.decode("utf-8")
    except UnicodeDecodeError:
        return _refuse(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            row=row,
            result=(
                f"{walk.url} answered with bytes that are not UTF-8 text. This "
                "build stores text and cites offsets into it, so a PDF or a "
                "scan is refused rather than stored as replacement characters."
            ),
            now=stamp,
        )

    # The same digest app/pipeline.py computes, so the comparison below is
    # against a like number rather than a related one. UTF-8 decoding and
    # re-encoding is exact, so this is also the hash of the bytes on the wire.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stored = sources.latest_version_from_source(
        session, company_id=company_id, source_id=source_id
    )

    if stored is not None and stored.source_sha256 == digest:
        result = (
            f"Unchanged: {walk.url} still hashes to sha256 {digest[:12]}, which "
            f"is what {stored.id} already holds. Nothing was stored."
        )
        sources.record_fetch_attempt(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=user_id,
            status=SOURCE_STATUS_CONNECTED,
            result=result,
            detail=f"retrieved {len(walk.body)} bytes from {walk.url}",
            refused=False,
            now=stamp,
        )
        return FetchOutcome(
            source_id=source_id,
            status=SOURCE_STATUS_CONNECTED,
            refused=False,
            result=result,
            changed=False,
            version_id=None,
            changes=0,
            digest=digest,
            url=walk.url,
        )

    version_id = f"{source_id}-{digest[:16]}"
    changes = ingest_and_diff(
        session,
        company_id=company_id,
        proceeding_id=proceeding_id,
        version_id=version_id,
        # Cut to the column. DocumentVersion.label is String(256), and a name
        # at the registry's own 256-character limit plus a date is longer than
        # that -- silently truncated on Postgres, silently kept on SQLite.
        label=f"{row.name}, retrieved {stamp:%Y-%m-%d}"[:256],
        status=document_status,
        source_text=text,
        previous_version_id=stored.id if stored is not None else None,
    )

    # The provenance the pipeline has no way to know: it is handed text, and
    # only this module knows which address the text came off and when. Written
    # here rather than left NULL, because a version that cannot say where it
    # came from is the gap docs/mrd.html already concedes for the hand-loaded
    # corpus, and there is no reason to add to it.
    #
    # THE FETCH IS SCOPED EVEN THOUGH THIS CALL JUST CREATED THE ROW. version_id
    # is built from the source registration and the content hash, so in practice
    # it is this company's row and the check never fires. In practice is not a
    # guarantee: the id is derived, not owned, and a bare session.get here would
    # stamp another tenant's version with this tenant's URL and retrieval time
    # the first time two derivations met. This was the fourth site doing
    # primary-key-then-use, and the only one that did not post-check at all,
    # which is the argument for the chokepoint rather than for four careful
    # authors. row_for_company raises on a crossing rather than returning None,
    # so it cannot degrade to the silent skip the None branch below is for.
    fetched = row_for_company(session, company_id, DocumentVersion, version_id)
    if fetched is not None:
        fetched.source_url = walk.url
        fetched.source_registration_id = source_id
        fetched.source_retrieved_at = stamp
        session.flush()

    result = (
        f"Changed: {walk.url} now hashes to sha256 {digest[:12]}. Stored as "
        f"{version_id}; the pipeline recorded {len(changes)} change(s)."
    )
    sources.record_fetch_attempt(
        session,
        company_id=company_id,
        source_id=source_id,
        user_id=user_id,
        status=SOURCE_STATUS_CONNECTED,
        result=result,
        detail=(
            f"retrieved {len(walk.body)} bytes from {walk.url} into {version_id} "
            f"under proceeding {proceeding_id} as {document_status}"
        ),
        refused=False,
        now=stamp,
    )
    return FetchOutcome(
        source_id=source_id,
        status=SOURCE_STATUS_CONNECTED,
        refused=False,
        result=result,
        changed=True,
        version_id=version_id,
        changes=len(changes),
        digest=digest,
        url=walk.url,
    )


class _Walk(NamedTuple):
    """The end of a redirect walk: the bytes, or the sentence that stopped it."""

    body: bytes
    url: str | None
    reason: str


def _walk(
    endpoint: str,
    transport: Transport,
    *,
    resolve: Callable[[str], tuple[str, ...]] | None,
    deadline: float | None,
) -> _Walk:
    """Follow up to MAX_REDIRECTS hops, checking every one from the top.

    THE LOOP IS THE GUARD. Nothing about a hop is trusted because the hop before
    it was allowed: a public commission host that answers 302 into 169.254.169.254
    is the ordinary shape of this attack, and the only defence is running the
    same checks on the address the redirect names.

    THE CLOCK IS CHECKED HERE AND NOT ONLY IN THE TRANSPORT, and the difference
    is the one a hostile server aims at. MAX_REDIRECTS bounds how many hops it
    gets; it bounds no time at all. Every hop may spend a whole
    HOP_TIMEOUT_SECONDS just connecting -- which HttpTransport's read loop never
    sees, because nothing has been read yet -- so a chain that redirects slowly
    costs HOP_TIMEOUT_SECONDS * (MAX_REDIRECTS + 1) and outlives the deadline
    this module promises. Refusing to START a hop there is no time for is what
    makes TOTAL_DEADLINE_SECONDS true across all hops rather than within one
    read. It also makes the deadline hold for ANY transport, including an
    injected one that watches no clock of its own.
    """
    url = endpoint
    for _hop in range(MAX_REDIRECTS + 1):
        if deadline is not None and time.monotonic() > deadline:
            return _Walk(
                b"",
                None,
                (
                    f"{endpoint} was still being redirected after "
                    f"{TOTAL_DEADLINE_SECONDS:.0f} seconds, so the walk was "
                    "abandoned before opening another connection. A chain of "
                    "slow hops holds this host open for as long as it likes, "
                    "and a limit on the number of redirects is not a limit on "
                    "the time they take."
                ),
            )

        verdict = check_url(url, resolve=resolve)
        if not verdict.allowed:
            where = "" if url == endpoint else f"A redirect led to {url}. "
            return _Walk(b"", None, where + verdict.reason)

        target = verdict.target
        try:
            answer = transport.open(
                HopRequest(
                    url=target.url,
                    scheme=target.scheme,
                    host=target.host,
                    port=target.port,
                    address=target.address,
                    path=target.path,
                    timeout=HOP_TIMEOUT_SECONDS,
                    max_bytes=MAX_BODY_BYTES,
                    deadline=deadline,
                )
            )
        except FetchFailed as failure:
            return _Walk(b"", None, str(failure))

        if answer.status in REDIRECT_STATUSES:
            if not answer.location:
                return _Walk(
                    b"",
                    None,
                    (
                        f"{target.url} answered {answer.status} with no Location "
                        "header, so there is nowhere to follow it to and nothing "
                        "was guessed."
                    ),
                )
            url = urljoin(target.url, answer.location)
            continue

        if answer.status != 200:
            return _Walk(
                b"",
                None,
                (
                    f"{target.url} answered {answer.status}. Nothing was stored, "
                    "and the previous version of this document is untouched."
                ),
            )

        if answer.oversize:
            return _Walk(
                b"",
                None,
                (
                    f"{target.url} is larger than the {MAX_BODY_BYTES} byte "
                    "ceiling, so it was refused rather than read short. A "
                    "truncated body hashes differently and would be reported as "
                    "a changed filing."
                ),
            )

        return _Walk(answer.body, target.url, "")

    return _Walk(
        b"",
        None,
        (
            f"{endpoint} redirected more than {MAX_REDIRECTS} times without "
            "arriving at a document, so the walk was abandoned."
        ),
    )


def _refuse(
    session: Session,
    *,
    company_id: str,
    source_id: str,
    user_id: str,
    row,
    result: str,
    now,
) -> FetchOutcome:
    """Record an accepted job that came back with nothing, and say why."""
    sources.record_fetch_attempt(
        session,
        company_id=company_id,
        source_id=source_id,
        user_id=user_id,
        status=SOURCE_STATUS_UNREACHABLE,
        result=result,
        detail=f"nothing was retrieved from {row.name}",
        refused=True,
        now=now,
    )
    return FetchOutcome(
        source_id=source_id,
        status=SOURCE_STATUS_UNREACHABLE,
        refused=True,
        result=result,
        changed=None,
        version_id=None,
        changes=0,
        digest=None,
        url=None,
    )


__all__ = [
    "ALLOWED_SCHEMES",
    "ANNOUNCEMENT_NO_TRANSPORT",
    "CONFIG_DOCUMENT_STATUS",
    "CONFIG_ENDPOINT",
    "CONFIG_PROCEEDING",
    "DOCUMENT_STATUSES",
    "FALLBACK_NO_TRANSPORT",
    "FETCHER_KINDS",
    "FETCH_ENABLED_ENV",
    "HOP_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "PRIVATE_NAME_SUFFIXES",
    "REDIRECT_STATUSES",
    "TOTAL_DEADLINE_SECONDS",
    "USER_AGENT",
    "FetchFailed",
    "FetchOutcome",
    "Hop",
    "HopRequest",
    "HttpTransport",
    "Target",
    "Transport",
    "Verdict",
    "address_problem",
    "check_url",
    "fetch_problem",
    "fetch_source",
    "system_resolve",
    "transport_from_environment",
]
