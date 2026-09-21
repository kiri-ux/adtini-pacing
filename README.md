# adtini · Pacing

Turns the daily client-serve drops in S3 into the two views the buying team
keeps by hand today: a pacing sheet per order, and one line per order across
every client.

Built to sit beside the quote builder - same Flask/gunicorn shape, same
`adtini.css`, same chrome - so it can move inside adtini later without a
rewrite.

---

## What it does

**Delivery in.** A daily `client-serve_YYYYMMDD_HHMM_N.csv` lands in
`s3://adtini-orders/orders/`. A cron job reads anything new and stores it one
row per day per strategy. Each drop is a rolling 31-day window, so days arrive
repeatedly; rows are replaced, not appended, and the numbers are always the
latest ones.

**Sold terms in.** The feed never carries what was sold, when the flight ends,
or what the client is owed - so that lives in the tool's own order book. The
skeleton (client, order, campaign elements) is built from the feed
automatically; a buyer types in the sold totals, dates and goal rate.

**Pacing out.** Three layouts, matching the three tabs:

| Type | Used for | Paces on |
|---|---|---|
| Impression | most orders | delivered impressions |
| Click | PPC, LinkedIn | spend |
| Event | Performance Max | spend |

Orders are classified automatically by product and data source, and a buyer
can override the type per order.

### The math

Read off the hand-kept sheets and checked against them in
`tests/test_pacing.py`:

```
flight_days  = (end - start) + 1              inclusive
day_target   = total_sold / flight_days
on_pace      = day_target x days_run          days_run capped at flight_days
to_date      = delivery summed inside the flight
pacing       = (on_pace - to_date) / on_pace
```

The sign convention is the sheet's: **negative is over-delivering, positive is
under.** Within ±10% shows as on pace.

Monthly pacing works the same way on the part of the calendar month the flight
actually covers - a flight starting on the 17th owes its monthly impressions
in 14 days, not 30.

---

## Setting it up on Render

You asked what to create: **a Web Service, a Cron Job and a Postgres
instance.** `render.yaml` defines all three, so the fastest path is a
Blueprint rather than making them by hand.

### 1. Create the services

Render dashboard → **New → Blueprint** → pick this repo. It reads
`render.yaml` and creates:

| Service | Type | Plan | What it does |
|---|---|---|---|
| `adtini-pacing` | Web Service (Python) | Starter | the tool itself |
| `adtini-pacing-ingest` | Cron Job (Python) | Starter | pulls S3 daily at 13:00 UTC |
| `adtini-pacing-db` | Postgres | Basic 256MB | shared by both |

Starter is the right size to begin with. The web service needs a paid plan
regardless - Free spins down after inactivity, and a cold start on a table
this size is a bad first impression for the team. Storage is the thing to
watch: a year of daily delivery at the current volume is roughly 1.5-2 GB, so
expect to move the database up a tier within the first year.

### 2. Set the secrets

Four values are marked `sync: false` in the blueprint, meaning Render prompts
you rather than storing them in git. Set them on **both** the web service and
the cron job:

| Key | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM key for the bucket |
| `AWS_SECRET_ACCESS_KEY` | its secret |
| `APP_PASSWORD` | shared password for the buying team |
| `SESSION_SECRET` | web service only; Render generates it |

`DATABASE_URL` is wired to the Postgres instance automatically. `S3_BUCKET`,
`S3_PREFIX` and `AWS_REGION` are already in the blueprint - change them there
if the bucket moves.

### 3. The IAM user

The credentials only need to read the one prefix. Anything wider is more
access than the tool has any use for:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::adtini-orders",
      "Condition": {"StringLike": {"s3:prefix": ["orders/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::adtini-orders/orders/*"
    }
  ]
}
```

### 4. pacing.reporting.zone

On the **web service** → Settings → Custom Domains → add
`pacing.reporting.zone`. Render then shows you the record to create at
whoever hosts `reporting.zone`:

```
Type   Name      Value
CNAME  pacing    adtini-pacing.onrender.com
```

Render issues the TLS certificate itself once the record resolves, usually
within a few minutes. No certificate to buy or renew.

### 5. First run

1. Open the site and sign in with `APP_PASSWORD`.
2. Go to **Data** → **Run sweep**. The first sweep loads every drop in the
   bucket, which takes a couple of minutes for a month of files.
3. Still on **Data**, hit **Sync** to build the order book from what loaded.
4. Go to **Pacing**, filter to **Sold terms: needs terms**, and work down the
   list entering each order's sold totals and dates.

Step 4 is the only real work, and it is one-off per order. After that the
cron job keeps the delivery side current on its own.

---

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the AWS keys, or skip and upload a file
python app.py                 # http://127.0.0.1:5000
```

With no `DATABASE_URL` it uses a local SQLite file, and with no `APP_PASSWORD`
it skips the login gate. To work without S3 access, drop a CSV or zip on the
**Data** page - it takes the same file the bucket does.

```bash
python -m pytest tests/ -q    # 37 tests
python scripts/ingest.py      # what the cron job runs
```

---

## Layout

```
app.py              Flask routes, template filters, the login gate
models.py           schema: order book, delivery feed, ingest log
config.py  db.py    environment and SQLAlchemy setup

pacing/
  calendar.py       flight windows, elapsed days, month clipping
  engine.py         the three pacing types, and the Total row

ingest/
  s3.py             listing and fetching the drops
  normalize.py      the export's quirks, all in one place
  loader.py         upsert into daily_delivery, ingest log

orderbook.py        builds the skeleton from the feed
views.py            read models for the two pages
exports.py          XLSX in the same column order as the sheets
scripts/ingest.py   the cron entrypoint
```

---

## Things worth knowing

**History starts where the feed does.** Each drop carries 31 days, so on day
one the tool only knows about the last month. An order whose flight started
earlier has a life-of-flight figure that is short by whatever ran before -
pages mark those `PARTIAL` rather than quietly under-reporting. Month-to-date
is unaffected, and the gap closes as daily drops accumulate. If there is an
archive of older drops in the bucket, the first sweep picks them all up.

**The feed's identifiers are messy, and the ingest layer absorbs it.**
`order_id` is blank for Adlib and beta rows (they fall back to the order-level
name), the header ships two columns both called `goal_cpm_` (neither is used -
`goal_internal_cpm` is), 17-digit Meta campaign ids have to be read as text or
they lose their last digits, and one strategy running several creatives
produces several rows for a day, which are summed. All of it is in
`ingest/normalize.py` with tests.

**Two strategies can share a name.** The feed ships distinct strategy ids
under one name - two Meta ad sets both called "Facebook/Instagram Premium" -
so labels get a `(2)` suffix to keep them apart. Rows a buyer cannot tell
apart are rows they cannot pace.

**Adtini reporting, later.** The pacing engine reads `DailyDelivery` and
nothing else. Pointing it at Adtini's own reporting instead of the S3 drops
means writing a second loader into the same table; the engine, views and
exports do not change.
