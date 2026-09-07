# X Industries Sales Hierarchy Data Platform

A Unity Catalog-governed data pipeline that ingests and conforms a field-sales
organization's data — divisions, the sales management hierarchy, field teams,
the customer/dealer network, and sales transactions against that network — into
a clean, validated star schema, then enforces **row-level security** over it so
every user sees only the customers their place in the hierarchy entitles them
to. Built on synthetic data modelled after the client's real sample.

## Status

| Layer | Code | Run in the workspace |
|---|---|---|
| Data generation (dims + sales fact) | ✅ | — |
| Bronze — Auto Loader ingestion | ✅ | ✅ |
| Silver — cleansing, validation, quarantine | ✅ | ✅ |
| Gold — conformed model, serving aggregates, access maps | ✅ | ❌ |
| Row-level security — filter functions, grants | ✅ | ❌ |
| Lakebase OLTP comments | ❌ | ❌ |
| Salesforce sync job | ❌ | ❌ |
| AI/BI Dashboard + Genie space | ❌ | ❌ |

> ⚠️ **Gold and the row filters have never executed against Spark or Unity
> Catalog.** They pass an offline logic check (`tests/verify_access_map_logic.py`)
> but that proves the entitlement algebra only. Treat gold as unverified until
> `sql/02_verify_rls.sql` checks 1–4 pass.

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
        │  canonicalization, ANSI-safe type casting, dedupe, join-based
        │  referential integrity checks against upstream silver tables
        ▼
Silver: pidilite_demo.silver.dim_*/fact_*             (cleansed, validated)
        pidilite_demo.silver.dim_*/fact_*_quarantine  (rows failing validation, held for review)
        │  explicit projections, plus the entitlement maps derived from the
        │  management chain already present on dim_field_team
        ▼
Gold:   pidilite_demo.gold.dim_*/fact_sales_transaction   (conformed model)
        pidilite_demo.gold.agg_sales_by_territory_month  (287 rows, serving)
        pidilite_demo.gold.agg_dealer_scorecard          (121 rows, serving)
        pidilite_demo.gold.access_map_customer           (847 rows, entitlement)
        pidilite_demo.gold.access_map_field_team         (119 rows, entitlement)
        │  row filters keyed on current_user()
        ▼
        AI/BI Dashboard + Genie space (not yet built)
