# X Industries Sales Hierarchy Data Platform

A Unity Catalog-governed data pipeline that ingests and conforms X Industries'
field-sales organization data — divisions, the sales management hierarchy,
field teams, the customer/dealer network, and sales transactions against
that network — into a clean, validated star schema, then enforces
**row-level security** over it so every user sees only the customers their
place in the hierarchy entitles them to.

## Architecture

A single [Lakeflow Declarative Pipeline](https://docs.databricks.com/aws/en/ldp/),
deployed as a [Databricks Asset Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/),
implementing a medallion architecture across multiple schemas in one Unity
Catalog catalog:

```
Source files (division, person, field_team, customer, sales_transaction)
        │
        ▼
Volume: pidilite_demo.bronze.landing/{division,person,field_team,customer,sales_transaction}/
        │  Auto Loader (cloudFiles), one incremental stream per entity
        ▼
Bronze: pidilite_demo.bronze.raw_*        (raw, schema-preserved, no transformation)
        │  column rename/normalization, whitespace trimming, enum
        │  canonicalization, safe type casting, dedupe, join-based
        │  referential integrity checks against upstream silver tables
        ▼
Silver: pidilite_demo.silver.dim_*/fact_*             (cleansed, validated)
        pidilite_demo.silver.dim_*/fact_*_quarantine  (rows failing validation, held for review)
        ▼
Gold:   pidilite_demo.gold.dim_*/fact_sales_transaction
        pidilite_demo.gold.agg_sales_by_territory_month  (serving, 288 rows)
        pidilite_demo.gold.agg_dealer_scorecard          (serving, 501 rows)
        pidilite_demo.gold.access_map_customer     (entitlements, user_email → customer_code)
        pidilite_demo.gold.access_map_field_team   (entitlements, user_email → field_team_code)
        │  row filters keyed on current_user()
        ▼
        Genie space (natural language)  ·  AI/BI Dashboard

Lakebase (OLTP Postgres, separate from the pipeline above)
        pidilite-comments instance → public.comments table
        │  registered as a UC catalog (pidilite_comments) - live federation,
        │  no separate sync job: a write is queryable from the analytical
        │  side immediately, verified end to end
        ▼
        Scoped dashboard app (read, per-viewer via OBO)  ·  comment app (write)
```

Bronze, silver and gold run as **one pipeline** with three source files
(`src/pidilite_demo/bronze.py`, `silver.py`, `gold.py`), so Lakeflow resolves
the dependency DAG and staleness tracking automatically end to end —
`fact_sales_transaction`'s cleansing depends on `dim_customer` and
`dim_person` already being validated, and the pipeline sequences that on its
own. Every entity publishes a clean table alongside a paired quarantine
table, so bad records are visible and inspectable rather than silently
dropped or failing the whole pipeline run.

## Row-level security

Entitlements are flattened once into `gold.access_map_*` so the row filter is
a cheap `EXISTS` probe rather than a recursive walk up the management chain
on every query. The maps are derived from the management chain already on
`dim_field_team`, never hand-maintained, so a promotion or a territory
reassignment rescopes every dashboard on the next pipeline run.

Setup and verification live in `sql/`:
- **`sql/01_row_filters.sql`** — the filter functions (`can_see_customer`,
  `can_see_field_team`), the `ALTER ... SET ROW FILTER` statements, and the
  grants (explicitly withholding `SELECT` on the access-map tables — a user
  needs `EXECUTE` on the filter function, not read access to the map itself).
- **`sql/02_verify_rls.sql`** — the verification checklist: is the filter
  attached, does it survive a pipeline update and a full refresh, does it
  correctly deny a user with no entitlement.

One correctness detail worth knowing: `gold.py`'s tables are Lakeflow
**materialized views**, not classic managed tables, so row filters attach via
`ALTER MATERIALIZED VIEW ... SET ROW FILTER`, not `ALTER TABLE` (which Unity
Catalog rejects with `EXPECT_TABLE_NOT_VIEW`). Both filter-attachment and
full-refresh survival have been verified against this workspace.

### Demo personas

Six real workspace logins, mapped onto roster rows in
`data_generation/generate_dims.py`. Two sets, proving two different things:

- **Vertical** — Abhinav (Territory) → Akshay (Zonal) → Shivam (National) →
  Neeraj (Head Office). Proves *containment*: 70 ⊂ 175 ⊂ 384 ⊂ 501 dealers.
- **Lateral** — two more Territory Managers, `+tm2` on WSSTTY2 and `+tm3` on
  WSSTTY3's **MDI** chain. Proves *isolation*: peers of the same rank see
  completely disjoint dealers. Containment alone invites "of course the boss
  sees more"; lateral answers whether one territory manager can see another's
  numbers, which is what a client actually asks. And the three territories
  under Akshay sum exactly to his own totals — 70 + 41 + 64 = 175 dealers,
  ₹10.52 + 4.73 + 6.63 = ₹21.88 Cr — so the roll-up is arithmetic rather than
  assertion.

The peer logins are plus-addressed, so invitations land in an existing inbox
and no new mailbox is needed. Only their email is overridden; the generated
names stay, so the roster reads like a sales organization rather than the same
few colleagues wearing every hat.

A persona needs privileges on **five separate surfaces** — SQL warehouse,
catalog and schema, the gold tables, the filter functions, and the Genie space
and dashboard — and missing any one fails in a way that does not point at the
cause. The warehouse grant was missed once and the symptom was *"not authorized
to use or monitor this SQL Endpoint"* from inside Genie, which reads like a
Genie fault. So it is one idempotent command:

```bash
./sql/03_grant_persona.sh <email> [profile]
```

`gold.access_map_*` is deliberately absent from what it grants: a user needs
`EXECUTE` on the row-filter function, never read access to the entitlement
tables.

`tests/verify_access_map_logic.py` proves the entitlement algebra offline, in
plain Python against the generated CSVs (containment, no cross-hierarchy
leak, coverage, no-identity-no-access) — useful for iterating on the
generator without a workspace, but it does not substitute for the `sql/`
checks against live Unity Catalog.

## Data model

- **`dim_division`** — top-level business line (e.g. Consumer & Bazaar).
- **`dim_person`** — the field-sales org roster (Territory/Area Sales Manager
  → Regional/Zonal Sales Manager → National Sales Manager → Head Office),
  keyed by `user_email` for downstream access control.
- **`dim_field_team`** — a sales territory with its Master/RA1/RA2 management
  chain. A `field_team_code` can legitimately appear under both
  `hierarchy_type = 'Sales Hierarchy'` and `'MDI Hierarchy'`, each with its
  own management chain.
- **`dim_customer`** — the dealer/retailer network served by each field team.
  Carries `hierarchy_type` alongside `field_team_code`, because a customer
  belongs to the *pair*, never to the code alone — a `field_team_code` can
  repeat under both `hierarchy_type`s with different managers, so joining on
  the code alone would grant one dealer to two different management chains.
- **`fact_sales_transaction`** — individual sales transactions against that
  customer network (`customer_code`, `transaction_date`, `product_category`,
  `quantity`, `revenue`, `salesperson_id`), attributed to the Territory/Area
  Sales Manager of the customer's field team.

Two serving aggregates sit alongside them, so a dashboard tile answers from a
few hundred pre-computed rows instead of scanning the fact on every render, and
Genie gets a table whose grain already matches the question rather than having
to derive it:

- **`agg_sales_by_territory_month`** — revenue, volume and active dealer count
  per territory per month. Backs the trend and territory-comparison views.
- **`agg_dealer_scorecard`** — one row per dealer: lifetime and recent value,
  recency, a dormancy flag and top category. Answers "which dealers need
  attention" without a hand-written query.

Both anchor recency on `as_of_date` — the latest transaction in the data — not
`current_date`, because the seed data is fixed and anchoring on the clock would
make every dormancy figure drift each day the demo is shown.

Every gold column carries a comment, declared through column metadata inside
the pipeline rather than an `ALTER` afterwards so a full refresh republishes
rather than wipes them. Genie reads column comments to pick the column that
answers a question, so a missing one is a silent downgrade in answer quality
rather than an error; `sql/02_verify_rls.sql` checks for empty ones.

Generation logic and volumes are in `data_generation/generate_dims.py` (dims)
and `data_generation/generate_sales.py` (fact table).

### Gaps against a production gold layer

Recorded rather than hidden, because the client's reporting team will find them:

- **No effective dating.** The access maps describe who can see what *now*, so
  a reorg rescopes history as well as the present — a Q1 figure can move because
  a territory changed in Q3. Production needs `valid_from`/`valid_to` on the
  management chain and as-of entitlement; the rule behind it ("after a
  reassignment, whose history is it?") is the client's business decision.
- **No surrogate keys or SCD Type 2** on the dimensions; natural keys only.
- **`access_map_customer` will not scale as-is.** Flattening to (user × dealer)
  is free at 3,507 rows and would not be at the real organization's size. The
  territory-grain map is the one that scales; at volume the customer grain
  becomes a query-time join, a group membership check, or a tag-based policy.
- **No PII classification or column masking**, no partitioning/clustering
  strategy, and no gold-level reconciliation assertions.

## Repo layout

```
databricks.yml                          # bundle config (workspace, targets)
resources/
├── pidilite_demo.pipeline.yml          # pipeline resource definition
└── pidilite_demo.genie_space.yml       # Genie space resource definition
src/pidilite_demo/
├── bronze.py                           # Auto Loader ingestion, one stream per entity
├── silver.py                           # cleansing, canonicalization, quarantine framework
└── gold.py                             # conformed model + derived access maps
sql/
├── 01_row_filters.sql                  # filter functions, ALTER ... SET ROW FILTER, grants
├── 02_verify_rls.sql                   # RLS verification checklist
└── 03_grant_persona.sh                 # all 14 grants for one persona, idempotent
dashboards/
├── x_industries_sales_overview.lvdash.json          # AI/BI dashboard definition
└── x_industries_sales_overview.dashboard.yml.reference  # bundle config, deliberately NOT wired in
genie/
├── pidilite_demo.geniespace.json       # Genie space definition (tables, instructions, benchmarks)
└── question_bank.md                    # per-persona questions with ground-truth answers
lakebase/
├── 01_create_comments_table.sql        # OLTP comments table DDL
└── 02_grants.sql                       # app service principal + persona grants
app/
├── app.py                               # comment form + auth check + recent comments (Streamlit)
├── app.yaml                             # Databricks App run command
└── requirements.txt
dashboard_app/
├── app.py                               # scoped dashboard, OBO auth, per-row comment links (Streamlit)
├── app.yaml
└── requirements.txt
tests/
└── verify_access_map_logic.py          # offline entitlement-algebra test (no workspace needed)
data_generation/
├── generate_dims.py                    # dim table generator, seeded from client sample
└── generate_sales.py                   # fact table generator
sample_data/                            # generated source files, landed into the bronze volume
├── division/  ├── person/  ├── field_team/  ├── customer/  └── sales_transaction/
```

## AI/BI dashboard

`dashboards/x_industries_sales_overview.lvdash.json` — KPI counters, revenue
trend by month, revenue by territory and category, top 10 dealers, and the full
dealer scorecard, all querying `pidilite_demo.gold.*` directly so the row
filters apply live per viewer. One dashboard object for everyone; the scope
narrows by itself.

Two details that matter more than they look:

- **Published without `embed_credentials`**, so each viewer's own identity runs
  the queries rather than the publisher's. With credentials embedded, every
  viewer would see the publisher's scope and row-level security would be
  bypassed entirely.
- **Its bundle config is kept as a `.reference` file outside `resources/`** on
  purpose. `bundle deploy` wants to *recreate* the dashboard with a new id and
  URL, which would invalidate the permissions already granted to the four
  personas. Check whether that recreate warning still applies before wiring it
  in, and re-grant afterwards if it does.

## Lakebase comments

A `pidilite-comments` Lakebase (OLTP Postgres) instance holds one table,
`public.comments` (`comment_id`, `scope_type`, `scope_id`, `user_email`,
`comment_text`, `created_at`) — see `lakebase/01_create_comments_table.sql`.
High-frequency, low-latency interactive writes are a poor fit for a batch
Delta table, which is the whole reason this piece exists in Postgres at all.

A UC catalog (`pidilite_comments`) is registered directly on top of the
instance (`databricks database create-database-catalog ...`). This is a
**live federation**, not a periodic sync — verified end to end: a row
written via a direct Postgres connection is queryable through
`pidilite_comments.public.comments` immediately. No scheduled sync job is
needed, simpler than the build guide's original "sync on a short interval"
assumption. That federation is **read-only** — Unity Catalog rejects writes
against it, confirmed directly (`PERMISSION_DENIED`) — so adding a comment
always goes through a direct Postgres connection, never the SQL warehouse.

The Lakeview AI/BI dashboard (`dashboards/x_industries_sales_overview.lvdash.json`)
stays **purely read-only analytics** — no comment integration in it. Lakeview
has no native write-back widget type, and its table-column link templating
(`linkUrlTemplate`) proved unreliable in practice (a second table using the
same pattern rendered as "Visualization has no fields selected"), so
comments live entirely in their own surface instead of half-working inside
this one.

### The comment app (`app/`)

A single-page Databricks App (Streamlit) that's the only way a comment gets
written:

- reads the viewer's real identity from the `X-Forwarded-Email` header
  Databricks Apps forwards automatically,
- **enforces the same access rule as the dashboard** before allowing a
  write — checks `pidilite_demo.gold.access_map_customer` /
  `access_map_field_team` for that user via the app's own service principal
  (granted narrow `SELECT` on just those two tables — never exposed to end
  users), verified for both an authorized and a rejected case,
- then writes to Postgres using a Lakebase OAuth database credential
  (`generate_database_credential`, refreshed periodically) rather than a
  static password,
- accepts `?scope_type=&scope_id=&hierarchy_type=&customer_name=` query
  params — arriving via a link, the record is pre-filled and there's nothing
  to type; opened directly, it falls back to a manual entry form.

### The scoped dashboard (`dashboard_app/`)

A second, custom Databricks App (Streamlit) — same KPIs/charts/tables as the
Lakeview dashboard, but with full control over the HTML, so every dealer and
every territory row carries a real, reliable **💬 Comment** link (opens the
comment app in a new tab, pre-filled) instead of depending on Lakeview's
templating.

The one thing that makes it "the same architecture, not a second one": it
runs every query as **the viewing user's own identity**, not the app's
service principal. Databricks Apps' on-behalf-of-user authorization
(`user_api_scopes: ["sql"]` at app creation, the viewer's token forwarded via
the `X-Forwarded-Access-Token` header, used directly with the
`databricks-sql-connector`) means the existing `can_see_customer` /
`can_see_field_team` row filters apply automatically — no second scoping
mechanism to write or drift out of sync with the real one. Abhinav opening
this app sees his 9 dealers; Neeraj sees all 121 — same rule, same filters,
just a different rendering surface than the Lakeview dashboard.

Grants for the comment app's service principal and each persona's access to
both apps and the comments table are in `lakebase/02_grants.sql`.

## Genie space

Deployed as code with the rest of the bundle — `resources/pidilite_demo.genie_space.yml`
points at `genie/pidilite_demo.geniespace.json`, which holds the attached tables,
the instructions, curated example SQL, starter questions and a benchmark set.

**Seven gold tables are attached. The two access maps deliberately are not.**
Those hold the entitlements themselves; a user needs `EXECUTE` on the filter
function, never read access to the map, and Genie has no business surfacing one.

Because the attached tables carry row filters, **answers are scoped to whoever
is asking** with no work in the space itself — the same question returns
₹0.50 Cr to a Territory Manager and ₹15.89 Cr to Head Office.

What the instructions have to teach it, and why:

- **Vocabulary** — Field Team = territory, Master / RA1 / RA2 = Territory /
  Zonal / National Sales Manager. Answers should use the business's words.
- **`field_team_code` is not a key.** Five codes exist under both management
  chains with different managers, so every join must carry `hierarchy_type`
  too. Joining on the code alone silently merges two unrelated territories.
- **Do not work around the row filter.** "My dealers" needs *no* predicate —
  scoping already happened. Adding `WHERE user_email = ...` double-filters to
  zero rows. And an empty result means "nothing visible to you", not "does not
  exist".
- **Which table for which question** — trends from `agg_sales_by_territory_month`,
  dealer health from `agg_dealer_scorecard`, transaction detail from the fact.
- **Dates** — the dataset is fixed and ends 2026-08-31, so relative periods
  anchor on `max(transaction_date)`, never `current_date()`. The fiscal year
  runs April–March.
- **Money** — rupees, formatted in lakh and crore, not raw digits.
- **Do not invent** — there is no target, quota, margin or stock data. Say so
  rather than substituting revenue.

### `parent_path` is not optional in practice

Left unset, the space is created inside the deploying user's private
`.bundle/.../resources` staging folder, whose ACL is owner + admins only.
`CAN_RUN` on the space is then not enough — every other persona gets
*"The resource was not found"*, which reads like a bad URL rather than a
permissions problem. So it is pinned to `/Shared/pidilite_demo`.

Note that `/Shared` grants `users -> CAN_MANAGE` by default, which the space
inherits, so in practice any workspace user can edit this space regardless of
the explicit `CAN_RUN` grants. That is fine for an internal demo workspace and
it changes nothing about what anyone can *see* — the row filters are enforced
on the tables against `current_user()`, not by the space's own permissions. For
a wider audience, put it in a folder with a tighter ACL instead.

`parent_path` is documented as immutable, but changing it on an existing space
was applied in place here (`Updated`, not recreated) with the id and URL
preserved. Do not rely on that; check the deploy plan before assuming it.

### Editing the space definition — three constraints the API enforces

`genie/pidilite_demo.geniespace.json` is validated strictly on create, and the
errors only appear at deploy time, so keep these in mind when editing it:

1. **`data_sources.tables` must be sorted by `identifier`.**
2. **Every list item needs an `id`**: a lowercase 32-hex string with no hyphens.
3. **Every list must be sorted by that `id`** — `text_instructions`,
   `example_question_sqls`, `sample_questions` and `benchmarks.questions` alike.

Constraints 2 and 3 fight each other if the ids are random, because sorting by
a random id scrambles the order things were authored in — and that order is
visible to users in the starter questions. So the ids here encode their intended
position in the first two hex characters, with the remainder random for
uniqueness. Sorting by id then reproduces the authored order. Keep that scheme
when adding an entry, or re-run the reindex rather than pasting a fresh UUID.

### Measuring it instead of trusting it

The space carries 12 **benchmark** questions with their expected SQL, so
accuracy is a number rather than an impression:

```bash
databricks genie genie-create-eval-run   --profile <profile>
databricks genie genie-list-eval-results --profile <profile>
```

`genie/question_bank.md` holds 8–9 questions per persona with the **correct
answer computed from the source CSVs**, plus the six failure modes worth testing
before a client sees them (relative dates, fiscal year, summing the two chains,
self-filtering, dropping `hierarchy_type`, inventing absent metrics).

Note that `databricks genie ask` and the eval runs execute as whoever the CLI
profile authenticates as. A profile that is not in the access map sees zero rows
and every answer looks empty — test personas from their own login.

## Prerequisites

- [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/) **v0.293.1 or later** for
  `databricks bundle` commands. Older CLI builds hit a known Terraform
  provider checksum bug ([databricks/cli#5022](https://github.com/databricks/cli/issues/5022))
  that breaks `bundle deploy`/`bind`. Regular (non-bundle) `databricks` CLI
  commands are unaffected by this.
- A configured CLI profile with access to the target workspace (see
  `~/.databrickscfg` — `databricks auth login --host <workspace-url> --profile <name>`).
- A Unity Catalog catalog named `pidilite_demo` (created via the workspace UI
  on metastores that block catalog creation with default storage over the CLI/API).

## Deploy

```bash
# validate the bundle against a target workspace
databricks bundle validate --profile <profile> -t dev

# deploy pipeline + source files to the workspace
databricks bundle deploy --profile <profile> -t dev

# trigger a pipeline update and stream its progress
databricks bundle run pidilite_demo_pipeline --profile <profile> -t dev
```

To regenerate the seed data (deterministic, same seed → same output):

```bash
python3 data_generation/generate_dims.py
python3 data_generation/generate_sales.py
```

Then land the source files into the bronze volume before running the pipeline:

```bash
databricks fs cp <entity>/<entity>.csv \
  dbfs:/Volumes/pidilite_demo/bronze/landing/<entity>/<entity>.csv \
  --overwrite --profile <profile>
```

Once the pipeline has run and published the gold layer, wire up row-level
security (one-time, or after a gold table gets dropped and recreated) by
running each statement in `sql/01_row_filters.sql` against a SQL warehouse —
note the `ALTER ... SET ROW FILTER` statements must use `ALTER MATERIALIZED
VIEW`, not `ALTER TABLE`, since `gold.py`'s tables are materialized views.
Then work through `sql/02_verify_rls.sql`'s checks before trusting it.

## License

See [LICENSE](LICENSE).
