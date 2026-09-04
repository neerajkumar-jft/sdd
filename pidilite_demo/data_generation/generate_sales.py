"""
Generate fact_sales_transaction for the X Industries demo.

The client's sample carries no transactional figures at all (no revenue,
quantity, product or date) - only master/hierarchy data - so the fact table has
to be invented. It is generated *from* the dim CSVs rather than regenerating its
own hierarchy, so customer -> field team -> salesperson stays consistent with
whatever generate_dims.py last produced.

Shape matters as much as volume here: uniform random noise reads as fake to
anyone who knows their own business. So this models
  - Pareto revenue concentration (a few dealers carry most of the value),
  - per-category seasonality,
  - dealer lifecycle (dormant / churned / newly onboarded).

Run generate_dims.py first. Deterministic: same seed -> same output.
"""
import csv
import os
import random
from datetime import date, timedelta

random.seed(4242)

# --- knobs -------------------------------------------------------------------

WINDOW_END = date(2026, 8, 31)
WINDOW_MONTHS = 18

INJECT_DIRTY = True

BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")
os.makedirs(os.path.join(BASE_DIR, "sales"), exist_ok=True)

# Category mix modeled on the client's real product lines, constrained by what
# each division sells.
DIVISION_CATEGORIES = {
    10: ["Adhesives", "Art & Craft", "Sealants"],              # Consumer & Bazaar
    20: ["Industrial Resins", "Adhesives"],                    # Industrial Resins
    30: ["Construction Chemicals", "Sealants"],                # Construction Chemicals
    40: ["Construction Chemicals", "Sealants"],                # Waterproofing Solutions
}

UNIT_PRICE = {
    "Adhesives": 350,
    "Sealants": 620,
    "Construction Chemicals": 1450,
    "Art & Craft": 180,
    "Industrial Resins": 2100,
}

# Monthly multipliers, index 0 = January.
# NOTE: these are *plausible demo assumptions, not client-confirmed facts* -
# waterproofing/construction demand is modeled as running ahead of the monsoon
# and slowing during it, and consumer/art & craft as lifting for school
# reopening and the festive quarter. Worth validating with X Industries rather than
# presenting as their actual seasonality.
SEASONALITY = {
    "Construction Chemicals": [0.80, 0.85, 1.15, 1.35, 1.40, 1.25, 0.70, 0.65, 0.90, 1.20, 1.15, 0.95],
    "Sealants":               [0.85, 0.90, 1.15, 1.30, 1.30, 1.20, 0.75, 0.70, 0.95, 1.15, 1.10, 0.95],
    "Adhesives":              [0.95, 0.95, 1.05, 1.00, 1.00, 1.05, 0.95, 0.95, 1.15, 1.30, 1.20, 0.95],
    "Art & Craft":            [0.80, 0.80, 0.85, 0.90, 1.00, 1.35, 1.30, 1.00, 1.20, 1.35, 1.15, 0.85],
    "Industrial Resins":      [1.00, 1.10, 1.20, 0.95, 1.00, 1.00, 0.95, 0.95, 1.00, 1.05, 1.05, 1.00],
}

# (share of dealers, orders/month multiplier) - drives the Pareto skew.
TIERS = [("A", 0.10, 15.0), ("B", 0.25, 2.5), ("C", 0.65, 0.5)]
BASE_ORDERS_PER_MONTH = 0.68

# (share of dealers, lifecycle) - "dormant" is what makes
# "which dealers have gone quiet?" a real question on the dashboard.
LIFECYCLES = [("full", 0.77), ("dormant", 0.12), ("churned", 0.05), ("new", 0.06)]

HIERARCHY_CANON = {
    "sales hierarchy": "Sales Hierarchy",
    "saleshierarchy": "Sales Hierarchy",
    "mdi hierarchy": "MDI Hierarchy",
    "mdihierarchy": "MDI Hierarchy",
    "mdi": "MDI Hierarchy",
}


def canon_hierarchy(value):
    key = " ".join(value.strip().lower().split())
    return HIERARCHY_CANON.get(key, value.strip())


def read_csv(entity):
    path = os.path.join(BASE_DIR, entity, f"{entity}.csv")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# --- resolve customer -> salesperson ----------------------------------------

# The field_team header may carry the client's original "Tzxntyoe" typo; accept
# either spelling so this doesn't break when that flag is toggled.
masters = {}
for row in read_csv("field_team"):
    hierarchy = row.get("hierarchy_type") or row.get("Tzxntyoe") or ""
    key = (row["field_team_code"].strip().upper(), canon_hierarchy(hierarchy))
    masters[key] = row["master_person_id"].strip().upper()

customers, unresolved = [], []
for row in read_csv("customer"):
    try:
        code = int(row["customer_code"])
    except (TypeError, ValueError):
        continue
    if not row["customer_name"].strip():
        unresolved.append((code, "blank customer_name"))
        continue
    key = (row["field_team_code"].strip().upper(), canon_hierarchy(row["hierarchy_type"]))
    if key not in masters:
        # Deliberately-orphaned dim rows land here; they are quarantined in
        # silver, so generating sales against them would just be noise.
        unresolved.append((code, f"no master for {key}"))
        continue
    try:
        division_id = int(row["division_id"])
    except (TypeError, ValueError):
        unresolved.append((code, "bad division_id"))
        continue
    customers.append({"code": code, "division_id": division_id, "salesperson_id": masters[key]})

