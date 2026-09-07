import dlt
from pyspark.sql import DataFrame
from pyspark.sql import functions as F

# bronze tables are read via dlt.read() by their short name (raw_division etc.)
# since bronze.py and silver.py are libraries on the SAME pipeline - this is
# what gives DLT the dependency edge for automatic DAG resolution and
# staleness tracking, instead of a disconnected spark.table() batch read.

# ---------------------------------------------------------------------------
# Canonical value maps - extend these as new spelling/casing variants show up
# in real data. Generic mechanism, dataset-specific config.
# ---------------------------------------------------------------------------

HIERARCHY_TYPE_MAP = {
    "SALES HIERARCHY": "Sales Hierarchy",
    "SALESHIERARCHY": "Sales Hierarchy",
    "MDI HIERARCHY": "MDI Hierarchy",
    "MDIHIERARCHY": "MDI Hierarchy",
    "MDI": "MDI Hierarchy",
    "ALL": "All",
}

ROLE_MAP = {
    "TERRITORY/AREA SALES MANAGER": "Territory/Area Sales Manager",
    "TERRITORY SALES MANAGER": "Territory/Area Sales Manager",
    "AREA SALES MANAGER": "Territory/Area Sales Manager",
    "REGIONAL/ZONAL SALES MANAGER": "Regional/Zonal Sales Manager",
    "REGIONAL SALES MANAGER": "Regional/Zonal Sales Manager",
    "ZONAL SALES MANAGER": "Regional/Zonal Sales Manager",
    "NATIONAL SALES MANAGER": "National Sales Manager",
    "HEAD OFFICE": "Head Office",
    "HO": "Head Office",
}

# raw source column (lower_snake_case) -> canonical target column, per entity
PRODUCT_CATEGORY_MAP = {
    "ADHESIVES": "Adhesives",
    "SEALANTS": "Sealants",
    "CONSTRUCTION CHEMICALS": "Construction Chemicals",
    "CONSTRUCTION CHEMICAL": "Construction Chemicals",
    "ART & CRAFT": "Art & Craft",
    "ART AND CRAFT": "Art & Craft",
    "ART&CRAFT": "Art & Craft",
    "INDUSTRIAL RESINS": "Industrial Resins",
    "INDUSTRIAL RESIN": "Industrial Resins",
}

RENAME_MAPS = {
    "division": {},
    "person": {},
    "field_team": {"tzxntyoe": "hierarchy_type"},  # known typo in the client sample
    "customer": {},
    "sales_transaction": {},
}

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

# ---------------------------------------------------------------------------
# Generic cleaning helpers - not tied to this dataset's specific errors.
# ---------------------------------------------------------------------------


def normalize_column_names(df: DataFrame) -> DataFrame:
    """lower_snake_case every incoming column so rename maps don't have to
    guess original casing/spacing (survives arbitrary header drift)."""
    for c in df.columns:
        clean = c.strip().lower().replace(" ", "_")
        if clean != c:
            df = df.withColumnRenamed(c, clean)
    return df


def apply_rename_map(df: DataFrame, rename_map: dict) -> DataFrame:
    """Fix mislabeled/typo'd source columns, e.g. {'tzxntyoe': 'hierarchy_type'}."""
    for src, dst in rename_map.items():
        if src in df.columns:
            df = df.withColumnRenamed(src, dst)
    return df


def trim_all_strings(df: DataFrame) -> DataFrame:
    """Strip leading/trailing whitespace from every string column."""
    for field in df.schema.fields:
        if field.dataType.typeName() == "string":
            df = df.withColumn(field.name, F.trim(F.col(field.name)))
    return df


def upper_col(df: DataFrame, column: str) -> DataFrame:
    return df.withColumn(column, F.upper(F.col(column))) if column in df.columns else df


def title_col(df: DataFrame, column: str) -> DataFrame:
    return df.withColumn(column, F.initcap(F.col(column))) if column in df.columns else df


