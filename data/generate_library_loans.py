"""
Makes a large library loans test file with known planted problems and duplicate rows.

    uv run --no-project --with-requirements requirements.txt python data/generate_library_loans.py
    uv run --no-project --with-requirements requirements.txt python data/generate_library_loans.py --rows 5000

Writes data/test_library_loans_<rows>.csv (not committed: run this to make it). Its twists:
text IDs (LN-012345), a 13-digit ISBN format rule, and dates written year first. The
rules are in schema/library_loans.json and, in plain English, in
data/test_library_loans_answer_key.md.
"""
import argparse
import csv
import random
from collections import Counter
from pathlib import Path

FIRST = ("Abel Bianca Cyrus Delia Emil Freya Gideon Hollie Idris Juno Kasper Lorna Milo Nadia Orla Pavel "
         "Rhea Silas Thea Ulric Vera Wade Xena Yusuf Zelda Arlo Bryn Cora Dax Elsie Flint Greer Hector Iona "
         "Jasper Keira Lars Mabel Nico Opal").split()
LAST = ("Abbott Barker Carver Dalton Ellis Fowler Gray Hale Irwin Judd Keane Lowe Marsh Noble Oakes Pike "
        "Quill Reeve Shaw Thorne Underwood Vance Wilde Yates Archer Booth Crane Drake Frost Glenn").split()
BRANCHES = ["Central", "Northside", "Riverside", "Westfield", "Harbour"]
ITEMS = ["Book", "DVD", "Audiobook", "Magazine"]
STATUSES = ["On Loan", "Returned", "Overdue", "Lost"]
NOTES = ["", "", "", "", "", "", "Reserved next", "Damaged spine", "Renewed once", "Gift aid"]


def _isbn() -> str:
    return "978" + "".join(random.choice("0123456789") for _ in range(10))


def _spaced(isbn: str) -> str:
    return f"{isbn[:3]} {isbn[3]} {isbn[4:6]} {isbn[6:12]} {isbn[12]}"


def _hyphenated(isbn: str) -> str:
    return f"{isbn[:3]}-{isbn[3]}-{isbn[4:6]}-{isbn[6:12]}-{isbn[12]}"


def _day_first(date: str) -> str:
    year, month, day = date.split("-")
    return f"{day}/{month}/{year}" if int(day) > 12 else "25/03/2026"


def _written_out(date: str) -> str:
    year, month, day = date.split("-")
    names = "January February March April May June July August September October November December".split()
    return f"{names[int(month) - 1]} {int(day)}, {year}"


# (column, value maker, what should happen): "fix" the agent may fix on its own; "suggest"
# is flagged with a fix to approve; "flag" is flagged with no safe fix.
PROBLEMS = [
    ("member_email", lambda r: r["member_email"].replace("@", " at ", 1), "flag"),
    ("member_email", lambda r: "", "flag"),
    ("member_email", lambda r: random.choice(["-", "unknown"]), "flag"),
    ("member_name", lambda r: "", "flag"),
    ("branch", lambda r: random.choice(["Centrel", "northside", "Westfeild", "harbour"]), "fix"),
    ("branch", lambda r: random.choice(["Mobile Library", "Online"]), "flag"),
    ("item_type", lambda r: random.choice(["book", "dvd", "Audiobok", "magazine"]), "fix"),
    ("item_type", lambda r: random.choice(["Vinyl", "Board Game"]), "flag"),
    ("isbn", lambda r: _spaced(r["isbn"]), "suggest"),                         # spaces: the digits are all there
    ("isbn", lambda r: _hyphenated(r["isbn"]), "flag"),                        # hyphens
    ("isbn", lambda r: r["isbn"][:10], "flag"),                                # too short
    ("borrowed_on", lambda r: _day_first(r["borrowed_on"]), "suggest"),       # day first, day > 12
    ("borrowed_on", lambda r: _written_out(r["borrowed_on"]), "fix"),          # written out
    ("borrowed_on", lambda r: "2026-02-30", "flag"),                           # February 30
    ("borrowed_on", lambda r: "", "flag"),                                     # blank
    ("due_days", lambda r: random.choice(["two weeks", "21 days"]), "flag"),
    ("due_days", lambda r: "0", "flag"),                                       # below 7
    ("due_days", lambda r: "60", "flag"),                                      # above 28
    ("due_days", lambda r: "seven", "suggest"),                               # number word
    ("late_fee", lambda r: "$" + r["late_fee"], "suggest"),                    # dollar sign
    ("late_fee", lambda r: "-" + r["late_fee"] if r["late_fee"] != "0.00" else "-0.50", "suggest"),
    ("late_fee", lambda r: random.choice(["waived", "TBC"]), "flag"),
    ("late_fee", lambda r: "75.00", "flag"),                                   # above 50
    ("status", lambda r: random.choice(["returned", "Overdu", "on loan"]), "fix"),
    ("status", lambda r: random.choice(["Damaged", "Missing"]), "flag"),
]


def make(rows: int, problem_rate: float, duplicates: int, seed: int):
    random.seed(seed)
    out = []
    for i in range(rows):
        first, last = random.choice(FIRST), random.choice(LAST)
        status = random.choice(STATUSES)
        fee = random.choice([0.00, 0.00, 0.00, 0.50, 1.00, 2.50, 4.00]) if status in ("Overdue", "Returned") else 0.00
        out.append({
            "loan_id": f"LN-{i + 1:06d}",
            "member_name": f"{first} {last}",
            "member_email": f"{first.lower()}{random.randint(10, 999)}@library.example",
            "branch": random.choice(BRANCHES),
            "item_type": random.choice(ITEMS),
            "isbn": _isbn(),
            "borrowed_on": f"2026-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "due_days": str(random.choice([7, 14, 14, 21, 28])),
            "late_fee": f"{fee:.2f}",
            "status": status,
            "notes": random.choice(NOTES),
        })

    copied = random.sample(range(rows // 2), duplicates)
    targets = random.sample(range(rows // 2, rows), duplicates)
    for source, target in zip(copied, targets):
        out[target] = dict(out[source], loan_id=out[target]["loan_id"])
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
    parser.add_argument("--duplicates", type=int, default=45)
    parser.add_argument("--seed", type=int, default=99)
    args = parser.parse_args()

    rows, planted, copy_rows = make(args.rows, args.problem_rate, args.duplicates, args.seed)
    path = Path(__file__).parent / f"test_library_loans_{args.rows}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB, {len(rows):,} rows)")
    print(f"Planted: {planted['fix']:,} the agent may fix, {planted['suggest']:,} to flag with a suggested fix, "
          f"{planted['flag']:,} to flag with no fix, and {len(copy_rows)} duplicate rows")
