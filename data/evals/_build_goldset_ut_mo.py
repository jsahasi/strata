"""Build the Utah/Missouri obligation gold set.

Offsets are computed, never estimated: each label names a start anchor and an end
anchor, the script finds them in the source file, and the quote is SLICED from
[start:end]. The script asserts quote == text[start:end] before it writes.
"""

import json
import os

BASE = "/Users/jayeshsahasi/github/strata/data/real/"
OUT = "/Users/jayeshsahasi/github/strata/data/evals/goldset_ut_mo.json"

_cache: dict[str, str] = {}


def text(fn: str) -> str:
    if fn not in _cache:
        with open(BASE + fn, encoding="utf-8") as fh:
            _cache[fn] = fh.read()
    return _cache[fn]


# (id, file, start_anchor, end_anchor, search_from, creates_obligation,
#  obligation_text, who_is_bound, ambiguous, why)
SPECS = [
    # ---------------- MISSOURI: ET-2025-0184 (Ameren Missouri large load) -----
    (
        "mo-01",
        "mo-ET-2025-0184-order-approving-large-load-rate-plan.txt",
        "The signatory parties are ordered",
        "of the Agreement.",
        0,
        True,
        "Ameren Missouri and the other signatories must comply with every term of the Amended Non-Unanimous Global Stipulation and Agreement.",
        "The signatory parties",
        False,
        "Ordering paragraph of an issued order, in the imperative; it is what makes the whole attached agreement enforceable.",
    ),
    (
        "mo-02",
        "mo-ET-2025-0184-order-approving-large-load-rate-plan.txt",
        "3. Ameren Missouri is authorized to file",
        "exemplar tariff.",
        0,
        False,
        None,
        "Ameren Missouri",
        True,
        "The words grant permission, not a duty: 'is authorized to file'. Ambiguous because in practice a compliance tariff had to be filed for the plan to take effect, and Ameren filed one on 5 December 2025; an analyst tracking deadlines would plausibly call this an obligation.",
    ),
    (
        "mo-03",
        "mo-ET-2025-0184-order-approving-large-load-rate-plan.txt",
        '"With this legislation',
        'for our citizens,"',
        0,
        False,
        None,
        None,
        False,
        "Direct quotation of the Governor at a bill signing. Pure recital of what somebody said; binds nobody.",
    ),
    (
        "mo-04",
        "mo-ET-2025-0184-order-approving-large-load-rate-plan.txt",
        "Section 393.130.7 requires that",
        "megawatts (MW) or more.",
        0,
        True,
        "An electrical corporation serving more than 250,000 customers must develop and file with the Commission rate schedules for customers projected above a 100 MW annual peak.",
        "electrical corporations providing electric service to more than 250,000 customers",
        True,
        "It states a real, continuing statutory duty on the utility, but it sits in the order's 'Relevant Law' section as description rather than command, and Ameren had already discharged it by filing this very application. A competent analyst could call this background instead of an obligation.",
    ),
    (
        "mo-05",
        "mo-ET-2025-0184-order-nunc-pro-tunc.txt",
        "Revenue Sharing – The Agreement provides that Ameren Missouri shall",
        "Earnings Review Surveillance report.",
        2400,
        True,
        "Ameren Missouri must file an Earnings Review Surveillance report every year.",
        "Ameren Missouri",
        False,
        "'shall file a yearly ... report' - a named party, a mandatory verb, a recurring filing. Sliced from the CORRECTED paragraph the nunc pro tunc order substitutes, not the superseded one earlier in the same file.",
    ),
    (
        "mo-06",
        "mo-ET-2025-0184-order-nunc-pro-tunc.txt",
        "1. The Order Regarding",
        "body of this order.",
        0,
        False,
        None,
        None,
        False,
        "An ordering paragraph that acts on a prior document, not on a party. Nobody is told to do anything.",
    ),
    (
        "mo-07",
        "mo-ET-2025-0184-amended-global-stipulation.txt",
        "No later than March 31 following",
        "limited adjustments described below.",
        0,
        True,
        "By 31 March each year, starting the year the first customer takes permanent LLCS service, Ameren Missouri must file an Earnings Review Surveillance report in a compliance docket, in the format of its Surveillance Monitoring Reports.",
        "the Company (Union Electric Company d/b/a Ameren Missouri)",
        False,
        "A named party, a hard date, a mandatory verb, and a named filing. The Commission ordered the signatories to comply with these terms, so the duty is live.",
    ),
    (
        "mo-08",
        "mo-ET-2025-0184-amended-global-stipulation.txt",
        "No later than May 31 of the applicable year",
        "ERS Report filed by the\n\n          Company.",
        0,
        False,
        None,
        "Staff and OPC",
        True,
        "A genuine dated duty, but it falls on Commission Staff and the Office of the Public Counsel, not on the utility. Labelled false because this gold set scores duties on the regulated utility. Flip it if the extractor is meant to capture duties on any party.",
    ),
    (
        "mo-09",
        "mo-ET-2025-0184-amended-global-stipulation.txt",
        "The Signatories agree that in conjunction with approval",
        "to all 11(M) customers).",
        0,
        False,
        None,
        None,
        False,
        "A recommendation about what the Commission 'should' do. The obligation-sounding word points at the regulator, and it is a request, not a duty.",
    ),
    (
        "mo-10",
        "mo-ET-2025-0184-amended-global-stipulation.txt",
        "The Company commits to meeting with Staff and OPC",
        "OPC, and the Company.",
        0,
        True,
        "Ameren Missouri must meet Staff and the Office of the Public Counsel at least once a year, on a highly confidential basis, to update them on the large load service.",
        "the Company (Ameren Missouri)",
        False,
        "'commits to' plus a stated frequency. The Commission ordered the signatories to comply with the agreement, so the commitment is enforceable.",
    ),
    (
        "mo-11",
        "mo-ET-2025-0184-compliance-tariff-revision.txt",
        "The Company shall calculate and provide the subscribing customer",
        "for a 2025 annual subscription).",
        0,
        True,
        "Ameren Missouri must work out each subscribing customer's total annual Nuclear Energy Credits and give the customer that figure in the first quarter of the following year.",
        "The Company",
        False,
        "Approved tariff language under a REPORTING heading: mandatory verb, named actor, fixed window.",
    ),
    (
        "mo-12",
        "mo-ET-2025-0184-compliance-tariff-revision.txt",
        "The Company shall not be obligated to proceed",
        "the Company shall provide Customer’s\n        electric service.",
        0,
        False,
        None,
        None,
        False,
        "Contains 'shall' twice and reads like a duty, but it does the opposite: it releases the utility from having to build until the customer signs. A disclaimer of obligation is not an obligation. Deliberate hard negative.",
    ),
    (
        "mo-13",
        "mo-ET-2025-0184-compliance-tariff-revision.txt",
        "The Large Load\n        Customer shall pay to Ameren Missouri the Exit Fee",
        "specify by notice.",
        0,
        False,
        None,
        "The Large Load Customer",
        True,
        "A hard payment deadline, but the payer is the customer and the payee is the utility. False under this set's rule that an obligation is a duty on the utility; a broader reading would call it true.",
    ),
    # ---------------- UTAH: 26-035-05 (RMP large-load service contract) -------
    (
        "ut-01",
        "ut-26-035-05-order-approving-settlement.txt",
        "RMP must also provide",
        "paid for by retail customers.”",
        0,
        True,
        "RMP must give the PSC any further information the PSC asks for to confirm that large-load service costs stay out of retail rates.",
        "RMP",
        True,
        "'RMP must also provide' is directive and names the utility, but the sentence is the order restating Utah Code 54-26-602(4)(d) rather than imposing something new in this docket.",
    ),
    (
        "ut-02",
        "ut-26-035-05-order-approving-settlement.txt",
        "2. [RMP] shall reduce",
        "run at or near the location of the LLSC load.",
        0,
        True,
        "RMP must cut system net power costs each hour by the customer's actual hourly energy use multiplied by the average CAISO EDAM locational marginal price near the load.",
        "[RMP]",
        False,
        "A settlement term the PSC approved in the same order; 'shall reduce', a named party, and a stated formula.",
    ),
    (
        "ut-03",
        "ut-26-035-05-order-approving-settlement.txt",
        "The Settling Parties further agree RMP may not seek",
        "through a separate proceeding.",
        0,
        True,
        "RMP must not ask to recover Proposed Resource costs in a future rate case unless the PSC first approves their inclusion in a separate proceeding.",
        "RMP",
        True,
        "A prohibition on the utility, approved by this order. Ambiguous because the framing is 'The Settling Parties further agree' - a report of an agreement - and the prohibition bites only in some later docket.",
    ),
    (
        "ut-04",
        "ut-26-035-05-order-approving-settlement.txt",
        "Consistent with Utah Code § 54-26-301(8)",
        "within 15 business days of execution.",
        0,
        False,
        None,
        None,
        False,
        "Reports an act already done, in the past tense. The deadline language is describing compliance, not requiring it. Deliberate hard negative: the same duty stated prospectively is ut-09.",
    ),
    (
        "ut-05",
        "ut-26-035-05-order-approving-settlement.txt",
        "Accordingly, UAE’s Motion is denied.",
        "Accordingly, UAE’s Motion is denied.",
        0,
        False,
        None,
        None,
        False,
        "Disposes of a motion. Nobody is told to do, file, or stop anything.",
    ),
    (
        "ut-06",
        "ut-26-035-05-settlement-stipulation.txt",
        "Following the expiration or termination of the LLSC",
        "through a separate proceeding.",
        0,
        True,
        "After the LLSC ends, RMP must not put Proposed Resource costs into its cost of service or net power costs unless the Commission approves them in a separate proceeding.",
        "the Company (PacifiCorp d.b.a. Rocky Mountain Power)",
        False,
        "'may not include ... unless' is a flat prohibition on the utility with a named escape route. The PSC approved this stipulation without change.",
    ),
    (
        "ut-07",
        "ut-26-035-05-settlement-stipulation.txt",
        "In the event that the Commission requires a hearing",
        "further support for this Stipulation.",
        0,
        True,
        "If the Commission calls a hearing on the stipulation, RMP must put up at least one witness to explain and support it.",
        "Rocky Mountain Power, the DPU, and OCS",
        False,
        "Conditional but real: a trigger, a named party, and a required act.",
    ),
    (
        "ut-08",
        "ut-26-035-05-settlement-stipulation.txt",
        "Except with regard to the obligations of the Settling Parties",
        "condition by the\n\nCommission.",
        0,
        False,
        None,
        None,
        False,
        "Says when the document starts to bind. It is a condition on the whole stipulation, not a duty to do anything. Hard negative: it contains 'shall' and the word 'obligations'.",
    ),
    (
        "ut-09",
        "ut-26-035-05-application-large-load-service-contract.txt",
        "Upon execution of a contract with a large load customer",
        "within 15 business days of execution of the contract.",
        0,
        True,
        "Once RMP signs a contract with a large load customer, it must file that contract with the Commission for review and approval within 15 business days.",
        "Rocky Mountain Power",
        True,
        "A named utility, a mandatory verb, a trigger and a deadline. Ambiguous because the applicant is reciting the statute in its own pleading, and it is describing a duty it has already met in this docket.",
    ),
    (
        "ut-10",
        "ut-26-035-05-application-large-load-service-contract.txt",
        "The Commission must approve the submitted application",
        "for the large\n\nload customer.”",
        0,
        False,
        None,
        "The Commission",
        True,
        "'must approve' is as directive as language gets, but the duty runs to the regulator, not the utility. False under this set's rule; a broader definition would make it true.",
    ),
    (
        "ut-11",
        "ut-26-035-05-application-large-load-service-contract.txt",
        "Delivery facilities have been fully paid for",
        "accommodate the load\n\nservice.",
        0,
        False,
        None,
        None,
        False,
        "Two statements of fact about work already finished and upgrades not needed. No duty on anyone.",
    ),
    (
        "ut-12",
        "ut-26-035-05-bieber-direct-testimony.txt",
        "Accordingly, I recommend that the Commission refrain",
        "another appropriate procedural mechanism.",
        0,
        False,
        None,
        None,
        False,
        "An intervenor witness's recommendation, and one the PSC did not adopt - it approved the accounting treatment and denied UAE's motion. A proposal the commission rejected is not an obligation.",
    ),
    (
        "ut-13",
        "ut-26-035-05-bieber-direct-testimony.txt",
        "Mr. Eller explains that the Company is responsible",
        "recovered\n\n101          through the Reservation Charge.",
        0,
        False,
        None,
        None,
        False,
        "'the Company is responsible for constructing ...' sounds like a duty, but one witness is reporting what another witness said. Recital, not obligation. Deliberate hard negative.",
    ),
]