def _try_cast(df: DataFrame, column: str, sql_type: str) -> DataFrame:
    """ANSI-safe cast: an unparseable value becomes NULL and is caught by a
    named expectation, instead of failing the pipeline update.

    Plain `.cast()` yields NULL only while ANSI mode is off; with it on, a
    malformed date or a non-numeric revenue raises and takes the whole update
    down with it. The seed data now carries exactly those rows deliberately
    (see INJECT_DIRTY in the generators), so the cast has to be the
    non-throwing one rather than relying on a session setting."""
    if column not in df.columns:
        return df
    return df.withColumn(column, F.expr(f"try_cast(`{column}` as {sql_type})"))


def safe_int(df: DataFrame, column: str) -> DataFrame:
    """Cast to INT without blowing up the run on a bad value - becomes NULL,
    caught by the null-check expectation instead of crashing the pipeline."""
    return _try_cast(df, column, "int")


def safe_decimal(df: DataFrame, column: str, precision: int = 18, scale: int = 2) -> DataFrame:
    """Cast to DECIMAL without blowing up the run on a bad value - same
    non-crashing pattern as safe_int."""
    return _try_cast(df, column, f"decimal({precision},{scale})")


def safe_date(df: DataFrame, column: str) -> DataFrame:
    """Cast to DATE without blowing up the run on a bad/unparseable value."""
    return _try_cast(df, column, "date")


def canonicalize(df: DataFrame, column: str, canonical_map: dict) -> DataFrame:
    """
    Normalize free-text enum drift (casing/underscores/hyphens) to one
    canonical value. Unmatched values pass through unchanged instead of
    being nulled out, so unseen-but-valid variants aren't silently destroyed
    - they just won't be normalized until someone adds them to the map.
    """
    if column not in df.columns:
        return df
    lookup_key = F.upper(F.regexp_replace(F.trim(F.col(column)), r"[\s_-]+", " "))
    mapping_expr = F.create_map([F.lit(x) for pair in canonical_map.items() for x in pair])
    return df.withColumn(column, F.coalesce(mapping_expr[lookup_key], F.col(column)))


def mark_fk_valid(df: DataFrame, fk_cols, ref_df: DataFrame, ref_cols, flag_col: str) -> DataFrame:
    """Referential check via join (not a hardcoded id list) - flags rows whose
    FK doesn't exist in the referenced table. Null FK isn't flagged here;
    that's the job of a separate not-null check so failure reasons don't overlap.
    flag_col must start with '__' - flag_quarantine drops every such internal
    scratch column before publish, so it never leaks into the published table.

    fk_cols/ref_cols accept either a single column name or a list, because a
    field team's real identity is (field_team_code, hierarchy_type): the code
    alone repeats across the Sales and MDI chains with different managers, so a
    single-column join matches a customer against BOTH management chains."""
    assert flag_col.startswith("__"), "mark_fk_valid flag_col must start with '__' so it gets dropped before publish"
    fk_cols = [fk_cols] if isinstance(fk_cols, str) else list(fk_cols)
    ref_cols = [ref_cols] if isinstance(ref_cols, str) else list(ref_cols)
    aliases = [f"__ref_key_{i}" for i in range(len(ref_cols))]

    ref = ref_df.select(*[F.col(c).alias(a) for c, a in zip(ref_cols, aliases)]).distinct()

    join_cond = None
    for fk, alias in zip(fk_cols, aliases):
        eq = df[fk] == ref[alias]
        join_cond = eq if join_cond is None else (join_cond & eq)

    any_fk_null = None
    for fk in fk_cols:
        is_null = F.col(fk).isNull()
        any_fk_null = is_null if any_fk_null is None else (any_fk_null | is_null)

    return (
        df.join(ref, join_cond, "left")
        .withColumn(flag_col, F.col(aliases[0]).isNotNull() | any_fk_null)
        .drop(*aliases)
    )


def flag_quarantine(df: DataFrame, checks: list) -> DataFrame:
    """
    checks: list of (reason: str, invalid_condition: Column).
    Generic pass/fail tagging - works for null checks, FK checks, format
    checks, whatever. Rows aren't dropped or the run failed (expect_or_fail
    is a footgun for a live demo); they're tagged so both a clean table and
    an inspectable quarantine table can be published from the same pass.
    """
    case_exprs = [F.when(cond, F.lit(reason)) for reason, cond in checks]
    df = df.withColumn("_quarantine_reasons", F.array(*case_exprs))
    df = df.withColumn("_quarantine_reasons", F.expr("filter(_quarantine_reasons, x -> x is not null)"))
    df = df.withColumn("_is_valid", F.size(F.col("_quarantine_reasons")) == 0)
    # drop internal scratch columns (e.g. mark_fk_valid's '__*_fk_valid' flags)
    # so they never leak into the published table.
    scratch_cols = [c for c in df.columns if c.startswith("__")]
    return df.drop(*scratch_cols) if scratch_cols else df


