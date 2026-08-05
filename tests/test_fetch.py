"""The first fetcher, and the forgery surface it opens.

WHAT IS BEING TESTED IS NOT "CAN IT DOWNLOAD A FILE". An administrator types a
URL and this server opens a connection to it. That is server-side request
forgery in one sentence, and the droplet this product runs on shares a kernel
with another project, so the interesting half of this file is every URL that
must NOT be reached: the loopback interface, the cloud metadata address at
169.254.169.254, a name that resolves into a private range, a public name that
REDIRECTS into one, an IPv4 address smuggled inside an IPv6 literal, a body
large enough to exhaust the host and a server that dribbles bytes forever.

NOTHING HERE TOUCHES THE NETWORK, AND THAT IS MEASURED RATHER THAN ASSERTED.
Two dependencies are injected, because both of them reach out: the transport
that opens the socket, and the RESOLVER that turns a name into an address. A
guard that called the real getaddrinfo would need DNS to run its own tests,
which is how an offline suite quietly stops being one. Every test below hands
in a resolver that answers from a dict and a transport that answers from a
dict, and one test proves a literal address consults no resolver at all.

THE BYTES ARE REAL. data/real holds 102 filings from eight commissions, each
with a provenance JSON naming the URL it came from, the exact byte count and
the SHA-256 of the file. The fake transport below serves those bytes at those
URLs, and the expected hashes are read out of the provenance files rather than
typed here, so "unchanged" and "changed" are measured against a real filing and
a real digest.

ONE TEST DOES OPEN A SOCKET, to 127.0.0.1, and it is the only way to prove the
real transport speaks HTTP at all. It skips itself if a loopback listener
cannot be bound. Note the irony it exercises: the guard refuses loopback, so
the transport can only be tested by calling it directly, underneath the guard.
That is stated in the transport's own docstring rather than hidden here.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import pathlib
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

from app.auth import policy
from app.seed import demo_account_list, ensure_accounts, load
from app.sources import fetch
from app.state import sources
from app.state.audit import ACTION_ACCESS_DENIED, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import user_by_email
from app.state.models import (
    FETCHABLE_SOURCE_KINDS,
    ROLE_ADMIN,
    ROLE_ANALYST,
    SOURCE_REGISTRATION_KIND_INTERNAL_STORE,
    SOURCE_REGISTRATION_KIND_MCP_SERVER,
    SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
    SOURCE_REGISTRATION_KIND_REST_API,
    SOURCE_STATUS_CONNECTED,
    SOURCE_STATUS_NEVER_TRIED,
    SOURCE_STATUS_NOT_IMPLEMENTED,
    SOURCE_STATUS_UNREACHABLE,
    AuditEvent,
    Change,
    DocumentVersion,
    SourceRegistration,
)

COMPANY = "MEP"
RIVAL = "RIVAL"

#: The proceeding the seeded corpus loads. Read from the corpus, not typed.
REAL_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "real"

#: One docket at one commission, three filings, all of them public.
CLEAN = "ga-44280-rules-regs-tariff-compliance-clean"
TRACKED = "ga-44280-rules-regs-tariff-compliance-tracked"
ORDER = "ga-44280-order-rules-regs-tariff-compliance"

#: An address that is genuinely on the public internet. NOT 203.0.113.5: the
#: documentation ranges are private as far as the ipaddress module is
#: concerned, so a test that used one would prove the guard refuses documented
#: examples rather than that it permits a real commission.
PUBLIC_IP = "93.184.216.34"
PUBLIC_IPV6 = "2606:4700:4700::1111"

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- the real corpus


def provenance(slug: str) -> dict:
    return json.loads((REAL_DIR / f"{slug}.provenance.json").read_text("utf-8"))


def filing_bytes(slug: str) -> bytes:
    return (REAL_DIR / f"{slug}.txt").read_bytes()


def test_the_corpus_provenance_is_what_this_file_measures_against():
    """If these three stop agreeing, every "unchanged" test below means nothing.

    The provenance file states the byte count and the SHA-256 of the filing
    beside it. This test is the one that would catch a corpus that moved under
    the suite, so the rest can rely on the digests.
    """
    for slug in (CLEAN, TRACKED, ORDER):
        meta = provenance(slug)
        raw = filing_bytes(slug)
        assert len(raw) == meta["bytes"], slug
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"], slug
        assert meta["source_url"].startswith("https://")


# ------------------------------------------------------------------- the fakes


class Resolver:
    """A name server that answers from a dict and refuses to be surprised.

    A name it was not told about raises, rather than returning nothing, because
    a test that silently resolved to an empty tuple would pass for the wrong
    reason: no address to check is not the same as every address checked.
    """

    def __init__(self, table: dict[str, tuple[str, ...]] | None = None):
        self.table = dict(table or {})
        self.asked: list[str] = []

    def __call__(self, host: str) -> tuple[str, ...]:
        self.asked.append(host)
        if host not in self.table:
            raise AssertionError(f"the test did not say what {host!r} resolves to")
        return self.table[host]


class NeverAsked:
    """A resolver that fails the test if anything asks it to resolve a name."""

    def __call__(self, host: str) -> tuple[str, ...]:
        raise AssertionError(f"a name was resolved when none should have been: {host!r}")


class FakeTransport:
    """One hop, answered from a dict. Records every request it was given.

    It answers by URL, and what it hands back is a fetch.Hop -- the same shape
    the real transport returns -- so nothing under test can tell them apart.
    """

    def __init__(self, answers: dict[str, fetch.Hop] | None = None):
        self.answers = dict(answers or {})
        self.requests: list[fetch.HopRequest] = []

    def open(self, request: fetch.HopRequest) -> fetch.Hop:
        self.requests.append(request)
        if request.url not in self.answers:
            raise AssertionError(f"nothing was staged at {request.url}")
        answer = self.answers[request.url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    @property
    def addresses(self) -> list[str]:
        return [request.address for request in self.requests]


def ok(body: bytes) -> fetch.Hop:
    return fetch.Hop(status=200, location=None, body=body, oversize=False)


def redirect(to: str, status: int = 302) -> fetch.Hop:
    return fetch.Hop(status=status, location=to, body=b"", oversize=False)


# ---------------------------------------------------------------- the fixtures


@pytest.fixture
def workspace():
    """A loaded workspace with the seeded accounts. One tenant, one proceeding."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    return COMPANY


