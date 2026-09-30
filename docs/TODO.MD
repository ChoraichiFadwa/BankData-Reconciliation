# TODO

This is a prioritized backlog. I do NOT need to complete everything.
Each item includes why it matters so I can decide whether the effort is worth it.

---

## Tier 1 — Cheap and highly visible

### CI with GitHub Actions
- [ ] Add GitHub Actions workflow
  - Run tests automatically on every push
  - Test on Windows and Linux
  - Add `ruff` linting to the workflow
  - Add a green CI badge to the README

**Why:**
A reviewer can immediately see that tests are automated rather than something I ran once manually. It is a small engineering improvement with high visibility.

**Effort/payoff:** High payoff / Low effort

---

### README screenshots
- [ ] Add screenshot of the Exceptions tab
- [ ] Add screenshot of the Rollforward tab
- [ ] Add the three-line console output
- [ ] Consider adding a short GIF showing the three-month close running

**Why:**
Most people reviewing a GitHub repository will not clone it immediately. Screenshots let them understand the result within seconds and make the project feel like a real usable tool rather than just source code.

**Effort/payoff:** High payoff / Very low effort

---

### Power BI dashboard
- [ ] Build the Power BI dashboard
- [ ] Read from `v_rollforward`
- [ ] Include the aging query
- [ ] Show match rate over time
- [ ] Show exception exposure over time
- [ ] Add aging buckets
- [ ] Add drill-through by category
- [ ] Distinguish the controller's view from the accountant's view
- [ ] Export screenshots or a PDF for the README

**Why:**
The reconciliation engine produces accounting data, but Power BI makes that output understandable to a finance/business audience. It also demonstrates that I can take data from a backend process and turn it into something useful for decision-making.

`.pbix` files do not render properly on GitHub, so screenshots/PDF are important.

**Effort/payoff:** Very high payoff / Medium effort

---

## Tier 2 — Engineering depth

### Scale test
- [ ] Generate a dataset with 50k transactions
- [ ] Generate a dataset with 100k transactions
- [ ] Measure reconciliation runtime
- [ ] Identify the current O(n²) bottleneck in Pass 3
- [ ] Optimize candidate matching using amount-range bucketing
- [ ] Compare before/after timings
- [ ] Add `EXPLAIN QUERY PLAN` for the carry-forward query
- [ ] Document which indexes matter and why

**Why:**
The current dataset is small, so "runs in under a second" does not demonstrate much about scalability. A scale test shows whether I understand how algorithmic complexity and database performance affect a real data pipeline.

This also gives me something concrete to discuss in a data engineering interview.

**Effort/payoff:** High payoff / Medium-high effort

---

### Multi-account support
- [ ] Add an `account` dimension to the schema
- [ ] Support multiple accounts for one company
  - Operating account
  - Payroll account
  - USD account
- [ ] Thread `account_id` through reconciliation
- [ ] Produce proofs per account
- [ ] Update tests for multiple accounts

**Why:**
The current model is centered around one account. Supporting several accounts makes the system look much closer to something a company could actually use rather than a single-account demonstration.

It is also a clean schema/design refactor rather than just adding another feature.

**Effort/payoff:** Medium-high payoff / Medium-high effort

---

### Data quality checks at ingest
- [ ] Check for duplicate references within each input file
- [ ] Check for dates outside the expected period
- [ ] Check for null required fields
- [ ] Check for zero-value transactions
- [ ] Detect statements that skip expected days
- [ ] Create a `dq_check` table
- [ ] Store the result of each load's checks
- [ ] Make fatal data-quality failures stop the load

**Why:**
The reconciliation currently validates the final balances, but a production pipeline should also validate the input data itself. Bad source data should be detected before it contaminates the reconciliation.

This demonstrates standard data-engineering thinking: **validate data at ingestion, not only after processing.**

**Effort/payoff:** High payoff / Medium effort

---

### Property-based tests with Hypothesis
- [ ] Add Hypothesis
- [ ] Generate random bank/GL transaction pairs
- [ ] Test that matching is one-to-one
- [ ] Test that a row cannot be both matched and an exception
- [ ] Test that reconciliation passes do not overlap
- [ ] Test matcher invariants across many generated datasets

**Why:**
The current tests prove that the system works for my chosen dataset. Property-based tests prove that important rules remain true across thousands of generated cases.

This is a more advanced testing technique and would make the repository stand out, but it is not necessary for the project to be useful.

**Effort/payoff:** Medium payoff / Medium-high effort

---

## Tier 3 — Domain depth

### Many-to-one matching
- [ ] Support multiple GL receipts settling as one bank deposit
- [ ] Limit the maximum number of transactions in a candidate group
- [ ] Require transactions to fall within a defined date window
- [ ] Reject ambiguous solutions instead of guessing
- [ ] Add tests for many-to-one reconciliation

**Why:**
Many-to-one settlement is a realistic reconciliation scenario that the current one-to-one matcher cannot handle.

It would add genuine domain depth, but it also introduces significantly more algorithmic complexity. This is a "do it if I want to deepen the project" item, not a requirement.

**Effort/payoff:** High payoff / High effort

---

### Bank format adapters
- [ ] Define a YAML configuration for bank formats
- [ ] Configure source column mappings
- [ ] Configure date formats
- [ ] Configure sign conventions
- [ ] Configure check/reference patterns
- [ ] Add a second synthetic bank with a different format
- [ ] Make the ingestion layer configuration-driven

**Why:**
Different banks do not necessarily provide statements in the same format. Configuration-driven ingestion means adding a new bank does not require rewriting the pipeline.

This demonstrates that I am thinking about maintainability and extensibility rather than only making the current dataset work.

**Effort/payoff:** Medium payoff / Medium-high effort

---

### Attribution and segregation of duties
- [ ] Record who ran each reconciliation
- [ ] Record who closed each period
- [ ] Prevent the person who ran a reconciliation from closing the same period
- [ ] Add tests for this rule

**Why:**
This introduces an actual accounting-control concept into the system: the person performing a control should not necessarily be the same person approving/closing it.

It is a relatively small technical change but gives the project a stronger internal-controls story.

**Effort/payoff:** Medium payoff / Medium effort

---

# Things to skip for now

### ML classification
- [ ] Do NOT add ML to classify reconciliation exceptions

**Why:**
The rule-based approach is currently a strength. Reconciliation decisions need to be explainable and auditable, and adding ML just to make the project look more "AI" would not necessarily improve it.

If I want to demonstrate ML, it makes more sense to use a separate project designed around an ML problem.

---

### Docker
- [ ] Do NOT Dockerize for now

**Why:**
The project already works with `pip install -r requirements.txt`. Docker would add infrastructure complexity without solving an important problem unless I also move the project toward a PostgreSQL-based deployment.

Reconsider this if the architecture changes.

---

# Minimum version worth finishing

If I don't want to spend weeks on this project, prioritize:

- [ ] CI with GitHub Actions
- [ ] README screenshots
- [ ] Power BI dashboard
- [ ] Scale test

The first three make the project much easier to review.
The scale test gives me a strong technical discussion point.

---

# Optional deeper version

Only continue if I still want to invest in the project:

- [ ] Multi-account support
- [ ] Data-quality checks
- [ ] Property-based tests
- [ ] Many-to-one matching
- [ ] Bank format adapters
- [ ] Segregation of duties

---

# Final cleanup

- [ ] Update README with the final architecture
- [ ] Document design decisions
- [ ] Document known limitations
- [ ] Document future work
- [ ] Add screenshots/results
- [ ] Make sure tests pass
- [ ] Make sure CI passes
- [ ] Review repository structure
- [ ] Remove unnecessary files