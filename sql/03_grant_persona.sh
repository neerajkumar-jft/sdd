#!/usr/bin/env bash
#
# Grant one demo persona everything it needs to actually use the demo.
#
#   ./sql/03_grant_persona.sh <email> [profile]
#
# Why this exists as a script: a persona needs privileges on FIVE separate
# surfaces, and missing any one of them fails in a way that does not point at
# the cause. The warehouse grant was missed once during setup and the symptom
# was "not authorized to use or monitor this SQL Endpoint" from inside Genie,
# which reads like a Genie problem. With six personas that is thirty grants to
# place by hand; doing it by hand is how one goes missing again.
#
# Deliberately NOT granted: SELECT on gold.access_map_*. A user needs EXECUTE
# on the row-filter function, never read access to the entitlement tables -
# otherwise anyone can read everyone's entitlements and work out exactly what
# they are not allowed to see. See 01_row_filters.sql.
#
# Idempotent: re-running for an existing persona is safe, and is the quickest
# way to backfill a grant that was missed.

EMAIL="${1:?usage: $0 <email> [profile]}"
PROFILE="${2:-pidilite}"

CATALOG="pidilite_demo"
SCHEMA="pidilite_demo.gold"
WAREHOUSE_ID="be5dd2cb70eb66ee"
GENIE_SPACE_ID="01f1aaa58a6c1e639422daac2f1a1dd9"
DASHBOARD_ID="01f1aaa04ebf15238fdeb516d2374269"

# Consumption tables only - the access maps are absent on purpose.
TABLES=(
  dim_division
  dim_person
  dim_field_team
  dim_customer
  fact_sales_transaction
  agg_sales_by_territory_month
  agg_dealer_scorecard
)
FUNCTIONS=(can_see_customer can_see_field_team)

fail=0
step() {  # step <label> <command...>
  local label="$1"; shift
  if out=$("$@" 2>&1); then
    printf '  ok    %s\n' "$label"
  else
    printf '  FAIL  %s\n        %s\n' "$label" "$(printf '%s' "$out" | head -2 | tr '\n' ' ')"
    fail=1
  fi
}

uc_grant() {  # uc_grant <securable_type> <full_name> <privilege...>
  local kind="$1" name="$2"; shift 2
  local privs
  privs=$(printf '"%s",' "$@"); privs="[${privs%,}]"
  databricks grants update "$kind" "$name" --profile "$PROFILE" \
    --json "{\"changes\":[{\"principal\":\"$EMAIL\",\"add\":$privs}]}" >/dev/null
}

acl_grant() {  # acl_grant <object_path> <permission_level>
  databricks api patch "/api/2.0/permissions/$1" --profile "$PROFILE" \
    --json "{\"access_control_list\":[{\"user_name\":\"$EMAIL\",\"permission_level\":\"$2\"}]}" >/dev/null
}

echo "Granting $EMAIL (profile: $PROFILE)"

# 1. Compute. Without this the space and dashboard open but every query dies.
step "warehouse CAN_USE" acl_grant "warehouses/$WAREHOUSE_ID" CAN_USE

# 2. Traversal. USE_* does not grant reading anything, only the right to
#    address objects inside.
step "catalog USE_CATALOG" uc_grant CATALOG "$CATALOG" USE_CATALOG
step "schema USE_SCHEMA"   uc_grant SCHEMA  "$SCHEMA"  USE_SCHEMA

# 3. The data itself. Table-level, never schema-level: GRANT SELECT ON SCHEMA
#    would hand over the access maps along with everything else.
for t in "${TABLES[@]}"; do
  step "table SELECT $t" uc_grant TABLE "$SCHEMA.$t" SELECT
done

# 4. The row filters. EXECUTE only - the function reads the entitlement map
#    with its own privileges, so the caller never needs to.
for f in "${FUNCTIONS[@]}"; do
  step "function EXECUTE $f" uc_grant FUNCTION "$SCHEMA.$f" EXECUTE
done

# 5. The two front doors.
step "genie space CAN_RUN" acl_grant "genie/$GENIE_SPACE_ID" CAN_RUN
step "dashboard CAN_RUN"   acl_grant "dashboards/$DASHBOARD_ID" CAN_RUN

if [ "$fail" -eq 0 ]; then
  echo "  all grants applied"
else
  echo "  one or more grants failed - see above" >&2
fi
exit "$fail"
