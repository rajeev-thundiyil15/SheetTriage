# Test registrations: answer key

File: data/test_registrations.csv (40 rows). Schema: schema/registrations.json.
Row numbers match a spreadsheet and the review page (header is row 1, so data row 3 is row 4).

## Rules to paste instead of importing the schema

registration_id is a whole number. attendee is required. email is required and must look like an email. ticket_type must be General, VIP, Student or Speaker. price is a number from 0 to 500. quantity is a whole number from 1 to 10. registered_on is a date, month first. country must be USA, Canada, UK, Germany or India. notes is optional free text.

## Planted problems

| Row | Column | Value | Problem | Expected |
|---|---|---|---|---|
| 4 | email | chloe.patel.example.com | email has no @ | Flagged |
| 5 | registered_on | 2026-03-15 | different date format, same date | Fixed or reformatted |
| 6 | ticket_type | vip | wrong case, fixable to VIP | Fixed or reformatted |
| 7 | country | usa | wrong case, fixable to USA | Fixed or reformatted |
| 8 | price | $45.00 | currency symbol in a number | Flagged |
| 9 | email | n/a | placeholder text instead of an email | Flagged |
| 10 | quantity | two | number written as a word | Flagged |
| 12 | ticket_type | Studnet | typo, fixable to Student | Fixed or reformatted |
| 13 | registered_on | March 5 2026 | written-out date | Fixed or reformatted |
| 14 | price | -20.00 | negative price | Flagged |
| 15 | country | U.S. | abbreviation not in the list | Flagged |
| 16 | email | (blank) | required email is blank | Flagged |
| 18 | quantity | 0 | below the minimum of 1 | Flagged |
| 20 | ticket_type | Early Bird | not an allowed ticket type | Flagged |
| 21 | registered_on | 13/02/2026 | day-first date; month 13 is impossible in month-first order | Flagged |
| 22 | attendee | (blank) | required name is blank | Flagged |
| 23 | price | free | text in a number column | Flagged |
| 25 | ticket_type | general | wrong case, fixable to General | Fixed or reformatted |
| 27 | quantity | 12 | above the maximum of 10 | Flagged |
| 28 | country | Untied Kingdom | misspelled, not in the list | Flagged |
| 29 | registered_on | (blank) | required date is blank | Flagged |
| 31 | price | 750.00 | above the 500 maximum | Flagged |
| 34 | quantity | 2.5 | not a whole number | Flagged |
| 37 | registered_on | 02/30/2026 | February 30 does not exist | Flagged |

## What to check

- Every row marked Flagged shows up in the error report.
- Fixable rows: ticket_type vip, Studnet and general become VIP, Student and General; usa becomes USA; 2026-03-15 and March 5 2026 become dates in the standard format.
- registration_id and notes have no problems, so they should finish Clean.
- 18 rows should be flagged and 22 should be clean. A different count is worth a look, for example if the agent also fixes $45.00 or U.S.
- Strict mode should leave the flagged rows out of the output CSV; Audit mode should change nothing.
