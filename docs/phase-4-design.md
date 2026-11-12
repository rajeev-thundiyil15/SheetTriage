# Phase 4 design: saved jobs, accounts and billing

Phase 4 turns the demo into something people can pay for, while keeping it cheap to run and keeping each person's data private. It runs Nov 11–20 in two parts:

1. **Saved jobs and accounts** (Nov 11–17): optional sign-in, results that survive restarts and belong to one owner, a daily run limit, and more than one run at a time.
2. **Billing** (Nov 18–20): Stripe Checkout, a webhook that changes the account's plan, and the customer portal.

Saved jobs and accounts ship together. Saved results are never reachable by link alone: every run belongs to an account or to the browser that made it.

## Status, Nov 12

Part 1 is built and tested on `phase-1-reliability`, not yet on master or live. What changed from the first version of this plan:

- **Sign-in is optional.** Anyone can clean a file without an account; results are kept for an hour in that browser. Signing in is only for keeping results.
- **Sign in with Google instead of Clerk:** free, and no extra company to rely on. A test sign-in (`AUTH_MODE=dev`) is used locally and in the browser tests.
- **The demo button is local only.** It's hidden on Railway.
- **One daily limit for everyone not paying:** 3 runs a day, visitors counted by a scrambled address and free accounts by account. Paid plans get a monthly number.
- **Two runs at once** by giving each run thread its own tool state, rather than passing a state object to every tool.

Still to do in Part 1: one real Google sign-in, merging to master and checking the live site, and measuring a 30,000-row run's memory on Railway.

## Who can do what

| | Without signing in | Free account | Paid account |
|---|---|---|---|
| Clean your own file | Yes, 3 runs a day | Yes, 3 runs a day | A monthly number of runs |
| Results kept | 1 hour, in that browser | 7 days, in My files | 30 days |
| Who can open the results | Only the browser that ran it | Only that account | Only that account |

The Try demo data button only appears when running locally. Visitors on the live site upload their own file, and a sample with planted problems is linked from the README.

## Security rules