def _email(role: str) -> str:
    return next(account.email for account in demo_account_list() if account.role == role)


def _user_id(role: str = ROLE_ADMIN, company_id: str = COMPANY) -> str:
    with session_scope() as session:
        return user_by_email(session, company_id, _email(role)).id


def _proceeding_id(company_id: str = COMPANY) -> str:
    with session_scope() as session:
        from app.state.claims import proceedings_for_company

        return proceedings_for_company(session, company_id)[0].id


def register(
    *,
    endpoint: str | None = None,
    company_id: str = COMPANY,
    kind: str = SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
    proceeding_id: str | None = "",
    document_status: str | None = "FINAL",
    name: str = "Georgia PSC docket 44280",
    enabled: bool = True,
) -> str:
    """Register one public docket that names a document, and hand back its id."""
    config: dict = {}
    if endpoint is None:
        endpoint = provenance(CLEAN)["source_url"]
    if endpoint:
        config[fetch.CONFIG_ENDPOINT] = endpoint
    if proceeding_id != "" and proceeding_id is not None:
        config[fetch.CONFIG_PROCEEDING] = proceeding_id
    elif proceeding_id == "":
        config[fetch.CONFIG_PROCEEDING] = _proceeding_id(company_id)
    if document_status is not None:
        config[fetch.CONFIG_DOCUMENT_STATUS] = document_status

    with session_scope() as session:
        row = sources.register_source(
            session,
            company_id=company_id,
            user_id=_user_id(company_id=company_id),
            name=name,
            kind=kind,
            config=config,
            now=NOW,
        )
        source_id = row.id
    if not enabled:
        with session_scope() as session:
            sources.set_source_enabled(
                session,
                company_id=company_id,
                source_id=source_id,
                user_id=_user_id(company_id=company_id),
                enabled=False,
                now=NOW,
            )
    return source_id


def _row(source_id: str, company_id: str = COMPANY) -> SourceRegistration:
    with session_scope() as session:
        return sources.source_registration_for_company(
            session, company_id=company_id, source_id=source_id
        )


def _versions(company_id: str = COMPANY) -> list[DocumentVersion]:
    with session_scope() as session:
        return (
            session.query(DocumentVersion)
            .filter(DocumentVersion.company_id == company_id)
            .all()
        )


def _actions(company_id: str = COMPANY) -> list[str]:
    with session_scope() as session:
        return [
            row.action
            for row in session.query(AuditEvent)
            .filter(AuditEvent.company_id == company_id)
            .order_by(AuditEvent.seq)
            .all()
        ]


def run_fetch(source_id: str, transport, *, resolve=None, company_id: str = COMPANY,
              role: str = ROLE_ADMIN, now: datetime = NOW):
    with session_scope() as session:
        return fetch.fetch_source(
            session,
            company_id=company_id,
            source_id=source_id,
            user_id=_user_id(role, company_id),
            transport=transport,
            resolve=resolve,
            now=now,
        )


def public_resolver(*hosts: str) -> Resolver:
    return Resolver({host: (PUBLIC_IP,) for host in hosts})


def host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname


# ================================================================= the guard
#
# Every one of these is a URL that tries it. They run without a database and
# without a resolver that can reach anything.


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://psc.example.gov/filing.txt",
        "gopher://psc.example.gov:70/1",
        "data:text/plain;base64,aGVsbG8=",
        "javascript:fetch('/')",
        "//psc.example.gov/filing.txt",
        "psc.example.gov/filing.txt",
    ],
)
def test_only_http_and_https_are_permitted(url):
    verdict = fetch.check_url(url, resolve=NeverAsked())
    assert not verdict.allowed, f"{url} would have been fetched"
    assert verdict.reason
    assert verdict.target is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.53:8080/filing",
        "http://localhost/filing",
        "http://10.0.0.1/",
        "http://10.255.255.254/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[2002:7f00:1::1]/",
        "http://[2002:a9fe:a9fe::1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[::]/",
        "http://224.0.0.1/",
        "http://[ff02::1]/",
        "http://240.0.0.1/",
    ],
)
def test_a_literal_address_inside_this_network_is_refused(url):
    """The whole list, one URL each, because a guard is only as good as its gaps.

    The five IPv6 forms are the ones a guard written from memory misses: the
    loopback interface and the metadata address wrapped as IPv4-mapped literals,
    the same two wrapped as 6to4, and the NAT64 prefix carrying 127.0.0.1 in its
    low bits. Every one of them reaches what 127.0.0.1 or 169.254.169.254
    reaches, and none of them matches a check that only knows how to read a
    dotted quad.
    """
    verdict = fetch.check_url(url, resolve=NeverAsked())
    assert not verdict.allowed, f"{url} would have been fetched"
    assert verdict.reason
    assert verdict.target is None


def test_an_ipv4_address_wrapped_in_an_ipv6_one_is_named_for_what_it_reaches():
    """The reason, not just the verdict, and this is the test the unwrapping
    earns its place with.

    2002:7f00:1::1 is 6to4 carrying 127.0.0.1. The ipaddress module calls it
    private, which is true and useless -- it is the loopback interface, and an
    administrator reading "a private range" would go looking for a firewall
    rule. Unwrapping it first is what produces the sentence that names the
    problem.
    """
    verdict = fetch.check_url("http://[2002:7f00:1::1]/", resolve=NeverAsked())
    assert not verdict.allowed
    assert "loopback" in verdict.reason


def test_a_literal_address_resolves_nothing():
    """No name, no lookup. NeverAsked would raise if anything asked it.

    It matters beyond tidiness: a guard that pushed every literal through a
    resolver would make the suite depend on DNS, and would hand an attacker a
    lookup of a name they chose.
    """
    verdict = fetch.check_url(f"https://[{PUBLIC_IPV6}]/filing", resolve=NeverAsked())
    assert verdict.allowed
    assert verdict.target.address == PUBLIC_IPV6


def test_a_name_that_resolves_into_a_private_range_is_refused():
    resolver = Resolver({"docket.example.gov": ("10.1.2.3",)})
    verdict = fetch.check_url("https://docket.example.gov/f.txt", resolve=resolver)
    assert not verdict.allowed
    assert resolver.asked == ["docket.example.gov"]


