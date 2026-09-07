"""
Generate synthetic dim tables for the Pidilite demo (Faker-seeded from the real client sample).
Order matters: division -> person -> field_team -> customer (FK dependency).
"""
import csv
import os
import random

from faker import Faker

fake = Faker("en_IN")
random.seed(42)
Faker.seed(42)

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")
for sub in ("division", "person", "field_team", "customer", "sales_transaction"):
    os.makedirs(os.path.join(OUT_DIR, sub), exist_ok=True)

# ---------- dim_division ----------
divisions = [
    (10, "Consumer & Bazaar"),
    (20, "Industrial Resins"),
    (30, "Construction Chemicals"),
    (40, "Waterproofing Solutions"),
]

with open(os.path.join(OUT_DIR, "division", "division.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["division_id", "division_name"])
    for did, name in divisions:
        w.writerow([did, name])

# ---------- dim_field_team ----------
# 4 field-team "slots" per division; one slot per division is deliberately
# duplicated under MDI Hierarchy (mirrors the real sample: WSSTTY1 appeared
# under both Sales Hierarchy and MDI Hierarchy with different management chains)
ROLES = {
    "master": "Territory/Area Sales Manager",
    "ra1": "Regional/Zonal Sales Manager",
    "ra2": "National Sales Manager",
    "ho": "Head Office",
}

persons = []          # (person_id, person_name, role, division_id, hierarchy_type, user_email)
field_teams = []       # (field_team_code, division_id, hierarchy_type, master_id, ra1_id, ra2_id)
person_counter = 1

def make_person(role, division_id, hierarchy_type):
    global person_counter
    pid = f"P{person_counter:03d}"
    person_counter += 1
    name = fake.name()
    email = f"{name.lower().replace(' ', '.')}@pidilitedemo.com"
    persons.append((pid, name, role, division_id if division_id is not None else "", hierarchy_type, email))
    return pid

# One RA2 (NSM) per hierarchy_type, shared across all divisions
ra2_sales = make_person(ROLES["master"].replace("Territory/Area", "National"), None, "Sales Hierarchy")
# fix role text properly
persons[-1] = (persons[-1][0], persons[-1][1], ROLES["ra2"], "", "Sales Hierarchy", persons[-1][5])

ra2_mdi = make_person("tmp", None, "MDI Hierarchy")
persons[-1] = (persons[-1][0], persons[-1][1], ROLES["ra2"], "", "MDI Hierarchy", persons[-1][5])

field_team_seq = 1
for did, _ in divisions:
    # 3 Sales Hierarchy field teams + 1 MDI Hierarchy field team per division
    ra1_sales = make_person("tmp", did, "Sales Hierarchy")
    persons[-1] = (persons[-1][0], persons[-1][1], ROLES["ra1"], did, "Sales Hierarchy", persons[-1][5])

    for _ in range(3):
        code = f"WSSTTY{field_team_seq}"
        master_id = make_person("tmp", did, "Sales Hierarchy")
        persons[-1] = (persons[-1][0], persons[-1][1], ROLES["master"], did, "Sales Hierarchy", persons[-1][5])
        field_teams.append((code, did, "Sales Hierarchy", master_id, ra1_sales, ra2_sales))
        field_team_seq += 1

    # MDI Hierarchy field team - reuses the SAME code as one of the sales
    # field teams just created for this division, but a different mgmt chain
    mdi_code = field_teams[-1][0]  # duplicate the last sales code for this division
    ra1_mdi = make_person("tmp", did, "MDI Hierarchy")
    persons[-1] = (persons[-1][0], persons[-1][1], ROLES["ra1"], did, "MDI Hierarchy", persons[-1][5])
    master_mdi = make_person("tmp", did, "MDI Hierarchy")
    persons[-1] = (persons[-1][0], persons[-1][1], ROLES["master"], did, "MDI Hierarchy", persons[-1][5])
    field_teams.append((mdi_code, did, "MDI Hierarchy", master_mdi, ra1_mdi, ra2_mdi))

# Head Office people - cross-division, not tied to one division_id
for _ in range(3):
    make_person("tmp", None, "All")
    persons[-1] = (persons[-1][0], persons[-1][1], ROLES["ho"], "", "All", persons[-1][5])

with open(os.path.join(OUT_DIR, "person", "person.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["person_id", "person_name", "role", "division_id", "hierarchy_type", "user_email"])
    for row in persons:
        w.writerow(row)

with open(os.path.join(OUT_DIR, "field_team", "field_team.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["field_team_code", "division_id", "hierarchy_type", "master_person_id", "ra1_person_id", "ra2_person_id"])
    for row in field_teams:
        w.writerow(row)

# ---------- dim_customer ----------
INDIAN_CITIES = [
    ("Mumbai", "Maharashtra"), ("Pune", "Maharashtra"), ("Ahmedabad", "Gujarat"),
    ("Surat", "Gujarat"), ("Bengaluru", "Karnataka"), ("Chennai", "Tamil Nadu"),
    ("Hyderabad", "Telangana"), ("Delhi", "Delhi"), ("Jaipur", "Rajasthan"),
    ("Lucknow", "Uttar Pradesh"), ("Kolkata", "West Bengal"), ("Indore", "Madhya Pradesh"),
]
BUSINESS_SUFFIXES = ["Hardware Store", "Paints & Hardware", "Traders", "Enterprises", "Building Materials", "Agencies"]

customers = []  # (customer_code, customer_name, division_id, field_team_code, city, state)
with open(os.path.join(OUT_DIR, "customer", "customer.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["customer_code", "customer_name", "division_id", "field_team_code", "city", "state"])
    for code in range(1, 121):
        ft = random.choice(field_teams)
        city, state = random.choice(INDIAN_CITIES)
        name = f"{fake.last_name()} {random.choice(BUSINESS_SUFFIXES)}"
        row = (code, name, ft[1], ft[0], city, state)
        customers.append(row)
        w.writerow(row)

# ---------- fact_sales_transaction ----------
# Not present in the client sample at all - fully invented on top of the
# hierarchy, since the actual ask is a sales dashboard, not just a roster.
import datetime

PRODUCT_UNIT_PRICE = {
    "Adhesives": 120,
    "Sealants": 250,
    "Construction Chemicals": 180,
    "Art & Craft": 60,
    "Industrial Resins": 500,
}

# a field_team_code can map to two rows (Sales + MDI hierarchy) with different
# Masters; dim_customer doesn't carry hierarchy_type (same ambiguity as the
# client sample), so default to the Sales Hierarchy master when both exist.
master_by_field_team = {}
for code, _did, htype, master_id, _ra1, _ra2 in field_teams:
    if code not in master_by_field_team or htype == "Sales Hierarchy":
        master_by_field_team[code] = master_id

TXN_COUNT = 4000
WINDOW_DAYS = 450  # ~15 months
END_DATE = datetime.date(2026, 9, 4)

with open(os.path.join(OUT_DIR, "sales_transaction", "sales_transaction.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["transaction_id", "customer_code", "transaction_date", "product_category", "quantity", "revenue", "salesperson_id"])
    for i in range(1, TXN_COUNT + 1):
        cust = random.choice(customers)
        category = random.choice(list(PRODUCT_UNIT_PRICE))
        quantity = random.randint(10, 500)
        unit_price = PRODUCT_UNIT_PRICE[category]
        revenue = round(quantity * unit_price * random.uniform(0.9, 1.1), 2)
        txn_date = END_DATE - datetime.timedelta(days=random.randint(0, WINDOW_DAYS))
        salesperson_id = master_by_field_team.get(cust[3], "")
        w.writerow([f"TXN{i:06d}", cust[0], txn_date.isoformat(), category, quantity, revenue, salesperson_id])

print(f"divisions={len(divisions)} field_teams={len(field_teams)} persons={len(persons)} customers=120 sales_transactions={TXN_COUNT}")
