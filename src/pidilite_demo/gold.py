"""
Gold layer: the conformed dimensional model, the serving aggregates, and the
pre-computed access maps that row-level security keys off.

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

Known gap against a production gold layer, worth stating rather than hiding:
there is no effective dating here. The access maps describe who can see what
*now*, so a reorg rescopes history as well as the present - somebody's Q1
number can change because a territory moved in Q3. Production needs
`valid_from`/`valid_to` on the management chain and as-of entitlement, and the
policy behind it ("after a reassignment, whose history is it?") is the client's
business rule, not ours to invent.
"""
import dlt
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

CATALOG = "pidilite_demo"
SILVER = f"{CATALOG}.silver"
GOLD = f"{CATALOG}.gold"

HEAD_OFFICE_ROLE = "Head Office"

# Dormancy threshold, in days without a transaction.
DORMANT_AFTER_DAYS = 180

# Window for the recency measure on the dealer scorecard, in days.
RECENT_WINDOW_DAYS = 90

# Explicit projections, column -> comment.
#
# Two jobs at once. The projection keeps silver's lineage columns
# (_ingested_at, _source_file) and FK helper flags out of the model the
# dashboard sees. The comments are not decoration: Genie reads column comments
# to decide which column answers a question, so a documented `revenue` and an
# undocumented one produce measurably different natural-language answers.
DIM_COLUMNS = {
    "division": {
        "division_id": "Business line identifier.",
        "division_name": "Business line name, e.g. Consumer & Bazaar.",
    },
    "person": {
        "person_id": "Internal identifier for a member of the sales organization.",
        "person_name": "Full name of the salesperson or Head Office user.",
        "role": (
            "Position in the sales organization: Territory/Area Sales Manager, "
            "Regional/Zonal Sales Manager, National Sales Manager, or Head Office."
        ),
        "division_id": "Business line this person belongs to. Null for National Sales Managers and Head Office, who span divisions.",
        "hierarchy_type": "Which management chain this person sits in: 'Sales Hierarchy', 'MDI Hierarchy', or 'All' for Head Office.",
        "user_email": "Login identity. Row-level security matches current_user() against this column.",
    },
    "field_team": {
        "field_team_code": (
            "Sales territory code. NOT unique on its own - the same code can appear "
            "under both management chains, so the real key is "
            "(field_team_code, hierarchy_type)."
        ),
        "division_id": "Business line this territory sells for.",
        "hierarchy_type": "Which management chain this territory reports through: 'Sales Hierarchy' or 'MDI Hierarchy'.",
        "master_person_id": "Territory/Area Sales Manager who owns this territory.",
        "ra1_person_id": "Regional/Zonal Sales Manager the territory manager reports to.",
        "ra2_person_id": "National Sales Manager the regional manager reports to.",
    },
    "customer": {
        "customer_code": "Dealer/retailer identifier.",
        "customer_name": "Dealer or retailer business name.",
        "division_id": "Business line this dealer buys from.",
        "field_team_code": "Sales territory serving this dealer.",
        "hierarchy_type": (
            "Management chain this dealer belongs to. Required alongside "
            "field_team_code, because the code alone matches both chains."
        ),
        "city": "Dealer city.",
        "state": "Indian state the dealer trades in.",
    },
}

FACT_COLUMNS = {
    "transaction_id": "Unique identifier for a single sale.",
    "customer_code": "Dealer the sale was made to.",
    "transaction_date": "Date the sale was booked.",
    "product_category": "Product line sold: Adhesives, Sealants, Construction Chemicals, Art & Craft, or Industrial Resins.",
    "quantity": "Units sold.",
    "revenue": "Sale value in Indian rupees.",
    "salesperson_id": "Territory/Area Sales Manager credited with the sale.",
}

AGG_TERRITORY_MONTH_COLUMNS = {
    "field_team_code": "Sales territory code.",
    "hierarchy_type": "Management chain the territory reports through.",
    "division_id": "Business line.",
    "month": "First day of the calendar month the figures cover.",
    "revenue": "Total sale value in Indian rupees for the territory in that month.",
    "quantity": "Total units sold.",
    "transactions": "Number of sales booked.",
    "active_dealers": "Distinct dealers that bought at least once in the month.",
}

