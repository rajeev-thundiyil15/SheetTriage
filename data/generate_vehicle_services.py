"""
Makes a wide test file (18 columns) of vehicle service records, with known planted problems
and duplicate rows.

    uv run --no-project --with-requirements requirements.txt python data/generate_vehicle_services.py
    uv run --no-project --with-requirements requirements.txt python data/generate_vehicle_services.py --rows 5000

Writes data/test_vehicle_services_<rows>.csv (not committed: run this to make it). Its twists:
18 columns, UK number plates with a format rule, dates written day first, an optional
second date, and several cost columns. The rules are in schema/vehicle_services.json and,
in plain English, in data/test_vehicle_services_answer_key.md.
"""
import argparse
import csv
import random
import string
from collections import Counter
from pathlib import Path

MAKES = ["Ford", "Toyota", "VW", "BMW", "Kia", "Tesla", "Nissan"]
FUELS = ["Petrol", "Diesel", "Hybrid", "Electric"]
SERVICES = {"Oil Change": (0.5, 40), "Tyres": (1.0, 180), "Brakes": (2.0, 220), "MOT": (1.0, 0),
            "Full Service": (3.0, 160), "Battery": (0.5, 120)}
TECHS = "Ade Bea Cal Dee Eli Fay Gus Hal Ida Jon Kit Lou Max Ned Ora Pip Raj Sam Tia Viv".split()
PAYMENTS = ["Card", "Cash", "Invoice"]
NOTES = [""] * 12 + ["Wiper", "Bulb", "Recall", "Noise", "Warranty"]
RATE = 60.0  # labour per hour


def _plate() -> str:
    letters = string.ascii_uppercase
    return f"{random.choice(letters)}{random.choice(letters)}{random.randint(10, 75)} " \
           f"{''.join(random.choice(letters) for _ in range(3))}"


def _date() -> tuple[int, int]:
    return random.randint(1, 28), random.randint(1, 12)


def _month_first(date: str) -> str:
    day, month, year = date.split("/")
    return f"{month}/{day}/{year}" if int(day) > 12 else "03/27/2026"


# (column, value maker, what should happen): "fix" the agent may fix on its own; "suggest"
# is flagged with a fix to approve; "flag" is flagged with no safe fix.
PROBLEMS = [
    ("plate", lambda r: r["plate"].lower(), "flag"),                      # lowercase
    ("plate", lambda r: r["plate"].replace(" ", ""), "flag"),             # no space
    ("plate", lambda r: "", "flag"),                                      # blank
    ("make", lambda r: random.choice(["ford", "Toyta", "vw", "Nisan"]), "fix"),
    ("make", lambda r: random.choice(["Skoda", "Unknown"]), "flag"),
    ("fuel", lambda r: random.choice(["petrol", "Disel", "hybrid"]), "fix"),
    ("fuel", lambda r: "LPG", "flag"),
    ("year", lambda r: random.choice(["1998", "2031"]), "flag"),          # outside 2005 to 2026
    ("year", lambda r: "twenty twenty", "flag"),
    ("mileage", lambda r: f"{int(r['mileage']):,}", "suggest"),          # thousands separator
    ("mileage", lambda r: "-" + r["mileage"], "suggest"),                 # minus sign
    ("mileage", lambda r: random.choice(["unknown", "n/k"]), "flag"),
    ("service_date", lambda r: _month_first(r["service_date"]), "suggest"),
    ("service_date", lambda r: "31/04/2026", "suggest"),                  # April 31
    ("service_date", lambda r: "", "flag"),
    ("service_type", lambda r: random.choice(["oil change", "Tyre", "Brakes ", "mot"]), "fix"),
    ("service_type", lambda r: random.choice(["Valet", "Diagnostics"]), "flag"),
    ("technician", lambda r: "", "flag"),
    ("labour_hours", lambda r: random.choice(["two", "three"]), "suggest"),  # number word
    ("labour_hours", lambda r: "15", "flag"),                             # above 12
    ("parts_cost", lambda r: "£" + r["parts_cost"], "suggest"),
    ("parts_cost", lambda r: random.choice(["TBC", "quote"]), "flag"),
    ("labour_cost", lambda r: "-" + r["labour_cost"] if r["labour_cost"] != "0.00" else "-30.00", "suggest"),
    ("total_cost", lambda r: "£" + r["total_cost"], "suggest"),
    ("total_cost", lambda r: "9500.00", "flag"),                          # above 7000
    ("paid", lambda r: random.choice(["yes", "no", "Y"]), "fix"),
    ("paid", lambda r: "Partly", "flag"),
    ("payment_method", lambda r: random.choice(["card", "cash", "invoice"]), "fix"),
    ("payment_method", lambda r: "Credit Card", "suggest"),               # Card is a whole word inside
    ("payment_method", lambda r: "Cheque", "flag"),
    ("email", lambda r: r["email"].replace("@", "", 1), "flag"),
    ("email", lambda r: "", "flag"),
    ("next_due", lambda r: "32/13/2027", "flag"),                         # not a date at all
]


