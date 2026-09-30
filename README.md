# Field Asset Check-Out Service

A small Django REST API that tracks equipment checked out to and returned by employees. It's built for the Artikate backend take-home.

- **Stack:** Django 5.1, Django REST Framework, PostgreSQL 16, Celery 5 with Redis, pytest-django, Docker Compose.
- **Written answers** for Parts B, C and D are in [`ANSWERS.md`](ANSWERS.md).
- **Screen recording:** _link to be added here_ (see Known gaps).

---

## Quick start (clone → working API)

Requires Docker with the Compose plugin.

```bash
git clone <this repo> && cd artikate-asset-checkout

docker compose up -d --build                              # web, db, redis, worker
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data   # prints an API token
```

The API is now on **http://localhost:8000/api/v1/**. If port 8000 is taken on your machine, run `WEB_PORT=8001 docker compose up -d --build` instead, and use that port below.

```bash
curl http://localhost:8000/api/v1/health/                 # {"status":"ok","database":"ok"}

TOKEN=<token printed by seed_demo_data>
curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/v1/reports/overdue/
```

**Run the tests** (inside the container, against the real Postgres):

```bash
docker compose exec web pytest -q                         # 49 tests
```

**Run the Celery task by hand** (Beat also runs it hourly at minute 0):

```bash
docker compose exec worker celery -A config call assets.tasks.flag_overdue_checkouts
docker compose logs worker | grep flag_overdue            # "created N notice(s) for <date>"
```

**Start over from an empty database:** `docker compose down -v`, then repeat the commands above.

### Running without Docker (optional)

You need a Postgres 16 and a Redis on localhost. The defaults in `config/settings.py` expect user, password and database all set to `assets`.

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate && python manage.py seed_demo_data
python manage.py runserver
pytest -q
```

SQLite is deliberately **not** supported. The concurrency rule relies on `SELECT ... FOR UPDATE` and a partial unique index.

---

## Authentication

This uses **DRF token authentication**. `seed_demo_data` creates the user `reviewer` (password `reviewer-demo-pass`, for demo use only) and prints its token. Any user can get a token with:

```bash
curl -X POST -d "username=reviewer&password=reviewer-demo-pass" http://localhost:8000/api/v1/auth/token/
```

Send it as `Authorization: Token <key>`. Session auth is also enabled, so after logging in at `/admin/` the browsable API works in a browser. Every endpoint except `/health/` requires authentication.

## Endpoints

Everything is under `/api/v1/`. List endpoints are paginated at 20 per page (`?page=2`).

| Method & path | Notes |
|---|---|
| `POST /assets/` | Create an asset. `status` may be `AVAILABLE` or `MAINTENANCE`. `CHECKED_OUT` is rejected (400), because only `POST /checkouts/` may produce it. |
| `GET /assets/` | Supports `?status=`, `?category=` and `?search=` (matches `name` or `asset_tag`, case-insensitive). |
| `GET /assets/{id}/` | Includes `current_holder`: `null`, or `{employee_code, full_name}`. |
| `POST /checkouts/` | Body `{asset_tag, employee_code, due_at}`. Returns 201. Rules 1–5, 7 and 8. |
| `GET /checkouts/`, `GET /checkouts/{id}/` | Convenience read endpoints (not required by the brief). |
| `POST /checkouts/{id}/return/` | Body `{condition_note, needs_maintenance}`. Returns 200. Rule 6. |
| `GET /employees/{employee_code}/summary/` | `lifetime_checkouts`, `currently_held`, `currently_overdue`, `mean_hold_days`, computed in **one** query. |
| `GET /reports/overdue/` | Open check-outs past due, most overdue first. Each row has asset name and tag, employee code and name, and `days_overdue`. One query per page. |
| `GET /health/` | Unauthenticated. Returns 200 `{"status":"ok","database":"ok"}`, or 503 if the database is unreachable. |
| `POST /auth/token/` | Exchange a username and password for a token. |

**Example check-out:**

```bash
curl -X POST http://localhost:8000/api/v1/checkouts/ \
  -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"asset_tag":"DEMO-LAP-02","employee_code":"EMP003","due_at":"2026-10-05T10:00:00Z"}'