def test_an_address_written_as_one_long_number_is_caught_by_the_resolver_check():
    """http://2130706433/ is 127.0.0.1 spelt as an integer.

    The ipaddress module will not read it, so it falls through to the resolver
    as if it were a name -- and getaddrinfo obliges, because the C library has
    always accepted that spelling. This is why the check is on the RESOLVED
    address and not on the text: a guard that pattern-matched dotted quads
    would wave this through.
    """
    resolver = Resolver({"2130706433": ("127.0.0.1",)})
    verdict = fetch.check_url("http://2130706433/", resolve=resolver)
    assert not verdict.allowed
    assert "loopback" in verdict.reason


def test_a_name_that_resolves_to_the_metadata_address_is_refused():
    """The one that turns an admin form into a set of cloud keys."""
    resolver = Resolver({"metadata.example": ("169.254.169.254",)})
    verdict = fetch.check_url("https://metadata.example/", resolve=resolver)
    assert not verdict.allowed
    assert "169.254" in verdict.reason or "link-local" in verdict.reason.lower()


def test_one_bad_address_among_several_refuses_the_whole_name():
    """Refusing on ANY address, not on the one that happens to be picked first.

    A name with a public A record and a private one is the cheap version of DNS
    rebinding: fetch it enough times and the resolver hands over the private
    one. A guard that checked only the address it was about to use would let
    that through on the second try and nowhere in the log would say why.
    """
    resolver = Resolver({"both.example.gov": (PUBLIC_IP, "192.168.0.9")})
    verdict = fetch.check_url("https://both.example.gov/f.txt", resolve=resolver)
    assert not verdict.allowed
    assert verdict.target is None


def test_a_name_that_resolves_to_nothing_is_refused_rather_than_allowed():
    """Absence is denial. No address is not the same as no objection."""
    resolver = Resolver({"gone.example.gov": ()})
    verdict = fetch.check_url("https://gone.example.gov/f.txt", resolve=resolver)
    assert not verdict.allowed


def test_a_resolver_that_fails_is_refused_and_says_which_name():
    def broken(host):
        raise OSError("temporary failure in name resolution")

    verdict = fetch.check_url("https://docket.example.gov/f.txt", resolve=broken)
    assert not verdict.allowed
    assert "docket.example.gov" in verdict.reason


def test_a_credential_in_the_url_is_refused():
    resolver = public_resolver("docket.example.gov")
    verdict = fetch.check_url(
        "https://user:hunter2@docket.example.gov/f.txt", resolve=resolver
    )
    assert not verdict.allowed
    assert "hunter2" not in verdict.reason, "the refusal repeated the credential"


def test_a_public_name_is_allowed_and_the_checked_address_comes_back():
    """The address is carried forward, not thrown away.

    A guard that resolved a name, approved it and then let the transport
    resolve the name again has checked one address and connected to another.
    Between the two lookups a DNS answer can change, which is the whole trick.
    So the verdict carries the address the transport must use.
    """
    resolver = public_resolver("psc.example.gov")
    verdict = fetch.check_url("https://psc.example.gov/f.txt", resolve=resolver)
    assert verdict.allowed
    assert verdict.target.address == PUBLIC_IP
    assert verdict.target.host == "psc.example.gov"
    assert verdict.target.port == 443
    assert verdict.target.scheme == "https"


@pytest.mark.parametrize(
    "url",
    ["", "   ", "http://:80/", "http://psc.example.gov:99999/f.txt", "http:///f.txt"],
)
def test_an_address_that_cannot_be_read_is_refused_rather_than_repaired(url):
    """No host, no port, no address. None of them is a thing to guess at."""
    verdict = fetch.check_url(url, resolve=NeverAsked())
    assert not verdict.allowed
    assert verdict.reason


def test_a_string_that_is_not_an_address_at_all_is_refused_by_name():
    assert fetch.address_problem("not-an-address")
    assert fetch.address_problem("")
    assert fetch.address_problem(PUBLIC_IP) == ""


def test_the_query_string_travels_with_the_request():
    """Commissions put the document id in the query. Dropping it fetches an index."""
    resolver = public_resolver("psc.example.gov")
    verdict = fetch.check_url(
        "https://psc.example.gov/get?documentId=222325&fileId=103475", resolve=resolver
    )
    assert verdict.allowed
    assert verdict.target.path == "/get?documentId=222325&fileId=103475"


def test_the_system_resolver_answers_in_the_shape_the_guard_reads():
    """The one call to the real resolver, and it stays inside this host.

    localhost is answered from /etc/hosts, so this needs no network. What it
    proves is the shape: literals, deduplicated, in a tuple -- and that the
    guard refuses what the real resolver hands back for a name that points
    here, which is the pairing that matters.
    """
    try:
        answers = fetch.system_resolve("localhost")
    except OSError as unavailable:  # pragma: no cover - a host without a hosts file
        pytest.skip(f"localhost does not resolve here: {unavailable}")
    assert answers
    assert len(set(answers)) == len(answers)
    for literal in answers:
        assert fetch.address_problem(literal), f"{literal} would have been fetched"


def test_the_default_port_follows_the_scheme():
    resolver = public_resolver("psc.example.gov")
    plain = fetch.check_url("http://psc.example.gov/f.txt", resolve=resolver)
    assert plain.target.port == 80
    odd = fetch.check_url("https://psc.example.gov:8443/f.txt", resolve=resolver)
    assert odd.target.port == 8443


# =============================================================== the redirects