AGG_DEALER_SCORECARD_COLUMNS = {
    "customer_code": "Dealer identifier.",
    "customer_name": "Dealer business name.",
    "field_team_code": "Sales territory serving this dealer.",
    "hierarchy_type": "Management chain this dealer belongs to.",
    "division_id": "Business line the dealer buys from.",
    "city": "Dealer city.",
    "state": "Indian state the dealer trades in.",
    "revenue_total": "Lifetime sale value to this dealer, in Indian rupees.",
    "revenue_recent": f"Sale value in the last {RECENT_WINDOW_DAYS} days of available data.",
    "transactions": "Lifetime number of sales to this dealer.",
    "first_sale": "Date of the dealer's first recorded sale.",
    "last_sale": "Date of the dealer's most recent sale. Null if they have never bought.",
    "days_since_last_sale": "Days between the last sale and the latest date in the data. Null if they have never bought.",
    "is_dormant": f"True when the dealer has not bought in over {DORMANT_AFTER_DAYS} days, or has never bought.",
    "top_category": "Product line this dealer spends the most on.",
    "as_of_date": "Latest transaction date in the dataset - the anchor every recency measure here is relative to.",
}

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


def _documented(df: DataFrame, columns: dict) -> DataFrame:
    """Project the listed columns, attaching each one's comment as column metadata.

    Declaring the comments here rather than applying them with an ALTER
    afterwards keeps the documentation inside the pipeline definition, so it is
    republished on every refresh instead of being wiped by the next full one.
    """
    return df.select(
        *[
            F.col(name).alias(name, metadata={"comment": comment})
            for name, comment in columns.items()
        ]
    )


# ---------------------------------------------------------------------------
# Conformed dimensions + fact
# ---------------------------------------------------------------------------


def _make_dim(entity: str):
    @dlt.table(
        name=f"{GOLD}.dim_{entity}",
        comment=f"Gold: conformed {entity} dimension, business-ready.",
    )
    def _dim():
        return _documented(_silver(f"dim_{entity}"), DIM_COLUMNS[entity])

    return _dim


for _entity in DIM_COLUMNS:
    _make_dim(_entity)


@dlt.table(
    name=f"{GOLD}.fact_sales_transaction",
    comment="Gold: sales transactions at dealer/date/category grain.",
)
def gold_fact_sales_transaction():
    return _documented(_silver("fact_sales_transaction"), FACT_COLUMNS)


# ---------------------------------------------------------------------------
# Serving aggregates
#
# Gold is a consumption layer, not a second copy of silver. These exist so a
# dashboard tile answers from a few hundred pre-computed rows instead of
# scanning the fact on every render, and so Genie has a table whose grain
# already matches the questions people actually ask ("how is each territory
# trending", "which dealers have gone quiet") rather than having to derive that
# grain itself on every attempt.
#
# Both read from silver, like everything else here, so they stay leaves and can
# safely carry their own row filters.
# ---------------------------------------------------------------------------


def _as_of(fact: DataFrame) -> DataFrame:
    """Single-row frame holding the latest transaction date in the data.

    Recency is measured against the data, not the clock: the seed data is fixed,
    so anchoring on current_date would make the dormant flag drift every day the
    demo is shown and quietly invalidate the expected counts.
    """
    return fact.select(F.max("transaction_date").alias("as_of_date"))


@dlt.table(
    name=f"{GOLD}.agg_sales_by_territory_month",
    comment=(
        "Gold: revenue, volume and active dealer count per territory per month. "
        "Serves the trend and territory-comparison views."
    ),
)
def gold_agg_sales_by_territory_month():
    customer = _silver("dim_customer").select(
        "customer_code", "field_team_code", "hierarchy_type", "division_id"
    )
    agg = (
        _silver("fact_sales_transaction")
        .join(customer, "customer_code", "inner")
        .withColumn("month", F.trunc("transaction_date", "month"))
        .groupBy("field_team_code", "hierarchy_type", "division_id", "month")
        .agg(
            F.sum("revenue").alias("revenue"),
            F.sum("quantity").alias("quantity"),
            F.count(F.lit(1)).alias("transactions"),
            F.countDistinct("customer_code").alias("active_dealers"),
        )
    )
    return _documented(agg, AGG_TERRITORY_MONTH_COLUMNS)


