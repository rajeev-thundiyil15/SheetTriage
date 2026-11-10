# Phase 4 design: saved jobs, accounts and billing

Phase 4 turns the demo into something people can pay for. It runs Nov 11–20 in three parts, in this order, because each part needs the one before it:

1. **Saved jobs** (Nov 11–13): results survive restarts, and more than one run can happen at a time.
2. **Accounts and limits** (Nov 14–17): sign-in, a free tier, and a monthly run count.
3. **Billing** (Nov 18–20): Stripe Checkout, a webhook that changes the account's tier, and the customer portal.

## What's held in memory today

| State | Where | Lost on restart? | Notes |
|---|---|---|---|
| Uploads (file bytes, name, rows, columns) | `app._uploads` | Yes | Capped at 20, expire after 1 hour |
| Jobs (events, output path, issues, review) | `app._jobs` | Yes | The results page replays `events` from the start |
| The one running job | `app._active_job` + `_job_lock` | Yes | Blocks a second run |
| The running job's DataFrame, error log, checked columns | `tools._state` (module-wide) | Yes | **One per process: this is why only one job can run** |
| Output and report CSVs | `data/` on the container disk | Yes, on redeploy | Deleted after 1 hour |

## Part 1: saved jobs

**Storage.** Attach a Railway volume (you add it in the Railway dashboard) at `/data`, and point a new `DATA_DIR` setting at it. Locally it stays `data/`. Everything below lives there:

```
/data/sheettriage.db           SQLite database
/data/uploads/<upload_id>.csv  the normalized upload
/data/jobs/<job_id>/clean.csv  output
/data/jobs/<job_id>/report.csv error report
/data/jobs/<job_id>/review.json the review page data, built once
```

**Tables.** SQLite through Python's built-in `sqlite3` module, so no new dependency. Three tables:

| Table | Columns | Replaces |
|---|---|---|
| `uploads` | `id`, `user_id` (empty until Part 2), `filename`, `rows`, `columns` (JSON), `path`, `created_at` | `_uploads` |
| `jobs` | `id`, `user_id`, `upload_id`, `mode`, `schema` (JSON), `status` (queued, running, done, error, interrupted), `error`, `flagged_rows`, `input_tokens`, `output_tokens`, `created_at`, `finished_at` | `_jobs` |
| `job_events` | `job_id`, `seq`, `event` (JSON) | `_jobs[...]["events"]` |

Writing each progress event to `job_events` lets the results page replay a run after a restart, the same way it replays one after a refresh today.

**Restarts.** On startup, any job still marked `running` is set to `interrupted`. The results page says the run was cut short and offers to run it again, without an error message.

**More than one run at a time.** Move `tools._state` into a `ToolState` object created per job and passed to each tool call. This is the biggest change in Part 1: every tool uses it, and so do the 51 tests in `test_tools.py`. Then replace the single-job lock with a small worker pool, starting at 2 runs at once, with others queued. Memory use per run hasn't been measured yet, so measure a 30,000-row run on Railway first, then set the number of runs at once to fit.

**Cost per run.** Record `input_tokens` and `output_tokens` from each Claude response on the job. This gives the real cost per run for pricing (story 39) instead of the estimates.

**How long to keep results.** 7 days on the free tier and 30 days on paid, replacing the 1-hour rule. A daily cleanup deletes expired files and rows.

**Tests.**
- pytest: each test gets a temporary `DATA_DIR`.
- New unit tests:
  - a job and its events survive re-opening the database;
  - a job left `running` becomes `interrupted` on startup;
  - two jobs with separate `ToolState` objects don't affect each other.
- New Playwright test: restart the scripted-agent server between a finished run and a page refresh, and check the review is still there.

## Part 2: accounts and limits

**Sign-in.** Use a managed provider instead of storing passwords. Clerk and Auth0 both have free tiers that cover a launch:

| | Clerk | Auth0 |
|---|---|---|
| Setup on a plain HTML site | Drop-in sign-in components and a script tag | Hosted login page and redirect |
| Free tier | Generous for a new product | Generous for a new product |
| Server check in FastAPI | Verify the session token (JWT) on each request | Verify the access token (JWT) on each request |

Recommendation: Clerk, because its drop-in components suit pages without a framework. Check both providers' current pricing before signing up.

**Limits.** A `users` table (`id`, `email`, `tier`, `stripe_customer_id`, `created_at`) and a monthly count of runs from `jobs`. The free tier reuses today's upload caps (5 MB, 30,000 rows) plus a run limit. The paid tier raises both. The demo button stays open without signing in, limited to the bundled file, so a recruiter can still try it in one click.

## Part 3: billing

- **Stripe Checkout** for the paid plan, so card details never reach SheetTriage's server.
- **A webhook** at `/webhook/stripe` that checks Stripe's signature and changes the account's `tier` when a payment succeeds or a subscription ends.
- **Stripe's customer portal** so people can cancel and update their card themselves.
- **Test mode first:** all of Part 3 is built and tested against Stripe's test keys, with Playwright covering the checkout redirect.

## Decisions needed before or during Phase 4

1. **Railway volume:** add one in the dashboard, mounted at `/data`. It costs a small amount per GB each month.
2. **Sign-in provider:** Clerk (recommended) or Auth0.
3. **Free tier numbers:** runs per month, and whether the 30,000-row cap stays free.
4. **Paid price.** First measure Claude cost per run (Part 1) and test Haiku 5.5, which is listed at about a tenth of Haiku 4.5's price.
5. **Runs at once:** start at 2, and change it once a 30,000-row run's memory use on Railway is measured.