def test_a_redirect_into_this_network_is_refused_at_the_hop_it_appears(workspace):
    """EVERY hop, not just the first. This is the bypass the guard exists for.

    A public name answers 302 Location: http://169.254.169.254/. A fetcher that
    checked only the URL an administrator typed would follow it, and the admin
    form would have become a reader of the cloud metadata service. The proof is
    two-part: the outcome refuses, and the transport was never asked to open
    the second address.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {"https://psc.example.gov/f.txt": redirect("http://169.254.169.254/latest/")}
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )

    assert outcome.refused
    assert transport.urls == ["https://psc.example.gov/f.txt"]
    assert _versions() == [] or all(
        version.source_registration_id != source_id for version in _versions()
    )
    assert _row(source_id).status == SOURCE_STATUS_UNREACHABLE


def test_a_redirect_to_a_public_address_is_followed_and_the_last_hop_is_the_document(
    workspace,
):
    body = filing_bytes(CLEAN)
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {
            "https://psc.example.gov/f.txt": redirect(
                "https://files.example.gov/222.txt", status=301
            ),
            "https://files.example.gov/222.txt": ok(body),
        }
    )
    outcome = run_fetch(
        source_id,
        transport,
        resolve=public_resolver("psc.example.gov", "files.example.gov"),
    )

    assert not outcome.refused
    assert outcome.changed is True
    assert transport.urls == [
        "https://psc.example.gov/f.txt",
        "https://files.example.gov/222.txt",
    ]
    stored = [v for v in _versions() if v.source_registration_id == source_id]
    assert len(stored) == 1
    # The address the bytes actually came from, not the one that was typed.
    assert stored[0].source_url == "https://files.example.gov/222.txt"


def test_a_relative_redirect_is_resolved_against_the_hop_it_came_from(workspace):
    body = filing_bytes(ORDER)
    source_id = register(endpoint="https://psc.example.gov/a/f.txt")
    transport = FakeTransport(
        {
            "https://psc.example.gov/a/f.txt": redirect("../b/g.txt"),
            "https://psc.example.gov/b/g.txt": ok(body),
        }
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert not outcome.refused
    assert transport.urls[-1] == "https://psc.example.gov/b/g.txt"


def test_a_redirect_with_no_location_is_refused(workspace):
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {
            "https://psc.example.gov/f.txt": fetch.Hop(
                status=302, location=None, body=b"", oversize=False
            )
        }
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused


def test_a_redirect_to_a_scheme_that_is_not_http_is_refused(workspace):
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {"https://psc.example.gov/f.txt": redirect("file:///etc/passwd")}
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused
    assert transport.urls == ["https://psc.example.gov/f.txt"]


def test_a_redirect_loop_stops_at_the_ceiling_rather_than_running_forever(workspace):
    source_id = register(endpoint="https://psc.example.gov/0")
    answers = {
        f"https://psc.example.gov/{n}": redirect(f"https://psc.example.gov/{n + 1}")
        for n in range(fetch.MAX_REDIRECTS + 4)
    }
    transport = FakeTransport(answers)
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused
    assert len(transport.requests) <= fetch.MAX_REDIRECTS + 1


def test_the_deadline_stops_the_walk_between_hops_and_not_only_inside_a_read(
    workspace,
):
    """A redirect chain must not outlive the deadline, one connect at a time.

    THE CEILING ON HOPS IS NOT A CEILING ON TIME. MAX_REDIRECTS bounds how many
    hops a hostile server gets; it says nothing about how long each one takes.
    Every hop may burn a full HOP_TIMEOUT_SECONDS just connecting, so six hops
    is HOP_TIMEOUT_SECONDS * (MAX_REDIRECTS + 1) -- comfortably past
    TOTAL_DEADLINE_SECONDS -- and a server that redirects slowly holds this
    process for longer than the module says it can be held.

    THE CHECK BELONGS IN THE WALK AND NOT IN THE TRANSPORT, which is the class
    rather than the line. The transport is injected: HttpTransport watches the
    clock while it reads, but the deadline is only a guarantee if it holds for
    whatever transport is handed in, and it must cover the connect it cannot
    see. So the walk refuses to start a hop it has no time for.

    The deadline here is already past, so no hop should be attempted at all.
    """
    source_id = register(endpoint="https://psc.example.gov/0")
    answers = {
        f"https://psc.example.gov/{n}": redirect(f"https://psc.example.gov/{n + 1}")
        for n in range(fetch.MAX_REDIRECTS + 4)
    }
    transport = FakeTransport(answers)
    walk = fetch._walk(
        "https://psc.example.gov/0",
        transport,
        resolve=public_resolver("psc.example.gov"),
        deadline=time.monotonic() - 1,
    )
    assert walk.reason, "an expired deadline let the walk run"
    assert transport.requests == [], (
        "a hop was opened after the deadline had already passed; the walk only "
        "consults the clock inside the transport's read loop"
    )
    assert str(int(fetch.TOTAL_DEADLINE_SECONDS)) in walk.reason


def test_a_slow_redirect_chain_cannot_outlive_the_total_deadline(workspace):
    """The same rule from the outside: hops stop once the clock runs out.

    The transport below spends real time on each hop, so this measures what the
    guard promises rather than restating it. The walk must stop partway rather
    than paying for every hop it is allowed.
    """
    source_id = register(endpoint="https://psc.example.gov/0")
    answers = {
        f"https://psc.example.gov/{n}": redirect(f"https://psc.example.gov/{n + 1}")
        for n in range(fetch.MAX_REDIRECTS + 4)
    }

    class Slow(FakeTransport):
        """Burns a slice of the budget per hop, the way a real connect does."""

        def open(self, request):
            time.sleep(0.05)
            return super().open(request)

    transport = Slow(answers)
    # Room for two hops, not for all six.
    walk = fetch._walk(
        "https://psc.example.gov/0",
        transport,
        resolve=public_resolver("psc.example.gov"),
        deadline=time.monotonic() + 0.12,
    )
    assert walk.reason
    assert len(transport.requests) < fetch.MAX_REDIRECTS + 1, (
        "the walk paid for every hop it was allowed even though the clock ran out"
    )


def test_a_walk_with_no_deadline_still_runs(workspace):
    """None means no clock, and it must not be read as a deadline already past."""
    transport = FakeTransport({"https://psc.example.gov/f.txt": ok(b"hello")})
    walk = fetch._walk(
        "https://psc.example.gov/f.txt",
        transport,
        resolve=public_resolver("psc.example.gov"),
        deadline=None,
    )
    assert walk.reason == ""
    assert walk.body == b"hello"


# ==================================================== size, time and encoding


def test_a_body_over_the_ceiling_is_refused_rather_than_hashed_short(workspace):
    """A truncated body has a different hash, and a different hash reads as a
    changed filing. Refusing is the only answer that does not invent a diff.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {
            "https://psc.example.gov/f.txt": fetch.Hop(
                status=200, location=None, body=b"x" * 64, oversize=True
            )
        }
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused
    assert outcome.digest is None
    assert _versions() == [] or all(
        v.source_registration_id != source_id for v in _versions()
    )
    assert str(fetch.MAX_BODY_BYTES) in _row(source_id).last_result or "large" in (
        _row(source_id).last_result or ""
    )