```

## Status codes, by business rule

| Rule | Response |
|---|---|
| 1. Asset not `AVAILABLE` | 409 |
| 2. Employee inactive | 400 |
| 3. Employee already holds 3 open check-outs | 409 |
| 4. `due_at` not in the future, or more than 30 days ahead | 400 |
| 5. Check-out row and asset status change together | one transaction |
| 6. Returning an already-returned check-out | 409 |
| 7. Two simultaneous check-outs of one asset | exactly one 201, the other 409 |
| 8. Unknown `asset_tag` or `employee_code` (or check-out id) | 404 |
| Missing or malformed fields | 400, never 500 |

---

## Design notes

### How the concurrency rule is solved at the database level (rule 7)

`assets/services.py::check_out` runs in one `transaction.atomic()` block:

1. `Asset ... select_for_update()` takes a **row lock** on the asset *before* reading its status. A second request for the same asset blocks on that lock until the first commits. It then reads the committed status (`CHECKED_OUT`) and returns 409.
2. `Employee ... select_for_update()` locks the employee too. Otherwise two simultaneous check-outs of *different* assets by the *same* employee would both count 2 open items and both succeed, breaking rule 3.
3. Locks are always taken in the order **Asset → Employee**, and the return path never locks an employee. The two code paths therefore can't deadlock each other.
4. **Backstop:** a partial unique index, `uniq_open_checkout_per_asset` on `(asset) WHERE returned_at IS NULL`. Even if some future code path skips the lock, the database refuses a second open check-out for the same asset. The resulting `IntegrityError` is also mapped to 409.

### Other decisions

- **The rules live in `services.py` and `selectors.py`, not in the views**, so they can be tested and reused without HTTP.
- **Employee summary** is one annotated query on `Employee`: conditional `COUNT`s plus an `AVG(returned_at - checked_out_at)`. There's a single join, so the counts can't be inflated by row fan-out. `test_computed_in_a_single_query` asserts exactly one query.
- **Overdue report** uses `select_related` and `only()`, so it costs one query per page no matter how many rows. It's served by the partial index `checkout_open_due_idx (due_at) WHERE returned_at IS NULL`.
- **The Celery task is idempotent in two ways:**
  - It excludes check-outs already noticed today.
  - It inserts with `ON CONFLICT DO NOTHING` against the `(checkout, notice_date)` unique constraint.

  Five runs a day, or two workers at once, still leave one notice per check-out per day. It streams ids in batches of 1,000.
- **`acks_late=True`:** if a worker crashes mid-task, the task is redelivered. That's safe *because* the task is idempotent.

### Extra schema items beyond the brief (no new fields)

- `CheckOut`: the partial unique index `uniq_open_checkout_per_asset` (explained above).
- `CheckOut`: the partial index `checkout_open_due_idx` on `due_at WHERE returned_at IS NULL`. It serves the overdue report and the Celery task.

---

## Assumptions

1. **"Overdue" means `due_at < now`, strictly.** An item due *exactly now* is not yet overdue; one microsecond later it is. This is tested.
2. **`days_overdue` counts whole days, rounded down.** Something due 5 minutes ago shows `0` days overdue but still appears in the report, because it is overdue.
3. **`due_at` of exactly now counts as "not in the future"** (400). Exactly 30 days ahead is allowed; beyond that is 400. `due_at` with no timezone is read as **UTC**.
4. **Everything runs in UTC,** including "today" for `OverdueNotice.notice_date`. For an India-based team I would set `TIME_ZONE = "Asia/Kolkata"`, so that notices are dated by the local business day. It's a one-line change, but it is a product decision.
5. **Order of checks on `POST /checkouts/`:** malformed input or bad `due_at` (400) → unknown asset or employee (404) → inactive employee (400) → asset not available (409) → employee at limit (409). The brief doesn't fix this order when several rules fail at once.
6. **Assets can be created as `AVAILABLE` or `MAINTENANCE`, not `CHECKED_OUT`.** Creating one as `CHECKED_OUT` would mean an asset that is checked out to nobody.
7. **Returning an asset** sets `returned_at` to the server's current time and overwrites `condition_note` with the value sent (an empty string if it's omitted).
8. **`mean_hold_days`** is the mean over *returned* items only, rounded to 2 decimal places. It's `null` when nothing has been returned.
9. **The summary endpoint returns 404 for an unknown employee code.** It works for inactive employees (their history is still meaningful).
10. **`seed_demo_data` rebuilds its own demo check-outs every run** (only those on `DEMO-` assets), so the overdue / on-time / late spread stays correct no matter when you run it. That also means it discards any check-outs you made against `DEMO-` assets.
11. **Celery Beat runs embedded in the worker (`-B`),** to stay within the four services asked for. With several workers in production, Beat should be its own single-replica service, or the schedule would fire once per worker.
12. **`GET /checkouts/` list and detail, and `POST /auth/token/`,** were added for convenience. The brief doesn't ask for them.

## Known gaps

- **Screen recording:** not linked yet. It will be added here before the repository is submitted.
- **No CRUD endpoints for employees.** The brief lists none. Employees come from `seed_demo_data` or `/admin/`.
- **The concurrency tests use threads within one process,** each with its own database connection. That is a real test of the database locking, but not of multiple gunicorn processes.
- **The single-asset race test passes even with the asset lock removed,** because the partial unique index still produces the 409 (found by deliberately removing the locks; see the commit history). The same-employee race test does fail without the employee lock, so it guards that lock.
- **No rate limiting and no per-user authorization.** Any authenticated user can check anything out to anyone. That's fine for an internal tool, but it's worth stating.

## The decision I'm least sure about

**Locking the Employee row on every check-out.** It's what makes rule 3 correct under concurrency, and the same-employee race test shows that without it an employee can end up holding four items. But it serializes *all* check-outs by one employee, even ones for different assets.

The alternative is to enforce the limit in the database: for example a counter on `Employee` with a `CHECK (open_count <= 3)`, maintained in the same transaction, or a trigger. That turns the rule into a constraint rather than a code path. But it adds a denormalized field the brief asks us not to add without a reason. For an internal tool where one person rarely checks out several items in the same second, the row lock is simpler and correct, so I went with it. At much higher write volume I would revisit it.

## Project layout

```
config/            settings, urls, celery app
assets/
  models.py        Asset, Employee, CheckOut, OverdueNotice (+ constraints and indexes)
  services.py      check_out / return_checkout: transactions and locking
  selectors.py     overdue query, employee summary, current-holder annotation
  serializers.py   request and response shapes
  views.py         DRF views (thin)
  tasks.py         flag_overdue_checkouts (Celery)
  management/commands/seed_demo_data.py
tests/             49 pytest-django tests (rules, concurrency, reports, task, seed)
ANSWERS.md         Parts B, C, D
.github/workflows/ci.yml
```