```

Bronze, silver and gold run as **one pipeline** with three source files
(`src/pidilite_demo/bronze.py`, `silver.py`, `gold.py`), so Lakeflow resolves
the dependency DAG and staleness tracking automatically end to end —
`fact_sales_transaction`'s cleansing depends on `dim_customer` and `dim_person`
already being validated, and the pipeline sequences that on its own. Every
entity publishes a clean table alongside a paired quarantine table, so bad
records are visible and inspectable rather than silently dropped or failing the
whole pipeline run.

## Data model

- **`dim_division`** — top-level business line (e.g. Consumer & Bazaar).
- **`dim_person`** — the field-sales org roster (Territory/Area Sales Manager
  → Regional/Zonal Sales Manager → National Sales Manager → Head Office),
  keyed by `user_email`, which is what row-level security matches
  `current_user()` against.
- **`dim_field_team`** — a sales territory with its Master/RA1/RA2 management
  chain. A `field_team_code` can legitimately appear under both
  `hierarchy_type = 'Sales Hierarchy'` and `'MDI Hierarchy'`, each with its
  own management chain.
- **`dim_customer`** — the dealer/retailer network served by each field team.
  Carries `hierarchy_type` alongside `field_team_code`, because a customer
  belongs to the *pair*, never to the code alone — see below.
- **`fact_sales_transaction`** — individual sales transactions against that
  customer network (`customer_code`, `transaction_date`, `product_category`,
  `quantity`, `revenue`, `salesperson_id`), attributed to the Territory/Area
  Sales Manager of the customer's field team. Absent from the client sample
  entirely, so invented outright — modelled with Pareto revenue concentration,
  per-category seasonality and dealer lifecycle (dormant / churned / newly
  onboarded) so the shape reads as a real business rather than uniform noise.
  The seasonality assumptions are *plausible, not client-confirmed* — flag them
  rather than presenting them as the client's actual curve.

### Why `field_team_code` alone is not a key

Five codes in the generated data appear under both management chains with
different Masters. The client's own sample has the same ambiguity, and the fact
table can live with it by defaulting to one chain when attributing a
salesperson. **Entitlement cannot.** Joining a customer to its field team on the
code alone matches it against *both* chains, so one dealer resolves to two
different Territory Managers and row-level security hands it to both — with no
error raised. Every entitlement therefore carries `hierarchy_type`, and
`mark_fk_valid` in `silver.py` takes composite keys for exactly this reason.

## What gold carries

Nine tables, in three groups:

- **Conformed model** — `dim_division`, `dim_person`, `dim_field_team`,
  `dim_customer`, `fact_sales_transaction`. Explicit projections, so silver's
  lineage columns (`_ingested_at`, `_source_file`) and FK helper flags never
  reach the model a dashboard sees.
- **Serving aggregates** — `agg_sales_by_territory_month` (revenue, volume and
  active dealer count per territory per month) and `agg_dealer_scorecard` (one
  row per dealer with lifetime and recent value, recency, a dormancy flag and
  top category). Gold is a consumption layer, not a second copy of silver: a
  dashboard tile should answer from a few hundred pre-computed rows rather than
  scanning the fact on every render, and Genie answers far more reliably from a
  table whose grain already matches the question ("how is each territory
  trending", "which dealers have gone quiet") than from one it has to derive
  that grain out of on every attempt.
- **Entitlement** — `access_map_customer`, `access_map_field_team`. See below.

Every gold column carries a **comment**, declared in the pipeline via column
metadata rather than applied by an `ALTER` afterwards, so a full refresh
republishes them instead of wiping them. These are not decoration: Genie reads
column comments to choose which column answers a question, so a documented
`revenue` and an undocumented one produce measurably different answers.

Recency on the aggregates is anchored to `as_of_date` — the latest transaction
in the data — not to `current_date`. The seed data is fixed, so anchoring on the
clock would make the dormancy figures drift every day the demo is shown.

### Known gaps against a production gold layer

Stated rather than hidden, because the client's reporting team will find them:

- **No effective dating.** The access maps describe who can see what *now*, so a
  reorg rescopes history as well as the present — somebody's Q1 figure can
  change because a territory moved in Q3. Production needs
  `valid_from`/`valid_to` on the management chain and as-of entitlement, and the
  rule behind it ("after a reassignment, whose history is it?") is the client's
  business decision, not ours to invent. This is the flip side of the
  self-healing behaviour below, and worth raising with them directly.
- **No surrogate keys or SCD Type 2** on the dimensions. Natural keys only.
- **`access_map_customer` does not scale as-is.** Flattening to
  (user × dealer) is fine at 847 rows and would not be at the real
  organization's size. The territory-grain map is the one that scales; at
  production volume the customer grain becomes a query-time join, a group
  membership check, or a tag-based ABAC policy.
- **No PII classification or column masking**, no partitioning/clustering
  strategy, and no gold-level reconciliation assertions.

## Row-level security

Entitlements are flattened once into `gold.access_map_*` so the row filter is a
cheap `EXISTS` probe rather than a recursive walk up the management chain on
every query. Two rules hold the design together:

- **Gold tables are leaf nodes.** Every read in `gold.py` comes from *silver*,
  never from another gold table. Row filters are attached to gold tables, and
  the pipeline's own service identity is not in the access map — so a gold→gold
  read would see zero rows and silently publish an empty table. Reading silver
  makes that impossible by construction rather than by remembering not to do it.
- **The maps are derived, never hand-maintained.** They are built from the
  management chain already on `dim_field_team`, so a promotion or a territory
  reassignment rescopes every dashboard on the next pipeline run, with no
  permission ticket and no edited table. A hand-kept map drifts from the org
  chart, and drift in an access map is a security bug that never raises an error.

```
dim_field_team.master_person_id  →  Territory/Area Sales Manager  ┐
dim_field_team.ra1_person_id     →  Regional/Zonal Sales Manager  ├→  access_map_field_team
dim_field_team.ra2_person_id     →  National Sales Manager        │            │
dim_person WHERE role='Head Office'  ×  every territory           ┘            │  join on
                                                                               │  (field_team_code,
                                                                               │   hierarchy_type)
                                                                               ↓
                                                                        access_map_customer