1. **Every job belongs to someone.** Each results, progress, review and download request checks that the job belongs to the signed-in user (or, for a demo run, to the browser's session cookie). Anything else gets "not found", the same as a job that doesn't exist, so a leaked or guessed link reveals nothing.
2. **No passwords stored.** Sign-in is handled by Google. SheetTriage checks Google's signed token once (signature, issuer, audience, expiry, verified email), then uses its own signed cookie.
3. **No card details.** Stripe Checkout and the customer portal handle all payment details. The database holds an email and a Stripe customer ID, nothing more.
4. **Spend is capped per person.** 3 runs a day for visitors and free accounts. The Anthropic key's monthly spending limit stays as a backstop.
5. **Keep as little as possible.**
   - Files are deleted automatically when their time is up.
   - A **Delete** button on the results page removes a job and its files at once.
   - **Delete my account** removes the account, its jobs and its files.
   - The volume has no backups, so deleted data is really gone.
6. **Stripe webhooks are verified** with Stripe's signature before any plan change.

## What's held in memory today

| State | Where | Lost on restart? | Notes |
|---|---|---|---|
| Uploads (file bytes, name, rows, columns) | `app._uploads` | Yes | Capped at 20, expire after 1 hour |
| Jobs (events, output path, issues, review) | `app._jobs` | Yes | The results page replays `events` from the start |
| The one running job | `app._active_job` + `_job_lock` | Yes | Blocks a second run |
| The running job's DataFrame, error log, checked columns | `tools._state` (module-wide) | Yes | **One per process: this is why only one job can run** |
| Output and report CSVs | `data/` on the container disk | Yes, on redeploy | Deleted after 1 hour |

## Part 1: saved jobs and accounts

### Storage

A Railway volume named `sheettriage-volume` is mounted at `/data` (added Nov 10). The app reads its folder from `DATA_DIR`, then `RAILWAY_VOLUME_MOUNT_PATH`, then falls back to `data/` locally.

```
/data/sheettriage.db            SQLite database
/data/uploads/<upload_id>.csv   the normalized upload
/data/jobs/<job_id>/clean.csv   output
/data/jobs/<job_id>/report.csv  error report
/data/jobs/<job_id>/review.json the review page data, built once
```

Only `/data` survives a redeploy. Anything written elsewhere on the container is still wiped.

### Tables

SQLite through Python's built-in `sqlite3` module, so no new dependency.

| Table | Columns | Replaces |
|---|---|---|
| `users` | `id` (`google_` plus Google's account ID), `email`, `plan` (free, paid), `stripe_customer_id`, `created_at` | nothing yet |
| `uploads` | `id`, `owner`, `filename`, `rows`, `columns` (JSON), `path`, `created_at`, `expires_at` | `_uploads` |
| `jobs` | `id`, `owner`, `upload_id`, `mode`, `schema` (JSON), `status` (queued, running, done, error, interrupted), `error`, `flagged_rows`, `input_tokens`, `output_tokens`, `created_at`, `finished_at`, `expires_at` | `_jobs` |
| `job_events` | `job_id`, `seq`, `event` (JSON) | `_jobs[...]["events"]` |

`owner` is either a user ID or, for a demo run, `session:<random id>` from a session cookie. Writing each progress event to `job_events` lets the results page replay a run after a restart, the same way it replays one after a refresh today.

### Sign-in

Google's "Sign in with Google" button on the sign-in page (`AUTH_MODE=google`, `GOOGLE_CLIENT_ID`). The server checks Google's token at `/auth/google`, which only takes JSON from the site's own pages, and then signs a cookie with `SESSION_SECRET`. A **My files** page lists the signed-in user's runs (file name, date, rows flagged, days left) on any device. The results page gets a **Copy link** button for the owner, which works only when they're signed in.

### Limits

- **Visitors and free accounts:** 3 runs in any 24 hours (`FREE_RUNS_PER_DAY`), plus the upload caps (5 MB, 30,000 rows). Visitors are counted by a scrambled form of their address: the last `X-Forwarded-For` entry, which Railway's proxy adds.
- **Paid:** `PAID_RUNS_PER_MONTH` (100 for now) and bigger files.
- **Demo:** the bundled file doesn't count, and is only offered locally.

### Restarts

On startup, any job still marked `running` is set to `interrupted`. The results page says the run was cut short and offers to run it again, without an error message. A deploy has a few seconds of downtime, because only one container can use the volume at a time.

### More than one run at a time

Move `tools._state` into a `ToolState` object created per job and passed to each tool call. This is the biggest code change: every tool uses it, and so do the 51 tests in `test_tools.py`. Then replace the single-job lock with a small worker pool, starting at 2 runs at once, with others queued. Memory use per run hasn't been measured yet, so measure a 30,000-row run on Railway first, then set the number of runs at once to fit.

### Cost per run

Record `input_tokens` and `output_tokens` from each Claude response on the job. This gives the real cost per run for pricing (story 39) instead of the estimates.

### How long to keep results

1 hour for demo runs, 7 days for free accounts, 30 days for paid. A cleanup that runs daily (and on startup) deletes expired files and rows. The upload page and results page say how long results are kept.

### Before taking payments

- A short **privacy notice**: what's stored, for how long, who can see it, and how to delete it.
- **Terms of use.**
- **Delete my account** in the account menu.

### Tests

- pytest: each test gets a temporary `DATA_DIR`. New unit tests:
  - a job and its events survive reopening the database;
  - a job left `running` becomes `interrupted` on startup;
  - two jobs with separate `ToolState` objects don't affect each other;
  - one user asking for another user's job, upload, review or download gets 404;
  - a demo run can't be opened from a different session;
  - expired jobs and their files are deleted by the cleanup;
  - **Delete** and **Delete my account** remove the rows and the files;
  - a run past the monthly limit is refused with a plain message.
- Playwright, with sign-in faked in the scripted-agent server:
  - restart the server between a finished run and a page refresh, and check the review is still there;
  - a second user opening the first user's results link sees "not found";
  - the demo still works without signing in.

## Part 2: billing

- **Stripe Checkout** for the paid plan, so card details never reach SheetTriage's server.
- **A webhook** at `/webhook/stripe` that checks Stripe's signature and changes the account's `plan` when a payment succeeds or a subscription ends.
- **Stripe's customer portal** so people can cancel and update their card themselves.
- **Test mode first:** all of Part 2 is built and tested against Stripe's test keys, with Playwright covering the checkout redirect.

## Running costs

| Item | Cost |
|---|---|
| Railway Hobby plan | About $5 a month, including some usage |
| Volume | A few cents a month at this size (well under 1 GB) |
| SQLite | Free, part of Python |
| Google sign-in | Free |
| Stripe | A fee per payment only |
| Claude Haiku 4.5 | A few cents per run; Part 1 measures the real figure |

## Decisions

1. ~~**Railway volume:** add one in the dashboard, mounted at `/data`.~~ Done Nov 10.
2. ~~**Sign-in provider.**~~ Google, decided Nov 10.
3. **Free tier numbers:** 3 runs a day for now. The largest free file is still 30,000 rows.
4. **Paid price.** First measure Claude cost per run (Part 1) and test Haiku 5.5, which is listed at about a tenth of Haiku 4.5's price.
5. **Runs at once:** start at 2, and change it once a 30,000-row run's memory use on Railway is measured.
