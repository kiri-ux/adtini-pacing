# adtini · Pacing

Turns the daily client-serve drops in S3 into the two views the buying team
keeps by hand today: a pacing sheet per order, and one line per order across
every client.

Built to sit beside the quote builder - same Flask/gunicorn shape, same
`adtini.css`, same chrome - so it can move inside adtini later without a
rewrite.

---

## What it does

Two kinds of file land in `s3://adtini-orders/orders/`, told apart by their
filename:

| Filename | What it carries | Grain |
|---|---|---|
| `client-serve*` | delivery - impressions, clicks, spend, conversions | one row per day per strategy |
| `orders*` | the sold side - totals, flight dates, budgets | one row per line item |

They join on the ids they share, `order_id` and `line_item_id`, so nothing is
matched on names. The orders file is read first on each sweep, so a line item
exists for that day's delivery to land against.

**Delivery in.** Each drop is a rolling 31-day window, so days arrive
repeatedly; rows are replaced, not appended, and the numbers are always the
latest ones.

**Sold terms in.** Imported from the orders drops, so nothing is typed in by
hand. Anything a buyer does edit is marked buyer-maintained and the next
import leaves it alone - budgets get adjusted mid-flight and the adjustment
has to survive. "Hand back to orders file" undoes that.

**Which orders get a page.** Only Insertion Orders. A Cancelled order is kept
rather than dropped, because it may have run for months before it was
cancelled - it appears if it delivered, and is marked `cancelled`. An order
that never ran at all (Draft, Declined) is out.

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

The orders file prices in budget and impressions rather than in a rate, so the
goal CPM is derived: `total_campaign_budget / total_campaign_impressions ×
1000`.

### Seeing the strategies apart

The order page charts delivery per strategy per day, so retargeting behaving
differently from behavioral is visible rather than buried in a line item
total. Lines are grouped by targeting, not by the feed's strategy id: one line
item routinely carries twenty ids that are the same targeting re-flighted, all
named identically, and twenty indistinguishable lines answer nothing. Past
eight series the tail folds into "Other" rather than colours being reused.

There is deliberately no daily-target line on that chart. The target is the
whole order's, and drawing it against a single strategy invites reading that
strategy as behind when the order is fine.

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
   bucket - orders first, then delivery - which takes a couple of minutes for
   a month of files.
3. Go to **Pacing**. Orders that came through with their sold terms are
   already pacing.
4. Filter to **Sold terms: needs terms** to see anything the orders files did
   not cover, and fill those in by hand.

After that the cron job keeps both sides current on its own.

---

## Schema changes

The schema is Alembic's, applied by the web service's pre-deploy command
(`alembic upgrade head`) before traffic moves to the new version. Nothing in
the app creates or alters tables.

That is not a style preference. The first deploy built its tables with
`create_all`, which creates missing tables but never alters existing ones, so
the columns added in the next deploy were never applied and every page failed
on `column orders.order_type does not exist`. `/healthz` now reports that
case specifically - it returns 503 naming the missing tables or the failing
column, rather than going green on a process that cannot serve a page.

After changing a model:

```bash
alembic revision --autogenerate -m "what changed"
alembic upgrade head
```

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the AWS keys, or skip and upload a file
alembic upgrade head          # `python app.py` also does this for you
python app.py                 # http://127.0.0.1:5000
```

With no `DATABASE_URL` it uses a local SQLite file, and with no `APP_PASSWORD`
it skips the login gate. To work without S3 access, drop a CSV or zip on the
**Data** page - it takes the same file the bucket does.

```bash
python -m pytest tests/ -q    # 51 tests
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

migrations/         Alembic; the schema's only owner
ingest/
  s3.py             listing and fetching the drops
  normalize.py      the delivery export's quirks
  orders.py         the orders export's quirks, and its alias table
  loader.py         routes by filename, upserts, ingest log

orderbook.py        imports orders; classification and labelling rules
views.py            read models for the two pages, and the chart series
exports.py          XLSX in the same column order as the sheets
static/chart.js     the per-strategy daily chart
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

**Both exports are messy, and the ingest layer absorbs it.**

For delivery (`ingest/normalize.py`): `order_id` is blank on Adlib and beta
rows, the header ships two columns both called `goal_cpm_` (neither is used -
`goal_internal_cpm` is), 17-digit Meta campaign ids have to be read as text or
they lose their last digits, and one strategy running several creatives
produces several rows for a day, which are summed. A blank `line_item_id` gets
a deterministic key of its own, because leaving it empty collapsed 3% of rows
across hundreds of unrelated orders into one bucket.

For orders (`ingest/orders.py`): ids arrive as HTML
(`<a href="...viewOrder/2873">2873</a>`), header names repeat (`start_date`
twice, `total_campaign_impressions` four times, `months_running`
thirty-four times) with the value in whichever copy happens to carry it, dates
carry a time, the buyer carries their email, and the flattened join repeats
every line item many times over. Column matching is by alias on a simplified
header name, so the two exports seen so far - which differ from each other -
both map cleanly. Anything unrecognised is reported on the **Data** page
rather than silently dropped, so a changed export gets noticed.

**Two strategies can share a name.** The feed ships distinct strategy ids
under one name - two Meta ad sets both called "Facebook/Instagram Premium" -
so labels get a `(2)` suffix to keep them apart. Rows a buyer cannot tell
apart are rows they cannot pace.

**Adtini reporting, later.** The pacing engine reads `DailyDelivery` and
nothing else. Pointing it at Adtini's own reporting instead of the S3 drops
means writing a second loader into the same table; the engine, views and
exports do not change.


**The chart's palette is computed, not chosen.** The eight categorical colours
in `static/chart.js` are validated against the lightness band, chroma floor,
colour-vision separation, normal-vision floor and 3:1 contrast on a white
surface. The order is the colour-vision safety mechanism, so it must not be
reordered, and a ninth series folds into "Other" rather than reusing a colour.
Strategy names arrive from the feed and the orders export is known to carry
HTML in its fields, so every label goes into the DOM with `textContent` and
the chart reads its data from a JSON block rather than from generated script.
