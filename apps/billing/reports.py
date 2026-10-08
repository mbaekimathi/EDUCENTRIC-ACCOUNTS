"""Accounts report catalogue and builders."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone as dj_timezone

REPORT_MAX_ROWS = 5000
REPORT_MAX_PERIOD_DAYS = 366
# Cap each cash-book source before merge so one stream cannot OOM a worker.
CASH_BOOK_SOURCE_FETCH_CAP = 2500

from apps.directory.models import Student

from .models import (
    AccountTopUp,
    AccountVote,
    AccountWithdraw,
    FeeCategory,
    FeeCharge,
    FeeStructure,
    Payment,
    SchoolAccount,
    StoreItem,
    StoreStockMovement,
    StoreSupplierPayment,
    StudentPocketMoneyEntry,
)

SUPPLIER_OPEN_PAYMENT_STATUSES = (
    StoreStockMovement.PaymentStatus.UNPAID,
    StoreStockMovement.PaymentStatus.PENDING,
    StoreStockMovement.PaymentStatus.PARTIAL,
)

REPORT_CATEGORIES = [
    {
        "key": "core_financial",
        "label": "1. Core Financial Statements (Compliance & Reporting)",
        "short_label": "Core Financial Statements",
        "description": "Core books of accounts and compliance financial statements.",
        "reports": [
            {
                "key": "cash_book",
                "label": "Cash Book",
                "description": "Daybook of receipts and payments for the selected period.",
                "support": "full",
            },
            {
                "key": "general_ledger",
                "label": "General Ledger",
                "description": "Ledger entries by school account with running balances.",
                "support": "full",
            },
            {
                "key": "trial_balance",
                "label": "Trial Balance",
                "description": "Debit and credit totals by school account for the selected period.",
                "support": "full",
            },
            {
                "key": "financial_performance",
                "label": "Statement of Financial Performance",
                "description": "Income and expenditure for the selected period.",
                "support": "full",
            },
            {
                "key": "financial_position",
                "label": "Statement of Financial Position",
                "description": "School fund balances by account as at the end date.",
                "support": "full",
            },
            {
                "key": "cash_flows",
                "label": "Statement of Cash Flows",
                "description": "Cash inflows and outflows classified for the selected period.",
                "support": "full",
            },
        ],
    },
    {
        "key": "revenue_fees",
        "label": "2. Revenue & Fee Management Reports",
        "short_label": "Revenue & Fee Management",
        "description": "Fee collections, arrears, expected revenue, and capitation utilisation.",
        "reports": [
            {
                "key": "fee_collection_register",
                "label": "Fee Collection Register",
                "description": "Learner fee receipts captured in the selected period.",
                "support": "full",
            },
            {
                "key": "expected_revenue",
                "label": "Expected Revenue Report",
                "description": "Expected fee income from active fee structures and posted charges.",
                "support": "full",
            },
            {
                "key": "overdue_payments",
                "label": "Overdue Payments Report",
                "description": "Charges past due date that still have an outstanding balance.",
                "support": "full",
            },
            {
                "key": "capitation_grant",
                "label": "Capitation Grant Utilisation Report",
                "description": "Top-ups and withdrawals on Capitation grant school accounts for the period.",
                "support": "full",
            },
        ],
    },
    {
        "key": "cost_expenditure",
        "label": "3. Cost & Expenditure Control Reports",
        "short_label": "Cost & Expenditure Control",
        "description": "Payroll, stores, payables, and transport-related expenditure control.",
        "reports": [
            {
                "key": "payroll",
                "label": "Payroll Report",
                "description": "Account withdrawals linked to payroll / salary votes or payees.",
                "support": "full",
            },
            {
                "key": "store_supplies_register",
                "label": "Store and Supplies Register",
                "description": "Store items and stock movements for the selected period.",
                "support": "full",
            },
            {
                "key": "accounts_payable",
                "label": "Accounts Payable Report",
                "description": "Supplier deliveries not fully paid, plus payments made in the period.",
                "support": "full",
            },
            {
                "key": "transport_vehicle",
                "label": "Transport and Vehicle Report",
                "description": "Transport fee activity and withdrawals linked to transport votes or accounts.",
                "support": "full",
            },
        ],
    },
    {
        "key": "budget_audit",
        "label": "4. Budgetary & Internal Audit Control Reports",
        "short_label": "Budgetary & Internal Audit Control",
        "description": "Budget variance, bank channel movements, and vote-book control.",
        "reports": [
            {
                "key": "vote_book_summaries",
                "label": "Vote Book Summaries",
                "description": "Summary of vote heads, allocation mode, amounts, and status by account.",
                "support": "full",
            },
            {
                "key": "budget_variance",
                "label": "Budget Variance Report",
                "description": "Vote allocations compared with fee collections against related vote heads.",
                "support": "full",
            },
            {
                "key": "bank_reconciliation",
                "label": "Bank Reconciliation Statement",
                "description": "Bank and cheque movements from fee receipts, top-ups, and withdrawals.",
                "support": "full",
            },
        ],
    },
    {
        "key": "pocket_money",
        "label": "5. Pocket Money Reports",
        "short_label": "Pocket Money",
        "description": "Learner pocket money ledger trial balance for active pocket money accounts.",
        "requires_active_pocket_money": True,
        "reports": [
            {
                "key": "pocket_money_trial_balance",
                "label": "Pocket Money Trial Balance",
                "description": (
                    "Trial balance of learner ledger accounts on active pocket money "
                    "school accounts for the selected period."
                ),
                "support": "full",
                "requires_active_pocket_money": True,
            },
        ],
    },
]

REPORT_TYPE_MAP = {
    report["key"]: {
        **report,
        "category_key": category["key"],
        "category_label": category["short_label"],
    }
    for category in REPORT_CATEGORIES
    for report in category["reports"]
}


def parse_report_date(value):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_report_period(date_from, date_to):
    days = (date_to - date_from).days + 1
    if days > REPORT_MAX_PERIOD_DAYS:
        return (
            f"Date range is too long ({days} days; limit {REPORT_MAX_PERIOD_DAYS}). "
            "Choose a shorter academic term or custom range."
        )
    return None


def money(value):
    return f"KES {(value or Decimal('0.00')):,.2f}"


def _period_datetime_bounds(date_from, date_to):
    """Inclusive calendar dates as aware [start, end) datetimes.

    Avoids SQLite `created_at__date` lookups, which can miss timezone-aware rows.
    """
    tz = dj_timezone.get_current_timezone()
    start = dj_timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end = dj_timezone.make_aware(
        datetime.combine(date_to + timedelta(days=1), time.min), tz
    )
    return start, end


def _period_filter(field, date_from, date_to):
    """Filter kwargs for a DateTimeField over inclusive local calendar dates."""
    period_start, period_end = _period_datetime_bounds(date_from, date_to)
    return {f"{field}__gte": period_start, f"{field}__lt": period_end}


def _before_date(field, day):
    """Filter kwargs for DateTimeField values strictly before a local calendar day."""
    period_start, _ = _period_datetime_bounds(day, day)
    return {f"{field}__lt": period_start}


def _through_date(field, day):
    """Filter kwargs for DateTimeField values through end of a local calendar day."""
    _, period_end = _period_datetime_bounds(day, day)
    return {f"{field}__lt": period_end}


def _sum_by_account_in_period(queryset, date_from, date_to, *, amount_field="amount"):
    return _sum_by_account(
        queryset.filter(**_period_filter("created_at", date_from, date_to)),
        amount_field=amount_field,
    )


def _sum_by_account(queryset, *, account_field="account_id", amount_field="amount"):
    return {
        row[account_field]: row["total"] or Decimal("0.00")
        for row in queryset.values(account_field).annotate(total=Sum(amount_field))
        if row[account_field] is not None
    }


def _limited_list(queryset, limit):
    items = list(queryset[: limit + 1])
    if len(items) > limit:
        return items[:limit], True
    return items, False


def _student_label_map(student_ids):
    return {
        student.id: student
        for student in Student.objects.filter(id__in=student_ids)
    }


def build_report(report_type, date_from, date_to, *, max_rows=REPORT_MAX_ROWS):
    """Return columns, rows, summary, and optional support note for a report."""
    period_error = validate_report_period(date_from, date_to)
    if period_error:
        raise ValueError(period_error)

    zero = Decimal("0.00")
    meta = REPORT_TYPE_MAP.get(report_type)
    if meta is None:
        raise ValueError("Unknown report type.")

    def finish(columns, rows, summary, support_note=None):
        row_total = len(rows)
        if row_total > max_rows:
            rows = rows[:max_rows]
        notes = [support_note or meta.get("support_note", "")]
        if row_total > max_rows:
            notes.append(
                f"Showing the first {max_rows:,} of {row_total:,} rows. "
                "Narrow the date range for full detail."
            )
        combined_note = " ".join(part for part in notes if part).strip()
        return {
            "columns": columns,
            "rows": rows,
            "summary": summary,
            "support": meta.get("support", "full"),
            "support_note": combined_note,
            "truncated": row_total > max_rows,
            "row_total": row_total,
        }

    if report_type == "financial_performance":
        fee_income = (
            Payment.objects.filter(
                **_period_filter("received_at", date_from, date_to),
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        other_income = (
            AccountTopUp.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountTopUp.Status.APPROVED,
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        expenditure = (
            AccountWithdraw.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountWithdraw.Status.APPROVED,
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        supplier_spend = (
            StoreSupplierPayment.objects.filter(
                **_period_filter("created_at", date_from, date_to),
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        total_income = fee_income + other_income
        total_expense = expenditure + supplier_spend
        rows = [
            ["Income", "Fee collections", money(fee_income)],
            ["Income", "Account top-ups / other receipts", money(other_income)],
            ["Expenditure", "Account withdrawals", money(expenditure)],
            ["Expenditure", "Supplier payments", money(supplier_spend)],
            ["Result", "Surplus / (deficit)", money(total_income - total_expense)],
        ]
        return finish(
            ["Section", "Particulars", "Amount"],
            rows,
            [
                {"label": "Total income", "value": money(total_income), "tone": "brand"},
                {"label": "Total expenditure", "value": money(total_expense), "tone": "accent"},
                {
                    "label": "Surplus / (deficit)",
                    "value": money(total_income - total_expense),
                    "tone": "",
                },
            ],
        )

    if report_type == "financial_position":
        accounts = list(SchoolAccount.objects.order_by("category", "name"))
        topup_map = _sum_by_account(
            AccountTopUp.objects.filter(status=AccountTopUp.Status.APPROVED)
        )
        withdraw_map = _sum_by_account(
            AccountWithdraw.objects.filter(status=AccountWithdraw.Status.APPROVED)
        )
        rows = []
        total = zero
        for account in accounts:
            balance = topup_map.get(account.id, zero) - withdraw_map.get(
                account.id, zero
            )
            total += balance
            rows.append(
                [
                    account.category_label,
                    account.name,
                    "Active" if account.is_active else "Suspended",
                    money(balance),
                ]
            )
        return finish(
            ["Fund / category", "Account", "Status", "Balance"],
            rows,
            [
                {"label": "Accounts", "value": str(len(rows)), "tone": ""},
                {"label": "Total funds", "value": money(total), "tone": "brand"},
            ],
        )

    if report_type == "cash_flows":
        fee_in = (
            Payment.objects.filter(
                **_period_filter("received_at", date_from, date_to),
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        topup_in = (
            AccountTopUp.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountTopUp.Status.APPROVED,
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        withdraw_out = (
            AccountWithdraw.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountWithdraw.Status.APPROVED,
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        supplier_out = (
            StoreSupplierPayment.objects.filter(
                **_period_filter("created_at", date_from, date_to),
            ).aggregate(total=Sum("amount"))["total"]
            or zero
        )
        net = fee_in + topup_in - withdraw_out - supplier_out
        rows = [
            ["Operating inflow", "Fee collections", money(fee_in)],
            ["Operating inflow", "Account top-ups", money(topup_in)],
            ["Operating outflow", "Withdrawals", money(withdraw_out)],
            ["Operating outflow", "Supplier payments", money(supplier_out)],
            ["Net cash flow", "Increase / (decrease) in cash", money(net)],
        ]
        return finish(
            ["Cash-flow class", "Particulars", "Amount"],
            rows,
            [
                {"label": "Cash inflows", "value": money(fee_in + topup_in), "tone": "brand"},
                {
                    "label": "Cash outflows",
                    "value": money(withdraw_out + supplier_out),
                    "tone": "accent",
                },
                {"label": "Net cash flow", "value": money(net), "tone": ""},
            ],
        )

    if report_type == "trial_balance":
        accounts = list(SchoolAccount.objects.order_by("name"))
        debit_map = _sum_by_account_in_period(
            AccountWithdraw.objects.filter(status=AccountWithdraw.Status.APPROVED),
            date_from,
            date_to,
        )
        credit_map = _sum_by_account_in_period(
            AccountTopUp.objects.filter(status=AccountTopUp.Status.APPROVED),
            date_from,
            date_to,
        )
        rows = []
        debit_total = zero
        credit_total = zero
        for account in accounts:
            debits = debit_map.get(account.id, zero)
            credits = credit_map.get(account.id, zero)
            if debits == zero and credits == zero:
                continue
            debit_total += debits
            credit_total += credits
            rows.append(
                [
                    account.name,
                    account.category_label,
                    money(debits),
                    money(credits),
                    money(credits - debits),
                ]
            )
        return finish(
            ["Account", "Category", "Debit", "Credit", "Net"],
            rows,
            [
                {"label": "Total debits", "value": money(debit_total), "tone": "accent"},
                {"label": "Total credits", "value": money(credit_total), "tone": "brand"},
                {
                    "label": "Difference",
                    "value": money(credit_total - debit_total),
                    "tone": "",
                },
            ],
        )

    if report_type == "pocket_money_trial_balance":
        pocket_accounts = list(
            SchoolAccount.objects.filter(
                category=SchoolAccount.Category.POCKET_MONEY,
                is_active=True,
            ).order_by("name")
        )
        columns = [
            "School account",
            "Ledger",
            "Particulars",
            "Debit",
            "Credit",
            "Closing balance",
        ]
        if not pocket_accounts:
            return finish(
                columns,
                [],
                [
                    {"label": "Active pocket accounts", "value": "0", "tone": ""},
                    {"label": "Ledger accounts", "value": "0", "tone": ""},
                ],
                support_note=(
                    "No active pocket money school account is available. "
                    "Activate a pocket money account to generate this trial balance."
                ),
            )

        account_ids = [account.id for account in pocket_accounts]
        period_start, period_end = _period_datetime_bounds(date_from, date_to)

        # Prefer learner pocket-money entries; also fall back to account float movements.
        entry_qs = StudentPocketMoneyEntry.objects.filter(
            account_id__in=account_ids,
            status=StudentPocketMoneyEntry.Status.APPROVED,
        )
        period_entry_rows = list(
            entry_qs.filter(
                created_at__gte=period_start,
                created_at__lt=period_end,
            ).values("account_id", "student_id", "direction", "amount")
        )
        closing_entry_rows = list(
            entry_qs.filter(created_at__lt=period_end).values(
                "account_id", "student_id", "direction", "amount"
            )
        )

        period_debit = defaultdict(lambda: zero)
        period_credit = defaultdict(lambda: zero)
        for row in period_entry_rows:
            key = (row["account_id"], row["student_id"])
            amount = row["amount"] or zero
            if row["direction"] == StudentPocketMoneyEntry.Direction.DEBIT:
                period_debit[key] += amount
            else:
                period_credit[key] += amount

        closing_debit = defaultdict(lambda: zero)
        closing_credit = defaultdict(lambda: zero)
        for row in closing_entry_rows:
            key = (row["account_id"], row["student_id"])
            amount = row["amount"] or zero
            if row["direction"] == StudentPocketMoneyEntry.Direction.DEBIT:
                closing_debit[key] += amount
            else:
                closing_credit[key] += amount

        # Account-level float (top-ups / withdrawals) for the same pocket accounts.
        account_period_credit = _sum_by_account_in_period(
            AccountTopUp.objects.filter(
                account_id__in=account_ids,
                status=AccountTopUp.Status.APPROVED,
            ),
            date_from,
            date_to,
        )
        account_period_debit = _sum_by_account_in_period(
            AccountWithdraw.objects.filter(
                account_id__in=account_ids,
                status=AccountWithdraw.Status.APPROVED,
            ),
            date_from,
            date_to,
        )
        account_closing_credit = _sum_by_account(
            AccountTopUp.objects.filter(
                account_id__in=account_ids,
                status=AccountTopUp.Status.APPROVED,
                created_at__lt=period_end,
            )
        )
        account_closing_debit = _sum_by_account(
            AccountWithdraw.objects.filter(
                account_id__in=account_ids,
                status=AccountWithdraw.Status.APPROVED,
                created_at__lt=period_end,
            )
        )

        ledger_keys = sorted(
            set(period_debit)
            | set(period_credit)
            | set(closing_debit)
            | set(closing_credit)
        )
        student_map = _student_label_map({key[1] for key in ledger_keys})

        rows = []
        debit_total = zero
        credit_total = zero
        balance_total = zero
        learner_count = 0

        for account in pocket_accounts:
            acc_debit = account_period_debit.get(account.id, zero)
            acc_credit = account_period_credit.get(account.id, zero)
            acc_closing = account_closing_credit.get(
                account.id, zero
            ) - account_closing_debit.get(account.id, zero)
            rows.append(
                [
                    account.name,
                    "School account",
                    "Pocket money float",
                    money(acc_debit),
                    money(acc_credit),
                    money(acc_closing),
                ]
            )
            debit_total += acc_debit
            credit_total += acc_credit
            balance_total += acc_closing

            learner_keys = [
                key for key in ledger_keys if key[0] == account.id
            ]
            learner_keys.sort(
                key=lambda item: (
                    (student_map[item[1]].display_name if item[1] in student_map else ""),
                    item[1],
                )
            )
            for account_id, student_id in learner_keys:
                debits = period_debit[(account_id, student_id)]
                credits = period_credit[(account_id, student_id)]
                closing = (
                    closing_credit[(account_id, student_id)]
                    - closing_debit[(account_id, student_id)]
                )
                if debits == zero and credits == zero and closing == zero:
                    continue
                student = student_map.get(student_id)
                learner_name = (
                    student.display_name
                    if student is not None
                    else f"Learner #{student_id}"
                )
                admission = "—"
                if student is not None:
                    admission = (
                        student.admission_number or student.assessment_number or "—"
                    )
                learner_count += 1
                rows.append(
                    [
                        account.name,
                        "Learner ledger",
                        f"{learner_name} ({admission})",
                        money(debits),
                        money(credits),
                        money(closing),
                    ]
                )

        return finish(
            columns,
            rows,
            [
                {
                    "label": "Active pocket accounts",
                    "value": str(len(pocket_accounts)),
                    "tone": "",
                },
                {
                    "label": "Learner ledgers",
                    "value": str(learner_count),
                    "tone": "",
                },
                {"label": "Total debits", "value": money(debit_total), "tone": "accent"},
                {"label": "Total credits", "value": money(credit_total), "tone": "brand"},
                {
                    "label": "School float closing",
                    "value": money(balance_total),
                    "tone": "brand",
                },
            ],
            support_note=(
                "Shows active pocket money school accounts and their learner ledgers. "
                "Debit/Credit are period movements; closing balance is as at the period end."
            ),
        )

    if report_type == "cash_book":
        source_cap = min(CASH_BOOK_SOURCE_FETCH_CAP, max_rows + 500)
        payments, _ = _limited_list(
            Payment.objects.filter(
                **_period_filter("received_at", date_from, date_to),
            ).select_related("charge", "charge__category"),
            source_cap,
        )
        topups, _ = _limited_list(
            AccountTopUp.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountTopUp.Status.APPROVED,
            ).select_related("account"),
            source_cap,
        )
        withdrawals, _ = _limited_list(
            AccountWithdraw.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountWithdraw.Status.APPROVED,
            ).select_related("account"),
            source_cap,
        )
        supplier_pays, _ = _limited_list(
            StoreSupplierPayment.objects.filter(
                **_period_filter("created_at", date_from, date_to),
            ).select_related("movement__supplier", "account"),
            source_cap,
        )
        student_map = _student_label_map({p.student_id for p in payments})
        entries = []
        for payment in payments:
            student = student_map.get(payment.student_id)
            entries.append(
                {
                    "sort": payment.received_at,
                    "date": payment.received_at.strftime("%d %b %Y"),
                    "reference": payment.reference or "—",
                    "particulars": (
                        f"Fee receipt · "
                        f"{student.display_name if student else f'Student #{payment.student_id}'}"
                        + (
                            f" · {payment.charge.category.name}"
                            if payment.charge_id
                            else ""
                        )
                    ),
                    "method": payment.get_method_display(),
                    "receipt": payment.amount or zero,
                    "payment": zero,
                }
            )
        for topup in topups:
            entries.append(
                {
                    "sort": topup.created_at,
                    "date": topup.created_at.strftime("%d %b %Y"),
                    "reference": topup.reference_code,
                    "particulars": f"Top-up · {topup.account.name}",
                    "method": topup.get_method_display(),
                    "receipt": topup.amount or zero,
                    "payment": zero,
                }
            )
        for withdrawal in withdrawals:
            entries.append(
                {
                    "sort": withdrawal.created_at,
                    "date": withdrawal.created_at.strftime("%d %b %Y"),
                    "reference": withdrawal.reference_code,
                    "particulars": (
                        f"Withdrawal · {withdrawal.account.name} · {withdrawal.payee}"
                    ),
                    "method": withdrawal.get_method_display(),
                    "receipt": zero,
                    "payment": withdrawal.amount or zero,
                }
            )
        for pay in supplier_pays:
            supplier = (
                pay.movement.supplier.name
                if pay.movement_id and pay.movement.supplier_id
                else "Supplier"
            )
            entries.append(
                {
                    "sort": pay.created_at,
                    "date": pay.created_at.strftime("%d %b %Y"),
                    "reference": pay.reference_code,
                    "particulars": f"Supplier payment · {supplier} · {pay.account.name}",
                    "method": pay.get_method_display(),
                    "receipt": zero,
                    "payment": pay.amount or zero,
                }
            )
        entries.sort(key=lambda row: row["sort"])
        running = zero
        rows = []
        total_receipts = zero
        total_payments = zero
        for entry in entries:
            total_receipts += entry["receipt"]
            total_payments += entry["payment"]
            running += entry["receipt"] - entry["payment"]
            rows.append(
                [
                    entry["date"],
                    entry["reference"],
                    entry["particulars"],
                    entry["method"],
                    money(entry["receipt"]) if entry["receipt"] else "—",
                    money(entry["payment"]) if entry["payment"] else "—",
                    money(running),
                ]
            )
        return finish(
            [
                "Date",
                "Reference",
                "Particulars",
                "Method",
                "Receipts",
                "Payments",
                "Balance",
            ],
            rows,
            [
                {"label": "Total receipts", "value": money(total_receipts), "tone": "brand"},
                {"label": "Total payments", "value": money(total_payments), "tone": "accent"},
                {"label": "Closing balance", "value": money(running), "tone": ""},
            ],
        )

    if report_type == "general_ledger":
        accounts = list(SchoolAccount.objects.order_by("category", "name"))
        opening_topups = _sum_by_account(
            AccountTopUp.objects.filter(
                status=AccountTopUp.Status.APPROVED,
                **_before_date("created_at", date_from),
            )
        )
        opening_withdrawals = _sum_by_account(
            AccountWithdraw.objects.filter(
                status=AccountWithdraw.Status.APPROVED,
                **_before_date("created_at", date_from),
            )
        )
        period_topups = list(
            AccountTopUp.objects.filter(
                status=AccountTopUp.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            ).order_by("created_at", "id")
        )
        period_withdrawals = list(
            AccountWithdraw.objects.filter(
                status=AccountWithdraw.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            ).order_by("created_at", "id")
        )
        period_supplier_pays = list(
            StoreSupplierPayment.objects.filter(
                **_period_filter("created_at", date_from, date_to),
            )
            .select_related("movement__supplier", "account")
            .order_by("created_at", "id")
        )
        topups_by_account = defaultdict(list)
        for topup in period_topups:
            topups_by_account[topup.account_id].append(topup)
        withdrawals_by_account = defaultdict(list)
        for withdrawal in period_withdrawals:
            withdrawals_by_account[withdrawal.account_id].append(withdrawal)
        pays_by_account = defaultdict(list)
        for pay in period_supplier_pays:
            pays_by_account[pay.account_id].append(pay)

        rows = []
        posted_accounts = 0
        movement_count = 0
        for account in accounts:
            opening = opening_topups.get(account.id, zero) - opening_withdrawals.get(
                account.id, zero
            )
            topups = topups_by_account.get(account.id, [])
            withdrawals = withdrawals_by_account.get(account.id, [])
            supplier_pays = pays_by_account.get(account.id, [])
            if not topups and not withdrawals and not supplier_pays and opening == zero:
                continue

            posted_accounts += 1
            balance = opening
            rows.append(
                [
                    account.name,
                    account.category_label,
                    date_from.strftime("%d %b %Y"),
                    "OPENING",
                    "Opening balance",
                    "—",
                    money(opening) if opening > 0 else "—",
                    money(abs(opening)) if opening < 0 else "—",
                    money(balance),
                ]
            )

            events = []
            for topup in topups:
                events.append(("C", topup.created_at, topup))
            for withdrawal in withdrawals:
                events.append(("D", withdrawal.created_at, withdrawal))
            for pay in supplier_pays:
                events.append(("S", pay.created_at, pay))
            events.sort(key=lambda item: (item[1], item[0], getattr(item[2], "id", 0)))

            for kind, _, item in events:
                movement_count += 1
                if kind == "C":
                    balance += item.amount or zero
                    rows.append(
                        [
                            account.name,
                            account.category_label,
                            item.created_at.strftime("%d %b %Y"),
                            item.reference_code,
                            item.description or "Account top-up",
                            item.get_method_display(),
                            money(item.amount),
                            "—",
                            money(balance),
                        ]
                    )
                elif kind == "D":
                    balance -= item.amount or zero
                    rows.append(
                        [
                            account.name,
                            account.category_label,
                            item.created_at.strftime("%d %b %Y"),
                            item.reference_code,
                            item.description or f"Withdrawal · {item.payee}",
                            item.get_method_display(),
                            "—",
                            money(item.amount),
                            money(balance),
                        ]
                    )
                else:
                    supplier = (
                        item.movement.supplier.name
                        if item.movement_id and item.movement.supplier_id
                        else "Supplier"
                    )
                    balance -= item.amount or zero
                    rows.append(
                        [
                            account.name,
                            account.category_label,
                            item.created_at.strftime("%d %b %Y"),
                            item.reference_code,
                            f"Supplier payment · {supplier}",
                            item.get_method_display(),
                            "—",
                            money(item.amount),
                            money(balance),
                        ]
                    )

        return finish(
            [
                "Account",
                "Category",
                "Date",
                "Reference",
                "Particulars",
                "Method",
                "Credit",
                "Debit",
                "Balance",
            ],
            rows,
            [
                {"label": "Accounts posted", "value": str(posted_accounts), "tone": ""},
                {"label": "Ledger lines", "value": str(movement_count), "tone": ""},
            ],
        )

    if report_type == "fee_collection_register":
        payments, _ = _limited_list(
            Payment.objects.filter(
                **_period_filter("received_at", date_from, date_to),
            )
            .select_related("charge", "charge__category", "received_by")
            .order_by("-received_at"),
            max_rows,
        )
        student_map = _student_label_map({p.student_id for p in payments})
        total = zero
        rows = []
        for payment in payments:
            student = student_map.get(payment.student_id)
            total += payment.amount or zero
            rows.append(
                [
                    payment.received_at.strftime("%d %b %Y %H:%M"),
                    payment.reference or "—",
                    student.display_name if student else f"Student #{payment.student_id}",
                    payment.charge.category.name if payment.charge_id else "Unallocated",
                    payment.get_method_display(),
                    money(payment.amount),
                ]
            )
        return finish(
            ["Date", "Reference", "Learner", "Fee category", "Method", "Amount"],
            rows,
            [
                {"label": "Receipts", "value": str(len(rows)), "tone": ""},
                {"label": "Total collected", "value": money(total), "tone": "brand"},
            ],
        )

    if report_type == "overdue_payments":
        charges, _ = _limited_list(
            FeeCharge.objects.exclude(
                status__in=[
                    FeeCharge.Status.CANCELLED,
                    FeeCharge.Status.WAIVED,
                    FeeCharge.Status.PAID,
                ]
            )
            .filter(due_date__isnull=False, due_date__lt=date_to)
            .select_related("category")
            .order_by("due_date", "created_at"),
            max_rows + 500,
        )
        student_map = _student_label_map({c.student_id for c in charges})
        outstanding = zero
        rows = []
        for charge in charges:
            balance = charge.balance
            if balance <= 0:
                continue
            outstanding += balance
            student = student_map.get(charge.student_id)
            days_overdue = (date_to - charge.due_date).days
            rows.append(
                [
                    charge.due_date.strftime("%d %b %Y"),
                    str(days_overdue),
                    charge.title,
                    student.display_name if student else f"Student #{charge.student_id}",
                    charge.category.name,
                    money(charge.amount),
                    money(charge.amount_paid),
                    money(balance),
                    charge.get_status_display(),
                ]
            )
        return finish(
            [
                "Due date",
                "Days overdue",
                "Charge",
                "Learner",
                "Category",
                "Charged",
                "Paid",
                "Balance",
                "Status",
            ],
            rows,
            [
                {"label": "Overdue items", "value": str(len(rows)), "tone": ""},
                {"label": "Overdue balance", "value": money(outstanding), "tone": "accent"},
            ],
        )

    if report_type == "expected_revenue":
        structures = list(
            FeeStructure.objects.filter(status=FeeStructure.Status.ACTIVE)
            .select_related("account", "financial_year", "financial_term")
            .prefetch_related("lines__vote")
        )
        rows = []
        expected_total = zero
        for structure in structures:
            structure_total = structure.total_amount
            expected_total += structure_total
            level_count = len(structure.academic_level_ids or [])
            rows.append(
                [
                    structure.display_name,
                    structure.account.name,
                    structure.financial_year.display_name,
                    structure.financial_term.name,
                    str(level_count),
                    str(structure.lines.count()),
                    money(structure_total),
                    structure.get_status_display(),
                ]
            )

        charges = FeeCharge.objects.exclude(
            status__in=[FeeCharge.Status.CANCELLED, FeeCharge.Status.WAIVED]
        ).filter(**_through_date("created_at", date_to))
        charged = charges.aggregate(total=Sum("amount"))["total"] or zero
        paid = charges.aggregate(total=Sum("amount_paid"))["total"] or zero
        rows.append(
            [
                "Posted charges (as at end date)",
                "All fee accounts",
                "—",
                "—",
                "—",
                str(charges.count()),
                money(charged),
                f"Collected {money(paid)}",
            ]
        )
        return finish(
            [
                "Structure / source",
                "Account",
                "Year",
                "Term",
                "Levels",
                "Lines / charges",
                "Expected / charged",
                "Status",
            ],
            rows,
            [
                {
                    "label": "Active structures total",
                    "value": money(expected_total),
                    "tone": "",
                },
                {"label": "Posted charges", "value": money(charged), "tone": "brand"},
                {"label": "Collected on charges", "value": money(paid), "tone": "accent"},
            ],
        )

    if report_type == "capitation_grant":
        # Linked to School Accounts: Capitation category / grant-labelled accounts.
        account_q = (
            Q(category=SchoolAccount.Category.CAPITATION)
            | Q(name__icontains="capitation")
            | Q(custom_category__icontains="capitation")
            | Q(name__icontains="grant")
            | Q(custom_category__icontains="grant")
        )
        accounts = list(SchoolAccount.objects.filter(account_q))
        account_ids = [account.id for account in accounts]
        topups = list(
            AccountTopUp.objects.filter(
                account_id__in=account_ids,
                status=AccountTopUp.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            ).select_related("account")
        )
        withdrawals = list(
            AccountWithdraw.objects.filter(
                account_id__in=account_ids,
                status=AccountWithdraw.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            ).select_related("account")
        )
        received = zero
        utilised = zero
        rows = []
        for topup in topups:
            received += topup.amount or zero
            rows.append(
                {
                    "date": topup.created_at,
                    "cells": [
                        topup.created_at.strftime("%d %b %Y"),
                        topup.reference_code,
                        "Receipt (top-up)",
                        topup.account.name,
                        topup.description or topup.reference_number,
                        money(topup.amount),
                        "—",
                    ],
                }
            )
        for withdrawal in withdrawals:
            utilised += withdrawal.amount or zero
            rows.append(
                {
                    "date": withdrawal.created_at,
                    "cells": [
                        withdrawal.created_at.strftime("%d %b %Y"),
                        withdrawal.reference_code,
                        "Utilisation (withdrawal)",
                        withdrawal.account.name,
                        withdrawal.description or withdrawal.payee,
                        "—",
                        money(withdrawal.amount),
                    ],
                }
            )
        rows.sort(key=lambda row: row["date"], reverse=True)
        return finish(
            [
                "Date",
                "Reference",
                "Type",
                "Account",
                "Particulars",
                "Received",
                "Utilised",
            ],
            [row["cells"] for row in rows],
            [
                {"label": "Capitation accounts", "value": str(len(accounts)), "tone": ""},
                {"label": "Received", "value": money(received), "tone": "brand"},
                {"label": "Utilised", "value": money(utilised), "tone": "accent"},
                {
                    "label": "Unutilised",
                    "value": money(received - utilised),
                    "tone": "",
                },
            ],
        )

    if report_type == "payroll":
        # Linked to School Accounts withdrawals + payroll/salary vote heads.
        vote_ids = list(
            AccountVote.objects.filter(
                Q(name__icontains="payroll")
                | Q(name__icontains="salary")
                | Q(name__icontains="wage")
                | Q(code__icontains="PAY")
            ).values_list("id", flat=True)
        )
        account_ids = list(
            AccountVote.objects.filter(id__in=vote_ids).values_list(
                "account_id", flat=True
            ).distinct()
        )
        withdraw_q = (
            Q(payee__icontains="payroll")
            | Q(payee__icontains="salary")
            | Q(payee__icontains="wage")
            | Q(description__icontains="payroll")
            | Q(description__icontains="salary")
            | Q(description__icontains="wage")
            | Q(account__name__icontains="payroll")
            | Q(account__name__icontains="salary")
        )
        if account_ids:
            withdraw_q |= Q(account_id__in=account_ids)
        entries, _ = _limited_list(
            AccountWithdraw.objects.filter(
                withdraw_q,
                status=AccountWithdraw.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            )
            .select_related("account")
            .order_by("-created_at"),
            max_rows,
        )
        rows = []
        total = zero
        for item in entries:
            total += item.amount or zero
            rows.append(
                [
                    item.created_at.strftime("%d %b %Y"),
                    item.reference_code,
                    item.payee,
                    item.account.name,
                    item.description or "—",
                    item.get_method_display(),
                    money(item.amount),
                ]
            )
        return finish(
            [
                "Date",
                "Reference",
                "Payee",
                "Account",
                "Description",
                "Method",
                "Amount",
            ],
            rows,
            [
                {"label": "Payroll entries", "value": str(len(rows)), "tone": ""},
                {"label": "Total paid", "value": money(total), "tone": "accent"},
            ],
        )

    if report_type == "store_supplies_register":
        movements, _ = _limited_list(
            StoreStockMovement.objects.filter(
                **_period_filter("created_at", date_from, date_to),
            )
            .select_related(
                "item",
                "item__expense_category",
                "item__department_station",
                "supplier",
                "destination_station",
            )
            .order_by("-created_at"),
            max_rows,
        )
        rows = []
        qty_in = zero
        qty_out = zero
        for movement in movements:
            if movement.direction == StoreStockMovement.Direction.IN:
                qty_in += movement.quantity or zero
                move_type = "Stock in"
                party = movement.supplier.name if movement.supplier_id else "—"
            else:
                qty_out += movement.quantity or zero
                move_type = movement.get_out_reason_display() or "Stock out"
                if movement.destination_station_id:
                    party = movement.destination_station.name
                elif movement.supplier_id:
                    party = movement.supplier.name
                else:
                    party = "—"
            rows.append(
                [
                    movement.created_at.strftime("%d %b %Y %H:%M"),
                    movement.reference_code,
                    move_type,
                    movement.item.name,
                    movement.item.expense_category.name,
                    movement.item.department_station.name,
                    f"{movement.quantity} {movement.item.get_measure_display().lower()}",
                    party,
                ]
            )
        item_count = StoreItem.objects.filter(is_active=True).count()
        return finish(
            [
                "Date",
                "Reference",
                "Type",
                "Item",
                "Expense category",
                "Department / station",
                "Quantity",
                "Supplier / destination",
            ],
            rows,
            [
                {"label": "Active catalogue items", "value": str(item_count), "tone": ""},
                {"label": "Quantity in", "value": f"{qty_in:,.2f}", "tone": "brand"},
                {"label": "Quantity out", "value": f"{qty_out:,.2f}", "tone": "accent"},
            ],
        )

    if report_type == "accounts_payable":
        open_deliveries = list(
            StoreStockMovement.objects.filter(
                direction=StoreStockMovement.Direction.IN,
                payment_status__in=SUPPLIER_OPEN_PAYMENT_STATUSES,
                **_through_date("created_at", date_to),
            )
            .select_related("item", "supplier")
            .order_by("-created_at")
        )
        paid = list(
            StoreSupplierPayment.objects.filter(
                **_period_filter("created_at", date_from, date_to),
            )
            .select_related("movement__supplier", "movement__item", "account")
            .order_by("-created_at")
        )
        rows = []
        open_count = len(open_deliveries)
        paid_total = zero
        for movement in open_deliveries:
            rows.append(
                [
                    movement.created_at.strftime("%d %b %Y"),
                    movement.reference_code,
                    "Payable",
                    movement.supplier.name if movement.supplier_id else "—",
                    movement.item.name,
                    movement.get_payment_status_display(),
                    "—",
                ]
            )
        for pay in paid:
            paid_total += pay.amount or zero
            supplier = (
                pay.movement.supplier.name
                if pay.movement_id and pay.movement.supplier_id
                else "—"
            )
            item_name = pay.movement.item.name if pay.movement_id else "—"
            rows.append(
                [
                    pay.created_at.strftime("%d %b %Y"),
                    pay.reference_code,
                    "Payment",
                    supplier,
                    item_name,
                    "Paid",
                    money(pay.amount),
                ]
            )
        return finish(
            [
                "Date",
                "Reference",
                "Type",
                "Supplier",
                "Item",
                "Status",
                "Amount paid",
            ],
            rows,
            [
                {"label": "Open payables", "value": str(open_count), "tone": "accent"},
                {"label": "Paid in period", "value": money(paid_total), "tone": "brand"},
            ],
        )

    if report_type == "transport_vehicle":
        # Linked to fee categories / votes / accounts named transport or vehicle.
        transport_cats = list(
            FeeCategory.objects.filter(
                Q(name__icontains="transport")
                | Q(code__icontains="TRANS")
                | Q(name__icontains="vehicle")
                | Q(name__icontains="bus")
            )
        )
        cat_ids = [c.id for c in transport_cats]
        fee_payments = list(
            Payment.objects.filter(
                **_period_filter("received_at", date_from, date_to),
                charge__category_id__in=cat_ids,
            ).select_related("charge", "charge__category")
            if cat_ids
            else []
        )
        withdraw_q = (
            Q(account__name__icontains="transport")
            | Q(account__name__icontains="vehicle")
            | Q(account__custom_category__icontains="transport")
            | Q(payee__icontains="transport")
            | Q(payee__icontains="fuel")
            | Q(description__icontains="transport")
            | Q(description__icontains="fuel")
            | Q(description__icontains="vehicle")
        )
        vote_account_ids = list(
            AccountVote.objects.filter(
                Q(name__icontains="transport")
                | Q(name__icontains="vehicle")
                | Q(name__icontains="fuel")
            ).values_list("account_id", flat=True)
        )
        if vote_account_ids:
            withdraw_q |= Q(account_id__in=vote_account_ids)
        withdrawals = list(
            AccountWithdraw.objects.filter(
                withdraw_q,
                status=AccountWithdraw.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            )
            .select_related("account")
            .order_by("-created_at")
        )
        student_map = _student_label_map({p.student_id for p in fee_payments})
        rows = []
        income = zero
        spend = zero
        for payment in fee_payments:
            student = student_map.get(payment.student_id)
            income += payment.amount or zero
            rows.append(
                {
                    "date": payment.received_at,
                    "cells": [
                        payment.received_at.strftime("%d %b %Y"),
                        payment.reference or "—",
                        "Transport fee receipt",
                        student.display_name if student else f"Student #{payment.student_id}",
                        payment.charge.category.name if payment.charge_id else "Transport",
                        money(payment.amount),
                        "—",
                    ],
                }
            )
        for item in withdrawals:
            spend += item.amount or zero
            rows.append(
                {
                    "date": item.created_at,
                    "cells": [
                        item.created_at.strftime("%d %b %Y"),
                        item.reference_code,
                        "Transport expense",
                        item.payee,
                        item.account.name,
                        "—",
                        money(item.amount),
                    ],
                }
            )
        rows.sort(key=lambda row: row["date"], reverse=True)
        return finish(
            [
                "Date",
                "Reference",
                "Type",
                "Party",
                "Category / account",
                "Income",
                "Spend",
            ],
            [row["cells"] for row in rows],
            [
                {"label": "Transport income", "value": money(income), "tone": "brand"},
                {"label": "Transport spend", "value": money(spend), "tone": "accent"},
                {"label": "Net", "value": money(income - spend), "tone": ""},
            ],
        )

    if report_type == "budget_variance":
        votes = list(
            AccountVote.objects.select_related("account").order_by(
                "account__name", "allocation_order"
            )
        )
        rows = []
        budget_total = zero
        actual_total = zero
        for vote in votes:
            budget = vote.amount or zero
            if vote.status == AccountVote.Status.APPROVED:
                budget_total += budget
            # Match collections on charges titled with the vote name or linked via structure lines.
            collected = (
                Payment.objects.filter(
                    **_period_filter("received_at", date_from, date_to),
                )
                .filter(
                    Q(charge__structure_line__vote=vote)
                    | Q(charge__title__iexact=vote.name)
                    | Q(charge__category__name__iexact=vote.name)
                )
                .aggregate(total=Sum("amount"))["total"]
                or zero
            )
            actual_total += collected
            variance = budget - collected
            rows.append(
                [
                    vote.account.name,
                    vote.name,
                    vote.code or vote.reference_code,
                    vote.get_status_display(),
                    money(budget),
                    money(collected),
                    money(variance),
                ]
            )
        return finish(
            [
                "Account",
                "Vote",
                "Code",
                "Status",
                "Budget",
                "Actual collections",
                "Variance",
            ],
            rows,
            [
                {"label": "Approved budget", "value": money(budget_total), "tone": ""},
                {"label": "Actual collections", "value": money(actual_total), "tone": "brand"},
                {
                    "label": "Variance",
                    "value": money(budget_total - actual_total),
                    "tone": "accent",
                },
            ],
        )

    if report_type == "bank_reconciliation":
        # Linked to existing bank/cheque channels in fees, top-ups, and withdrawals.
        bank_payments = list(
            Payment.objects.filter(
                method=Payment.Method.BANK,
                **_period_filter("received_at", date_from, date_to),
            ).select_related("charge", "charge__category")
        )
        bank_topups = list(
            AccountTopUp.objects.filter(
                **_period_filter("created_at", date_from, date_to),
                status=AccountTopUp.Status.APPROVED,
            )
            .filter(
                Q(method=AccountTopUp.Method.CHEQUE)
                | Q(description__icontains="bank")
                | Q(reference_number__icontains="BANK")
            )
            .select_related("account")
        )
        bank_withdrawals = list(
            AccountWithdraw.objects.filter(
                method__in=[
                    AccountWithdraw.Method.BANK,
                    AccountWithdraw.Method.CHEQUE,
                ],
                status=AccountWithdraw.Status.APPROVED,
                **_period_filter("created_at", date_from, date_to),
            ).select_related("account")
        )
        student_map = _student_label_map({p.student_id for p in bank_payments})
        rows = []
        credits = zero
        debits = zero
        for payment in bank_payments:
            student = student_map.get(payment.student_id)
            credits += payment.amount or zero
            rows.append(
                {
                    "date": payment.received_at,
                    "cells": [
                        payment.received_at.strftime("%d %b %Y"),
                        payment.reference or "—",
                        "Fee bank receipt",
                        student.display_name if student else f"Student #{payment.student_id}",
                        money(payment.amount),
                        "—",
                    ],
                }
            )
        for topup in bank_topups:
            credits += topup.amount or zero
            rows.append(
                {
                    "date": topup.created_at,
                    "cells": [
                        topup.created_at.strftime("%d %b %Y"),
                        topup.reference_code,
                        "Account bank / cheque top-up",
                        topup.account.name,
                        money(topup.amount),
                        "—",
                    ],
                }
            )
        for withdrawal in bank_withdrawals:
            debits += withdrawal.amount or zero
            rows.append(
                {
                    "date": withdrawal.created_at,
                    "cells": [
                        withdrawal.created_at.strftime("%d %b %Y"),
                        withdrawal.reference_code,
                        f"Bank payment ({withdrawal.get_method_display()})",
                        f"{withdrawal.account.name} · {withdrawal.payee}",
                        "—",
                        money(withdrawal.amount),
                    ],
                }
            )
        rows.sort(key=lambda row: row["date"], reverse=True)
        return finish(
            [
                "Date",
                "Reference",
                "Type",
                "Particulars",
                "Book credit",
                "Book debit",
            ],
            [row["cells"] for row in rows],
            [
                {"label": "Book credits", "value": money(credits), "tone": "brand"},
                {"label": "Book debits", "value": money(debits), "tone": "accent"},
                {
                    "label": "Net book movement",
                    "value": money(credits - debits),
                    "tone": "",
                },
            ],
        )

    if report_type == "vote_book_summaries":
        votes = list(
            AccountVote.objects.select_related("account").order_by(
                "account__name", "allocation_order"
            )
        )
        rows = []
        approved_total = zero
        for vote in votes:
            if vote.status == AccountVote.Status.APPROVED:
                approved_total += vote.amount or zero
            allocation = (
                f"{vote.percentage:.0f}% of top-ups"
                if vote.allocation_mode == AccountVote.AllocationMode.PERCENTAGE
                and vote.percentage is not None
                else "Fixed amount"
            )
            rows.append(
                [
                    vote.account.name,
                    vote.name,
                    vote.code or vote.reference_code,
                    allocation,
                    money(vote.amount),
                    str(vote.allocation_order),
                    vote.get_status_display(),
                    vote.account.get_vote_fund_allocation_display(),
                ]
            )
        return finish(
            [
                "Account",
                "Vote",
                "Code",
                "Allocation mode",
                "Amount",
                "Order",
                "Status",
                "Account allocation rule",
            ],
            rows,
            [
                {"label": "Vote heads", "value": str(len(rows)), "tone": ""},
                {
                    "label": "Approved allocation",
                    "value": money(approved_total),
                    "tone": "brand",
                },
            ],
        )

    raise ValueError("Unknown report type.")
