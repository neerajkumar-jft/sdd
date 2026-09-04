# X Industries Capability Demo

Synthetic-data build for a time-boxed Databricks capability demo for X
Industries — proving row-level security, an AI/BI dashboard, a Genie space,
Lakebase OLTP comments, and a Salesforce sync job, entirely on generated data
modeled after the client's real sample. This is explicitly **not** the
production build; see the companion Use Case document for the two-phase
model.

## Status

| Layer | Status |
|---|---|
| Data generation (Faker, seeded from client sample) | ✅ Done |
| Bronze (Auto Loader ingestion) | ✅ Done |
| Silver (cleansing, validation, quarantine) | ✅ Done |
| Gold (conformed dims + derived access maps) | ✅ Code written |
| Row-level security (filter functions + grants) | ✅ SQL written |
| `fact_sales_transaction` (generator + bronze + silver) | ✅ Done |
| Lakebase OLTP comments | ⏳ Not started |
| Salesforce sync | ⏳ Not started |
| AI/BI Dashboard + Genie space | ⏳ Not started |

> ⚠️ **Only bronze has actually run in the workspace.** Silver, gold, the sales
> fact and the row filters exist as code and pass an offline logic check
> (`pidilite_demo/tests/verify_access_map_logic.py`), but have never been
> executed against Spark or Unity Catalog. Treat every row below bronze as
> unverified until `sql/02_verify_rls.sql` checks 1–4 pass.

## Architecture