def main() -> None:
    labels = []
    for (
        lid,
        fn,
        a,
        b,
        frm,
        creates,
        obl,
        bound,
        amb,
        why,
    ) in SPECS:
        t = text(fn)
        start = t.find(a, frm)
        assert start >= 0, f"{lid}: start anchor not found"
        j = t.find(b, start)
        assert j >= 0, f"{lid}: end anchor not found"
        end = j + len(b)
        quote = t[start:end]
        assert quote == t[start:end]
        assert quote.startswith(a) and quote.endswith(b), lid
        rec = {
            "id": lid,
            "source_file": fn,
            "start": start,
            "end": end,
            "quote": quote,
            "creates_obligation": creates,
            "obligation_text": obl,
            "who_is_bound": bound,
            "ambiguous": amb,
            "why": why,
        }
        labels.append(rec)

    doc = {
        "name": "goldset_ut_mo",
        "jurisdictions": ["UT", "MO"],
        "corpus_root": "data/real/",
        "labelled_by": "human-directed labelling pass, regulatory text only",
        "labelling_rules": {
            "obligation": (
                "A duty the filing places on the regulated utility: something it "
                "must do, must not do, must file, must report, or must do by a date."
            ),
            "not_an_obligation": [
                "statement of fact, including a report of an act already completed",
                "recital of what a person or another witness said",
                "a proposal or recommendation the commission has not adopted",
                "a grant of permission, discretion, or authority",
                "a disclaimer that removes a duty",
                "a condition on when a document becomes binding",
            ],
            "party_rule": (
                "creates_obligation is scored against duties on the UTILITY. Dated, "
                "mandatory duties that fall only on the Commission, its Staff, the "
                "Public Counsel, or the customer are labelled false AND ambiguous=true, "
                "with the bound party recorded, so the rule can be flipped without "
                "relabelling."
            ),
            "offsets": (
                "Character offsets into the UTF-8 decoded file. Quotes were sliced from "
                "[start:end] by the build script, never retyped."
            ),
        },
        "known_limits": [
            "One labeller, no second pass, no inter-annotator agreement number. The "
            "ambiguous flag is this labeller's own judgement of contestability, not a "
            "measured disagreement rate.",
            "Two Missouri dockets and one Utah docket only; both concern large-load "
            "service. Nothing here tests obligation extraction on rate design, "
            "safety, or environmental filings.",
            "Passages are single sentences or short paragraphs chosen by hand. They "
            "are not a random sample, so per-class accuracy on this set does not "
            "estimate accuracy over a whole document.",
        ],
        "labels": labels,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    pos = sum(1 for x in labels if x["creates_obligation"])
    amb = sum(1 for x in labels if x["ambiguous"])
    print(f"wrote {OUT}")
    print(f"n={len(labels)} positive={pos} negative={len(labels) - pos} ambiguous={amb}")


if __name__ == "__main__":
    main()
