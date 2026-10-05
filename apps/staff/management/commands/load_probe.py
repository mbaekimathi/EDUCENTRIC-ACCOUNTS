"""Concurrent HTTP load / latency probe against a running Accounts server."""

from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.core.management.base import BaseCommand

from apps.directory.models import Employee
from apps.staff.models import AccountsUser
from apps.staff.views import sync_accounts_user_from_employee

DEFAULT_PATHS = (
    "/",
    "/accounts-dashboard/",
    "/accounts-dashboard/student-fees/",
    "/accounts-dashboard/reports/",
    "/accounts-dashboard/invoices/",
    "/accounts-dashboard/store-management/",
)


@dataclass
class Sample:
    path: str
    status: int
    ms: float
    bytes: int
    error: str = ""


@dataclass
class PathStats:
    path: str
    samples: list[Sample] = field(default_factory=list)

    def add(self, sample: Sample):
        self.samples.append(sample)


def _auth_cookie(user: AccountsUser) -> str:
    store = SessionStore()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    store.save()
    return store.session_key


def _pick_users(limit: int) -> list[AccountsUser]:
    employees = list(
        Employee.objects.filter(
            is_active=True,
            approval_status="APPROVED",
            role__in=Employee.PORTAL_ROLES,
        ).order_by("id")[:limit]
    )
    users = []
    for employee in employees:
        users.append(sync_accounts_user_from_employee(employee))
    return users[:limit]


class Command(BaseCommand):
    help = "Probe Accounts portal latency, concurrent capacity, and error rates."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8002")
        parser.add_argument("--users", type=int, default=20)
        parser.add_argument("--rounds", type=int, default=2)
        parser.add_argument("--timeout", type=float, default=30.0)
        parser.add_argument(
            "--ramp",
            type=str,
            default="1,5,10,15,20,30,40",
            help="Comma-separated concurrent user counts for breakpoint ramp.",
        )
        parser.add_argument("--json-out", default="")

    def handle(self, *args, **options):
        base = options["base_url"].rstrip("/")
        paths = list(DEFAULT_PATHS)
        users = _pick_users(options["users"])
        if not users:
            self.stderr.write("No portal employees found for probe sessions.")
            return
        cookie_name = "edu_accounts_sessionid"
        cookies = {u.pk: _auth_cookie(u) for u in users}

        self.stdout.write(self.style.NOTICE(f"Base URL: {base}"))
        self.stdout.write(
            self.style.NOTICE(f"Users prepared: {len(users)} (sessions: {len(cookies)})")
        )

        page_stats: dict[str, PathStats] = {p: PathStats(p) for p in paths}
        probe_user = users[0]
        session = requests.Session()
        session.cookies.set(cookie_name, cookies[probe_user.pk])
        for path in paths:
            url = f"{base}{path}"
            t0 = time.perf_counter()
            try:
                resp = session.get(url, timeout=options["timeout"], allow_redirects=True)
                ms = (time.perf_counter() - t0) * 1000
                sample = Sample(path, resp.status_code, ms, len(resp.content))
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000
                sample = Sample(path, 0, ms, 0, error=str(exc)[:160])
            page_stats[path].add(sample)
            mark = "OK" if sample.status and sample.status < 400 else "FAIL"
            self.stdout.write(
                f"  [{mark}] {sample.status:3}  {sample.ms:7.1f} ms  {path}"
                + (f"  ({sample.error})" if sample.error else "")
            )

        ramp = [int(x.strip()) for x in options["ramp"].split(",") if x.strip()]
        ramp_results = []
        for n in ramp:
            n = min(n, len(users))
            if n < 1:
                continue
            result = self._run_wave(
                base=base,
                paths=paths,
                users=users[:n],
                cookies=cookies,
                cookie_name=cookie_name,
                rounds=options["rounds"],
                timeout=options["timeout"],
            )
            ramp_results.append(result)
            self.stdout.write(
                f"  users={result['users']:3d}  ok={result['ok_pct']:5.1f}%  "
                f"p95={result['p95_ms']:7.1f}ms  err500={result['err500']:3d}"
            )
            if result["ok_pct"] < 90 or result["p95_ms"] > 8000:
                self.stdout.write(
                    self.style.WARNING(
                        f"  >> Breakpoint signal around {n} concurrent users"
                    )
                )
                break

        summary = {"page_timings": {}, "ramp": ramp_results}
        if options["json_out"]:
            with open(options["json_out"], "w", encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)
            self.stdout.write(self.style.SUCCESS(f"Wrote {options['json_out']}"))
        self.stdout.write(self.style.SUCCESS("Load probe complete."))

    def _run_wave(self, *, base, paths, users, cookies, cookie_name, rounds, timeout):
        jobs = []
        for _round in range(rounds):
            for idx, user in enumerate(users):
                jobs.append((user, paths[idx % len(paths)]))

        samples: list[Sample] = []
        t_start = time.perf_counter()

        def hit(user, path):
            url = f"{base}{path}"
            t0 = time.perf_counter()
            try:
                resp = requests.get(
                    url,
                    cookies={cookie_name: cookies[user.pk]},
                    timeout=timeout,
                    allow_redirects=True,
                )
                return Sample(
                    path, resp.status_code, (time.perf_counter() - t0) * 1000, len(resp.content)
                )
            except Exception as exc:
                return Sample(path, 0, (time.perf_counter() - t0) * 1000, 0, error=str(exc)[:160])

        with ThreadPoolExecutor(max_workers=max(len(users), 1)) as pool:
            futures = [pool.submit(hit, u, p) for u, p in jobs]
            for fut in as_completed(futures):
                samples.append(fut.result())

        elapsed = max(time.perf_counter() - t_start, 0.001)
        latencies = sorted(s.ms for s in samples) if samples else [0]
        ok = [s for s in samples if 200 <= s.status < 400 and not s.error]
        err500 = [s for s in samples if s.status >= 500]

        def pct(p):
            idx = min(len(latencies) - 1, int(round((p / 100) * (len(latencies) - 1))))
            return latencies[idx]

        return {
            "users": len(users),
            "requests": len(samples),
            "ok_pct": round(100 * len(ok) / max(len(samples), 1), 1),
            "err500": len(err500),
            "p50_ms": round(pct(50), 1),
            "p95_ms": round(pct(95), 1),
            "max_ms": round(max(latencies), 1),
            "rps": round(len(samples) / elapsed, 1),
        }