def test_a_transport_that_gives_up_is_recorded_and_writes_no_version(workspace):
    """A slow loris and a dead host reach this product the same way: an
    exception out of the transport, and a row that must not read as connected.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {"https://psc.example.gov/f.txt": fetch.FetchFailed("the read timed out")}
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused
    assert _row(source_id).status == SOURCE_STATUS_UNREACHABLE
    assert "timed out" in _row(source_id).last_result


@pytest.mark.parametrize("status", [301, 400, 403, 404, 429, 500, 503])
def test_an_answer_that_is_not_a_document_is_recorded_in_plain_words(
    workspace, status
):
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    hop = (
        redirect("https://psc.example.gov/f.txt", status=status)
        if status == 301
        else fetch.Hop(status=status, location=None, body=b"nope", oversize=False)
    )
    transport = FakeTransport({"https://psc.example.gov/f.txt": hop})
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused
    assert _row(source_id).status == SOURCE_STATUS_UNREACHABLE
    if status != 301:
        assert str(status) in _row(source_id).last_result


def test_an_empty_answer_is_a_failure_and_not_a_filing_that_lost_its_text(workspace):
    """200 with nothing in it. Storing it would report every paragraph removed."""
    url = provenance(CLEAN)["source_url"]
    source_id = register(endpoint=url)
    resolver = public_resolver(host_of(url))
    run_fetch(source_id, FakeTransport({url: ok(filing_bytes(CLEAN))}), resolve=resolver)
    before = {v.id for v in _versions()}

    outcome = run_fetch(
        source_id,
        FakeTransport({url: ok(b"")}),
        resolve=resolver,
        now=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    assert outcome.refused
    assert {v.id for v in _versions()} == before
    assert _row(source_id).status == SOURCE_STATUS_UNREACHABLE


def test_a_body_that_is_not_text_is_refused_rather_than_guessed(workspace):
    """A PDF is bytes this build cannot read. It says so instead of storing
    replacement characters and calling the result a filing.
    """
    source_id = register(endpoint="https://psc.example.gov/f.pdf")
    transport = FakeTransport(
        {"https://psc.example.gov/f.pdf": ok(b"%PDF-1.7\n\xff\xfe\x00binary")}
    )
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver("psc.example.gov")
    )
    assert outcome.refused
    assert _versions() == [] or all(
        v.source_registration_id != source_id for v in _versions()
    )


# ========================================================== what a fetch does


def test_the_first_fetch_stores_the_filing_and_records_where_it_came_from(workspace):
    """Retrieve, hash, store, and keep the address. Real bytes, real digest."""
    meta = provenance(CLEAN)
    body = filing_bytes(CLEAN)
    source_id = register(endpoint=meta["source_url"])
    transport = FakeTransport({meta["source_url"]: ok(body)})
    outcome = run_fetch(
        source_id, transport, resolve=public_resolver(host_of(meta["source_url"]))
    )

    assert not outcome.refused
    assert outcome.changed is True
    assert outcome.digest == meta["sha256"]

    stored = [v for v in _versions() if v.source_registration_id == source_id]
    assert len(stored) == 1
    version = stored[0]
    assert version.source_sha256 == meta["sha256"]
    assert version.source_text.encode("utf-8") == body
    assert version.source_url == meta["source_url"]
    assert version.source_retrieved_at == NOW
    assert version.status == "FINAL"

    row = _row(source_id)
    assert row.status == SOURCE_STATUS_CONNECTED
    assert row.last_scanned_at == NOW
    assert row.last_result


def test_a_second_fetch_of_the_same_bytes_says_unchanged_and_writes_nothing(workspace):
    """The hash is compared against what is stored, and equal means stop.

    Not "store it again and let the pipeline notice". A second identical row is
    how a count becomes a lie by the second morning.
    """
    meta = provenance(CLEAN)
    source_id = register(endpoint=meta["source_url"])
    transport = FakeTransport({meta["source_url"]: ok(filing_bytes(CLEAN))})
    resolver = public_resolver(host_of(meta["source_url"]))

    run_fetch(source_id, transport, resolve=resolver)
    before = {v.id for v in _versions()}
    changes_before = _change_count()

    later = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    outcome = run_fetch(source_id, transport, resolve=resolver, now=later)

    assert not outcome.refused
    assert outcome.changed is False
    assert outcome.version_id is None
    assert {v.id for v in _versions()} == before
    assert _change_count() == changes_before

    row = _row(source_id)
    assert row.status == SOURCE_STATUS_CONNECTED
    # The attempt is still recorded. "Nothing changed" is a fact worth a row.
    assert row.last_scanned_at == later
    assert "unchanged" in row.last_result.lower()


def test_changed_bytes_are_stored_and_the_existing_pipeline_does_the_diff(workspace):
    """Two real filings from one docket, the second replacing the first.

    The changes are asserted through their ids, which app/pipeline.py derives
    from the two version ids -- so this passes only if ingest_and_diff did the
    work, and would fail if the fetcher had written its own diff.
    """
    meta = provenance(CLEAN)
    url = meta["source_url"]
    source_id = register(endpoint=url)
    resolver = public_resolver(host_of(url))

    first = run_fetch(source_id, FakeTransport({url: ok(filing_bytes(CLEAN))}), resolve=resolver)
    assert first.changed is True

    later = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    second = run_fetch(
        source_id,
        FakeTransport({url: ok(filing_bytes(TRACKED))}),
        resolve=resolver,
        now=later,
    )

    assert second.changed is True
    assert second.digest == provenance(TRACKED)["sha256"]
    assert second.version_id != first.version_id
    assert second.changes > 0

    with session_scope() as session:
        rows = (
            session.query(Change)
            .filter(Change.company_id == COMPANY)
            .filter(Change.to_version_id == second.version_id)
            .all()
        )
    assert rows, "the pipeline recorded no change between the two filings"
    from app.pipeline import change_id

    assert rows[0].id == change_id(first.version_id, second.version_id, 0)
    assert all(row.from_version_id == first.version_id for row in rows)


def test_a_version_id_is_derived_from_the_bytes_so_a_repeat_writes_once(workspace):
    """The id is the registration and the digest, and nothing else.

    A generated id would mint a second row for the same filing on every retry.
    This one is a pure function of what came off the wire, so the pipeline's
    own idempotence does the rest.
    """
    url = provenance(ORDER)["source_url"]
    source_id = register(endpoint=url)
    resolver = public_resolver(host_of(url))
    transport = FakeTransport({url: ok(filing_bytes(ORDER))})

    first = run_fetch(source_id, transport, resolve=resolver)
    assert provenance(ORDER)["sha256"][:16] in first.version_id
    assert source_id in first.version_id


def test_a_document_that_reverts_does_not_grow_a_second_copy_of_itself(workspace):
    """A → B → A. Three fetches, two versions, and the last one is a re-diff.

    A commission withdrawing an amended filing is the ordinary case, and a
    fetcher that minted an id per retrieval would store the original twice and
    report a change count that keeps climbing.
    """
    url = provenance(CLEAN)["source_url"]
    source_id = register(endpoint=url)
    resolver = public_resolver(host_of(url))
    clean = FakeTransport({url: ok(filing_bytes(CLEAN))})
    tracked = FakeTransport({url: ok(filing_bytes(TRACKED))})

    a = run_fetch(source_id, clean, resolve=resolver)
    b = run_fetch(
        source_id, tracked, resolve=resolver, now=datetime(2026, 8, 5, tzinfo=timezone.utc)
    )
    again = run_fetch(
        source_id, clean, resolve=resolver, now=datetime(2026, 8, 6, tzinfo=timezone.utc)
    )

    assert again.version_id == a.version_id != b.version_id
    assert again.changed is True
    stored = [v.id for v in _versions() if v.source_registration_id == source_id]
    assert sorted(stored) == sorted({a.version_id, b.version_id})


def test_every_attempt_is_audited_and_the_chain_still_verifies(workspace):
    url = provenance(CLEAN)["source_url"]
    source_id = register(endpoint=url)
    resolver = public_resolver(host_of(url))

    run_fetch(source_id, FakeTransport({url: ok(filing_bytes(CLEAN))}), resolve=resolver)
    run_fetch(
        source_id,
        FakeTransport({url: fetch.FetchFailed("connection refused")}),
        resolve=resolver,
        now=datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc),
    )

    actions = _actions()
    assert sources.ACTION_SOURCE_FETCHED in actions
    assert sources.ACTION_SOURCE_FETCH_REFUSED in actions
    with session_scope() as session:
        assert verify_chain(session, COMPANY)


def test_the_audit_row_never_carries_the_body(workspace):
    """A filing is public, but a reason field is not a place to put a document."""
    url = provenance(CLEAN)["source_url"]
    source_id = register(endpoint=url)
    run_fetch(
        source_id,
        FakeTransport({url: ok(filing_bytes(CLEAN))}),
        resolve=public_resolver(host_of(url)),
    )
    with session_scope() as session:
        rows = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .filter(AuditEvent.action == sources.ACTION_SOURCE_FETCHED)
            .all()
        )
    assert rows
    for row in rows:
        assert len(row.reason) < 512
        assert "GEORGIA PUBLIC SERVICE COMMISSION" not in row.reason.upper()


def test_last_result_fits_the_column_it_is_written_to(workspace):
    """String(256). A sentence longer than the column is a row that truncates
    silently on another database and reads as a different outcome.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport(
        {"https://psc.example.gov/f.txt": redirect("http://169.254.169.254/latest/")}
    )
    run_fetch(source_id, transport, resolve=public_resolver("psc.example.gov"))
    assert len(_row(source_id).last_result) <= 256


