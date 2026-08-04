# Strata — AI Fund Build Challenge

A citation-grade regulatory intelligence and operations workspace for regulated enterprises.
Built for the AI Fund Engineer-in-Residence challenge. **Deadline: 48 hours from 2026-08-03.**

---

## How this submission is actually judged

Read from AI Fund's own prep guide. These are not aspirations, they are the rubric. Every
decision in this repo is answerable against them.

**Four evaluation dimensions:**

1. **AI excellence and technical depth.** Can you go deep on *every* architectural choice, at
   the code level rather than the slide level? Can you say where modern AI fails and how you
   designed around it? *"Vague architecture talk is a fail signal."*
2. **Product instinct.** A clear, specific point of view on who the user is and what their pain
   is. Defend the choices; acknowledge the ones you would change.
3. **User empathy.** *Did you talk to real users before you built? Did their feedback change
   what you built?* Can you describe the user's day, their workflow, and the exact moment this
   product helps?
4. **Founder behaviour.** Energy about the market, urgency on the hard parts (integration,
   distribution), the kind of person others want to join.

**Hard rules that follow from the rubric:**

- **One user. Not two.** "If your answer to *who is this for* includes more than one type of
  person, you have not finished the work." Pick one, design for them, say no to the rest.
- **Talk to 2-3 real regulatory-affairs people before submitting.** The guide calls this the
  single most useful thing you can do. Record what they did not understand, what they ignored,
  what changed as a result. That story goes in the PRD and gets told at the panel.
- **Every feature maps to a named user pain.** An internal eval dashboard built for a
  non-technical user will be challenged. Have the honest answer ready or cut the feature.
- **The hardest technical decision must be VISIBLE IN THE PRODUCT**, not buried in the stack.
  For us that is citation verification — a reviewer must be able to see a claim refuse to
  assert itself when its citation does not verify.
- **Every technology choice needs its alternatives, trade-offs and constraints written down**
  at the time it is made, not reconstructed afterwards. A diagram without reasoning is worth
  little. `docs/.ai/decisions.html` is where this lives.
- **Log what the AI wrote versus what you rewrote or rejected, as you go.** The submission
  template demands it and the interview reconciles it against the repo. Reconstructing it
  later is both harder and less honest.
- **"I don't know" is a positive signal.** Bluffing is a red flag. Where a doc is uncertain,
  say so in the doc.

**Submission mechanics:** keep the full commit history, do not squash — they read it. The exact
run command and the exact test command must work on a reviewer's machine; they execute both
before scheduling a panel.

---

## Conventions

- **Docs are HTML, not Markdown** (`README.md` excepted — tooling expects it). Structured
  project memory lives in `docs/.ai/`: `decisions.html` (ADRs) and `tasks.html` (the board).
- **PRD and MRD are living documents.** Update them when the build changes, not at the end.
- **TDD before implementation** for anything with logic worth trusting. Every fix ships a
  regression guard.
- **Fix the class, not the line.** If a bug has a forward and a backward path, fix both.
- **Absence is denial.** Any failure to verify a citation, resolve a scope, or confirm a fact
  degrades to escalation, never to a confident guess.
- Prose follows Orwell's rules: short words, active voice, cut what can be cut. No emoji.

`docs/best-practices.html` is the portable engineering playbook carried over from the peer
project — 27 principles, each with the failure that taught it. Sections 26 and 27 (a fallback
must announce itself; a derived corpus migrates all at once) are directly load-bearing here.

## Layout

```
app/        application code
tests/      pytest; offline by default
data/       synthetic proceeding versions + company context
docs/       prd.html mrd.html tdd.html architecture.html security.html
            future-enhancements.html user-research.html submission.html
docs/.ai/   decisions.html (ADRs) tasks.html (board)
```
