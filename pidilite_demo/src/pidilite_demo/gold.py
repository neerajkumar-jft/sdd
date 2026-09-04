"""
Gold layer: the conformed dimensional model, plus the pre-computed access maps
that row-level security keys off.

Two rules this file follows deliberately.

1. Gold tables are LEAF nodes. Every read here comes from SILVER, never from
   another gold table. Row filters are declared on gold tables, and the
   pipeline's own service identity is not in the access map - so if the pipeline
   read a filtered gold table downstream it would see zero rows and silently
   publish an empty table. Reading silver makes that impossible by construction
   rather than by remembering not to do it.

2. The access maps are DERIVED, never hand-maintained. When someone is promoted
   or a territory is reassigned, the map regenerates on the next pipeline run
   and every dashboard rescopes itself - no permission tickets, no manual edits.
   A hand-kept mapping table drifts from the org chart, and drift in an access
   map is a silent security bug: the wrong person keeps seeing the wrong data
   and nothing errors.
"""
import dlt
from pyspark.sql import DataFrame
from pyspark.sql import functions as F

CATALOG = "pidilite_demo"
SILVER = f"{CATALOG}.silver"
GOLD = f"{CATALOG}.gold"

HEAD_OFFICE_ROLE = "Head Office"

# Explicit projections: silver carries lineage columns (_ingested_at,
# _source_file) and FK helper flags (_*_fk_valid), and none of that should reach
# the model the dashboard and Genie see.
DIM_COLUMNS = {
    "division": ["division_id", "division_name"],
    "person": [
        "person_id", "person_name", "role",
        "division_id", "hierarchy_type", "user_email",
    ],
    "field_team": [
        "field_team_code", "division_id", "hierarchy_type",
        "master_person_id", "ra1_person_id", "ra2_person_id",
    ],
    "customer": [
        "customer_code", "customer_name", "division_id",
        "field_team_code", "hierarchy_type", "city", "state",
    ],
}

FACT_COLUMNS = [
    "transaction_id", "customer_code", "transaction_date",
    "product_category", "quantity", "revenue", "salesperson_id",
]

# Which management-chain column on dim_field_team grants access, and the role
# that grant represents. Driven off dim_field_team's own columns so the
# entitlement is always the org chart itself, not a second copy of it.
MANAGEMENT_SCOPES = [
    ("master_person_id", "Territory/Area Sales Manager"),
    ("ra1_person_id", "Regional/Zonal Sales Manager"),
    ("ra2_person_id", "National Sales Manager"),
]


def _silver(table: str) -> DataFrame:
    """Read a silver table by fully-qualified name.

    dlt.read (not spark.table) so Lakeflow resolves the dependency edge and
    staleness tracking across bronze -> silver -> gold in one pipeline.
    """
    return dlt.read(f"{SILVER}.{table}")


# ---------------------------------------------------------------------------
# Conformed dimensions + fact
# ---------------------------------------------------------------------------


def _make_dim(entity: str):
    @dlt.table(
        name=f"{GOLD}.dim_{entity}",
        comment=f"Gold: conformed {entity} dimension, business-ready.",
    )
    def _dim():
        return _silver(f"dim_{entity}").select(*DIM_COLUMNS[entity])

    return _dim


for _entity in DIM_COLUMNS:
    _make_dim(_entity)


@dlt.table(
    name=f"{GOLD}.fact_sales_transaction",
    comment="Gold: sales transactions at customer/date/category grain.",
)
def gold_fact_sales_transaction():
    return _silver("fact_sales_transaction").select(*FACT_COLUMNS)


# ---------------------------------------------------------------------------
# Access maps - the entitlement layer row-level security reads
# ---------------------------------------------------------------------------


def _field_team_grants() -> DataFrame:
    """One row per (person, field team) the person is entitled to see.

    A field team's identity is (field_team_code, hierarchy_type), never the code
    alone: the same code exists under both the Sales and MDI chains with
    different managers. Carrying hierarchy_type through every grant is what
    keeps a territory's two management lines separate.

    `via_role` is not needed by the row filter, but it makes the reverse lookup
    - "who can see this row, and in what capacity?" - a single query, which is
    both an audit answer and the most convincing way to show the filter is real.
    """
    field_team = _silver("dim_field_team")
    person = _silver("dim_person")

    grants = None
    for column, via_role in MANAGEMENT_SCOPES:
        part = field_team.select(
            F.col(column).alias("person_id"),
            "field_team_code",
            "hierarchy_type",
            F.lit(via_role).alias("via_role"),
        ).filter(F.col("person_id").isNotNull())
        grants = part if grants is None else grants.unionByName(part)

    # Head Office sits outside any single division and sees everything. Kept in
    # the map rather than special-cased in the row filter, so there is exactly
    # one entitlement mechanism to reason about and HO rescopes itself like
    # everyone else. At production scale these rows multiply (every HO user x
    # every customer) - that is where IS_ACCOUNT_GROUP_MEMBER('head_office') in
    # the filter earns its place instead. The UDF keeps that escape hatch.
    head_office = (
        person.filter(F.col("role") == HEAD_OFFICE_ROLE)
        .select("person_id")
        .crossJoin(field_team.select("field_team_code", "hierarchy_type").distinct())
        .withColumn("via_role", F.lit(HEAD_OFFICE_ROLE))
    )
    grants = grants.unionByName(head_office)

    # Inner join on the roster: a person with no resolvable user_email has no
    # identity to match current_user() against, so they get no entitlement.
    # Quarantined roster rows drop out here by construction.
    return (
        grants.join(person.select("person_id", "user_email"), "person_id", "inner")
        .select("user_email", "person_id", "via_role", "field_team_code", "hierarchy_type")
        .distinct()
    )


@dlt.table(
    name=f"{GOLD}.access_map_field_team",
    comment=(
        "Gold: flattened territory entitlements (user_email -> field team). "
        "Derived from the management chain on dim_field_team - regenerates when "
        "the org chart changes."
    ),
)
def gold_access_map_field_team():
    return _field_team_grants()


@dlt.table(
    name=f"{GOLD}.access_map_customer",
    comment=(
        "Gold: flattened customer entitlements (user_email -> customer_code), "
        "pre-computed so the row filter is a cheap EXISTS lookup instead of a "
        "recursive hierarchy walk on every query."
    ),
)
def gold_access_map_customer():
    # The composite join is the whole point: joining on field_team_code alone
    # would match a dealer against BOTH management chains and hand it to two
    # different Masters.
    customer = _silver("dim_customer").select(
        "customer_code", "field_team_code", "hierarchy_type"
    )
    return (
        _field_team_grants()
        .join(customer, ["field_team_code", "hierarchy_type"], "inner")
        .select("user_email", "person_id", "via_role", "customer_code")
        .distinct()
    )
