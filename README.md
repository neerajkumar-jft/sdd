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
        pidilite_demo.gold.access_map_customer     (entitlements, user_email → customer_code)
        pidilite_demo.gold.access_map_field_team   (entitlements, user_email → field_team_code)
        │  row filters keyed on current_user()
        ▼
        AI/BI Dashboard + Genie space (not yet built)
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

Generation logic and volumes are in `data_generation/generate_dims.py` (dims)
and `data_generation/generate_sales.py` (fact table).

## Repo layout

```
databricks.yml                          # bundle config (workspace, targets)
resources/pidilite_demo.pipeline.yml    # pipeline resource definition
src/pidilite_demo/
├── bronze.py                           # Auto Loader ingestion, one stream per entity
├── silver.py                           # cleansing, canonicalization, quarantine framework
└── gold.py                             # conformed model + derived access maps
sql/
├── 01_row_filters.sql                  # filter functions, ALTER ... SET ROW FILTER, grants
└── 02_verify_rls.sql                   # RLS verification checklist
tests/
└── verify_access_map_logic.py          # offline entitlement-algebra test (no workspace needed)
data_generation/
├── generate_dims.py                    # dim table generator, seeded from client sample
└── generate_sales.py                   # fact table generator
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
