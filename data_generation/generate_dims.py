"""
Generate synthetic dim tables for the X Industries demo (Faker-seeded from the real client sample).

Order matters: division -> person -> field_team -> customer (FK dependency).

Deterministic: same seed -> same output, so the landing volume can be re-seeded
without row counts moving under the demo.
"""
import csv
import os
import random

from faker import Faker

fake = Faker("en_IN")
random.seed(42)
Faker.seed(42)

# --- knobs -------------------------------------------------------------------

TOTAL_CUSTOMERS = 500

# Dealers are NOT spread evenly across business lines, and getting this wrong
# was the single biggest realism bug in the earlier seed. A consumer business
# sells through a very large dealer network at low unit prices; an industrial
# business sells to comparatively few large accounts. With even spreading,
# Consumer & Bazaar came out with the FEWEST dealers and the smallest revenue -
# backwards for the real organization, and something any sales head spots in
# five seconds. Weighting dealer counts by division fixes the ranking at its
# actual cause rather than inflating prices to compensate.
DIVISION_DEALER_WEIGHT = {
    10: 3.0,   # Consumer & Bazaar      - large dealer network
    20: 1.0,   # Industrial Resins      - few large accounts
    30: 1.2,   # Construction Chemicals
    40: 1.2,   # Waterproofing Solutions
}

# Land the client's own column typo ("Tzxntyoe" for hierarchy_type) in the
# field_team header so silver's RENAME_MAPS demonstrably does work instead of
# being dead config. Silver normalizes it back to hierarchy_type.
USE_CLIENT_TYPO_HEADER = True

# Inject deliberately messy rows, of two distinct kinds:
#   - cleansable  -> silver normalizes them (casing, enum drift, whitespace)
#   - quarantined -> silver tags them and routes them to dim_*_quarantine
# Without these, the quarantine tables come out empty and there is nothing to
# show for the data-quality half of the demo.
INJECT_DIRTY = True

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")
for sub in ("division", "person", "field_team", "customer"):
    os.makedirs(os.path.join(OUT_DIR, sub), exist_ok=True)


