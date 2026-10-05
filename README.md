# Educentric ACCOUNTS — Finance / Bursar system

Separate Django app for school fees and payments. Shares MySQL with
ADMINISTRATION and CLIENTS without owning their tables.

## Ownership

| Tables | Owner |
|---|---|
| `accounts_user`, `accounts_fee_*`, `accounts_payment` | **ACCOUNTS** (managed) |
| `admissions_student`, `admissions_parentguardian`, `employees_schoolprofile` | ADMINISTRATION (read-only mirrors) |

Sessions use cookie `edu_accounts_sessionid` so they never clash with the other apps.

## Setup

```bash
cd ACCOUNTS
pip install -r requirements.txt
copy .env.example .env
python manage.py ensure_db            # creates ACCOUNTS tables if missing (idempotent migrate)
python manage.py bootstrap_accounts
python manage.py runserver 8002
```

Open http://127.0.0.1:8002/auth/login/

Default login uses Administration employee accounts with role **Accountant** or **Store Manager**
(6-digit employee code + password from `employees_employee`).

## Ports (suggested)

- ADMINISTRATION → 8000
- CLIENTS → 8001
- ACCOUNTS → 8002

## Independence

If ADMINISTRATION or CLIENTS crash, Accounts keeps running as long as MySQL is up.
Accounts does not write admissions/employee tables.

## cPanel capacity

See `scripts/cpanel_capacity_checklist.txt` for Passenger pool size, `.env` flags
(`LOCAL=False`, `HOSTED_DEBUG=False`, `DB_CONN_MAX_AGE=0`, `HOSTED_SERVE_MEDIA=False`
after Apache media works), shared `MEDIA_ROOT` / `.htaccess`, and overload guards
(per-user report lock, report row/period caps, fee-structure apply lock/cap, fee level pagination).

Load test: `python manage.py load_probe --base-url http://127.0.0.1:8002 --users 20`

After deploy: `touch tmp/restart.txt`. Ensure `tmp/django_cache/` is writable.