def publish(table_name: str, clean_fn, comment: str):
    """Runs clean_fn once, publishes a valid table + a paired quarantine table.
    table_name is the full silver table name (e.g. 'dim_division',
    'fact_sales_transaction') - callers control the prefix, dim_/fact_/etc.
    ponytail: recomputes cleaning per call site (valid vs quarantine) - fine
    at demo volumes; cache the staging DF if this needs to scale to millions
    of fact rows."""

    @dlt.table(name=f"pidilite_demo.silver.{table_name}", comment=f"Silver: cleansed, validated {comment}.")
    def _valid():
        return clean_fn().filter("_is_valid").drop("_is_valid", "_quarantine_reasons")

    @dlt.table(
        name=f"pidilite_demo.silver.{table_name}_quarantine",
        comment=f"Silver: {comment} that failed validation - kept for inspection, not dropped silently.",
    )
    def _quarantine():
        return clean_fn().filter("NOT _is_valid")


# ---------------------------------------------------------------------------
# Per-entity cleaning - each is generic-helper calls + a config, not one-off
# string-literal patches.
# ---------------------------------------------------------------------------


def _clean_division() -> DataFrame:
    df = dlt.read("pidilite_demo.bronze.raw_division")
    df = normalize_column_names(df)
    df = apply_rename_map(df, RENAME_MAPS["division"])
    df = trim_all_strings(df)
    df = safe_int(df, "division_id")
    df = df.dropDuplicates(["division_id"])
    return flag_quarantine(
        df,
        [
            ("division_id_is_null", F.col("division_id").isNull()),
            ("division_name_is_null", F.col("division_name").isNull() | (F.col("division_name") == "")),
        ],
    )


def _clean_person() -> DataFrame:
    df = dlt.read("pidilite_demo.bronze.raw_person")
    df = normalize_column_names(df)
    df = apply_rename_map(df, RENAME_MAPS["person"])
    df = trim_all_strings(df)
    df = title_col(df, "person_name")  # fixes casing drift like "person6" vs "Person1"
    df = canonicalize(df, "role", ROLE_MAP)
    df = canonicalize(df, "hierarchy_type", HIERARCHY_TYPE_MAP)
    df = safe_int(df, "division_id")
    if "user_email" in df.columns:
        df = df.withColumn("user_email", F.lower(F.trim(F.col("user_email"))))
    df = df.dropDuplicates(["person_id"])
    return flag_quarantine(
        df,
        [
            ("person_id_is_null", F.col("person_id").isNull()),
            ("person_name_is_null", F.col("person_name").isNull() | (F.col("person_name") == "")),
            ("role_is_null", F.col("role").isNull()),
            ("user_email_is_null", F.col("user_email").isNull() | (F.col("user_email") == "")),
            ("user_email_bad_format", ~F.col("user_email").rlike(EMAIL_RE)),
        ],
    )


def _clean_field_team() -> DataFrame:
    df = dlt.read("pidilite_demo.bronze.raw_field_team")
    df = normalize_column_names(df)
    df = apply_rename_map(df, RENAME_MAPS["field_team"])
    df = trim_all_strings(df)
    for c in ["field_team_code", "master_person_id", "ra1_person_id", "ra2_person_id"]:
        df = upper_col(df, c)
    df = canonicalize(df, "hierarchy_type", HIERARCHY_TYPE_MAP)
    df = safe_int(df, "division_id")
    # field_team_code legitimately repeats across hierarchy_type (Sales vs MDI
    # can share a code with a different mgmt chain) - dedupe key must be composite.
    df = df.dropDuplicates(["field_team_code", "hierarchy_type"])

    division_ref = _clean_division().filter("_is_valid")
    df = mark_fk_valid(df, "division_id", division_ref, "division_id", "__division_fk_valid")

    return flag_quarantine(
        df,
        [
            ("field_team_code_is_null", F.col("field_team_code").isNull()),
            ("division_id_is_null", F.col("division_id").isNull()),
            ("hierarchy_type_is_null", F.col("hierarchy_type").isNull()),
            ("master_person_id_is_null", F.col("master_person_id").isNull()),
            ("division_id_orphan_fk", ~F.col("__division_fk_valid")),
        ],
    )