One [Lakeflow Declarative Pipeline](https://docs.databricks.com/aws/en/ldp/)
(formerly DLT), deployed as a [Databricks Asset
Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/), publishing to
multiple schemas in a single Unity Catalog catalog:

```
CSV (Faker-generated, seeded from the client sample)
        │
        ▼
Volume: pidilite_demo.bronze.landing/{division,person,field_team,customer}/
        │  Auto Loader (cloudFiles), one stream per entity
        ▼
Bronze: pidilite_demo.bronze.raw_*        (all STRING, no transformation)
        │  generic cleansing: rename map, trim, canonicalize enums,
        │  safe/try casts, dedupe, join-based FK checks (composite where the
        │  key is composite)
        ▼
Silver: pidilite_demo.silver.dim_*        (cleansed + validated)
        pidilite_demo.silver.fact_sales_transaction
        pidilite_demo.silver.*_quarantine   (rows that failed validation)
        │
        ▼
Gold:   pidilite_demo.gold.dim_* / fact_sales_transaction
        pidilite_demo.gold.access_map_customer     (847 rows)
        pidilite_demo.gold.access_map_field_team   (119 rows)
        │  row filters keyed on current_user()
        ▼
        AI/BI Dashboard + Genie space (not yet built)
```

### Row-level security

Entitlements are **flattened once** into `gold.access_map_*` so the row filter
is a cheap `EXISTS` lookup rather than a recursive walk up the management chain
on every query. Two rules hold the design together:

- **Gold tables are leaf nodes.** Every read in `gold.py` comes from *silver*,
  never from another gold table. Row filters are attached to gold tables, and
  the pipeline's own service identity is not in the access map — so a gold→gold
  read would see zero rows and silently publish an empty table. Reading silver
  makes that impossible by construction rather than by remembering not to do it.
- **The maps are derived, never hand-maintained.** They are built from the
  management chain already on `dim_field_team`, so a promotion or territory
  reassignment rescopes every dashboard on the next pipeline run, with no
  permission tickets. A hand-kept map drifts from the org chart, and drift in an
  access map is a silent security bug.

`field_team_code` alone is **not** a key — the same code exists under both the
Sales and MDI chains with different managers, so every entitlement carries
`hierarchy_type` alongside it. `dim_person` and `dim_division` are deliberately
left unfiltered (see the reasoning inline in `sql/01_row_filters.sql`).

Bronze and silver run as **one pipeline** with two source files
(`src/pidilite_demo/bronze.py`, `src/pidilite_demo/silver.py`) so Lakeflow
resolves the dependency DAG and staleness tracking automatically, instead of
manually sequencing separate pipelines. Silver splits every entity into a
`dim_*` table and a paired `dim_*_quarantine` table rather than failing the
whole pipeline on a bad row (`expect_or_fail` is a footgun for a live demo).

## Data model

Four dimension tables, modeled on the client's real sample and X Industries'
actual field-sales org structure:

- **`dim_division`** — top-level business line (e.g. Consumer & Bazaar).
- **`dim_person`** — internal sales org roster (Territory/Area Sales Manager
  → Regional/Zonal Sales Manager → National Sales Manager → Head Office).
  Carries `user_email`, the key row-level security will be built on.
- **`dim_field_team`** — a territory, with its Master/RA1/RA2 management
  chain. The same `field_team_code` can legitimately appear twice, once under
  `hierarchy_type = 'Sales Hierarchy'` and once under `'MDI Hierarchy'`, each
  with a different management chain — this is preserved from the real sample,
  not an error.
- **`dim_customer`** — dealers/retailers being sold to (never log in, no
  `user_email`). Carries `hierarchy_type` alongside `field_team_code`: a
  customer belongs to a *(field_team_code, hierarchy_type)* pair, never to the
  code alone. Without it, a dealer under a code shared by both chains resolves
  to two different Masters and row-level security would expose it to both.
- **`fact_sales_transaction`** — invented outright; the client's sample carries
  no revenue, quantity, product or date at all. Modeled with Pareto revenue
  concentration, per-category seasonality, and dealer lifecycle (dormant /
  churned / newly onboarded) so the shape reads as a real business rather than
  uniform noise. Seasonality assumptions are *plausible, not client-confirmed* —
  flag them rather than presenting them as X Industries' actual curve.

See `pidilite_demo/data_generation/generate_dims.py` for exact volumes and
generation logic.

## Repo layout

```
pidilite_demo/
├── databricks.yml                          # bundle config (workspace, targets)
├── resources/pidilite_demo.pipeline.yml    # pipeline resource definition
├── src/pidilite_demo/
│   ├── bronze.py                           # Auto Loader ingestion, one stream per entity
│   ├── silver.py                           # generic cleansing + quarantine framework
│   └── gold.py                             # conformed model + derived access maps
├── sql/
│   ├── 01_row_filters.sql                  # filter functions, ALTER, grants
│   └── 02_verify_rls.sql                   # the 4 checks + demo queries
├── tests/
│   └── verify_access_map_logic.py          # offline entitlement-algebra check
├── data_generation/
│   ├── generate_dims.py                    # Faker-based dim generator
│   └── generate_sales.py                   # fact generator (reads the dim CSVs)
└── sample_data/                            # generated CSVs, landed into the bronze volume
    ├── division/  ├── person/  ├── field_team/
    ├── customer/  └── sales/
```

## Prerequisites

- [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/) **v0.293.1 or later** for
  `databricks bundle` commands. Older CLI builds hit a known Terraform
  provider checksum bug ([databricks/cli#5022](https://github.com/databricks/cli/issues/5022))
  that breaks `bundle deploy`/`bind`. Regular (non-bundle) `databricks` CLI
  commands are unaffected by this.
- A configured CLI profile with access to the target workspace (see
  `~/.databrickscfg` — `databricks auth login --host <workspace-url> --profile <name>`).
- Unity Catalog must allow catalog creation via the workspace UI in this
  account (`pidilite_demo` catalog was created there — the CLI/API path is
  blocked for default-storage catalogs on this metastore).

## Deploy

```bash
cd pidilite_demo

# validate the bundle against a target workspace
databricks bundle validate --profile <profile> -t dev

# deploy pipeline + source files to the workspace
databricks bundle deploy --profile <profile> -t dev

# trigger a pipeline update and stream its progress
databricks bundle run pidilite_demo_pipeline --profile <profile> -t dev
```

To regenerate the seed data (deterministic, same seed → same output):

```bash
python3 pidilite_demo/data_generation/generate_dims.py   # dims first (FK order)
python3 pidilite_demo/data_generation/generate_sales.py  # then the fact table
```

Both generators deliberately inject a handful of messy rows (`INJECT_DIRTY`),
split between *cleansable* (casing/enum/whitespace drift that silver
normalizes) and *quarantine-bound* (null email, bad email format, orphan FKs,
unparseable date, non-positive quantity). Without them the `*_quarantine`
tables come out empty and there is nothing to show for the data-quality half of
the demo. `generate_dims.py` also lands the client's original `Tzxntyoe` column
typo in the field_team header, so silver's rename map demonstrably does work.

Then land the CSVs into the bronze volume before running the pipeline:

```bash
for e in division person field_team customer sales; do
  databricks fs cp "pidilite_demo/sample_data/$e/$e.csv" \
    "dbfs:/Volumes/pidilite_demo/bronze/landing/$e/$e.csv" \
    --overwrite --profile <profile>
done
```

**Gotcha:** Auto Loader tracks files by path, so overwriting a CSV in place is
*not* re-ingested by an incremental update — and the dim schemas have changed
(`dim_customer` gained `hierarchy_type`). After re-seeding, run a full refresh
so bronze re-reads everything from scratch:

```bash
databricks bundle run pidilite_demo_pipeline --full-refresh-all --profile <profile> -t dev
```

## Row-level security setup

After the pipeline has published gold at least once:

```bash
# 1. filter functions + attach + grants
#    (run the file in a SQL editor, or split it into statements for the API)

# 2. verify BEFORE building anything on top - a permission bug found later is
#    indistinguishable from a dashboard bug
#    sql/02_verify_rls.sql, checks 1-4
```

Check 2 is the load-bearing one: the filters are attached by an external
`ALTER` while Lakeflow owns the gold tables, so confirm they survive a
`bundle deploy` + pipeline update. If they do not, declare the filter inside the
pipeline's own table definition, or move it onto consumption views over gold —
do not just re-run the script after every deploy.

The entitlement algebra can be checked with no workspace at all:

```bash
python3 pidilite_demo/tests/verify_access_map_logic.py
```

It asserts containment (Master ⊆ RA1 ⊆ RA2 ⊆ Head Office), that no customer
resolves to both management chains, that every real Master has at least one
customer (an empty dashboard for a persona kills the live demo), and that a
roster row with no usable email gets no entitlement. It proves none of Unity
Catalog's enforcement — that is what `02_verify_rls.sql` is for.

## License

See [LICENSE](LICENSE).