@dlt.table(
    name=f"{GOLD}.agg_dealer_scorecard",
    comment=(
        "Gold: one row per dealer with lifetime and recent value, recency and a "
        "dormancy flag. Answers 'which dealers need attention' without a "
        "hand-written query."
    ),
)
def gold_agg_dealer_scorecard():
    fact = _silver("fact_sales_transaction")
    customer = _silver("dim_customer")
    as_of = _as_of(fact)

    lifetime = fact.groupBy("customer_code").agg(
        F.sum("revenue").alias("revenue_total"),
        F.count(F.lit(1)).alias("transactions"),
        F.min("transaction_date").alias("first_sale"),
        F.max("transaction_date").alias("last_sale"),
    )

    recent = (
        fact.crossJoin(as_of)
        .filter(F.datediff("as_of_date", "transaction_date") <= RECENT_WINDOW_DAYS)
        .groupBy("customer_code")
        .agg(F.sum("revenue").alias("revenue_recent"))
    )

    # Highest-revenue category per dealer. Ties break on category name so the
    # result is deterministic across runs.
    top_category = (
        fact.groupBy("customer_code", "product_category")
        .agg(F.sum("revenue").alias("category_revenue"))
        .withColumn(
            "_rank",
            F.row_number().over(
                Window.partitionBy("customer_code").orderBy(
                    F.col("category_revenue").desc(), F.col("product_category").asc()
                )
            ),
        )
        .filter(F.col("_rank") == 1)
        .select("customer_code", F.col("product_category").alias("top_category"))
    )

    # Left joins throughout: a dealer with no sales at all is a real and
    # interesting row on this table, not one to drop.
    scorecard = (
        customer.join(lifetime, "customer_code", "left")
        .join(recent, "customer_code", "left")
        .join(top_category, "customer_code", "left")
        .crossJoin(as_of)
        .withColumn("revenue_total", F.coalesce("revenue_total", F.lit(0).cast("decimal(18,2)")))
        .withColumn("revenue_recent", F.coalesce("revenue_recent", F.lit(0).cast("decimal(18,2)")))
        .withColumn("transactions", F.coalesce("transactions", F.lit(0).cast("bigint")))
        .withColumn("days_since_last_sale", F.datediff("as_of_date", "last_sale"))
        .withColumn(
            "is_dormant",
            F.coalesce(F.col("days_since_last_sale") > DORMANT_AFTER_DAYS, F.lit(True)),
        )
    )
    return _documented(scorecard, AGG_DEALER_SCORECARD_COLUMNS)


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
    name=f"{GOLD}.access_map_person",
    comment=(
        "Gold: flattened roster entitlements (user_email -> person_id). Who each "
        "user can see on the org roster: their own management chain, plus "
        "themselves. Head Office sees everyone."
    ),
)
def gold_access_map_person():
    """One row per (person, other person they may see on the roster).

    Without this, dim_person is unfiltered and every user can read the entire
    org chart - names, roles, divisions, hierarchy_type and email addresses for
    people in chains they have nothing to do with. That surfaced through Genie:
    asked about a National Manager, it volunteered that there are two of them,
    named the one running the other hierarchy, and mentioned that hierarchy
    exists. No revenue leaked, but the structure did.

    Visibility follows the territories you are already entitled to: for each of
    those, you may see its Master, RA1 and RA2. So a Territory Manager sees
    their own chain upward, a Zonal Manager sees their Masters and their own
    National Manager, and neither sees the other management chain at all.

    Self is included explicitly and is NOT optional - the dashboard app reads
    the viewer's own role from dim_person to decide which chart sections to
    show, and a person at the bottom of a chain is not in any team's RA
    columns.
    """
    field_team = _silver("dim_field_team")
    person = _silver("dim_person")

    # The chain above and below each territory the user can see.
    chain = None
    for column, _via in MANAGEMENT_SCOPES:
        part = field_team.select(
            "field_team_code", "hierarchy_type", F.col(column).alias("person_id")
        ).filter(F.col("person_id").isNotNull())
        chain = part if chain is None else chain.unionByName(part)

    via_territory = (
        _field_team_grants()
        .select("user_email", "field_team_code", "hierarchy_type")
        .join(chain, ["field_team_code", "hierarchy_type"], "inner")
        .select("user_email", "person_id")
    )

    roster = person.select("person_id", "user_email").filter(F.col("user_email").isNotNull())

    # Yourself, always.
    myself = roster.select("user_email", "person_id")

    # Head Office spans every division and both chains, so it also spans the
    # roster - including the other Head Office members, who sit in no team's
    # management columns and would otherwise be invisible to each other.
    head_office = (
        person.filter(F.col("role") == HEAD_OFFICE_ROLE)
        .select("user_email")
        .filter(F.col("user_email").isNotNull())
        .crossJoin(roster.select("person_id"))
    )

    return (
        via_territory.unionByName(myself).unionByName(head_office)
        .select(F.lower("user_email").alias("user_email"), "person_id")
        .distinct()
    )


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
