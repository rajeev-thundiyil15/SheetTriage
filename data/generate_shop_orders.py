"""
Makes a large shop orders test file with known planted problems and duplicate rows.

    uv run --no-project --with-requirements requirements.txt python data/generate_shop_orders.py
    uv run --no-project --with-requirements requirements.txt python data/generate_shop_orders.py --rows 5000

Writes data/test_shop_orders_<rows>.csv (not committed: run this to make it) and prints what
was planted. The same seed always gives the same file. It uses the same schema as the
40-row file, schema/shop_orders.json.
"""
import argparse
import csv
import random
from collections import Counter
from pathlib import Path

FIRST = ("Aisha Bruno Carmen Diego Emma Farah Gavin Hana Ivan Jade Kofi Lena Marco Nina Omar Priya Quinn "
         "Rosa Sven Tara Uma Victor Wren Xavier Yara Zane Amir Bella Chen Dara Eli Fiona Goran Ines Jonah "
         "Kira Luis Maya Niko Olga").split()
LAST = ("Khan Silva Lopez Moreau Novak Haddad Reid Sato Petrov Okafor Becker Costa Dubois Evans Fischer "
        "Garcia Hughes Ito Jensen Kowalski Larsen Mensah Nguyen Ortiz Patel Quinn Rossi Schmidt Torres Weber").split()
PRODUCTS = {"Laptop": 1199.00, "Monitor": 249.99, "Keyboard": 79.50, "Mouse": 29.99, "Headset": 89.00, "Webcam": 64.95}
STATUSES = ["Pending", "Shipped", "Delivered", "Cancelled"]
PAYMENTS = ["Card", "PayPal", "Bank Transfer"]
GIFTS = ["", "", "", "", "Happy birthday!", "Congratulations!", "Thank you"]

# Planted problems: (column, value maker, what should happen). "fix" means the agent may
# fix it on its own; "flag" means it must be flagged; "suggest" means it's flagged and
# the Fix flagged cells tab offers a fix to approve.
PROBLEMS = [
    ("email", lambda r: r["email"].rsplit(".", 1)[0], "flag"),                    # no dot after the @
    ("email", lambda r: "", "flag"),                                              # blank
    ("email", lambda r: random.choice(["TBC", "n/a", "unknown"]), "flag"),         # placeholder
    ("customer", lambda r: "", "flag"),                                           # blank name
    ("unit_price", lambda r: "€" + r["unit_price"], "suggest"),                   # currency sign
    ("unit_price", lambda r: "-" + r["unit_price"], "suggest"),                   # minus sign
    ("unit_price", lambda r: random.choice(["call for price", "TBD", "free"]), "flag"),
    ("unit_price", lambda r: "4500.00", "flag"),                                  # above 3000
    ("quantity", lambda r: random.choice(["two", "three", "five"]), "suggest"),   # number word
    ("quantity", lambda r: "0", "flag"),                                          # below 1
    ("quantity", lambda r: "25", "flag"),                                         # above 20
    ("quantity", lambda r: "1.5", "suggest"),                                     # not whole
    ("order_date", lambda r: _day_first(r["order_date"]), "suggest"),             # day first, day > 12
    ("order_date", lambda r: "02/29/2026", "suggest"),                            # not a leap year
    ("order_date", lambda r: "", "flag"),                                         # blank
    ("order_date", lambda r: _year_first(r["order_date"]), "fix"),                # same date, other format
    ("status", lambda r: random.choice(["shiped", "Delivred", "pending", "CANCELLED"]), "fix"),
    ("status", lambda r: random.choice(["Returned", "Refunded"]), "flag"),
    ("payment_method", lambda r: random.choice(["paypal", "card", "bank transfer"]), "fix"),
    ("payment_method", lambda r: "Credit Card", "suggest"),
    ("payment_method", lambda r: random.choice(["Bitcoin", "Cash"]), "flag"),
    ("product", lambda r: random.choice(["Moniter", "keyboard", "Headsett", "webcam"]), "fix"),
    ("product", lambda r: random.choice(["Laptop Stand", "USB Cable"]), "flag"),
]


def _day_first(date: str) -> str:
    month, day, year = date.split("/")
    return f"{day}/{month}/{year}" if int(day) > 12 else "31/01/2026"


def _year_first(date: str) -> str:
    month, day, year = date.split("/")
    return f"{year}-{month}-{day}"


def make(rows: int, problem_rate: float, duplicates: int, seed: int) -> tuple[list[dict], Counter, list[int]]:
    random.seed(seed)
    out = []
    for i in range(rows):
        first, last = random.choice(FIRST), random.choice(LAST)
        product = random.choice(list(PRODUCTS))
        month, day = random.randint(1, 12), random.randint(1, 28)
        out.append({
            "order_id": str(100001 + i),
            "customer": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{random.randint(1, 99)}@example.org",
            "product": product,
            "unit_price": f"{PRODUCTS[product]:.2f}",
            "quantity": str(random.randint(1, 8)),
            "order_date": f"{month:02d}/{day:02d}/2026",
            "status": random.choice(STATUSES),
            "payment_method": random.choice(PAYMENTS),
            "gift_message": random.choice(GIFTS),
        })

    # Copies of clean rows under a new order number, placed later in the file.
    copied = random.sample(range(rows // 2), duplicates)
    targets = random.sample(range(rows // 2, rows), duplicates)
    for source, target in zip(copied, targets):
        out[target] = dict(out[source], order_id=out[target]["order_id"])
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
    parser.add_argument("--seed", type=int, default=30)
    args = parser.parse_args()

    rows, planted, copy_rows = make(args.rows, args.problem_rate, args.duplicates, args.seed)
    path = Path(__file__).parent / f"test_shop_orders_{args.rows}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB, {len(rows):,} rows)")
    print(f"Planted: {planted['fix']:,} the agent may fix, {planted['suggest']:,} to flag with a suggested fix, "
          f"{planted['flag']:,} to flag with no fix, and {len(copy_rows)} duplicate rows")
