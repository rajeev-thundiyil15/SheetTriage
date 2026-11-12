# Test shop orders: answer key

File: data/test_shop_orders.csv (40 orders, 10 columns). Schema: schema/shop_orders.json.
Row numbers match a spreadsheet and the review page (the header is row 1).

Upload the schema file on the schema page (Advanced, then choose `schema/shop_orders.json`). The page's own draft is looser: it reads `quantity` as a decimal and sets no maximums, so it would miss the problems in rows 18, 27, 30 and 32.

## Rules to paste instead of importing the schema

order_id is a whole number. customer is required. email is required and must look like an email. product must be Laptop, Monitor, Keyboard, Mouse, Headset or Webcam. unit_price is a number from 0 to 3000. quantity is a whole number from 1 to 20. order_date is a date, month first. status must be Pending, Shipped, Delivered or Cancelled. payment_method must be Card, PayPal or Bank Transfer. gift_message is optional free text.

## Planted problems

| Row | Column | Value | Problem | Expected |
|---|---|---|---|---|
| 3 | email | bruno.silva@example | no dot after the @ | Flagged |
| 5 | status | shiped | close typo of Shipped | Fixed |
| 6 | unit_price | €249.99 | currency symbol in a number | Flagged |
| 7 | order_date | 2026-02-11 | year first, same date | Reformatted |
| 8 | payment_method | paypal | wrong case of PayPal | Fixed |
| 9 | quantity | three | number written as a word | Flagged |
| 11 | product | Laptop Stand | not a product in the list | Flagged |
| 12 | customer | (blank) | required name is blank | Flagged |
| 13 | unit_price | -29.99 | negative price | Flagged |
| 14 | order_date | Feb 3 2026 | written-out date | Reformatted |
| 16 | status | Returned | not an allowed status | Flagged |
| 17 | email | TBC | placeholder instead of an email | Flagged |
| 18 | quantity | 0 | below the minimum of 1 | Flagged |
| 19 | product | keyboard | wrong case of Keyboard | Fixed |
| 21 | payment_method | Credit Card | looks like Card, but that's a guess | Flagged |
| 22 | order_date | 31/01/2026 | day first; month 31 is impossible month-first | Flagged |
| 24 | unit_price | call for price | text in a number column | Flagged |
| 25 | status | Delivred | close typo of Delivered | Fixed |
| 27 | quantity | 25 | above the maximum of 20 | Flagged |
| 28 | email | (blank) | required email is blank | Flagged |
| 30 | unit_price | 4500.00 | above the 3,000 maximum | Flagged |
| 32 | quantity | 1.5 | not a whole number | Flagged |
| 34 | order_date | 02/29/2026 | 2026 isn't a leap year | Flagged |
| 36 | product | Moniter | close typo of Monitor | Fixed |
| 38 | payment_method | Bitcoin | not an accepted payment method | Flagged |
| 40 | order_date | (blank) | required date is blank | Flagged |

## What to check

- **19 rows flagged** in Clean + report mode: 3, 6, 9, 11, 12, 13, 16, 17, 18, 21, 22, 24, 27, 28, 30, 32, 34, 38 and 40. Every row stays in the output.
- **5 cells corrected:** rows 5, 8, 19, 25 and 36. Every other date is rewritten as YYYY-MM-DD and counted as reformatted, not corrected.
- **Nothing guessed:** Credit Card, Laptop Stand, Returned and Bitcoin are flagged with their original text, never changed to a similar allowed value.
- **Strict mode:** the output has 21 rows, and the 19 flagged rows are only in the error report.
- **Audit mode:** nothing changes, and 24 rows are flagged: the 19 above plus the 5 fixable values (rows 5, 8, 19, 25 and 36). The dates in rows 7 and 14 are valid dates in another format, so they aren't flagged.

Checked against SheetTriage's own tools without an API call: applying only the fixes the tools allow, then flagging what's left, gives exactly these 19 rows.