# --- assign tier + lifecycle -------------------------------------------------

random.shuffle(customers)
n = len(customers)

cursor = 0
for _tier, share, multiplier in TIERS:
    take = round(share * n)
    for c in customers[cursor:cursor + take]:
        c["tier"] = _tier
        c["multiplier"] = multiplier
    cursor += take
for c in customers[cursor:]:  # rounding remainder
    c["tier"], c["multiplier"] = "C", 0.5

cursor = 0
for name, share in LIFECYCLES:
    take = round(share * n)
    for c in customers[cursor:cursor + take]:
        c["lifecycle"] = name
    cursor += take
for c in customers[cursor:]:
    c["lifecycle"] = "full"

# --- month window ------------------------------------------------------------


def month_starts(end, months):
    """Oldest-first list of (year, month) covering `months` up to `end`."""
    out = []
    year, month = end.year, end.month
    for _ in range(months):
        out.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


MONTHS = month_starts(WINDOW_END, WINDOW_MONTHS)


def active_window(lifecycle):
    """Inclusive (first, last) month index a dealer transacts in."""
    last = WINDOW_MONTHS - 1
    if lifecycle == "dormant":
        # nothing in the trailing 6 months - that is the whole point
        return 0, random.randint(WINDOW_MONTHS - 12, WINDOW_MONTHS - 7)
    if lifecycle == "churned":
        return 0, random.randint(3, WINDOW_MONTHS // 2)
    if lifecycle == "new":
        return random.randint(WINDOW_MONTHS - 9, WINDOW_MONTHS - 6), last
    return 0, last


def days_in(year, month):
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


# --- generate ----------------------------------------------------------------

rows = []
seq = 0
for c in customers:
    first_m, last_m = active_window(c["lifecycle"])
    categories = DIVISION_CATEGORIES[c["division_id"]]

    for m_idx in range(first_m, last_m + 1):
        year, month = MONTHS[m_idx]
        for category in categories:
            season = SEASONALITY[category][month - 1]
            expected = BASE_ORDERS_PER_MONTH * c["multiplier"] * season / len(categories)
            count = int(expected) + (1 if random.random() < (expected % 1) else 0)

            for _ in range(count):
                seq += 1
                day = random.randint(1, days_in(year, month))
                qty = max(1, int(random.gauss(24, 12) * (2.2 if c["tier"] == "A" else 1.0)))
                price = UNIT_PRICE[category] * random.uniform(0.88, 1.12)
                rows.append([
                    f"T{seq:06d}",
                    c["code"],
                    date(year, month, day).isoformat(),
                    category,
                    qty,
                    round(qty * price, 2),
                    c["salesperson_id"],
                ])

rows.sort(key=lambda r: r[2])
for i, r in enumerate(rows, start=1):  # renumber so ids run in date order
    r[0] = f"T{i:06d}"

# --- deliberate dirt ---------------------------------------------------------

if INJECT_DIRTY and rows:
    sample = rows[len(rows) // 2]
    rows.append(["T900001", 99999, "2026-05-14", "Adhesives", 12, 4620.00, sample[6]])          # orphan customer_code
    rows.append(["T900002", sample[1], "2026-13-45", "Sealants", 8, 5120.00, sample[6]])         # unparseable date
    rows.append(["T900003", sample[1], "2026-06-02", "Sealants", -5, 3100.00, sample[6]])        # negative quantity
    rows.append(["T900004", sample[1], "2026-06-11", "adhesives", 15, "", sample[6]])            # null revenue + casing drift

# --- write -------------------------------------------------------------------

with open(os.path.join(BASE_DIR, "sales", "sales.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow([
        "transaction_id", "customer_code", "transaction_date",
        "product_category", "quantity", "revenue", "salesperson_id",
    ])
    w.writerows(rows)

# --- summary (sanity-check the shape, not just the count) --------------------

clean = [r for r in rows if not str(r[0]).startswith("T9000")]
by_customer = {}
for r in clean:
    by_customer[r[1]] = by_customer.get(r[1], 0.0) + float(r[5])
ranked = sorted(by_customer.values(), reverse=True)
total = sum(ranked) or 1.0
top20 = sum(ranked[: max(1, len(ranked) // 5)]) / total

print(f"transactions={len(rows)} (clean={len(clean)}, dirty={len(rows) - len(clean)})")
print(f"customers_with_sales={len(by_customer)} of {len(customers)} resolved "
      f"({len(unresolved)} dim rows skipped: {[u[1] for u in unresolved]})")
print(f"date_range={clean[0][2]} .. {clean[-1][2]}")
print(f"revenue_total=Rs {total/1e7:.2f} Cr | top 20% of dealers = {top20*100:.0f}% of revenue")
print("lifecycle:", {k: sum(1 for c in customers if c['lifecycle'] == k) for k, _ in LIFECYCLES})