def make(rows: int, problem_rate: float, duplicates: int, seed: int):
    random.seed(seed)
    out = []
    for i in range(rows):
        service = random.choice(list(SERVICES))
        hours, parts = SERVICES[service]
        parts = round(parts * random.choice([0, 0.8, 1, 1, 1.2, 1.5]), 2)
        labour = round(hours * RATE, 2)
        day, month = _date()
        due_day, due_month = _date()
        out.append({
            "service_id": f"SV-{i + 1:06d}",
            "plate": _plate(),
            "make": random.choice(MAKES),
            "fuel": random.choice(FUELS),
            "year": str(random.randint(2005, 2026)),
            "mileage": str(random.randint(1000, 180000)),
            "service_date": f"{day:02d}/{month:02d}/2026",
            "service_type": service,
            "technician": random.choice(TECHS),
            "labour_hours": f"{hours:.1f}",
            "parts_cost": f"{parts:.2f}",
            "labour_cost": f"{labour:.2f}",
            "total_cost": f"{parts + labour:.2f}",
            "paid": random.choice(["Yes", "Yes", "Yes", "No"]),
            "payment_method": random.choice(PAYMENTS),
            "email": f"c{random.randint(1000, 99999)}@mail.co",
            "next_due": random.choice(["", f"{due_day:02d}/{due_month:02d}/2027"]),
            "notes": random.choice(NOTES),
        })

    copied = random.sample(range(rows // 2), duplicates)
    targets = random.sample(range(rows // 2, rows), duplicates)
    for source, target in zip(copied, targets):
        out[target] = dict(out[source], service_id=out[target]["service_id"])
    protected = set(copied) | set(targets)

    planted = Counter()
    for i in range(rows):
        if i in protected or random.random() >= problem_rate:
            continue
        column, maker, outcome = random.choice(PROBLEMS)
        out[i][column] = maker(out[i])
        planted[outcome] += 1
    return out, planted, sorted(t + 2 for t in targets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--problem-rate", type=float, default=0.05)
    parser.add_argument("--duplicates", type=int, default=40)
    parser.add_argument("--seed", type=int, default=18)
    args = parser.parse_args()

    rows, planted, copy_rows = make(args.rows, args.problem_rate, args.duplicates, args.seed)
    path = Path(__file__).parent / f"test_vehicle_services_{args.rows}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path} ({path.stat().st_size / 1e6:.2f} MB, {len(rows):,} rows, {len(rows[0])} columns)")
    print(f"Planted: {planted['fix']:,} the agent may fix, {planted['suggest']:,} to flag with a suggested fix, "
          f"{planted['flag']:,} to flag with no fix, and {len(copy_rows)} duplicate rows")
