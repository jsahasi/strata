"""The five metrics, each computed from the manifest against the live code.

Every metric states three things before it states a number: what it measures,
how it is computed, and the threshold it has to clear. A score without those is
a number nobody can argue with, which is the opposite of useful.

Two rules govern this module.

**The oracle is independent.** Expected answers come from `data/manifest.json`,
whose offsets were computed by a separate script and verified against the bytes.
No metric asks the code under test what the right answer is.

**Nothing here calls a model or the network.** These metrics score the
deterministic spine -- the verifier, the diff, the draft/final routing table.
That is a deliberate limit, printed in the caveat rather than left for a reader
to discover.
"""

import re
from dataclasses import dataclass, field

from app.diff.engine import RESTRUCTURE_CONFIDENCE_CEILING, Change, diff
from app.evals.corpus import Corpus
from app.evals.report import Sample
from app.interpretation.action import (
    ACTION_COMMENT,
    ACTION_COMPLY,
    ACTION_MONITOR,
    action_vocabulary,
    requires_effective_date,
)
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_QUOTE_MISMATCH,
    Citation,
    verify_citation,
)

# The fabricated-quote probe alters one number inside a real quote at real
# offsets: the cost floor a Large Load Customer bears under CHG-1. That is the
# failure ADR-003 exists for -- fluent, correctly cited, and wrong in the one
# token that decides who pays. Hard-coded rather than derived, because a probe
# that quietly retargets when the corpus moves is not a probe. If the phrase is
# gone, the metric says so and fails instead of passing on a no-op.
FABRICATION_FROM = "fifty percent (50%)"
FABRICATION_TO = "seventy percent (70%)"

_HEADING = re.compile(r"^\s*\d+(?:\.\d+)*\s+([^.]+)\.")


@dataclass(frozen=True)
class Metric:
    """One scored dimension, with its counts and the evidence behind them.

    A metric supplies a `Sample` -- the identity of every row of evidence it
    gathered -- and never a sample size. `sample_size` is a property here for
    that reason: there is no constructor slot to pass an inflated one into.
    """

    key: str
    title: str
    measures: str
    method: str
    threshold: str
    hits: int
    total: int
    unit: str
    sample: Sample
    blocking: bool = True
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.hits == self.total and not self.failures

    @property
    def sample_size(self) -> int:
        """Independent items, derived. The renderer reads this to gate a rate."""
        return self.sample.size

    @property
    def sample_unit(self) -> str:
        return self.sample.phrase


@dataclass(frozen=True)
class Observation:
    """Something worth printing that is deliberately not scored."""

    title: str
    lines: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 1. Citation verification
# ---------------------------------------------------------------------------


def citation_verification(corpus: Corpus) -> Metric:
    sites = corpus.citation_sites()
    failures: list[str] = []
    verified = 0
    boilerplate = sum(1 for site in sites if site.subject == corpus.boilerplate_subject)

    for site in sites:
        result = verify_citation(
            Citation(site.version_id, site.start, site.end, site.quoted_text),
            corpus.text(site.version_id),
            expected_occurrence=site.expected_occurrence,
        )
        if result.verified:
            verified += 1
        else:
            failures.append(f"{site.label} at {site.where} -- {result.reason}")

    return Metric(
        key="citation_verification",
        title="Citation verification",
        measures=(
            "Whether every offset the manifest records still re-reads to the "
            "text the manifest says sits there."
        ),
        method=(
            "Each recorded (version, start, end, quoted_text) is handed to "
            "app.verification.verifier.verify_citation against the version "
            "file, read as bytes and decoded. Repeated spans also state the "
            "occurrence index the manifest's own ordering gives them."
        ),
        threshold=(
            "Every offset, with no exception. Anything less is a release "
            "blocker: an offset that has stopped verifying means either the "
            "corpus moved under the product or the verifier broke, and both "
            "make every citation in the product unsafe to show."
        ),
        hits=verified,
        total=len(sites),
        unit="manifest offsets verify",
        # The subject each offset is evidence about, not the offset. Nine of
        # these twenty quote one boilerplate sentence and two more quote a
        # wording recorded twice because a revision left it alone; six subjects
        # is what the corpus holds. Reporting 20 samples here printed the only
        # percentage on the page, and it was the one number the harness had not
        # earned. Corpus.citation_sites carries the full argument.
        sample=Sample(
            unit="subjects",
            probe_unit="recorded offsets",
            identities=tuple(site.subject for site in sites),
        ),
        blocking=True,
        failures=tuple(failures),
        notes=(
            f"{len(sites)} recorded offsets, "
            f"{len({site.quoted_text for site in sites})} distinct quoted "
            f"strings, {len({site.subject for site in sites})} subjects. "
            "All three are printed because the first is the work done and the "
            "last is the evidence: every offset re-reads correctly, and that "
            "is a count, not a rate.",
            f"{len(sites) - boilerplate} offsets sit on the "
            f"{len(corpus.changes)} labelled changes and {boilerplate} on the "
            "repeated boilerplate. CHG-4 contributes one -- it has no before "
            "side, and that absence is checked by the draft-versus-final "
            "metric.",
        ),
    )