# ============================================ the jobs the fetcher will not take


def test_a_disabled_source_is_not_fetched_and_nothing_is_claimed_about_it(workspace):
    """enabled is what the administrator wants, and the answer is no.

    Nothing is recorded either: writing last_scanned_at for a call that opened
    no socket would say the product scanned when it refused to try.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt", enabled=False)
    transport = FakeTransport()
    with pytest.raises(sources.SourceRefused):
        run_fetch(source_id, transport, resolve=NeverAsked())
    assert transport.requests == []
    row = _row(source_id)
    assert row.last_scanned_at is None
    assert row.last_result is None


@pytest.mark.parametrize(
    "kind",
    [
        SOURCE_REGISTRATION_KIND_INTERNAL_STORE,
        SOURCE_REGISTRATION_KIND_REST_API,
        SOURCE_REGISTRATION_KIND_MCP_SERVER,
    ],
)
def test_a_kind_this_build_cannot_fetch_is_refused_by_name(workspace, kind):
    source_id = register(endpoint="https://psc.example.gov/f.txt", kind=kind)
    transport = FakeTransport()
    with pytest.raises(sources.SourceRefused) as refusal:
        run_fetch(source_id, transport, resolve=NeverAsked())
    assert kind in str(refusal.value)
    assert transport.requests == []


def test_a_registration_that_does_not_say_which_proceeding_is_refused(workspace):
    """Absence is denial. A filing has to land somewhere, and guessing which
    docket it belongs to is the kind of confident wrong answer this product
    exists to refuse.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt", proceeding_id=None)
    transport = FakeTransport()
    with pytest.raises(sources.SourceRefused):
        run_fetch(source_id, transport, resolve=NeverAsked())
    assert transport.requests == []


def test_a_proceeding_that_is_not_in_this_workspace_is_refused_before_any_socket(
    workspace,
):
    source_id = register(
        endpoint="https://psc.example.gov/f.txt", proceeding_id="MPUC-NOT-A-DOCKET"
    )
    transport = FakeTransport()
    with pytest.raises(sources.SourceRefused):
        run_fetch(source_id, transport, resolve=NeverAsked())
    assert transport.requests == []


