# Test library loans: answer key

File: data/test_library_loans_30000.csv, made by `data/generate_library_loans.py` (30,000 loans, 11 columns, 3.3 MB). Schema: schema/library_loans.json. Its twists: text IDs like LN-012345, a 13-digit ISBN format rule, and dates written **year first** (2026-03-14).

## Rules to paste

loan_id is required and looks like LN- followed by six digits. member_name is required. member_email is required and must look like an email. branch must be Central, Northside, Riverside, Westfield or Harbour. item_type must be Book, DVD, Audiobook or Magazine. isbn is required and must be exactly 13 digits. borrowed_on is a date, year first. due_days is a whole number from 7 to 28. late_fee is a number from 0 to 50. status must be On Loan, Returned, Overdue or Lost. notes is optional free text.

After the rules are applied, check on the schema page that isbn has a 13-digit pattern, borrowed_on reads **year first**, due_days runs 7 to 28, and late_fee 0 to 50.

## What was planted

| Kind | Count | Examples | Expected |
|---|---|---|---|
| Fixable by the agent | 242 | Centrel, northside, Westfeild, book, Audiobok, Overdu, on loan, March 14, 2026 | Fixed or reformatted |
| Flagged, with a fix to approve | 320 | ISBN typed with spaces, 25/03/2026 (day first), seven, $2.50, -1.00 | Flagged, with a suggestion on the Fix flagged cells tab |
| Flagged, no safe fix | 938 | ISBN with hyphens or too short, 2026-02-30, blank email, name or date, "two weeks", "21 days", 0 or 60 days, "waived", 75.00, Mobile Library, Vinyl, Damaged, Missing | Flagged |
| Duplicate loans | 45 | a loan copied later in the file under a new loan_id | Offered as duplicates to remove |

## What to check

- **About 1,260 rows flagged** in Clean + report mode, if the agent fixes all the typos. Checked with the app's own tools and no API call: 1,258.
- **Fix flagged cells:** about 320 cells with a suggested fix, such as an ISBN without its spaces, 25/03/2026 read day first, seven as 7, and $2.50 as 2.50. About 110 distinct values go to Claude for a recommendation.
- **Needs your input:** Damaged and Missing can be allowed as statuses with one click, and 60 days can raise the maximum.
- **45 duplicate groups.** loan_id is left out of the comparison because every loan has its own.
- A 30,000-row clean run takes about 2 minutes and costs roughly 2 to 5 cents on Haiku 5.5.