# ---------------------------------------------------------------------------
# 2. Deliberate-corruption rejection
# ---------------------------------------------------------------------------


def corruption_rejection(corpus: Corpus) -> Metric:
    failures: list[str] = []
    notes: list[str] = []
    rejected = 0

    # Probe 1: a fabricated quote at real offsets.
    after = corpus.change("CHG-1")["after"]
    genuine = after["exact_text"]
    fabricated = genuine.replace(FABRICATION_FROM, FABRICATION_TO)
    where = f"{after['version']}:{after['start']}:{after['end']}"

    if fabricated == genuine:
        failures.append(
            f"probe 1 did not fabricate anything: the phrase this probe alters "
            f"is no longer in CHG-1's quote at {where}. Fix the probe rather "
            f"than reading its result."
        )
    else:
        result = verify_citation(
            Citation(after["version"], after["start"], after["end"], fabricated),
            corpus.text(after["version"]),
        )
        if not result.verified and result.reason == REASON_QUOTE_MISMATCH:
            rejected += 1
            notes.append(
                f"probe 1 rejected -- fabricated quote at the real offsets "
                f"{where}, one numeral in the cost floor altered: "
                f"{result.reason}"
            )
        else:
            failures.append(
                f"probe 1 was not rejected as a quote mismatch at {where}: "
                f"verified={result.verified}, reason={result.reason!r}. A "
                f"fabricated quote asserted itself."
            )

    # Probe 2: a real repeated quote, cited without saying which occurrence.
    spans = corpus.boilerplate_by_version()["v1"]
    first = spans[0]
    citation = Citation("v1", first["start"], first["end"], corpus.boilerplate_sentence)
    result = verify_citation(citation, corpus.text("v1"))
    where = f"v1:{first['start']}:{first['end']}"

    if not result.verified and result.reason == REASON_AMBIGUOUS_OCCURRENCE:
        rejected += 1
        notes.append(
            f"probe 2 rejected -- the boilerplate sentence at {where} "
            f"(section {first['section']}), quoted correctly with no "
            f"occurrence stated: {result.reason}"
        )
    else:
        failures.append(
            f"probe 2 was not rejected at {where}: verified={result.verified}, "
            f"reason={result.reason!r}. A quote that appears three times "
            f"asserted itself without saying which one it meant."
        )

    return Metric(
        key="corruption_rejection",
        title="Deliberate-corruption rejection",
        measures=(
            "Whether the verifier refuses two corruptions built to slip past "
            "it: text that reads right at offsets that are right, and text "
            "that is right at offsets that are right but means three "
            "different things."
        ),
        method=(
            "Two probes are constructed from the corpus and sent through the "
            "same verify_citation the product uses. Each must come back "
            "unverified, and with the reason that names what was wrong -- a "
            "rejection for the wrong reason is not a pass."
        ),
        threshold=(
            "Both. This is the direction the product cannot fail in: a missed "
            "change costs a review, an asserted fabrication costs the reason "
            "the product exists."
        ),
        hits=rejected,
        total=2,
        unit="corruption probes rejected",
        # Two probes, two corruptions, nothing folded: a fabricated quote and
        # an unstated occurrence are different evidence.
        sample=Sample(
            unit="probes",
            probe_unit="probes",
            identities=("fabricated quote", "unstated occurrence"),
        ),
        blocking=True,
        failures=tuple(failures),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 3. Diff completeness
# ---------------------------------------------------------------------------


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _diffs_by_pair(corpus: Corpus) -> dict[tuple[str, str], list[Change]]:
    pairs = {tuple(change["version_pair"]) for change in corpus.changes}
    return {
        pair: diff(corpus.passages(pair[0]), corpus.passages(pair[1]))
        for pair in sorted(pairs)
    }


def _covering(reported: list[Change], manifest_change: dict) -> list[Change]:
    """Reported changes that account for both sides of a manifest change.

    One reported change has to line up with both the before span and the after
    span. Letting two unrelated changes satisfy the two halves would call a
    manifest change 'found' when nothing had actually been paired.
    """
    before, after = manifest_change["before"], manifest_change["after"]
    found: list[Change] = []
    for change in reported:
        if change.after is None or not _overlaps(
            change.after.char_start, change.after.char_end, after["start"], after["end"]
        ):
            continue
        if before.get("version") is not None:
            if change.before is None or not _overlaps(
                change.before.char_start,
                change.before.char_end,
                before["start"],
                before["end"],
            ):
                continue
        found.append(change)
    return found


def _touching(reported: list[Change], manifest_change: dict) -> list[Change]:
    """Reported changes overlapping either side of a manifest change."""
    before, after = manifest_change["before"], manifest_change["after"]
    found: list[Change] = []
    for change in reported:
        hit = change.after is not None and _overlaps(
            change.after.char_start, change.after.char_end, after["start"], after["end"]
        )
        if not hit and before.get("version") is not None and change.before is not None:
            hit = _overlaps(
                change.before.char_start,
                change.before.char_end,
                before["start"],
                before["end"],
            )
        if hit:
            found.append(change)
    return found


def diff_completeness(corpus: Corpus) -> Metric:
    diffs = _diffs_by_pair(corpus)
    failures: list[str] = []
    notes: list[str] = []
    found = 0

    for manifest_change in corpus.changes:
        pair = tuple(manifest_change["version_pair"])
        reported = diffs[pair]
        covering = _covering(reported, manifest_change)
        if covering:
            found += 1
            confidences = ", ".join(f"{c.alignment_confidence:.2f}" for c in covering)
            plural = "change" if len(covering) == 1 else "changes"
            notes.append(
                f"{manifest_change['id']} ({manifest_change['type']}) found in "
                f"{pair[0]}->{pair[1]}: {len(covering)} reported {plural}, "
                f"alignment confidence {confidences}"
            )
        else:
            failures.append(
                f"{manifest_change['id']} ({manifest_change['type']}) not found "
                f"in {pair[0]}->{pair[1]}: no reported change lines up with the "
                f"manifest spans. A change the corpus was built around is "
                f"missing from the diff."
            )

    # CHG-5 must escalate. A renumbering is dangerous because the words barely
    # move, so text similarity runs high exactly when structural identity has
    # changed (findings.html #2, ADR-004).
    restructure = corpus.change("CHG-5")
    touching = _touching(diffs[tuple(restructure["version_pair"])], restructure)
    if not touching:
        failures.append("CHG-5 produced no reported change at all, which is silence.")
    else:
        highest = max(change.alignment_confidence for change in touching)
        if highest <= RESTRUCTURE_CONFIDENCE_CEILING:
            notes.append(
                f"CHG-5 escalates: {len(touching)} reported changes across the "
                f"relocated section, highest alignment confidence "
                f"{highest:.2f}, at or below the ceiling of "
                f"{RESTRUCTURE_CONFIDENCE_CEILING:.2f}. It asks rather than "
                f"asserts."
            )
        else:
            failures.append(
                f"CHG-5 asserted itself: highest alignment confidence "
                f"{highest:.2f} is above the ceiling of "
                f"{RESTRUCTURE_CONFIDENCE_CEILING:.2f}. A wholesale "
                f"renumbering is presenting as a settled match."
            )

    # And the other direction. If everything escalated the review queue would
    # be useless, so an in-section edit must keep its true score.
    for change_id in ("CHG-2", "CHG-3"):
        manifest_change = corpus.change(change_id)
        covering = _covering(
            diffs[tuple(manifest_change["version_pair"])], manifest_change
        )
        if not covering:
            continue
        highest = max(c.alignment_confidence for c in covering)
        if highest > RESTRUCTURE_CONFIDENCE_CEILING:
            notes.append(
                f"{change_id} does not escalate: alignment confidence "
                f"{highest:.2f} is above the ceiling, so an ordinary "
                f"in-section edit still reports as a confident pairing."
            )
        else:
            failures.append(
                f"{change_id} escalated at alignment confidence {highest:.2f}. "
                f"An in-section edit that escalates makes the review queue "
                f"a list of everything, which nobody empties."
            )

    return Metric(
        key="diff_completeness",
        title="Diff completeness",
        measures=(
            "Whether the deterministic diff finds all five labelled changes, "
            "and whether the one that cannot be aligned honestly says so."
        ),
        method=(
            "Each version is split into passages by ingestion's own splitter, "
            "then app.diff.engine.diff runs on each version pair. A manifest "
            "change counts as found when one reported change overlaps its "
            "before span and its after span. CHG-5's alignment confidence must "
            "sit at or below the restructure ceiling; CHG-2's and CHG-3's must "
            "sit above it."
        ),
        threshold=(
            "All five found, CHG-5 escalating, CHG-2 and CHG-3 not escalating. "
            "Counts only: five items cannot carry a recall figure."
        ),
        hits=found,
        total=len(corpus.changes),
        unit="labelled changes found",
        # One identity per labelled change. Nothing folds here; five authored
        # changes are five subjects, and five is still under the floor.
        sample=Sample(
            unit="changes",
            probe_unit="changes",
            identities=tuple(change["id"] for change in corpus.changes),
        ),
        blocking=True,
        failures=tuple(failures),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 4. Occurrence disambiguation
# ---------------------------------------------------------------------------


def occurrence_disambiguation(corpus: Corpus) -> Metric:
    sentence = corpus.boilerplate_sentence
    failures: list[str] = []
    # One entry per probe, naming the span it asked about. Three entries share
    # a span, so the sample deflates from twenty-seven to nine on its own.
    identities: list[str] = []
    passed = 0

    for version_id, occurrences in sorted(corpus.boilerplate_by_version().items()):
        text = corpus.text(version_id)
        for index, occurrence in enumerate(occurrences):
            citation = Citation(
                version_id, occurrence["start"], occurrence["end"], sentence
            )
            where = f"{version_id}:{occurrence['start']}:{occurrence['end']}"
            section = occurrence.get("section")

            identities.append(where)
            stated = verify_citation(citation, text, expected_occurrence=index)
            if stated.verified:
                passed += 1
            else:
                failures.append(
                    f"{where} (section {section}) stating occurrence {index} "
                    f"did not verify -- {stated.reason}"
                )

            identities.append(where)
            silent = verify_citation(citation, text)
            if not silent.verified and silent.reason == REASON_AMBIGUOUS_OCCURRENCE:
                passed += 1
            else:
                failures.append(
                    f"{where} (section {section}) verified with no occurrence "
                    f"stated. The same words in a different section mean a "
                    f"different thing."
                )

            identities.append(where)
            wrong_index = (index + 1) % len(occurrences)
            wrong = verify_citation(citation, text, expected_occurrence=wrong_index)
            if not wrong.verified and wrong.reason == REASON_AMBIGUOUS_OCCURRENCE:
                passed += 1
            else:
                failures.append(
                    f"{where} (section {section}) verified while claiming to "
                    f"be occurrence {wrong_index}. A citation may not be right "
                    f"about the words and wrong about which sentence it read."
                )

    return Metric(
        key="occurrence_disambiguation",
        title="Occurrence disambiguation",
        measures=(
            "Whether a sentence that appears three times per version can only "
            "be cited by saying which of the three it is."
        ),
        method=(
            "Three probes against each of the nine recorded spans: cite it "
            "with the occurrence index the manifest's ordering gives it "
            "(must verify), cite it with no occurrence stated (must be "
            "refused), and cite it as the next occurrence along (must be "
            "refused). Both refusals must name the ambiguity as the reason."
        ),
        threshold=(
            "Every probe. Text equality is necessary and not sufficient, which "
            "is the whole argument for this metric existing beside citation "
            "verification rather than inside it."
        ),
        hits=passed,
        total=len(identities),
        unit="occurrence probes correct",
        # Nine spans, not twenty-seven probes. Three questions about one span
        # are not three samples, and treating them as such would manufacture
        # the denominator that licenses a rate. The identities carry the span,
        # so the deflation happens in Sample rather than in a variable this
        # function has to remember to increment.
        #
        # The nine spans are one sentence, and the citation metric folds them
        # into one subject. Here they stay nine, because this metric's subject
        # is the span: each one has a different right answer, and telling them
        # apart is the thing being scored. Reasonable people could argue for
        # one. Either way it is under the floor and no rate prints.
        sample=Sample(
            unit="spans",
            probe_unit="probes",
            identities=tuple(identities),
        ),
        blocking=True,
        failures=tuple(failures),
        notes=(
            "The boilerplate sentence sits in three sections per version -- "
            "study records, collateral records, general compliance records. "
            "Same words, three subject matters.",
        ),
    )


# ---------------------------------------------------------------------------
# 5. Draft-versus-final routing
# ---------------------------------------------------------------------------


def _lands_in(manifest_change: dict) -> str:
    return manifest_change["after"]["version"]


def draft_final_routing(corpus: Corpus) -> Metric:
    failures: list[str] = []
    notes: list[str] = []
    passed = 0
    checks = 0

    final_only = corpus.change("CHG-4")
    landing = _lands_in(final_only)
    heading_match = _HEADING.match(final_only["after"]["exact_text"])

    # 1-2. The manifest says this obligation has no draft-stage counterpart.
    # Re-read the bytes rather than believing the manifest's note.
    if heading_match is None:
        failures.append(
            "CHG-4's quoted text no longer starts with a numbered heading, so "
            "the absence check has nothing to search for."
        )
        checks += 2
    else:
        heading = heading_match.group(1)
        for version_id in ("v1", "v2"):
            checks += 1
            if heading not in corpus.text(version_id):
                passed += 1
                notes.append(
                    f"absent from {version_id} ({corpus.status(version_id)}): "
                    f"'{heading}' does not occur"
                )
            else:
                failures.append(
                    f"'{heading}' occurs in {version_id}, so CHG-4 is not "
                    f"final-only and this metric is scoring the wrong change."
                )

    # 3. It lands in a version the manifest marks FINAL.
    checks += 1
    status = corpus.status(landing)
    if status == "FINAL":
        passed += 1
    else:
        failures.append(
            f"CHG-4 lands in {landing}, whose status is {status!r}, not FINAL."
        )

    vocabulary = action_vocabulary(status)

    # 4. It routes to comply.
    checks += 1
    if ACTION_COMPLY in vocabulary:
        passed += 1
    else:
        failures.append(
            f"CHG-4 lands in {landing} ({status}) and comply is not on offer: "
            f"{vocabulary}. A binding obligation with no way to act on it."
        )

    # 5. It never routes to monitor-and-comment.
    checks += 1
    stray = [
        action for action in (ACTION_MONITOR, ACTION_COMMENT) if action in vocabulary
    ]
    if not stray:
        passed += 1
    else:
        failures.append(
            f"CHG-4 lands in {landing} ({status}) and still offers {stray}. "
            f"Treating a final order as a draft misses a binding deadline."
        )

    # 6. A final change binds from a date.
    checks += 1
    if requires_effective_date(status):
        passed += 1
    else:
        failures.append(f"a change in {landing} ({status}) needs no effective date.")

    # 7. The draft side, in the other direction.
    checks += 1
    draft_leaks: list[str] = []
    for manifest_change in corpus.changes:
        version_id = _lands_in(manifest_change)
        version_status = corpus.status(version_id)
        offered = action_vocabulary(version_status)
        notes.append(
            f"{manifest_change['id']} lands in {version_id} "
            f"({version_status}) -> {', '.join(offered)}"
        )
        if version_status == "DRAFT" and ACTION_COMPLY in offered:
            draft_leaks.append(manifest_change["id"])
    if not draft_leaks:
        passed += 1
    else:
        failures.append(
            f"draft-stage changes {draft_leaks} offer comply. Acting on a draft "
            f"spends money on something that may not survive comment."
        )

    return Metric(
        key="draft_final_routing",
        title="Draft-versus-final routing",
        measures=(
            "Whether CHG-4 -- an obligation with no draft-stage counterpart, "
            "appearing for the first time in the Final Order -- produces a "
            "comply action and never monitor-and-comment."
        ),
        method=(
            "CHG-4's heading is searched for in v1 and v2 to confirm the "
            "absence the manifest claims, reading the bytes rather than "
            "trusting the note. The version it lands in supplies its status, "
            "and app.interpretation.action decides the vocabulary from that "
            "status alone. The draft side is checked in the other direction: "
            "no change landing in a DRAFT version may offer comply."
        ),
        threshold=(
            "Every check. ADR-005 calls this the error with the highest cost "
            "in this domain, in both directions."
        ),
        hits=passed,
        total=checks,
        unit="routing checks pass",
        # Seven checks, one change. Asking CHG-4 seven questions does not make
        # seven changes, and the seventh sweeps the other four only to prove a
        # negative about the draft side.
        sample=Sample(
            unit="change (CHG-4)",
            probe_unit="routing checks",
            identities=("CHG-4",) * checks,
        ),
        blocking=True,
        failures=tuple(failures),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Observation: why no precision figure appears anywhere above
# ---------------------------------------------------------------------------


def diff_noise(corpus: Corpus) -> Observation:
    diffs = _diffs_by_pair(corpus)
    lines: list[str] = []
    reported_total = 0
    labelled_total = 0

    for pair, reported in sorted(diffs.items()):
        labelled: set[int] = set()
        for manifest_change in corpus.changes:
            if tuple(manifest_change["version_pair"]) != pair:
                continue
            for change in _touching(reported, manifest_change):
                labelled.add(id(change))
        reported_total += len(reported)
        labelled_total += len(labelled)
        lines.append(
            f"{pair[0]}->{pair[1]}: {len(reported)} changes reported, "
            f"{len(labelled)} of them touch a span the manifest labels"
        )

    lines.append(
        f"across both pairs: {reported_total} reported, {labelled_total} "
        f"labelled, {reported_total - labelled_total} unlabelled"
    )
    lines.append(
        "The manifest does not say the unlabelled ones are wrong. It says "
        "nothing about them: it labels five changes, not every change. Some "
        "are real edits nobody wrote down -- the title, the status line, the "
        "recitals, and the renumbering ripple CHG-5 drags behind it."
    )
    lines.append(
        "So no precision figure is printed here, and none can be. Precision "
        "needs negative labels and this corpus has none. Building the "
        "denominator by hand from these numbers would be inventing evidence."
    )

    return Observation(
        title="Observation -- diff noise, and why precision is absent",
        lines=tuple(lines),
    )


ALL_METRICS = (
    citation_verification,
    corruption_rejection,
    diff_completeness,
    occurrence_disambiguation,
    draft_final_routing,
)

ALL_OBSERVATIONS = (diff_noise,)