@pytest.mark.parametrize("document_status", [None, "", "final", "PROPOSED"])
def test_a_registration_that_does_not_say_draft_or_final_is_refused(
    workspace, document_status
):
    """ADR-005: the status is explicit and never inferred. A fetcher cannot
    read a document and know whether a commission has adopted it, so a
    registration that does not say is not fetchable.
    """
    source_id = register(
        endpoint="https://psc.example.gov/f.txt", document_status=document_status
    )
    transport = FakeTransport()
    with pytest.raises(sources.SourceRefused):
        run_fetch(source_id, transport, resolve=NeverAsked())
    assert transport.requests == []


def test_a_registration_that_names_a_commission_and_not_a_document_is_refused(
    workspace,
):
    """The eight corpus rows are this shape: origins, dockets, and no filing.

    A commission's front door is a search page. Fetching it would store a page
    of navigation as if it were an order.
    """
    with session_scope() as session:
        outcome = sources.register_corpus_sources(
            session, company_id=COMPANY, user_id=_user_id(), now=NOW
        )
        registered = list(outcome.registered)
    assert registered

    transport = FakeTransport()
    for source_id in registered:
        with pytest.raises(sources.SourceRefused):
            run_fetch(source_id, transport, resolve=NeverAsked())
    assert transport.requests == []


def test_another_companys_source_cannot_be_fetched(workspace):
    with session_scope() as session:
        session.add(
            SourceRegistration(
                id="SRC-rival",
                company_id=RIVAL,
                name="A rival's docket",
                kind=SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
                status=SOURCE_STATUS_NOT_IMPLEMENTED,
                config={fetch.CONFIG_ENDPOINT: "https://psc.example.gov/f.txt"},
                credential_ref=None,
                created_by_user_id=_user_id(),
                enabled=True,
            )
        )
    transport = FakeTransport()
    with pytest.raises(LookupError):
        run_fetch("SRC-rival", transport, resolve=NeverAsked())
    assert transport.requests == []


def test_a_reader_without_the_permission_cannot_fetch_and_the_refusal_is_logged(
    workspace,
):
    """The refusal is caught inside the session on purpose.

    policy.require() writes the denial through the caller's session, so a test
    that let the exception escape session_scope would roll the row back and then
    assert it was never written. That is a property of the audit design, not of
    this test: a caller that rolls back loses the record of its own refusal.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    transport = FakeTransport()
    analyst = _user_id(ROLE_ANALYST)
    with session_scope() as session:
        with pytest.raises(policy.PermissionDenied):
            fetch.fetch_source(
                session,
                company_id=COMPANY,
                source_id=source_id,
                user_id=analyst,
                transport=transport,
                resolve=NeverAsked(),
                now=NOW,
            )
    assert transport.requests == []
    assert ACTION_ACCESS_DENIED in _actions()


def test_with_no_transport_the_path_is_off_and_says_so(workspace):
    """The same shape app/interpretation/propose.py uses for a missing key.

    Off is a state the product can say out loud, not a silent no-op and not a
    quiet fall back to reading the corpus off disk.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    with pytest.raises(sources.SourceRefused) as refusal:
        run_fetch(source_id, None, resolve=NeverAsked())
    assert fetch.ANNOUNCEMENT_NO_TRANSPORT in str(refusal.value)
    assert _row(source_id).last_scanned_at is None


def test_the_transport_is_off_unless_the_environment_switches_it_on():
    assert fetch.transport_from_environment({}) is None
    assert fetch.transport_from_environment({fetch.FETCH_ENABLED_ENV: "0"}) is None
    assert fetch.transport_from_environment({fetch.FETCH_ENABLED_ENV: "no"}) is None
    switched_on = fetch.transport_from_environment({fetch.FETCH_ENABLED_ENV: "1"})
    assert isinstance(switched_on, fetch.HttpTransport)


# ================================================= what the registry now claims


def test_the_registry_says_a_public_docket_is_fetchable_and_the_others_are_not():
    assert sources.can_fetch(SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET)
    for kind in (
        SOURCE_REGISTRATION_KIND_INTERNAL_STORE,
        SOURCE_REGISTRATION_KIND_REST_API,
        SOURCE_REGISTRATION_KIND_MCP_SERVER,
    ):
        assert not sources.can_fetch(kind)


def test_a_kind_the_product_does_not_define_is_refused_before_anything_else():
    """fetch_problem() is asked by the registry too, and it is asked with
    whatever is in the row. A kind nobody defined gets a sentence, not a
    KeyError from a lookup table.
    """
    problem = fetch.fetch_problem("ftp_mirror", {fetch.CONFIG_ENDPOINT: "https://x.gov/a"})
    assert "ftp_mirror" in problem


def test_the_registry_never_claims_more_than_a_fetcher_handles():
    """The tuple a screen reads must be a subset of the kinds code can serve.

    This is the guard against the next person adding a kind to the registry's
    list because the screen looked wrong, and shipping a row that says a scan
    is pending with nothing behind it.
    """
    from app.sources import FETCHER_KINDS

    assert set(sources.FETCHABLE_KINDS) <= set(FETCHER_KINDS)
    assert set(FETCHABLE_SOURCE_KINDS) <= set(sources.FETCHABLE_KINDS)


def test_a_public_docket_that_names_a_document_starts_at_never_tried(workspace):
    source_id = register(endpoint="https://psc.example.gov/f.txt")
    assert _row(source_id).status == SOURCE_STATUS_NEVER_TRIED
    assert _row(source_id).last_scanned_at is None


