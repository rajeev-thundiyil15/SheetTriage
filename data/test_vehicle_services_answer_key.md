# Test vehicle services: answer key

File: data/test_vehicle_services_30000.csv, made by `data/generate_vehicle_services.py` (30,000 service records, **18 columns**, 3.7 MB). Schema: schema/vehicle_services.json. Its twists: UK number plates with a format rule, dates written **day first**, an optional second date (next_due), and four money columns.

## Rules to paste

service_id is required and looks like SV- followed by six digits. plate is required and looks like two capital letters, two digits, a space and three capital letters, such as AB12 CDE. make must be Ford, Toyota, VW, BMW, Kia, Tesla or Nissan. fuel must be Petrol, Diesel, Hybrid or Electric. year is a whole number from 2005 to 2026. mileage is a whole number from 0 to 400000. service_date is a date, day first. service_type must be Oil Change, Tyres, Brakes, MOT, Full Service or Battery. technician is required. labour_hours is a number from 0 to 12. parts_cost is a number from 0 to 5000. labour_cost is a number from 0 to 2000. total_cost is a number from 0 to 7000. paid must be Yes or No. payment_method must be Card, Cash or Invoice. email is required and must look like an email. next_due is an optional date, day first. notes is optional free text.

After the rules are applied, check on the schema page that plate has its pattern, both date columns read **day first**, next_due and notes can be blank, and the number ranges match.

## What was planted

| Kind | Count | Examples | Expected |
|---|---|---|---|
| Fixable by the agent | about 200 | ford, Toyta, Nisan, Disel, oil change, mot, yes, card | Fixed |
| Flagged, with a fix to approve | 411 | 54,321 (thousands separator), -67532, two (hours), 03/27/2026 (month first), 31/04/2026, £144.00, Credit Card | Flagged, with a suggestion on the Fix flagged cells tab |
| Flagged, no safe fix | about 875 | lowercase plate or no space, blank plate, technician, email or date, Skoda, LPG, 1998 or 2031, unknown mileage, Valet, 15 hours, TBC, 9500.00, Partly, Cheque, Y, 32/13/2027 | Flagged |
| Duplicate records | 40 | a service copied later in the file under a new service_id | Offered as duplicates to remove |

`Y` in paid is flagged rather than fixed: it isn't a typo of Yes by the app's rule, so it waits for a person.

## What to check

- **About 1,280 rows flagged** in Clean + report mode, if the agent fixes all the typos. Checked with the app's own tools and no API call: 1,278, across all 16 checked columns.
- **Fix flagged cells:** about 410 cells with a suggested fix, in about 325 groups in total. About 160 distinct values go to Claude for a recommendation.
- **Needs your input:** Skoda can be allowed as a make, and 15 hours can raise labour_hours to 15. A lowercase plate typed back in capitals (such as AB12 CDE) is accepted; typed without the space it's refused.
- **40 duplicate groups.** service_id is left out of the comparison because every service has its own.
- With 18 columns the run takes longer than the narrower files: roughly 2 to 4 minutes, and roughly 15 to 50 cents.