def write_csv(entity, header, rows):
    path = os.path.join(OUT_DIR, entity, f"{entity}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# --- dim_division ------------------------------------------------------------

divisions = [
    (10, "Consumer & Bazaar"),
    (20, "Industrial Resins"),
    (30, "Construction Chemicals"),
    (40, "Waterproofing Solutions"),
]

# --- dim_person / dim_field_team --------------------------------------------

ROLES = {
    "master": "Territory/Area Sales Manager",
    "ra1": "Regional/Zonal Sales Manager",
    "ra2": "National Sales Manager",
    "ho": "Head Office",
}

persons = []       # [person_id, person_name, role, division_id, hierarchy_type, user_email]
field_teams = []   # [field_team_code, division_id, hierarchy_type, master_id, ra1_id, ra2_id]
_person_seq = 0
_used_emails = set()


def make_person(role, division_id, hierarchy_type):
    """Append a person, return its id.

    Emails are deduped: access_mapping keys off user_email, so a collision
    would silently merge two people's permissions.
    """
    global _person_seq
    _person_seq += 1
    pid = f"P{_person_seq:03d}"
    name = fake.name()
    base = name.lower().replace(" ", ".").replace("'", "")
    email, suffix = f"{base}@salesdemo.com", 2
    while email in _used_emails:
        email = f"{base}{suffix}@salesdemo.com"
        suffix += 1
    _used_emails.add(email)
    persons.append([pid, name, role, "" if division_id is None else division_id, hierarchy_type, email])
    return pid


# One RA2 (National Sales Manager) per hierarchy_type, shared across divisions
ra2_sales = make_person(ROLES["ra2"], None, "Sales Hierarchy")
ra2_mdi = make_person(ROLES["ra2"], None, "MDI Hierarchy")

_ft_seq = 0
for did, _ in divisions:
    ra1_sales = make_person(ROLES["ra1"], did, "Sales Hierarchy")
    for _ in range(3):
        _ft_seq += 1
        master = make_person(ROLES["master"], did, "Sales Hierarchy")
        field_teams.append([f"WSSTTY{_ft_seq}", did, "Sales Hierarchy", master, ra1_sales, ra2_sales])

    # The MDI line deliberately reuses one of this division's field_team_codes
    # with a *different* management chain - preserved from the real client
    # sample, not an error. This is why (field_team_code, hierarchy_type) is the
    # real key, and why dim_customer must carry hierarchy_type too.
    mdi_code = field_teams[-1][0]
    ra1_mdi = make_person(ROLES["ra1"], did, "MDI Hierarchy")
    master_mdi = make_person(ROLES["master"], did, "MDI Hierarchy")
    field_teams.append([mdi_code, did, "MDI Hierarchy", master_mdi, ra1_mdi, ra2_mdi])

# Head Office - cross-division, not tied to a single division_id
ho_ids = []
for _ in range(3):
    ho_ids.append(make_person(ROLES["ho"], None, "All"))

# --- persona overrides ---------------------------------------------------
# Map real workspace identities onto specific roster rows so RLS can be tested
# and demoed with real logins, not just synthetic @salesdemo.com addresses that
# nobody can authenticate as.
#
# Looked up by (code, chain) rather than by list index. Index arithmetic here is
# what previously made it look like Abhinav managed WSSTTY3 when he manages
# WSSTTY1 - a wrong mapping is invisible in the data and only shows up when
# somebody checks an answer by hand.
#
# Two shapes of override, serving two different demo arguments:
#
#   VERTICAL - one real reporting chain (Master -> RA1 -> RA2 -> Head Office),
#   so containment is directly demonstrable. Both name and email are replaced,
#   because seeing your own name on the dashboard is the point.
#
#   LATERAL - two more Territory Managers, so isolation between PEERS is
#   demonstrable as well. Containment alone invites "well of course the boss
#   sees more"; two managers at the same level with completely disjoint dealers
#   answers the question a client actually asks, which is whether one territory
#   manager can see another's numbers. Here only the EMAIL is replaced - the
#   generated names stay, so the roster still reads like a real sales
#   organization rather than four colleagues wearing every hat.
#
# The pyramid only widens downward: there is exactly one Head Office and one
# National Sales Manager by definition, so extra peer identities are only
# meaningful at territory level.


def _master_of(code, chain):
    return next(f[3] for f in field_teams if f[0] == code and f[2] == chain)


def _ra1_of(code, chain):
    return next(f[4] for f in field_teams if f[0] == code and f[2] == chain)


PERSONA_OVERRIDES = {
    # vertical chain - (name, email)
    _master_of("WSSTTY1", "Sales Hierarchy"): (
        "Abhinav Sarkar", "abhinav.sarkar@jellyfishtechnologies.com"),
    _ra1_of("WSSTTY1", "Sales Hierarchy"): (
        "Akshay Siraswar", "akshay.siraswar@jellyfishtechnologies.com"),
    ra2_sales: ("Shivam Pandey", "shivam.pandey@jellyfishtechnologies.com"),
    ho_ids[0]: ("Neeraj Kumar", "neeraj.kumar@jellyfishtechnologies.com"),

    # lateral peers - (None = keep the generated name, override the email only)
    _master_of("WSSTTY2", "Sales Hierarchy"): (
        None, "akshay.siraswar+tm2@jellyfishtechnologies.com"),   # peer under the same Zonal Manager
    _master_of("WSSTTY3", "MDI Hierarchy"): (
        None, "akshay.siraswar+tm3@jellyfishtechnologies.com"),   # same territory code, other chain
}
for _row in persons:
    if _row[0] in PERSONA_OVERRIDES:
        _name, _email = PERSONA_OVERRIDES[_row[0]]
        if _name:
            _row[1] = _name
        _row[5] = _email

# --- dim_customer ------------------------------------------------------------

INDIAN_CITIES = [
    ("Mumbai", "Maharashtra"), ("Pune", "Maharashtra"), ("Ahmedabad", "Gujarat"),
    ("Surat", "Gujarat"), ("Bengaluru", "Karnataka"), ("Chennai", "Tamil Nadu"),
    ("Hyderabad", "Telangana"), ("Delhi", "Delhi"), ("Jaipur", "Rajasthan"),
    ("Lucknow", "Uttar Pradesh"), ("Kolkata", "West Bengal"), ("Indore", "Madhya Pradesh"),
]
BUSINESS_SUFFIXES = [
    "Hardware Store", "Paints & Hardware", "Traders", "Enterprises",
    "Building Materials", "Agencies",
]

# Every field team gets at least one customer first: a Master persona with zero
# customers means an empty dashboard for that persona in the live demo. The
# remainder is distributed on a Pareto-ish weighting so territories are
# genuinely uneven (the guide asks for uneven; uniform random looks synthetic
# to anyone who knows their own business).
# Dealer *counts* stay moderately uneven - territories are geographic, so no
# territory legitimately has 40x another's dealer count. The heavy Pareto skew
# belongs on revenue instead (see the tier weights in generate_sales.py).
_weighted = field_teams[:]
_weights = [
    random.uniform(0.6, 1.8) * DIVISION_DEALER_WEIGHT.get(f[1], 1.0)
    for f in _weighted
]

assignments = field_teams[:] + random.choices(
    _weighted, weights=_weights, k=TOTAL_CUSTOMERS - len(field_teams)
)
random.shuffle(assignments)

customers = []  # [customer_code, customer_name, division_id, field_team_code, hierarchy_type, city, state]
for code, ft in enumerate(assignments, start=1):
    city, state = random.choice(INDIAN_CITIES)
    customers.append([
        code,
        f"{fake.last_name()} {random.choice(BUSINESS_SUFFIXES)}",
        ft[1],   # division_id
        ft[0],   # field_team_code
        ft[2],   # hierarchy_type  <- without this, customer -> field_team is
                 #                    ambiguous for codes shared by both
                 #                    hierarchies, and RLS would leak a
                 #                    customer to both management chains.
        city,
        state,
    ])

# --- deliberate dirt ---------------------------------------------------------

if INJECT_DIRTY:
    # (a) cleansable - silver should normalize these, not quarantine them
    persons.append(["P901", "person6", "HO", "", "ALL", " Rhea.Menon@SALESDEMO.COM "])
    # Casing/enum drift on a field team. Reuses division 20's real MDI managers
    # rather than inventing one: a manager holding two territories is ordinary,
    # whereas wiring a Head Office person in as a territory Master is not, and
    # it muddies any test of chain separation.
    _mdi_20 = next(f for f in field_teams if f[1] == 20 and f[2] == "MDI Hierarchy")
    field_teams.append(["  wsstty5  ", 20, "MDI", _mdi_20[3], _mdi_20[4], ra2_mdi])
    customers.append([901, "Kapoor Traders", 10, "wsstty1", "sales hierarchy", "  pune  ", "  maharashtra  "])

    # (b) quarantine-bound - each row trips exactly one named check in silver
    persons.append(["P902", "Devansh Rathore", ROLES["master"], 10, "Sales Hierarchy", ""])            # user_email_is_null
    persons.append(["P903", "Meher Bajaj", ROLES["master"], 10, "Sales Hierarchy", "not-an-email"])    # user_email_bad_format
    field_teams.append(["WSSTTZ99", 99, "Sales Hierarchy", "P902", "P902", ra2_sales])                 # division_id_orphan_fk
    customers.append([902, "Orphan Hardware", 10, "WSSTTQ77", "Sales Hierarchy", "Delhi", "Delhi"])    # field_team_code_orphan_fk
    customers.append([903, "", 20, "WSSTTY4", "Sales Hierarchy", "Surat", "Gujarat"])                 # customer_name_is_null

# --- write -------------------------------------------------------------------

write_csv("division", ["division_id", "division_name"], divisions)

write_csv(
    "person",
    ["person_id", "person_name", "role", "division_id", "hierarchy_type", "user_email"],
    persons,
)

write_csv(
    "field_team",
    [
        "field_team_code",
        "division_id",
        "Tzxntyoe" if USE_CLIENT_TYPO_HEADER else "hierarchy_type",
        "master_person_id",
        "ra1_person_id",
        "ra2_person_id",
    ],
    field_teams,
)

write_csv(
    "customer",
    ["customer_code", "customer_name", "division_id", "field_team_code", "hierarchy_type", "city", "state"],
    customers,
)

print(
    f"divisions={len(divisions)} field_teams={len(field_teams)} "
    f"persons={len(persons)} customers={len(customers)} dirty_injected={INJECT_DIRTY}"
)