```

`dim_person` and `dim_division` are deliberately left **unfiltered** — Genie
needs the roster to answer "who manages this territory", and an org chart is not
normally confidential per-territory. Confirm that with the client; the reasoning
and the alternative are recorded inline in `sql/01_row_filters.sql`.

## Repo layout

```
databricks.yml                          # bundle config (workspace, targets)
resources/pidilite_demo.pipeline.yml    # pipeline resource definition
src/pidilite_demo/
├── bronze.py                           # Auto Loader ingestion, one stream per entity
├── silver.py                           # cleansing, canonicalization, quarantine framework
└── gold.py                             # conformed model + derived access maps
sql/
├── 01_row_filters.sql                  # filter functions, ALTER, grants
└── 02_verify_rls.sql                   # the four load-bearing checks + demo queries
tests/
└── verify_access_map_logic.py          # offline entitlement-algebra check, no workspace needed
data_generation/
├── generate_dims.py                    # dimension generator, seeded from the client sample
└── generate_sales.py                   # fact generator (reads the dim CSVs)
sample_data/                            # generated source files, landed into the bronze volume
├── division/  ├── person/  ├── field_team/  ├── customer/  └── sales_transaction/
```

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

## Regenerate the seed data

Deterministic — same seed, same output. Dimensions first; the fact generator
reads the dimension CSVs so the customer → field team → salesperson chain stays
consistent with whatever was last generated.

```bash
python3 data_generation/generate_dims.py    # dims (FK order matters)
python3 data_generation/generate_sales.py   # then the fact table
```

Both generators deliberately inject a handful of messy rows (`INJECT_DIRTY`),
split between *cleansable* (casing, enum and whitespace drift that silver
normalizes) and *quarantine-bound* (null email, malformed email, orphan FKs,
unparseable date, non-positive quantity). Without them the `*_quarantine` tables
come out empty and there is nothing to show for the data-quality half of the
work. `generate_dims.py` also lands the client's original `Tzxntyoe` column
typo in the field_team header, so silver's rename map demonstrably does work
instead of being dead config.

## Deploy

```bash
# land the source files into the bronze volume
for e in division person field_team customer sales_transaction; do
  databricks fs cp "sample_data/$e/$e.csv" \
    "dbfs:/Volumes/pidilite_demo/bronze/landing/$e/$e.csv" \
    --overwrite --profile <profile>
done

databricks bundle validate --profile <profile> -t dev
databricks bundle deploy   --profile <profile> -t dev
databricks bundle run pidilite_demo_pipeline --profile <profile> -t dev
```

**Gotcha:** Auto Loader tracks files by path, so overwriting a CSV in place is
*not* re-ingested by an incremental update — and `dim_customer` has gained
`hierarchy_type`. After re-seeding, force a full re-read:

```bash
databricks bundle run pidilite_demo_pipeline --full-refresh-all --profile <profile> -t dev
```

## Apply and verify row-level security

Run `sql/01_row_filters.sql` once gold has published, then work through
`sql/02_verify_rls.sql` **before** building anything on top — a permission bug
found after the dashboard exists is indistinguishable from a dashboard bug.

Check 2 is the load-bearing one: the filters are attached by an external `ALTER`
while Lakeflow owns the gold tables, so confirm they survive a `bundle deploy`
plus a pipeline update. If they do not, declare the filter inside the pipeline's
own table definition, or move it onto consumption views over gold — do not just
re-run the script after every deploy.

The entitlement algebra can be checked with no workspace at all:

```bash
python3 tests/verify_access_map_logic.py
```

It asserts containment (Territory ⊆ Zonal ⊆ National ⊆ Head Office), that no
customer resolves to both management chains, that every real Master has at least
one customer (an empty dashboard for a persona kills a live demo), and that a
roster row with no usable email receives no entitlement. It proves none of Unity
Catalog's enforcement — that is what `02_verify_rls.sql` is for.

## Known gap: demo identities

The generated roster uses `@salesdemo.com` addresses, which are **not real
workspace users** — nobody can authenticate as one, so `current_user()` will
never return one. Until real loginable identities exist (or service principals
stand in), the persona checks run only through Unity Catalog's
query-as-another-user path, and a live persona-switching walkthrough is not yet
demonstrable.

## License

See [LICENSE](LICENSE).
