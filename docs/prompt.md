# AI usage and prompt history

This project was built with an AI coding assistant, as the challenge allows. This page explains how: who did
what, where AI sped the work up, where I relied on it heavily, what I reviewed, changed or validated myself,
and the prompts I gave it.

**Tool:** Claude Code (Anthropic), running in VS Code with access to the repository, a terminal and git.

## How the work was split

**My estimate: about 60% AI, 40% me, measured by effort and decisions.** That isn't a line count: the AI typed
almost all of the code, tests and documentation. My 40% is direction and judgement: what to build, in what
order, which trade-offs to accept, what counted as done, and what to check before trusting a result.

| Activity | Led by | How the other side contributed |
|---|---|---|
| Understanding the challenge and planning the architecture | AI drafted, I decided | I asked for the challenge to be explained with the architecture and file structure, then set scope and order |
| Writing the code (generators, engine, validation, exporters, dashboard) | AI | I set requirements and constraints, and reviewed results rather than every line |
| Writing tests | AI | I required regression tests for every fix and asked for checks to be rerun after every change |
| Choosing data sources (synthetic vs Kaggle) | Me | AI evaluated six datasets and reported licences and data-quality problems; I chose the hybrid approach |
| Engineering audit of the finished system | AI ran it from my brief | I wrote the audit brief (inspection only) and decided which findings to fix, in what order |
| Fix rounds (H1, M1–M5, H2) | AI implemented | I set the priorities and the rules: preserve earlier fixes, don't weaken tests, never claim untested results |
| Deciding when something was a real problem | Together | AI measured; I asked "is this a real issue?" before any fix (for example the alert calibration) |
| Final verification (fresh clone, reproducibility, live dashboard) | AI ran it | I asked for a fresh-clone test before calling anything done |
| Git, publishing and what goes public | Me | I set the git identity, approved every push, kept `reports/` private, and banned AI co-author lines in commits |

## Where AI accelerated the work

- **Volume.** About 30 tables, 137 tests, 200+ validation checks and a dashboard in a few days. Writing that by hand would have taken weeks.
- **Exactness that's tedious to do manually.** Integer money arithmetic, half-even rounding, effective-dated FX, and variance causes that add up to the cent.
- **Verification loops.** It reran the test suite, both pipeline scales and the two-run reproducibility check after every change, and reported the actual numbers.
- **Documentation.** The README, data dictionary, business rules, anomaly catalogue and decisions write-up, kept in step with the code.
- **Investigation.** It measured before fixing. For example, it found that the biggest "unforecast" cash flow was payroll not yet booked, not small sundry payments.

## Where I relied heavily on AI

- Writing the implementation itself: pandas data processing, the matching and variance algorithms, the validation framework.
- Low-level library behaviour: pandas 2 vs 3 differences, deterministic random streams, canonical CSV/JSON output.
- The dashboard's HTML, CSS and JavaScript, and its accessibility and responsive behaviour.
- First drafts of every document, including this one.

## What I reviewed, changed or validated myself

**Decisions I made:**
- Use real public data where it adds realism (Kaggle FX rates and a payment-delay calibration), after asking whether Kaggle data could be used and comparing it with fully synthetic data.
- Commission an independent engineering audit of the finished work, as an inspection-only exercise, and then fix its findings in rounds with explicit rules.
- Choose the order of improvements: run-rate forecast, then the decisions write-up, then the dashboard drill-down.
- Keep the internal audit report private (`reports/` is never pushed).
- Remove comments from the source and test files, and set the commit identity to my own account.
- Publish the dashboard on GitHub Pages so reviewers can open it with one click.

**Checks I insisted on, and what they caught:**
- **A fresh-clone test before calling the work done.** It found that a newer pandas wrote missing values as `nan` in the ground-truth file. That was fixed, and the outputs are now proven identical across pandas versions.
- **"Is this a real issue?" before any fix.** For the alerts finding this confirmed a real calibration problem: only the hypothetical stress payment could ever trigger an alert. The fix was a documented policy change, not data tuning.
- **Repeated "what is done and what is left" checkpoints,** so nothing was reported as finished without evidence.
- **Reproducibility runs before every push.** Full and starter outputs are byte-identical across two runs.

**AI mistakes that the review process caught** (all fixed, most with a regression test):
- The first bank-reconciliation matcher "explained" a changed ledger amount instead of reporting a break.
- The first top-10 dashboard list showed a forecast item and its own settlement as two large offsetting rows.
- A test assertion that compared the wrong kind of ID and could never fail.
- A README paragraph that was garbled during an edit, and a rounding slip in a report.
- Earlier: a cash-pool sweep of zero that aborted generation, and pandas 3 incompatibilities.

## How we worked together

1. I wrote a brief: the goal, the constraints, and what "done" means.
2. The AI inspected the code, proposed an approach and implemented it.
3. The AI verified it with tests, validation, reproducibility and, for visual work, screenshots, and reported actual numbers.
4. I reviewed the results, asked follow-up questions, and either approved the push or redirected the work.

Changes made to the repository outside this session were treated like any other input: checked, tested and
then included. These were an FX-configuration refactor and an analysis of the liquidity-alert issue.

## Prompt history (condensed, in order)

The full session transcript is available on request. These are the prompts that set direction, in my own words.

**Understanding and building**
1. "read it first and explain and also give the architecture in detail and file structure" (the challenge statement)
2. The build brief: *"Project: Liquidity Forecasting and Risk Monitoring System…"*, asking to implement, run and test everything
3. "what happened and why are they take lots of time"
4. "tell me what part is left and what is complete"
5. "do one thing first remove comments in src/ tests files only"
6. "do more better readme file and properly explain everything"
7. "Co-Authored-By: Claude … never add this line in commit"

**Data**
8. "tell me one thing can we use kaggle data if available"; several dataset candidates shared with "check" or "test it only"
9. "which one is best use this data or synthetic data", then "yes start"

**Verification and audit**
10. "ok first do fresh clone test"
11. The engineering audit brief: inspection only, write `reports/ENGINEERING_AUDIT_REPORT.md`
12. The H1 fix brief; Round 2 brief (M1, M5, H2, M3, then M2/M4); Round 3 brief (M2 revision history, M4 independent bank reconciliation, full verification, docs, final report)
13. "audit report is updated or not"

**Dashboard and publishing**
14. "can be work dashboard or not", "still has 1 day and 13 hours to submit"
15. "how to see dashboard", "open it", "give me link", "how to share … link to others"
16. "ok push it but do not push reports folder and audit report.md file"
17. "ok commit and push it and do again final fresh clone test"

**Improvements**
18. "what part we improve can you tell me"
19. "ok do one by one": run-rate forecast, decisions write-up, dashboard drill-down
20. "also check this is real issue or not if real issue fix it" (liquidity alerts)
21. "check everything again step by step if any issue fix it and complete dashboard then commit it and push it"
22. "check it and if any error fix it then push it" (about FX-configuration edits found in the working tree)
