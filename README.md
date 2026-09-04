# Pidilite Sales Hierarchy Data Platform

A Unity Catalog-governed data pipeline that ingests and conforms Pidilite's
field-sales organization data — divisions, the sales management hierarchy,
field teams, and the customer/dealer network — into a clean, validated set of
dimension tables ready for downstream access control, analytics, and
reporting.

## Architecture

A single [Lakeflow Declarative Pipeline](https://docs.databricks.com/aws/en/ldp/),
deployed as a [Databricks Asset Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/),
implementing a medallion architecture across multiple schemas in one Unity
Catalog catalog:

```
Source files (division, person, field_team, customer)
        │
        ▼
Volume: pidilite_demo.bronze.landing/{division,person,field_team,customer}/
        │  Auto Loader (cloudFiles), one incremental stream per entity
        ▼
Bronze: pidilite_demo.bronze.raw_*        (raw, schema-preserved, no transformation)
        │  column rename/normalization, whitespace trimming, enum
        │  canonicalization, safe type casting, dedupe, referential
        │  integrity checks
        ▼
Silver: pidilite_demo.silver.dim_*             (cleansed, validated)
        pidilite_demo.silver.dim_*_quarantine  (rows failing validation, held for review)
```

Bronze and silver run as **one pipeline** with two source files
(`src/pidilite_demo/bronze.py`, `src/pidilite_demo/silver.py`), so Lakeflow
resolves the dependency DAG and staleness tracking automatically end to end.
Every entity publishes a clean table alongside a paired quarantine table, so
bad records are visible and inspectable rather than silently dropped or
failing the whole pipeline run.

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

Generation logic and volumes are in `pidilite_demo/data_generation/generate_dims.py`.

## Repo layout

```
pidilite_demo/
├── databricks.yml                          # bundle config (workspace, targets)
├── resources/pidilite_demo.pipeline.yml    # pipeline resource definition
├── src/pidilite_demo/
│   ├── bronze.py                           # Auto Loader ingestion, one stream per entity
│   └── silver.py                           # cleansing, canonicalization, quarantine framework
├── data_generation/
│   └── generate_dims.py                    # synthetic data generator, seeded from client sample
└── sample_data/                            # generated source files, landed into the bronze volume
    ├── division/  ├── person/  ├── field_team/  └── customer/
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
python3 pidilite_demo/data_generation/generate_dims.py
```

Then land the source files into the bronze volume before running the pipeline:

```bash
databricks fs cp <entity>/<entity>.csv \
  dbfs:/Volumes/pidilite_demo/bronze/landing/<entity>/<entity>.csv \
  --profile <profile>
```

## License

See [LICENSE](LICENSE).
