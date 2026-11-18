"""
Makes a large hotel bookings test file with known planted problems and duplicate rows.

    uv run --no-project --with-requirements requirements.txt python data/generate_hotel_bookings.py
    uv run --no-project --with-requirements requirements.txt python data/generate_hotel_bookings.py --rows 5000

Writes data/test_hotel_bookings_<rows>.csv (not committed: run this to make it) and prints
what was planted. Dates are written day first (25/03/2026), unlike the shop orders file.
The matching rules are in schema/hotel_bookings.json, and in plain English in the
answer key, data/test_hotel_bookings_answer_key.md.
"""
import argparse
import csv
import random
from collections import Counter
from pathlib import Path

FIRST = ("Alice Ben Chloe Daniel Ella Finn Grace Harry Isla Jack Katie Leo Mia Noah Olivia Owen Poppy "
         "Rory Sophie Tom Una Will Zara Adam Bea Cara Dev Erin Felix Gemma Hugo Ivy Jamie Kai Lily Max "
         "Nora Oscar Paige Reece").split()
LAST = ("Murphy Kelly Walsh Byrne Smith Jones Brown Taylor Wilson Martin Dubois Leroy Muller Schulz "
        "Garcia Ruiz Rossi Conti Novak Silva Brennan Doyle Hughes Price Lewis Clarke Moore Ward Hall Young").split()
ROOMS = {"Single": 89.00, "Double": 129.00, "Suite": 289.00, "Family": 169.00}
COUNTRIES = ["UK", "Ireland", "France", "Germany", "Spain", "USA"]
STATUSES = ["Confirmed", "Checked In", "Checked Out", "Cancelled"]
CHANNELS = ["Website", "Phone", "Travel Agent"]
NOTES = ["", "", "", "", "", "Late arrival", "Cot needed", "Quiet room please", "Anniversary"]


def _month_first(date: str) -> str:
    day, month, year = date.split("/")
    return f"{month}/{day}/{year}" if int(day) > 12 else "03/25/2026"


def _year_first(date: str) -> str:
    day, month, year = date.split("/")
    return f"{year}-{month}-{day}"


def _written_out(date: str) -> str:
    day, month, year = date.split("/")
    names = "January February March April May June July August September October November December".split()
    return f"{int(day)} {names[int(month) - 1]} {year}"


# (column, value maker, what should happen): "fix" the agent may fix on its own; "suggest"
# is flagged with a fix to approve; "flag" is flagged with no safe fix.
PROBLEMS = [
    ("email", lambda r: r["email"].replace("@", ".", 1), "flag"),                  # no @
    ("email", lambda r: "", "flag"),                                               # blank
    ("email", lambda r: random.choice(["none", "TBC", "-"]), "flag"),              # placeholder
    ("guest_name", lambda r: "", "flag"),                                          # blank name
    ("room_type", lambda r: random.choice(["Doubel", "suite", "Familly", "single"]), "fix"),
    ("room_type", lambda r: random.choice(["Penthouse", "Twin"]), "flag"),
    ("nights", lambda r: random.choice(["two", "three", "seven"]), "suggest"),     # number word
    ("nights", lambda r: "0", "flag"),                                             # below 1
    ("nights", lambda r: "45", "flag"),                                            # above 30
    ("nights", lambda r: "2.5", "suggest"),                                        # not whole
    ("nightly_rate", lambda r: "£" + r["nightly_rate"], "suggest"),                # currency sign
    ("nightly_rate", lambda r: "-" + r["nightly_rate"], "suggest"),                # minus sign
    ("nightly_rate", lambda r: random.choice(["on request", "TBC", "complimentary"]), "flag"),
    ("nightly_rate", lambda r: "2500.00", "flag"),                                 # above 1500
    ("check_in", lambda r: _month_first(r["check_in"]), "suggest"),                # month first, day > 12
    ("check_in", lambda r: "30/02/2026", "suggest"),                               # February 30
    ("check_in", lambda r: "", "flag"),                                            # blank
    ("check_in", lambda r: _year_first(r["check_in"]), "fix"),                     # same date, other format
    ("check_in", lambda r: _written_out(r["check_in"]), "fix"),                    # written out
    ("country", lambda r: random.choice(["Frnace", "germany", "usa", "Irland"]), "fix"),
    ("country", lambda r: "U.K.", "suggest"),                                      # close to UK, but a guess
    ("country", lambda r: random.choice(["Scotland", "Italy"]), "flag"),
    ("status", lambda r: random.choice(["confirmed", "Cancelld", "checked out"]), "fix"),
    ("status", lambda r: "No Show", "flag"),
    ("booking_channel", lambda r: random.choice(["website", "Phonee", "travel agent"]), "fix"),
    ("booking_channel", lambda r: "Walk-in", "flag"),
]


def make(rows: int, problem_rate: float, duplicates: int, seed: int):
    random.seed(seed)
    out = []
    for i in range(rows):
        first, last = random.choice(FIRST), random.choice(LAST)
        room = random.choice(list(ROOMS))
        day, month = random.randint(1, 28), random.randint(1, 12)
        out.append({
            "booking_id": str(700001 + i),
            "guest_name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{random.randint(1, 99)}@example.net",
            "room_type": room,
            "nights": str(random.randint(1, 14)),
            "nightly_rate": f"{ROOMS[room]:.2f}",
            "check_in": f"{day:02d}/{month:02d}/2026",
            "country": random.choice(COUNTRIES),
            "status": random.choice(STATUSES),
            "booking_channel": random.choice(CHANNELS),
            "notes": random.choice(NOTES),
        })

    copied = random.sample(range(rows // 2), duplicates)
    targets = random.sample(range(rows // 2, rows), duplicates)
    for source, target in zip(copied, targets):
        out[target] = dict(out[source], booking_id=out[target]["booking_id"])
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
    parser.add_argument("--duplicates", type=int, default=50)
    parser.add_argument("--seed", type=int, default=24)
    args = parser.parse_args()

    rows, planted, copy_rows = make(args.rows, args.problem_rate, args.duplicates, args.seed)
    path = Path(__file__).parent / f"test_hotel_bookings_{args.rows}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB, {len(rows):,} rows)")
    print(f"Planted: {planted['fix']:,} the agent may fix, {planted['suggest']:,} to flag with a suggested fix, "
          f"{planted['flag']:,} to flag with no fix, and {len(copy_rows)} duplicate rows")