def test_a_public_docket_that_names_no_document_starts_at_not_implemented(workspace):
    """Two rows of one kind, two honest statuses, because the row decides.

    A status that read never_tried on a registration nothing can fetch would
    promise a scan that will never come.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt", proceeding_id=None)
    assert _row(source_id).status == SOURCE_STATUS_NOT_IMPLEMENTED


def test_a_kind_with_no_fetcher_still_starts_at_not_implemented(workspace):
    source_id = register(
        endpoint="https://acme.example/v1", kind=SOURCE_REGISTRATION_KIND_REST_API
    )
    assert _row(source_id).status == SOURCE_STATUS_NOT_IMPLEMENTED


def _edit(source_id: str, config: dict, name: str = "Georgia PSC docket 44280"):
    with session_scope() as session:
        return sources.update_source(
            session,
            company_id=COMPANY,
            source_id=source_id,
            user_id=_user_id(),
            name=name,
            config=config,
            now=NOW,
        )


def test_filling_in_the_missing_half_of_a_registration_moves_its_status(workspace):
    """The status is derived from the config, so editing the config moves it.

    An administrator who adds the document address and the docket to a row that
    had neither, and then reads "this build cannot fetch from it", has been told
    their edit did nothing.
    """
    source_id = register(endpoint="https://psc.example.gov/f.txt", proceeding_id=None)
    assert _row(source_id).status == SOURCE_STATUS_NOT_IMPLEMENTED

    _edit(
        source_id,
        {
            fetch.CONFIG_ENDPOINT: "https://psc.example.gov/f.txt",
            fetch.CONFIG_PROCEEDING: _proceeding_id(),
            fetch.CONFIG_DOCUMENT_STATUS: "FINAL",
        },
    )
    assert _row(source_id).status == SOURCE_STATUS_NEVER_TRIED
    assert sources.ACTION_SOURCE_UPDATED in _actions()


def test_editing_a_row_into_an_unfetchable_shape_takes_the_status_back(workspace):
    """The dangerous direction, and it applies even to a row that has scanned.

    A source that was reached yesterday and has since had its document address
    removed cannot be fetched today, whatever it says about yesterday. The
    measurement stays in last_result; the status tells the truth about now.
    """
    url = provenance(CLEAN)["source_url"]
    source_id = register(endpoint=url)
    run_fetch(
        source_id,
        FakeTransport({url: ok(filing_bytes(CLEAN))}),
        resolve=public_resolver(host_of(url)),
    )
    assert _row(source_id).status == SOURCE_STATUS_CONNECTED

    _edit(source_id, {fetch.CONFIG_ENDPOINT: url})
    row = _row(source_id)
    assert row.status == SOURCE_STATUS_NOT_IMPLEMENTED
    # What happened yesterday is not erased. It is just no longer the status.
    assert row.last_scanned_at == NOW
    assert row.last_result


def test_a_measured_status_survives_an_edit_that_leaves_it_fetchable(workspace):
    """connected is a measurement. Renaming a row is not a reason to forget it."""
    url = provenance(CLEAN)["source_url"]
    source_id = register(endpoint=url)
    run_fetch(
        source_id,
        FakeTransport({url: ok(filing_bytes(CLEAN))}),
        resolve=public_resolver(host_of(url)),
    )
    _edit(
        source_id,
        {
            fetch.CONFIG_ENDPOINT: url,
            fetch.CONFIG_PROCEEDING: _proceeding_id(),
            fetch.CONFIG_DOCUMENT_STATUS: "FINAL",
        },
        name="Georgia PSC docket 44280, tariff compliance",
    )
    assert _row(source_id).status == SOURCE_STATUS_CONNECTED


# =========================================================== the real transport
#
# The only test in this file that opens a socket, and it opens it to 127.0.0.1
# -- which the guard above refuses, so it has to call the transport directly.
# That is the honest shape of the limit: the transport can be proved to speak
# HTTP, and cannot be proved against a real commission from an offline suite.


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b"a small filing"
    mode = "ok"

    def do_GET(self):  # noqa: N802 -- http.server's spelling
        if type(self).mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://elsewhere.example/x")
            self.end_headers()
            return
        body = type(self).payload
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    except OSError as unavailable:  # pragma: no cover - sandbox without loopback
        pytest.skip(f"no loopback listener available: {unavailable}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()


def _request(address, *, max_bytes=fetch.MAX_BODY_BYTES):
    host, port = address
    return fetch.HopRequest(
        url=f"http://{host}:{port}/f.txt",
        scheme="http",
        host=host,
        port=port,
        address=host,
        path="/f.txt",
        timeout=5.0,
        max_bytes=max_bytes,
        deadline=None,
    )


def test_the_real_transport_speaks_http_and_hands_back_the_bytes(local_server):
    _Handler.mode = "ok"
    _Handler.payload = b"a small filing"
    hop = fetch.HttpTransport().open(_request(local_server))
    assert hop.status == 200
    assert hop.body == b"a small filing"
    assert hop.oversize is False


def test_the_real_transport_stops_at_the_ceiling_instead_of_reading_the_lot(
    local_server,
):
    _Handler.mode = "ok"
    _Handler.payload = b"x" * 5000
    hop = fetch.HttpTransport().open(_request(local_server, max_bytes=100))
    assert hop.oversize is True
    assert len(hop.body) <= 100 + 1


def test_the_real_transport_does_not_follow_the_redirect_itself(local_server):
    """One hop, always. Following a redirect inside the transport would put it
    beyond the guard, which checks every hop from the outside.
    """
    _Handler.mode = "redirect"
    hop = fetch.HttpTransport().open(_request(local_server))
    assert hop.status == 302
    assert hop.location == "https://elsewhere.example/x"
    assert hop.body == b""
    _Handler.mode = "ok"


def test_the_real_transport_abandons_a_read_that_runs_past_the_deadline(local_server):
    """The slow loris answer. A socket timeout alone does not catch one.

    A server that sends one byte inside every timeout window keeps a connection
    open for as long as it likes, and each individual read looks healthy. The
    wall clock across the whole fetch is what stops it, so the read checks the
    clock and gives up. Here the deadline is already past when the read begins.
    """
    _Handler.mode = "ok"
    _Handler.payload = b"a small filing"
    request = _request(local_server)._replace(deadline=time.monotonic() - 1)
    with pytest.raises(fetch.FetchFailed) as failure:
        fetch.HttpTransport().open(request)
    assert "abandoned" in str(failure.value)


def test_the_real_transport_refuses_a_host_it_cannot_reach():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    request = fetch.HopRequest(
        url=f"http://127.0.0.1:{dead_port}/f.txt",
        scheme="http",
        host="127.0.0.1",
        port=dead_port,
        address="127.0.0.1",
        path="/f.txt",
        timeout=2.0,
        max_bytes=fetch.MAX_BODY_BYTES,
        deadline=None,
    )
    with pytest.raises(fetch.FetchFailed):
        fetch.HttpTransport().open(request)


# ------------------------------------------------------------------- helpers


def _change_count(company_id: str = COMPANY) -> int:
    with session_scope() as session:
        return session.query(Change).filter(Change.company_id == company_id).count()
