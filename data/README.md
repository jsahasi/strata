# Strata synthetic test corpus (T3)

This directory is the single source of ground truth for the citation verifier
(ADR-003), the deterministic diff engine (ADR-004), and draft-versus-final
handling (ADR-005). Everything here is invented. No text is copied from a
real regulatory filing, no company, docket, commission, or person named below
is real.

## What is in here

- `v1_notice_of_proposed_rulemaking.txt` -- a fictional Meridian Public
  Utilities Commission NPRM, Docket No. MPUC-2026-0142, on interconnection
  standards and cost allocation for large load (data center) customers.
  Status: DRAFT.
- `v2_revised_proposed_rule.txt` -- the same docket's revised proposal after
  a comment round. Status: DRAFT.
- `v3_final_order.txt` -- the same docket's final order. Status: FINAL.
- `company_context.json` -- a fictional multi-state utility (Meridian
  Electric & Power Company, "MEP"), with 8 internal obligations, 3 projects,
  and 4 documents, each obligation with a named owner.
- `manifest.json` -- the ground truth. Every deliberate change between
  versions, its type, the exact before/after text, and its character
  offsets in the actual files; every occurrence of the repeated boilerplate
  sentence with its offsets; and which company obligation each change maps
  to.

The three proceeding documents are ~1,220-1,450 words each, structured as
numbered sections and subsections (SECTION 1 ... SECTION 8, with 1.1, 1.2,
etc.), written in regulatory register: defined terms, "shall" obligations,
cross-references between sections, and stated effective/compliance dates.
This is deliberate -- a demo run against obviously fake filler text would not
exercise offset-sensitive citation verification or section-aware diffing in
any way that resembles the real task.

## Offset methodology (read this before trusting a number in manifest.json)

Every `(start, end)` pair in `manifest.json` was computed programmatically
from the three `.txt` files as committed, not estimated by hand:

1. Each file was opened in binary mode and decoded as UTF-8 (not opened in
   text mode), so no newline translation could occur between what is on disk
   and what was measured.
2. Each `exact_text` string was located in the decoded file text with
   `str.find`. For every single-occurrence change, the script asserted the
   substring occurs exactly once (raising on zero or multiple matches) before
   recording its offset, so no offset was taken from an ambiguous match.
3. For the repeated boilerplate sentence, the script asserted exactly three
   occurrences per file (not "at least three" -- exactly three, so the count
   in the manifest is exact) and recorded all three.
4. `end` is exclusive: `file_text[start:end] == exact_text` holds for every
   entry.
5. As a separate verification pass, a second script re-opened all three
   files, re-read `manifest.json`, and re-checked every offset in the
   manifest against the live file bytes -- independent of the script that
   generated the numbers. That pass reported zero mismatches across all 11
   change offsets and all 9 boilerplate occurrences. Re-run it any time the
   `.txt` files change:

   ```
   python3 -c "
   import json
   m = json.load(open('manifest.json'))
   def load(p):
       return open(p,'rb').read().decode('utf-8')
   for chg in m['changes']:
       for k in ('before','after','also_present_unchanged_in'):
           e = chg.get(k)
           if e and e.get('file'):
               text = load(e['file'])
               assert text[e['start']:e['end']] == e['exact_text'], chg['id']
   for occ in m['repeated_boilerplate']['occurrences']:
       text = load(occ['file'])
       assert text[occ['start']:occ['end']] == m['repeated_boilerplate']['sentence']
   print('all offsets verified')
   "
   ```

If anyone edits the `.txt` files by hand, `manifest.json` offsets go stale
immediately -- regenerate them with the same method, do not hand-adjust
numbers.

## The six test cases, and why each one exists

1. **Material change (CHG-1, v1 to v2, Section 5.2-5.3).** The Utility's
   cost-allocation duty changes from 100% customer-pays to a shared
   allocation with a 50% customer floor. This is a change in the substance
   of an obligation. It exercises the materiality classifier (ADR-004) and
   is the flagship impact-mapping case: `company_context.json` obligation
   `OBL-005` states MEP's *old* internal practice, which the change makes
   stale without anyone editing `OBL-005` itself -- exactly the failure a
   change-to-action product exists to catch (ADR-002).

