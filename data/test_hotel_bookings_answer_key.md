# Test hotel bookings: answer key

File: data/test_hotel_bookings_30000.csv, made by `data/generate_hotel_bookings.py` (30,000 bookings, 11 columns, 3.1 MB). Schema: schema/hotel_bookings.json. Dates are written **day first** (25/03/2026).

## Rules to paste

booking_id is a whole number. guest_name is required. email is required and must look like an email. room_type must be Single, Double, Suite or Family. nights is a whole number from 1 to 30. nightly_rate is a number from 0 to 1500. check_in is a date, day first. country must be UK, Ireland, France, Germany, Spain or USA. status must be Confirmed, Checked In, Checked Out or Cancelled. booking_channel must be Website, Phone or Travel Agent. notes is optional free text.

After the rules are applied, check on the schema page that check_in reads **day first**, nights runs 1 to 30, and nightly_rate 0 to 1500.

## What was planted

| Kind | Count | Examples | Expected |
|---|---|---|---|
| Fixable by the agent | 350 | Doubel, suite, Frnace, usa, Cancelld, Phonee, 2026-04-11, 11 April 2026 | Fixed or reformatted |
| Flagged, with a fix to approve | 426 | £129.00, -89.00, two, 2.5, 03/25/2026 (month first), 30/02/2026, U.K. | Flagged, with a suggestion on the Fix flagged cells tab |
| Flagged, no safe fix | 762 | blank email, guest name or date, "on request", 2500.00, 0 or 45 nights, Penthouse, Scotland, No Show, Walk-in | Flagged |
| Duplicate bookings | 50 | a booking copied later in the file under a new booking_id | Offered as duplicates to remove |

## What to check

- **About 1,190 rows flagged** in Clean + report mode, if the agent fixes all the typos. Checked with the app's own tools and no API call: 1,188.
- **Fix flagged cells** offers a fix for about 460 cells, for example £129.00 to 129.00, 03/25/2026 to 2026-03-25, 30/02/2026 to the 28th or March 1, and U.K. to UK. Scotland, Italy, No Show, Walk-in, Penthouse and Twin get no suggestion.
- **50 duplicate groups**, each a pair of bookings.
- **Nothing guessed:** U.K. is flagged, not changed to UK, until you approve it.
- A 30,000-row clean run takes about 2 minutes and costs roughly 10 to 40 cents.