def _clean_customer() -> DataFrame:
    df = dlt.read("pidilite_demo.bronze.raw_customer")
    df = normalize_column_names(df)
    df = apply_rename_map(df, RENAME_MAPS["customer"])
    df = trim_all_strings(df)
    df = safe_int(df, "customer_code")
    df = safe_int(df, "division_id")
    df = upper_col(df, "field_team_code")
    # A customer belongs to (field_team_code, hierarchy_type), never to the code
    # alone. The client sample leaves this ambiguous and the fact table can live
    # with that by defaulting to one chain, but entitlement cannot: resolving a
    # dealer against both chains hands it to two different Masters, and
    # row-level security would expose it to both without erroring.
    df = canonicalize(df, "hierarchy_type", HIERARCHY_TYPE_MAP)
    df = title_col(df, "city")
    df = title_col(df, "state")
    df = df.dropDuplicates(["customer_code"])

    field_team_ref = _clean_field_team().filter("_is_valid")
    df = mark_fk_valid(
        df,
        ["field_team_code", "hierarchy_type"],
        field_team_ref,
        ["field_team_code", "hierarchy_type"],
        "__field_team_fk_valid",
    )

    return flag_quarantine(
        df,
        [
            ("customer_code_is_null", F.col("customer_code").isNull()),
            ("customer_name_is_null", F.col("customer_name").isNull() | (F.col("customer_name") == "")),
            ("field_team_code_is_null", F.col("field_team_code").isNull()),
            ("hierarchy_type_is_null", F.col("hierarchy_type").isNull() | (F.col("hierarchy_type") == "")),
            ("field_team_key_orphan_fk", ~F.col("__field_team_fk_valid")),
        ],
    )


def _clean_sales_transaction() -> DataFrame:
    df = dlt.read("pidilite_demo.bronze.raw_sales_transaction")
    df = normalize_column_names(df)
    df = apply_rename_map(df, RENAME_MAPS["sales_transaction"])
    df = trim_all_strings(df)
    df = canonicalize(df, "product_category", PRODUCT_CATEGORY_MAP)
    df = safe_int(df, "customer_code")
    df = safe_date(df, "transaction_date")
    df = safe_decimal(df, "quantity", precision=12, scale=2)
    df = safe_decimal(df, "revenue", precision=18, scale=2)
    if "salesperson_id" in df.columns:
        df = upper_col(df, "salesperson_id")
    df = df.dropDuplicates(["transaction_id"])

    customer_ref = _clean_customer().filter("_is_valid")
    df = mark_fk_valid(df, "customer_code", customer_ref, "customer_code", "__customer_fk_valid")

    person_ref = _clean_person().filter("_is_valid")
    df = mark_fk_valid(df, "salesperson_id", person_ref, "person_id", "__salesperson_fk_valid")

    return flag_quarantine(
        df,
        [
            ("transaction_id_is_null", F.col("transaction_id").isNull()),
            ("customer_code_is_null", F.col("customer_code").isNull()),
            ("transaction_date_is_null", F.col("transaction_date").isNull()),
            ("quantity_not_positive", F.col("quantity").isNull() | (F.col("quantity") <= 0)),
            ("revenue_not_positive", F.col("revenue").isNull() | (F.col("revenue") <= 0)),
            ("customer_code_orphan_fk", ~F.col("__customer_fk_valid")),
            ("salesperson_id_orphan_fk", ~F.col("__salesperson_fk_valid")),
        ],
    )


publish("dim_division", _clean_division, "division rows")
publish("dim_person", _clean_person, "person/roster rows")
publish("dim_field_team", _clean_field_team, "field team rows")
publish("dim_customer", _clean_customer, "customer rows")
publish("fact_sales_transaction", _clean_sales_transaction, "sales transaction rows")