2. **Cosmetic change (CHG-2, v1 to v2, Section 2.1).** The "Large Load
   Customer" definition is restated in different words with the same 20 MW
   threshold. Text differs; the obligation does not. Exercises the false
   positive path -- the system must call this immaterial and must not fire
   a change action against `OBL-003`.

3. **Final-only change (CHG-4, first appears in v3, Section 6.5).** A new
   curtailment-and-compensation obligation appears only in the Final Order,
   with no draft-stage precedent, and the text says so explicitly. Exercises
   ADR-005: a final-only change must produce a comply action with an
   effective date, never a monitor-and-comment action, because there is no
   draft version of it to have been monitoring. Maps to `OBL-007`, an
   informal internal capability that only becomes a Commission-enforceable
   duty at v3.

4. **Deadline move (CHG-3, v1 to v2, Section 7.1).** The load-forecast
   compliance date moves from March 1, 2027 to June 1, 2027, with every
   other word in the sentence unchanged. This is the change type an analyst
   most fears missing, per the build board's own framing -- it tests that
   the diff is sensitive at sub-sentence, sub-word granularity and does not
   get lost inside an otherwise-identical paragraph.

5. **Repeated boilerplate (`repeated_boilerplate` block, all three
   versions).** The sentence "The Utility shall maintain records sufficient
   to demonstrate compliance with this Order for a period of not less than
   five (5) years." appears exactly three times per version, in three
   different sections about three different kinds of records (study
   records, collateral records, general recordkeeping). This is the
   wrong-occurrence citation trap named in the task brief and in the TDD's
   failure-mode list: a citation with a correct quoted string and a correct
   offset can still be wrong if the offset points at occurrence A while the
   claim's reasoning was actually about occurrence B. Exact-offset
   verification (ADR-003) passes that citation, because the text matches --
   which is precisely why offset verification is described as
   necessary-but-not-sufficient, and why a citation test suite needs a case
   where "the text matches" and "the citation is correct" can diverge. All
   nine offsets (three per version) are listed individually in
   `manifest.json`.

6. **Section restructure (CHG-5, v2 to v3, Section 6 to Section 5.4).** The
   entire Collateral and Financial Assurance section moves from a top-level
   Section 6 to a subsection 5.4 nested under Cost Allocation, with every
   subsection number and internal cross-reference renumbered, while the
   collateral-amount and collateral-timing sentences stay word-for-word
   identical. Exercises the disclosed alignment-fallback failure mode in
   ADR-004: a naive positional or number-keyed alignment sees this as a
   deletion plus an unrelated addition; a content-aware alignment should
   instead recognize it as one relocated section with zero materiality.
   There is a second, smaller ripple from the same restructure -- Section
   4.5's cross-reference to the collateral section changes from "Section 6"
   to "Section 5.4" -- noted in CHG-5's description but not separately
   itemized, since it is a consequence of the same structural move, not an
   independent test case.

## Company context: why the wording deliberately does not match the docket

`company_context.json` has 8 obligations, each written in MEP's own internal
voice, each with a named owner (for reviewer routing), each linked to a
source document and a project. None of the 8 obligations use the docket's
exact phrasing on purpose -- "post security" instead of "post collateral",
"annual cycle" instead of a calendar date, "six years" instead of "five
years" -- because the semantic join between docket language and company
language is named in the ADRs (ADR-008) as the product's core difficulty,
and a corpus where the wording already matches would not test it. Where an
obligation's internal number actually conflicts with the docket's number
(the six-year vs. five-year retention period in `OBL-004`), that mismatch is
deliberate and should be surfaced by the impact-mapping step, not silently
reconciled.

## What this corpus does not claim

The manifest enumerates the *deliberate* test-case changes and the
boilerplate occurrences. It is not an exhaustive diff of every difference
between the three files. Realistic incidental differences also exist outside
the manifest -- the status line, the caption ("NOTICE OF PROPOSED
RULEMAKING" / "REVISED PROPOSED RULE" / "FINAL ORDER"), the introductory
paragraph, the Section 7.1/8.1 effective-date language (which is
intentionally worded differently in each version, since a proposed rule and
a final order state effective dates differently), and the final order's
closing "IT IS HEREBY ORDERED" / "BY ORDER OF THE COMMISSION" block, which
has no draft-stage counterpart but is not one of the six named test cases.
A diff engine run against the full files will find more changes than the six
listed here; that is expected and realistic. The manifest is ground truth
for the deliberate test cases, not a claim that those are the only
differences.
