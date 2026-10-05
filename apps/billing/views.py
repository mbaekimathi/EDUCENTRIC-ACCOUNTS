from datetime import datetime, timedelta
from decimal import Decimal
import json

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from apps.directory.models import AcademicLevel, Student
from apps.staff.views import portal_access_required

from .financial_year import (
    FinancialYearForm,
    apply_default_current_term,
    ensure_default_terms,
    suggest_current_term,
)
from .forms import DarajaSettingsForm, StoreItemForm
from .models import (
    AccountTopUp,
    AccountVote,
    AccountWithdraw,
    DarajaSettings,
    FeeCategory,
    FeeCharge,
    FeeStructure,
    FeeStructureLine,
    FinancialTerm,
    FinancialYear,
    MpesaCallbackLog,
    Payment,
    SchoolAccount,
    StoreDepartmentStation,
    StoreItem,
    StoreStockMovement,
    StoreSupplier,
    StoreSupplierPayment,
    StkPushRequest,
    allocate_payment_to_charges,
    ensure_store_lookups,
    generate_supplier_payment_reference_code,
    get_or_create_store_supplier,
    normalize_supplier_phone,
    school_account_available_balance,
    store_item_quantity_on_hand,
    student_balance,
)
from .reports import (
    REPORT_CATEGORIES,
    REPORT_TYPE_MAP,
    SUPPLIER_OPEN_PAYMENT_STATUSES,
    build_report,
    parse_report_date,
)
from .mpesa import (
    MpesaApiError,
    decode_callback_body,
    initiate_stk_push,
    process_stk_callback,
    refresh_stk_request_status,
    stk_request_payload,
)


def upper_input(value: str) -> str:
    """Normalize non-description text fields to uppercase."""
    return (value or "").strip().upper()


def parse_school_account_form(request, level_map):
    category = upper_input(request.POST.get("category"))
    custom_category = upper_input(request.POST.get("custom_category"))
    name = upper_input(request.POST.get("name"))
    description = (request.POST.get("description") or "").strip()
    payment_modes = request.POST.getlist("payment_modes")
    selected_levels = []
    for raw in request.POST.getlist("academic_levels"):
        if str(raw).isdigit():
            level_id = int(raw)
            if level_id in level_map:
                selected_levels.append(level_id)
    valid_categories = {choice for choice, _ in SchoolAccount.Category.choices}
    valid_modes = {choice for choice, _ in SchoolAccount.PaymentMode.choices}
    payment_modes = [mode for mode in payment_modes if mode in valid_modes]
    vote_fund_allocation = upper_input(request.POST.get("vote_fund_allocation"))
    valid_allocations = {
        choice for choice, _ in SchoolAccount.VoteFundAllocation.choices
    }
    if category != SchoolAccount.Category.OTHER:
        custom_category = ""
    data = {
        "category": category,
        "custom_category": custom_category,
        "name": name,
        "description": description,
        "payment_modes": payment_modes,
        "academic_levels": selected_levels,
        "vote_fund_allocation": vote_fund_allocation,
    }
    error = None
    if category not in valid_categories:
        error = "Select a valid account category."
    elif category == SchoolAccount.Category.OTHER and not custom_category:
        error = "Enter a custom category name."
    elif category == SchoolAccount.Category.OTHER and len(custom_category) > 120:
        error = "Custom category must be 120 characters or fewer."
    elif not name:
        error = "Account name is required."
    elif not payment_modes:
        error = "Select at least one mode of payment."
    elif vote_fund_allocation not in valid_allocations:
        error = "Select how money should be allocated to votes."
    return data, error


def apply_school_account_form(account, form_data):
    account.category = form_data["category"]
    account.custom_category = form_data["custom_category"]
    account.name = form_data["name"]
    account.description = form_data["description"]
    account.payment_modes = form_data["payment_modes"]
    account.academic_level_ids = form_data["academic_levels"]
    account.vote_fund_allocation = form_data["vote_fund_allocation"]


def petty_cashbook_account_balance(account):
    topups = (
        AccountTopUp.objects.filter(
            account=account,
            status=AccountTopUp.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    withdrawals = (
        AccountWithdraw.objects.filter(
            account=account,
            status=AccountWithdraw.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    return topups - withdrawals


def petty_cashbook_topup_methods(mpesa_enabled):
    methods = [
        choice
        for choice in AccountTopUp.Method.choices
        if mpesa_enabled or choice[0] != AccountTopUp.Method.STK_PUSH
    ]
    return methods


@portal_access_required
@require_GET
def dashboard(request):
    charge_stats = FeeCharge.objects.exclude(
        status__in=[FeeCharge.Status.CANCELLED, FeeCharge.Status.WAIVED]
    ).aggregate(
        total_charged=Sum("amount"),
        total_collected=Sum("amount_paid"),
        open_count=Count("id"),
    )
    total_charged = charge_stats["total_charged"] or Decimal("0.00")
    total_collected = charge_stats["total_collected"] or Decimal("0.00")
    outstanding = total_charged - total_collected

    recent_payments = Payment.objects.select_related("charge", "received_by")[:8]
    recent_charges = FeeCharge.objects.select_related("category")[:8]
    categories = FeeCategory.objects.filter(is_active=True).order_by("name")
    learner_count = Student.objects.filter(is_suspended=False).count()

    modules = [
        {
            "title": "Students fees",
            "blurb": "Post and track tuition and other learner fee charges.",
            "url_name": "billing:student_fees",
            "meta": f"{charge_stats['open_count'] or 0} open charges",
        },
        {
            "title": "Student pocket money",
            "blurb": "Manage learner pocket-money deposits, balances, and withdrawals.",
            "url_name": "billing:pocket_money",
            "meta": "Cash desk",
        },
        {
            "title": "Petty cashbook",
            "blurb": "Track petty cash floats, top-ups, and withdrawals.",
            "url_name": "billing:petty_cashbook",
            "meta": "Cash desk",
        },
        {
            "title": "Billing and invoices",
            "blurb": "Review invoices, receipts, and payment collections.",
            "url_name": "billing:invoices",
            "meta": f"KES {total_collected:,.2f} collected",
        },
        {
            "title": "School accounts",
            "blurb": "School-level fee categories, ledgers, and finance overview.",
            "url_name": "billing:school_accounts",
            "meta": f"{categories.count()} categories",
        },
        {
            "title": "Reports",
            "blurb": "Generate compliance, revenue, expenditure, and audit reports.",
            "url_name": "billing:reports",
            "meta": "Reporting",
        },
        {
            "title": "Store management",
            "blurb": "Inventory, stock issues, and school store operations.",
            "url_name": "billing:store_management",
            "meta": "Stores",
        },
    ]

    return render(
        request,
        "billing/dashboard.html",
        {
            "total_charged": total_charged,
            "total_collected": total_collected,
            "outstanding": outstanding,
            "open_count": charge_stats["open_count"] or 0,
            "recent_payments": recent_payments,
            "recent_charges": recent_charges,
            "categories": categories,
            "learner_count": learner_count,
            "category_count": categories.count(),
            "modules": modules,
        },
    )


@portal_access_required
@require_GET
def student_fees(request):
    academic_levels = list(AcademicLevel.objects.all().order_by("order", "name"))
    level_map = {level.id: level for level in academic_levels}
    balance_zero = Value(
        Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2)
    )
    accounts_qs = (
        SchoolAccount.objects.filter(category=SchoolAccount.Category.STUDENT_FEES)
        .annotate(
            balance=Coalesce(
                Sum(
                    "top_ups__amount",
                    filter=Q(top_ups__status=AccountTopUp.Status.APPROVED),
                ),
                balance_zero,
            ),
            pending_topups=Count(
                "top_ups",
                filter=Q(top_ups__status=AccountTopUp.Status.PENDING),
            ),
            votes_count=Count("votes"),
        )
        .order_by("-is_active", "name")
    )
    accounts_list = list(accounts_qs)
    account_ids = [account.id for account in accounts_list]

    # Expected / collected from applied fee charges on each fees account.
    charge_totals_by_account = {
        row["fee_structure__account_id"]: row
        for row in FeeCharge.objects.filter(
            fee_structure__account_id__in=account_ids,
        )
        .exclude(
            status__in=[FeeCharge.Status.CANCELLED, FeeCharge.Status.WAIVED]
        )
        .values("fee_structure__account_id")
        .annotate(
            expected=Coalesce(
                Sum("amount"),
                balance_zero,
            ),
            collected=Coalesce(
                Sum("amount_paid"),
                balance_zero,
            ),
        )
    }

    fee_accounts = []
    total_balance = Decimal("0.00")
    total_expected = Decimal("0.00")
    total_collected = Decimal("0.00")
    active_count = 0
    for account in accounts_list:
        linked = [
            level_map[level_id]
            for level_id in (account.academic_level_ids or [])
            if level_id in level_map
        ]
        balance = account.balance or Decimal("0.00")
        totals = charge_totals_by_account.get(account.id) or {}
        expected = totals.get("expected") or Decimal("0.00")
        collected = totals.get("collected") or Decimal("0.00")
        outstanding = expected - collected
        total_balance += balance
        total_expected += expected
        total_collected += collected
        if account.is_active:
            active_count += 1
        fee_accounts.append(
            {
                "account": account,
                "levels": linked,
                "balance": balance,
                "expected": expected,
                "collected": collected,
                "outstanding": outstanding,
                "pending_topups": account.pending_topups or 0,
                "votes_count": account.votes_count or 0,
            }
        )

    pending_topup_count = AccountTopUp.objects.filter(
        account__category=SchoolAccount.Category.STUDENT_FEES,
        status=AccountTopUp.Status.PENDING,
    ).count()

    return render(
        request,
        "billing/student_fees.html",
        {
            "fee_accounts": fee_accounts,
            "total_balance": total_balance,
            "total_expected": total_expected,
            "total_collected": total_collected,
            "total_outstanding": total_expected - total_collected,
            "active_count": active_count,
            "pending_topup_count": pending_topup_count,
        },
    )


def _student_level_key_from_curriculum(level: AcademicLevel) -> str | None:
    """Map curriculum AcademicLevel to Student.academic_level choice value."""
    valid = {choice for choice, _ in Student.AcademicLevel.choices}
    name_key = (
        (level.name or "")
        .strip()
        .upper()
        .replace("-", " ")
        .replace("  ", " ")
        .replace(" ", "_")
    )
    if name_key in valid:
        return name_key
    code = (level.code or "").strip().upper()
    code_map = {
        "G1": "GRADE_1",
        "G2": "GRADE_2",
        "G3": "GRADE_3",
        "G4": "GRADE_4",
        "G5": "GRADE_5",
        "G6": "GRADE_6",
        "G7": "GRADE_7",
        "G8": "GRADE_8",
        "G9": "GRADE_9",
        "PP1": "PRE_PRIMARY_1",
        "PP2": "PRE_PRIMARY_2",
        "F1": "FORM_1",
        "F2": "FORM_2",
        "F3": "FORM_3",
        "F4": "FORM_4",
    }
    mapped = code_map.get(code)
    if mapped in valid:
        return mapped
    return None


def _student_fees_account(account_id: int) -> SchoolAccount:
    return get_object_or_404(
        SchoolAccount,
        pk=account_id,
        category=SchoolAccount.Category.STUDENT_FEES,
    )


def _fee_category_for_vote(vote: AccountVote) -> FeeCategory:
    code = (vote.code or f"VOT{vote.id}").strip().upper()[:40] or f"VOT{vote.id}"
    category = FeeCategory.objects.filter(code=code).first()
    if category:
        return category
    return FeeCategory.objects.create(
        code=code,
        name=(vote.name or code)[:120],
        description=f"Fee vote head from {vote.account.name}",
        is_active=True,
    )


def _students_for_structure_levels(level_ids):
    """Active learners whose academic level matches the selected curriculum levels."""
    student_keys = set()
    for level in AcademicLevel.objects.filter(id__in=level_ids):
        key = _student_level_key_from_curriculum(level)
        if key:
            student_keys.add(key)
    if not student_keys:
        return Student.objects.none()
    return Student.objects.filter(
        academic_level__in=student_keys,
        is_suspended=False,
    )


def _apply_fee_structure_to_students(structure: FeeStructure, user) -> dict:
    """
    Post one FeeCharge per structure line for every student on the structure's levels.
    Skips lines already posted to a student for this structure.
    """
    lines = list(structure.lines.select_related("vote"))
    students = list(_students_for_structure_levels(structure.academic_level_ids or []))
    if not lines or not students:
        return {
            "students": len(students),
            "created": 0,
            "skipped": 0,
        }

    year_label = structure.financial_year.display_name
    term_label = structure.financial_term.name
    existing = set(
        FeeCharge.objects.filter(
            fee_structure=structure,
            structure_line_id__isnull=False,
            student_id__in=[s.id for s in students],
        ).values_list("student_id", "structure_line_id")
    )

    to_create = []
    skipped = 0
    category_cache = {}
    for student in students:
        for line in lines:
            key = (student.id, line.id)
            if key in existing:
                skipped += 1
                continue
            vote = line.vote
            if vote.id not in category_cache:
                category_cache[vote.id] = _fee_category_for_vote(vote)
            to_create.append(
                FeeCharge(
                    student_id=student.id,
                    category=category_cache[vote.id],
                    title=vote.name,
                    academic_year=year_label,
                    term=term_label,
                    amount=line.amount,
                    amount_paid=Decimal("0.00"),
                    status=FeeCharge.Status.OPEN,
                    notes=f"From fee structure {structure.reference_code}",
                    fee_structure=structure,
                    structure_line=line,
                    created_by=user,
                )
            )

    if to_create:
        FeeCharge.objects.bulk_create(to_create, batch_size=500)

    return {
        "students": len(students),
        "created": len(to_create),
        "skipped": skipped,
    }


def _parse_fee_structure_form(request, votes, linked_level_ids):
    year_id = (request.POST.get("financial_year_id") or "").strip()
    term_id = (request.POST.get("financial_term_id") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    selected_levels = []
    for raw in request.POST.getlist("academic_levels"):
        if str(raw).isdigit():
            level_id = int(raw)
            if level_id in linked_level_ids:
                selected_levels.append(level_id)

    vote_amounts = {}
    lines = []
    error = None
    for vote in votes:
        raw = (request.POST.get(f"vote_amount_{vote.id}") or "").strip()
        vote_amounts[str(vote.id)] = raw
        if not raw:
            continue
        try:
            amount = Decimal(raw)
            if amount < 0:
                raise ValueError
        except Exception:
            error = f"Enter a valid amount for vote “{vote.name}”."
            break
        if amount > 0:
            lines.append((vote, amount))

    form = {
        "financial_year_id": year_id,
        "financial_term_id": term_id,
        "name": "",
        "notes": notes,
        "academic_levels": selected_levels,
        "vote_amounts": vote_amounts,
    }

    year = FinancialYear.objects.filter(pk=year_id).first() if year_id.isdigit() else None
    term = (
        FinancialTerm.objects.filter(pk=term_id, financial_year=year).first()
        if year and term_id.isdigit()
        else None
    )

    if error is None and year is None:
        error = "Select a financial year (session year)."
    elif error is None and term is None:
        error = "Select a term for this session."
    elif error is None and not selected_levels:
        error = "Select at least one academic level."
    elif error is None and not votes:
        error = "Register account votes first — they form the fee structure lines."
    elif error is None and not lines:
        error = "Enter an amount greater than zero for at least one vote."
    elif error is None and len(notes) > 255:
        error = "Notes must be 255 characters or fewer."

    return form, year, term, selected_levels, lines, error


def _sync_fee_structure_lines(structure, lines, year, term):
    """
    Upsert vote lines. Remove cleared lines only when no payments exist.
    Sync unpaid charge amounts for updated lines.
    Raises ValueError if a line with payments would be removed.
    """
    existing_by_vote = {line.vote_id: line for line in structure.lines.select_related("vote")}
    keep_vote_ids = set()

    for vote, amount in lines:
        keep_vote_ids.add(vote.id)
        line = existing_by_vote.get(vote.id)
        if line is None:
            FeeStructureLine.objects.create(structure=structure, vote=vote, amount=amount)
            continue
        if line.amount != amount:
            line.amount = amount
            line.save(update_fields=["amount"])
            unpaid = FeeCharge.objects.filter(
                structure_line=line,
                amount_paid=Decimal("0.00"),
            ).exclude(
                status__in=[FeeCharge.Status.WAIVED, FeeCharge.Status.CANCELLED]
            )
            for charge in unpaid:
                charge.amount = amount
                charge.academic_year = year.display_name
                charge.term = term.name
                charge.refresh_status(save=True)

    for vote_id, line in existing_by_vote.items():
        if vote_id in keep_vote_ids:
            continue
        if FeeCharge.objects.filter(structure_line=line, amount_paid__gt=0).exists():
            raise ValueError(
                f"Cannot remove “{line.vote.name}”: learners have payments against it. "
                "Set the amount instead, or archive the structure."
            )
        FeeCharge.objects.filter(structure_line=line).delete()
        line.delete()

    FeeCharge.objects.filter(
        fee_structure=structure,
        amount_paid=Decimal("0.00"),
    ).exclude(
        status__in=[FeeCharge.Status.WAIVED, FeeCharge.Status.CANCELLED]
    ).update(
        academic_year=year.display_name,
        term=term.name,
    )


@portal_access_required
@require_GET
def student_fees_account_levels(request, account_id):
    account = _student_fees_account(account_id)
    level_ids = list(account.academic_level_ids or [])
    levels = list(
        AcademicLevel.objects.filter(id__in=level_ids).order_by("order", "name")
    )
    # Preserve account link order when possible.
    order_index = {level_id: index for index, level_id in enumerate(level_ids)}
    levels.sort(key=lambda level: (order_index.get(level.id, 9999), level.order, level.name))

    level_rows = []
    for level in levels:
        student_key = _student_level_key_from_curriculum(level)
        student_count = 0
        if student_key:
            student_count = Student.objects.filter(
                academic_level=student_key,
                is_suspended=False,
            ).count()
        level_rows.append(
            {
                "level": level,
                "student_key": student_key,
                "student_count": student_count,
            }
        )

    return render(
        request,
        "billing/student_fees_levels.html",
        {
            "account": account,
            "level_rows": level_rows,
        },
    )


def _fee_payment_method_options(account: SchoolAccount, mpesa_enabled: bool, stk_ready: bool = False):
    """Payment options for student fee collection based on account modes + Daraja."""
    modes = set(account.payment_modes or [])
    options = []
    if SchoolAccount.PaymentMode.CASH in modes:
        options.append(
            {
                "value": "CASH",
                "label": "Cash",
                "input": "reference",
                "payment_method": Payment.Method.CASH,
            }
        )
    if SchoolAccount.PaymentMode.CHEQUE in modes:
        options.append(
            {
                "value": "CHEQUE",
                "label": "Cheque",
                "input": "reference",
                "payment_method": Payment.Method.CHEQUE,
            }
        )
    if SchoolAccount.PaymentMode.BANK in modes:
        options.append(
            {
                "value": "BANK",
                "label": "Bank transfer",
                "input": "reference",
                "payment_method": Payment.Method.BANK,
            }
        )
    if SchoolAccount.PaymentMode.MPESA in modes:
        mpesa_options = []
        if mpesa_enabled and stk_ready:
            mpesa_options.append(
                {
                    "value": "MPESA_STK",
                    "label": "M-Pesa STK Push",
                    "input": "phone",
                    "payment_method": Payment.Method.MPESA,
                }
            )
        mpesa_options.append(
            {
                "value": "MPESA_MANUAL",
                "label": "Manual M-Pesa",
                "input": "reference",
                "payment_method": Payment.Method.MPESA,
            }
        )
        # Prefer STK at the top of the list as the default fee collection method.
        options = mpesa_options + options
    if SchoolAccount.PaymentMode.OTHER in modes:
        options.append(
            {
                "value": "OTHER",
                "label": "Other",
                "input": "reference",
                "payment_method": Payment.Method.OTHER,
            }
        )
    return options


def _normalize_msisdn(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("254") and len(digits) >= 12:
        return digits[:12]
    if digits.startswith("0") and len(digits) >= 10:
        return "254" + digits[1:10]
    if len(digits) == 9:
        return "254" + digits
    return digits


@portal_access_required
@require_http_methods(["GET", "POST"])
def student_fees_level_students(request, account_id, level_id):
    account = _student_fees_account(account_id)
    linked_ids = set(account.academic_level_ids or [])
    if level_id not in linked_ids:
        messages.error(request, "That academic level is not linked to this account.")
        return redirect("billing:student_fees_account_levels", account_id=account.id)

    level = get_object_or_404(AcademicLevel, pk=level_id)
    student_key = _student_level_key_from_curriculum(level)
    q = (request.GET.get("q") or "").strip()
    daraja = DarajaSettings.load()
    mpesa_enabled = bool(daraja.is_enabled)
    mpesa_stk_ready = daraja.stk_ready()
    stk_missing = daraja.stk_missing_fields()
    payment_options = _fee_payment_method_options(
        account, mpesa_enabled, stk_ready=mpesa_stk_ready
    )
    payment_options_by_value = {opt["value"]: opt for opt in payment_options}

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "record_fee_payment":
            student_id_raw = (request.POST.get("student_id") or "").strip()
            amount_raw = (request.POST.get("amount") or "").strip()
            method_key = (request.POST.get("method") or "").strip()
            reference = upper_input(request.POST.get("reference"))
            phone_raw = (request.POST.get("phone") or "").strip()
            notes = (request.POST.get("notes") or "").strip()

            option = payment_options_by_value.get(method_key)
            error = None
            student = None
            amount = None

            if not student_id_raw.isdigit():
                error = "Select a learner to update fees for."
            else:
                student = Student.objects.filter(
                    pk=int(student_id_raw), is_suspended=False
                ).select_related("parent_guardian").first()
                if student is None:
                    error = "Learner not found or is suspended."
                elif student_key and student.academic_level != student_key:
                    error = "That learner is not in this academic level."

            if error is None:
                try:
                    amount = Decimal(amount_raw)
                    if amount <= 0:
                        raise ValueError
                except Exception:
                    error = "Enter a valid payment amount greater than zero."

            if error is None and option is None:
                error = "Select a valid payment method for this account."

            if error is None and option["value"] == "MPESA_STK":
                error = (
                    "Use Send STK prompt in the payment popup for M-Pesa STK Push. "
                    "The payment is recorded only after Safaricom confirms success."
                )

            if error is None and option["input"] == "phone":
                phone = _normalize_msisdn(phone_raw)
                if len(phone) < 12:
                    error = "Enter a valid M-Pesa phone number (e.g. 07… or 2547…)."
                elif not reference:
                    reference = f"STK-{phone[-9:]}"
            elif error is None and option["input"] == "reference":
                if not reference:
                    error = "Reference code is required for this payment method."

            if error is None and len(notes) > 500:
                error = "Notes must be 500 characters or fewer."

            if error:
                messages.error(request, error)
                redirect_url = reverse(
                    "billing:student_fees_level_students",
                    kwargs={"account_id": account.id, "level_id": level.id},
                )
                if q:
                    redirect_url = f"{redirect_url}?q={q}"
                return redirect(redirect_url)
            else:
                note_parts = [notes] if notes else []
                if option["value"] == "MPESA_MANUAL":
                    note_parts.insert(0, "Manual M-Pesa")
                result = allocate_payment_to_charges(
                    student_id=student.id,
                    amount=amount,
                    method=option["payment_method"],
                    reference=reference,
                    received_by=request.user,
                    notes=" · ".join(part for part in note_parts if part),
                )
                messages.success(
                    request,
                    f"Payment of KES {amount} recorded for {student.display_name} "
                    f"via {option['label']} (ref {reference}). "
                    f"KES {result['allocated']} applied to charges.",
                )
                redirect_url = reverse(
                    "billing:student_fees_level_students",
                    kwargs={"account_id": account.id, "level_id": level.id},
                )
                if q:
                    redirect_url = f"{redirect_url}?q={q}"
                return redirect(redirect_url)

    students = []
    totals = {
        "charged": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "balance": Decimal("0.00"),
    }
    students_payload = {}

    if student_key:
        qs = Student.objects.filter(
            academic_level=student_key,
            is_suspended=False,
        ).select_related("parent_guardian")
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(admission_number__icontains=q)
                | Q(assessment_number__icontains=q)
            )
        students = list(qs.order_by("last_name", "first_name"))
        student_ids = [s.id for s in students]
        finance_by_student = {
            row["student_id"]: row
            for row in FeeCharge.objects.filter(student_id__in=student_ids)
            .exclude(
                status__in=[FeeCharge.Status.CANCELLED, FeeCharge.Status.WAIVED]
            )
            .values("student_id")
            .annotate(
                charged=Coalesce(
                    Sum("amount"),
                    Value(
                        Decimal("0.00"),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    ),
                ),
                paid=Coalesce(
                    Sum("amount_paid"),
                    Value(
                        Decimal("0.00"),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    ),
                ),
            )
        }
        for student in students:
            row = finance_by_student.get(student.id) or {}
            charged = row.get("charged") or Decimal("0.00")
            paid = row.get("paid") or Decimal("0.00")
            balance = charged - paid
            student.total_charged = charged
            student.total_paid = paid
            student.ledger_balance = balance
            totals["charged"] += charged
            totals["paid"] += paid
            totals["balance"] += balance
            parent_phone = ""
            if student.parent_guardian_id and student.parent_guardian:
                parent_phone = student.parent_guardian.phone_number or ""
            students_payload[str(student.id)] = {
                "id": student.id,
                "name": student.display_name,
                "admission": student.admission_number or student.assessment_number or "",
                "balance": f"{balance:.2f}",
                "phone": parent_phone,
            }

    return render(
        request,
        "billing/student_fees_students.html",
        {
            "account": account,
            "level": level,
            "student_key": student_key,
            "students": students,
            "q": q,
            "totals": totals,
            "payment_options": payment_options,
            "payment_options_json": json.dumps(
                {
                    opt["value"]: {"input": opt["input"], "label": opt["label"]}
                    for opt in payment_options
                }
            ),
            "students_json": json.dumps(students_payload),
            "mpesa_enabled": mpesa_enabled,
            "mpesa_stk_ready": mpesa_stk_ready,
            "stk_missing_fields": stk_missing,
            "stk_initiate_url": reverse(
                "billing:student_fees_stk_initiate",
                kwargs={"account_id": account.id, "level_id": level.id},
            ),
            "stk_status_url_template": reverse(
                "billing:student_fees_stk_status",
                kwargs={
                    "account_id": account.id,
                    "level_id": level.id,
                    "stk_id": 0,
                },
            ),
        },
    )


@portal_access_required
@require_http_methods(["POST"])
def student_fees_stk_initiate(request, account_id, level_id):
    account = _student_fees_account(account_id)
    linked_ids = set(account.academic_level_ids or [])
    if level_id not in linked_ids:
        return JsonResponse({"ok": False, "error": "Level is not linked to this account."}, status=400)

    level = get_object_or_404(AcademicLevel, pk=level_id)
    student_key = _student_level_key_from_curriculum(level)
    daraja = DarajaSettings.load()
    if "MPESA" not in (account.payment_modes or []):
        return JsonResponse(
            {"ok": False, "error": "M-Pesa is not enabled on this fee account."},
            status=400,
        )
    if not daraja.stk_ready():
        missing = daraja.stk_missing_fields()
        detail = ", ".join(missing) if missing else "incomplete configuration"
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "STK Push is not ready. Complete Daraja setup in "
                    f"System settings → Payments ({detail})."
                ),
            },
            status=400,
        )

    student_id_raw = (request.POST.get("student_id") or "").strip()
    amount_raw = (request.POST.get("amount") or "").strip()
    phone_raw = (request.POST.get("phone") or "").strip()

    if not student_id_raw.isdigit():
        return JsonResponse({"ok": False, "error": "Select a learner."}, status=400)
    student = Student.objects.filter(
        pk=int(student_id_raw), is_suspended=False
    ).first()
    if student is None:
        return JsonResponse({"ok": False, "error": "Learner not found."}, status=400)
    if student_key and student.academic_level != student_key:
        return JsonResponse(
            {"ok": False, "error": "Learner is not in this academic level."},
            status=400,
        )

    try:
        amount = Decimal(amount_raw)
        if amount <= 0:
            raise ValueError
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Enter a valid amount greater than zero."},
            status=400,
        )

    account_reference = f"STU{student.id}"[:12]
    phone = _normalize_msisdn(phone_raw)
    if len(phone) != 12 or not phone.startswith("254"):
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Enter a valid Kenyan M-Pesa number "
                    f"(got “{phone_raw or 'empty'}” → {phone or 'invalid'})."
                ),
            },
            status=400,
        )

    try:
        initiated = initiate_stk_push(
            phone_number=phone,
            amount=amount,
            account_reference=account_reference,
            transaction_desc="SchoolFees",
            settings_obj=daraja,
        )
    except MpesaApiError as exc:
        detail = str(exc)
        # Common sandbox causes when Safaricom returns bare HTTP 400.
        if exc.status_code == 400 and "HTTP 400" in detail:
            detail = (
                f"{detail} Check: phone is a Safaricom line registered for sandbox "
                "testing (or a real line in production), callback URL is HTTPS and "
                "reachable, and shortcode/passkey match the Daraja app."
            )
        return JsonResponse(
            {
                "ok": False,
                "error": detail,
                "daraja": exc.payload if isinstance(exc.payload, dict) else {},
            },
            status=400,
        )

    stk = StkPushRequest.objects.create(
        student_id=student.id,
        account=account,
        amount=amount,
        phone_number=phone,
        account_reference=account_reference,
        merchant_request_id=initiated["merchant_request_id"],
        checkout_request_id=initiated["checkout_request_id"],
        status=StkPushRequest.Status.PENDING,
        result_desc=initiated.get("customer_message") or "STK prompt sent.",
        created_by=request.user,
    )
    return JsonResponse(
        {
            "ok": True,
            "message": initiated.get("customer_message")
            or f"STK prompt sent to {phone}. Ask the payer to enter their M-Pesa PIN.",
            "stk": stk_request_payload(stk),
        }
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def student_fees_stk_status(request, account_id, level_id, stk_id):
    account = _student_fees_account(account_id)
    linked_ids = set(account.academic_level_ids or [])
    if level_id not in linked_ids:
        return JsonResponse({"ok": False, "error": "Level is not linked to this account."}, status=400)

    stk = get_object_or_404(StkPushRequest, pk=stk_id, account=account)
    if stk.status == StkPushRequest.Status.PENDING or (
        stk.status == StkPushRequest.Status.SUCCESS and not stk.mpesa_receipt
    ):
        stk = refresh_stk_request_status(stk, received_by=request.user)

    payload = stk_request_payload(stk)
    failed = stk.status in {
        StkPushRequest.Status.FAILED,
        StkPushRequest.Status.CANCELLED,
    }
    return JsonResponse(
        {
            "ok": True,
            "stk": payload,
            "done": stk.status != StkPushRequest.Status.PENDING,
            "failed": failed,
            "success": (
                stk.status == StkPushRequest.Status.SUCCESS
                and bool(stk.mpesa_receipt)
            ),
        }
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def student_fees_fee_structure(request, account_id):
    account = _student_fees_account(account_id)
    linked_level_ids = list(account.academic_level_ids or [])
    levels = list(
        AcademicLevel.objects.filter(id__in=linked_level_ids).order_by("order", "name")
    )
    order_index = {level_id: index for index, level_id in enumerate(linked_level_ids)}
    levels.sort(key=lambda level: (order_index.get(level.id, 9999), level.order, level.name))

    votes = list(
        AccountVote.objects.filter(
            account=account,
            status=AccountVote.Status.APPROVED,
        ).order_by("allocation_order", "name")
    )
    years = list(FinancialYear.objects.prefetch_related("terms").order_by("-start_date"))
    current_year = FinancialYear.current()
    current_term = FinancialTerm.current()

    form = {
        "financial_year_id": str(current_year.id) if current_year else "",
        "financial_term_id": str(current_term.id) if current_term else "",
        "name": "",
        "notes": "",
        "academic_levels": list(linked_level_ids),
        "vote_amounts": {str(vote.id): "" for vote in votes},
    }
    open_structure_modal = False
    editing_structure_id = None

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action in {"register_structure", "edit_structure"}:
            form, year, term, selected_levels, lines, error = _parse_fee_structure_form(
                request, votes, linked_level_ids
            )
            structure_id = (request.POST.get("structure_id") or "").strip()

            if action == "edit_structure":
                structure = get_object_or_404(
                    FeeStructure, pk=structure_id, account=account
                )
                editing_structure_id = structure.id
                if structure.status != FeeStructure.Status.ACTIVE:
                    error = error or "Only active fee structures can be edited."

            if error:
                messages.error(request, error)
                open_structure_modal = True
            elif action == "register_structure":
                with transaction.atomic():
                    structure = FeeStructure(
                        account=account,
                        financial_year=year,
                        financial_term=term,
                        name=f"{term.name} · {year.display_name}",
                        notes=form["notes"],
                        academic_level_ids=selected_levels,
                        status=FeeStructure.Status.ACTIVE,
                        created_by=request.user,
                    )
                    structure.save()
                    FeeStructureLine.objects.bulk_create(
                        [
                            FeeStructureLine(
                                structure=structure, vote=vote, amount=amount
                            )
                            for vote, amount in lines
                        ]
                    )
                    applied = _apply_fee_structure_to_students(structure, request.user)
                level_count = len(selected_levels)
                messages.success(
                    request,
                    f"Fee structure “{structure.display_name}” registered for "
                    f"{level_count} level{'s' if level_count != 1 else ''} "
                    f"({structure.reference_code}). "
                    f"Posted {applied['created']} charge(s) to {applied['students']} learner(s).",
                )
                return redirect("billing:student_fees_fee_structure", account_id=account.id)
            else:
                try:
                    with transaction.atomic():
                        structure.financial_year = year
                        structure.financial_term = term
                        structure.notes = form["notes"]
                        structure.academic_level_ids = selected_levels
                        structure.save()
                        _sync_fee_structure_lines(structure, lines, year, term)
                        applied = _apply_fee_structure_to_students(
                            structure, request.user
                        )
                except ValueError as exc:
                    messages.error(request, str(exc))
                    open_structure_modal = True
                    editing_structure_id = structure.id
                else:
                    messages.success(
                        request,
                        f"Fee structure “{structure.display_name}” updated. "
                        f"{applied['created']} new charge(s) posted to "
                        f"{applied['students']} learner(s).",
                    )
                    return redirect(
                        "billing:student_fees_fee_structure", account_id=account.id
                    )

        elif action == "apply_structure":
            structure_id = (request.POST.get("structure_id") or "").strip()
            structure = get_object_or_404(
                FeeStructure, pk=structure_id, account=account
            )
            if structure.status != FeeStructure.Status.ACTIVE:
                messages.error(request, "Only active fee structures can be applied to learners.")
            else:
                applied = _apply_fee_structure_to_students(structure, request.user)
                messages.success(
                    request,
                    f"Applied “{structure.display_name}” to learners: "
                    f"{applied['created']} new charge(s), "
                    f"{applied['skipped']} already posted, "
                    f"{applied['students']} learner(s) in scope.",
                )
            return redirect("billing:student_fees_fee_structure", account_id=account.id)

        elif action == "archive_structure":
            structure_id = (request.POST.get("structure_id") or "").strip()
            structure = get_object_or_404(
                FeeStructure, pk=structure_id, account=account
            )
            if structure.status == FeeStructure.Status.ARCHIVED:
                messages.info(request, f"“{structure.display_name}” is already archived.")
            else:
                structure.status = FeeStructure.Status.ARCHIVED
                structure.save(update_fields=["status", "updated_at"])
                messages.success(
                    request, f"Fee structure “{structure.display_name}” archived."
                )
            return redirect("billing:student_fees_fee_structure", account_id=account.id)

        elif action == "restore_structure":
            structure_id = (request.POST.get("structure_id") or "").strip()
            structure = get_object_or_404(
                FeeStructure, pk=structure_id, account=account
            )
            structure.status = FeeStructure.Status.ACTIVE
            structure.save(update_fields=["status", "updated_at"])
            messages.success(
                request, f"Fee structure “{structure.display_name}” restored to active."
            )
            return redirect("billing:student_fees_fee_structure", account_id=account.id)

        elif action == "delete_structure":
            structure_id = (request.POST.get("structure_id") or "").strip()
            structure = get_object_or_404(
                FeeStructure, pk=structure_id, account=account
            )
            label = structure.display_name
            if FeeCharge.objects.filter(
                fee_structure=structure, amount_paid__gt=0
            ).exists():
                messages.error(
                    request,
                    f"Cannot delete “{label}”: learners have payments against it. "
                    "Archive it instead.",
                )
            else:
                with transaction.atomic():
                    FeeCharge.objects.filter(fee_structure=structure).delete()
                    structure.delete()
                messages.success(request, f"Fee structure “{label}” deleted.")
            return redirect("billing:student_fees_fee_structure", account_id=account.id)

    structures = list(
        FeeStructure.objects.filter(account=account)
        .select_related("financial_year", "financial_term")
        .prefetch_related("lines__vote")
        .order_by("-created_at")
    )
    level_map = {level.id: level for level in levels}
    structure_rows = []
    structures_payload = {}
    for structure in structures:
        charged_students = (
            FeeCharge.objects.filter(fee_structure=structure)
            .values("student_id")
            .distinct()
            .count()
        )
        has_payments = FeeCharge.objects.filter(
            fee_structure=structure, amount_paid__gt=0
        ).exists()
        vote_amounts = {str(vote.id): "" for vote in votes}
        for line in structure.lines.all():
            vote_amounts[str(line.vote_id)] = f"{line.amount:.2f}"
        structures_payload[str(structure.id)] = {
            "id": structure.id,
            "financial_year_id": str(structure.financial_year_id),
            "financial_term_id": str(structure.financial_term_id),
            "notes": structure.notes or "",
            "academic_levels": list(structure.academic_level_ids or []),
            "vote_amounts": vote_amounts,
            "status": structure.status,
            "display_name": structure.display_name,
        }
        structure_rows.append(
            {
                "structure": structure,
                "levels": [
                    level_map[level_id]
                    for level_id in (structure.academic_level_ids or [])
                    if level_id in level_map
                ],
                "total": structure.total_amount,
                "line_count": structure.lines.count(),
                "student_count": charged_students,
                "has_payments": has_payments,
            }
        )

    terms_by_year = {
        str(year.id): [
            {"id": term.id, "name": term.name}
            for term in year.terms.all().order_by("start_date", "name")
        ]
        for year in years
    }

    return render(
        request,
        "billing/student_fees_fee_structure.html",
        {
            "account": account,
            "levels": levels,
            "votes": votes,
            "years": years,
            "terms_by_year": terms_by_year,
            "terms_by_year_json": json.dumps(terms_by_year),
            "structure_rows": structure_rows,
            "structures_json": json.dumps(structures_payload),
            "form": form,
            "vote_amounts_json": json.dumps(form["vote_amounts"]),
            "form_levels_json": json.dumps(form["academic_levels"]),
            "open_structure_modal": open_structure_modal,
            "editing_structure_id": editing_structure_id,
            "current_year": current_year,
            "current_term": current_term,
        },
    )


@portal_access_required
@require_GET
def pocket_money(request):
    academic_levels = list(AcademicLevel.objects.all().order_by("order", "name"))
    level_map = {level.id: level for level in academic_levels}
    balance_zero = Value(
        Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2)
    )
    accounts_qs = (
        SchoolAccount.objects.filter(category=SchoolAccount.Category.POCKET_MONEY)
        .annotate(
            balance=Coalesce(
                Sum(
                    "top_ups__amount",
                    filter=Q(top_ups__status=AccountTopUp.Status.APPROVED),
                ),
                balance_zero,
            ),
            pending_topups=Count(
                "top_ups",
                filter=Q(top_ups__status=AccountTopUp.Status.PENDING),
            ),
            votes_count=Count("votes"),
        )
        .order_by("-is_active", "name")
    )

    pocket_accounts = []
    total_balance = Decimal("0.00")
    active_count = 0
    for account in accounts_qs:
        linked = [
            level_map[level_id]
            for level_id in (account.academic_level_ids or [])
            if level_id in level_map
        ]
        balance = account.balance or Decimal("0.00")
        total_balance += balance
        if account.is_active:
            active_count += 1
        pocket_accounts.append(
            {
                "account": account,
                "levels": linked,
                "balance": balance,
                "pending_topups": account.pending_topups or 0,
                "votes_count": account.votes_count or 0,
            }
        )

    pending_topup_count = AccountTopUp.objects.filter(
        account__category=SchoolAccount.Category.POCKET_MONEY,
        status=AccountTopUp.Status.PENDING,
    ).count()

    return render(
        request,
        "billing/pocket_money.html",
        {
            "pocket_accounts": pocket_accounts,
            "total_balance": total_balance,
            "active_count": active_count,
            "pending_topup_count": pending_topup_count,
        },
    )


@portal_access_required
@require_GET
def petty_cashbook(request):
    balance_zero = Value(
        Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2)
    )
    accounts_qs = (
        SchoolAccount.objects.filter(category=SchoolAccount.Category.PETTY_CASHBOOK)
        .annotate(
            topped_up=Coalesce(
                Sum(
                    "top_ups__amount",
                    filter=Q(top_ups__status=AccountTopUp.Status.APPROVED),
                ),
                balance_zero,
            ),
            withdrawn=Coalesce(
                Sum(
                    "withdrawals__amount",
                    filter=Q(withdrawals__status=AccountWithdraw.Status.APPROVED),
                ),
                balance_zero,
            ),
            pending_topups=Count(
                "top_ups",
                filter=Q(top_ups__status=AccountTopUp.Status.PENDING),
            ),
        )
        .order_by("-is_active", "name")
    )

    petty_accounts = []
    total_balance = Decimal("0.00")
    active_count = 0
    for account in accounts_qs:
        topped_up = account.topped_up or Decimal("0.00")
        withdrawn = account.withdrawn or Decimal("0.00")
        balance = topped_up - withdrawn
        total_balance += balance
        if account.is_active:
            active_count += 1
        petty_accounts.append(
            {
                "account": account,
                "balance": balance,
                "topped_up": topped_up,
                "withdrawn": withdrawn,
                "pending_topups": account.pending_topups or 0,
            }
        )

    recent_topups = (
        AccountTopUp.objects.filter(account__category=SchoolAccount.Category.PETTY_CASHBOOK)
        .select_related("account")
        .order_by("-created_at")[:10]
    )
    recent_withdrawals = (
        AccountWithdraw.objects.filter(account__category=SchoolAccount.Category.PETTY_CASHBOOK)
        .select_related("account")
        .order_by("-created_at")[:10]
    )

    return render(
        request,
        "billing/petty_cashbook.html",
        {
            "petty_accounts": petty_accounts,
            "total_balance": total_balance,
            "active_count": active_count,
            "recent_topups": recent_topups,
            "recent_withdrawals": recent_withdrawals,
        },
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def petty_cashbook_detail(request, account_id):
    account = get_object_or_404(
        SchoolAccount,
        pk=account_id,
        category=SchoolAccount.Category.PETTY_CASHBOOK,
    )
    daraja = DarajaSettings.load()
    mpesa_enabled = bool(daraja.is_enabled)
    open_topup_modal = False
    open_withdraw_modal = False
    topup_form = {
        "amount": "",
        "method": "",
        "description": "",
        "reference_number": "",
    }
    withdraw_form = {
        "amount": "",
        "method": "",
        "payee": "",
        "description": "",
        "reference_number": "",
    }

    def parse_topup_form():
        amount_raw = (request.POST.get("amount") or "").strip()
        method = upper_input(request.POST.get("method"))
        description = (request.POST.get("description") or "").strip()
        reference_number = upper_input(request.POST.get("reference_number"))
        data = {
            "amount": amount_raw,
            "method": method,
            "description": description,
            "reference_number": reference_number,
        }
        valid_methods = {choice for choice, _ in petty_cashbook_topup_methods(mpesa_enabled)}
        error = None
        amount = None
        if not account.is_active:
            error = "This account is suspended and cannot be topped up."
        if error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid top-up amount greater than zero."
        if error is None and method not in valid_methods:
            error = "Select a valid top-up method."
        if error is None and not reference_number:
            error = "Reference number is required."
        if error is None and len(description) > 255:
            error = "Description must be 255 characters or fewer."
        return data, amount, error

    def parse_withdraw_form():
        amount_raw = (request.POST.get("amount") or "").strip()
        method = upper_input(request.POST.get("method"))
        payee = upper_input(request.POST.get("payee"))
        description = (request.POST.get("description") or "").strip()
        reference_number = upper_input(request.POST.get("reference_number"))
        data = {
            "amount": amount_raw,
            "method": method,
            "payee": payee,
            "description": description,
            "reference_number": reference_number,
        }
        valid_methods = {choice for choice, _ in AccountWithdraw.Method.choices}
        error = None
        amount = None
        balance = petty_cashbook_account_balance(account)
        if not account.is_active:
            error = "This account is suspended and cannot be withdrawn from."
        if error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid withdrawal amount greater than zero."
        if error is None and method not in valid_methods:
            error = "Select a valid withdrawal method."
        if error is None and not payee:
            error = "Payee name is required."
        if error is None and len(payee) > 160:
            error = "Payee name must be 160 characters or fewer."
        if error is None and not reference_number:
            error = "Reference / voucher number is required."
        if error is None and len(description) > 255:
            error = "Description must be 255 characters or fewer."
        if error is None and amount > balance:
            error = (
                f"Withdrawal KES {amount} exceeds available balance "
                f"KES {balance.quantize(Decimal('0.01'))}."
            )
        return data, amount, error

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "topup":
            topup_form, amount, error = parse_topup_form()
            if error:
                messages.error(request, error)
                open_topup_modal = True
            else:
                topup = AccountTopUp(
                    account=account,
                    amount=amount,
                    method=topup_form["method"],
                    description=topup_form["description"],
                    reference_number=topup_form["reference_number"],
                    status=AccountTopUp.Status.APPROVED,
                    created_by=request.user,
                )
                topup.save()
                messages.success(
                    request,
                    f"Topped up “{account.name}” with KES {topup.amount}. "
                    f"Reference code: {topup.reference_code}.",
                )
                return redirect("billing:petty_cashbook_detail", account_id=account.id)
        elif action == "withdraw":
            withdraw_form, amount, error = parse_withdraw_form()
            if error:
                messages.error(request, error)
                open_withdraw_modal = True
            else:
                withdrawal = AccountWithdraw(
                    account=account,
                    amount=amount,
                    method=withdraw_form["method"],
                    payee=withdraw_form["payee"],
                    description=withdraw_form["description"],
                    reference_number=withdraw_form["reference_number"],
                    status=AccountWithdraw.Status.APPROVED,
                    created_by=request.user,
                )
                withdrawal.save()
                messages.success(
                    request,
                    f"Withdrew KES {withdrawal.amount} from “{account.name}”. "
                    f"Reference code: {withdrawal.reference_code}.",
                )
                return redirect("billing:petty_cashbook_detail", account_id=account.id)

    balance = petty_cashbook_account_balance(account)
    topped_up = (
        AccountTopUp.objects.filter(
            account=account,
            status=AccountTopUp.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    withdrawn = (
        AccountWithdraw.objects.filter(
            account=account,
            status=AccountWithdraw.Status.APPROVED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    pending_topups = AccountTopUp.objects.filter(
        account=account,
        status=AccountTopUp.Status.PENDING,
    ).count()
    topups = AccountTopUp.objects.filter(account=account).order_by("-created_at")
    withdrawals = AccountWithdraw.objects.filter(account=account).order_by("-created_at")

    return render(
        request,
        "billing/petty_cashbook_detail.html",
        {
            "account": account,
            "balance": balance,
            "topped_up": topped_up,
            "withdrawn": withdrawn,
            "pending_topups": pending_topups,
            "topups": topups,
            "withdrawals": withdrawals,
            "topup_methods": petty_cashbook_topup_methods(mpesa_enabled),
            "withdraw_methods": AccountWithdraw.Method.choices,
            "topup_form": topup_form,
            "withdraw_form": withdraw_form,
            "open_topup_modal": open_topup_modal,
            "open_withdraw_modal": open_withdraw_modal,
            "mpesa_enabled": mpesa_enabled,
        },
    )


STORE_MODULES = [
    {
        "slug": "requisitions",
        "url_name": "billing:store_requisitions",
        "title": "Requisitions",
        "meta": "Purchasing",
        "blurb": "Raise and track store requisition requests.",
        "copy": "Create and follow store requisitions through review and approval.",
    },
    {
        "slug": "lpo",
        "url_name": "billing:store_lpo",
        "title": "LPO",
        "meta": "Purchasing",
        "blurb": "Prepare and manage local purchase orders.",
        "copy": "Raise local purchase orders against approved store buying.",
    },
    {
        "slug": "register-item",
        "url_name": "billing:store_register_item",
        "title": "Register item",
        "meta": "Catalogue",
        "blurb": "Add and maintain store item records.",
        "copy": "Register items used in the school store and keep their details current.",
    },
    {
        "slug": "stock-in-out",
        "url_name": "billing:store_stock_in_out",
        "title": "Stock in & out",
        "meta": "Movement",
        "blurb": "Record stock receipts and issues.",
        "copy": "Capture stock coming into the store and stock issued out.",
    },
    {
        "slug": "stock-analysis",
        "url_name": "billing:store_stock_analysis",
        "title": "Stock analysis",
        "meta": "Reports",
        "blurb": "Review movement, usage, and stock trends.",
        "copy": "Analyse store movement and usage to support restocking decisions.",
    },
    {
        "slug": "current-stock",
        "url_name": "billing:store_current_stock",
        "title": "Current stock",
        "meta": "Inventory",
        "blurb": "See quantities currently on hand.",
        "copy": "View current stock balances for registered store items.",
    },
    {
        "slug": "suppliers",
        "url_name": "billing:store_suppliers",
        "title": "Suppliers",
        "meta": "Directory",
        "blurb": "Manage supplier records for store buying.",
        "copy": "Keep supplier details used for requisitions and purchase orders.",
    },
    {
        "slug": "stock-audit",
        "url_name": "billing:store_stock_audit",
        "title": "Stock audit",
        "meta": "Control",
        "blurb": "Count and verify store holdings.",
        "copy": "Run stock counts and verify store holdings against records.",
    },
]
STORE_MODULE_MAP = {module["slug"]: module for module in STORE_MODULES}


def _store_workspace_context(**extra):
    context = {
        "store_workspace": True,
        "store_modules": STORE_MODULES,
    }
    context.update(extra)
    return context


@portal_access_required
@require_GET
def store_management(request):
    return render(
        request,
        "billing/store_management.html",
        _store_workspace_context(),
    )


@portal_access_required
@require_GET
def store_section(request, slug):
    module = STORE_MODULE_MAP.get(slug)
    if module is None:
        raise Http404("Unknown store section.")
    return render(
        request,
        "billing/store_section.html",
        _store_workspace_context(module=module),
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def store_register_item(request):
    module = STORE_MODULE_MAP["register-item"]
    ensure_store_lookups()
    open_item_modal = False
    form = StoreItemForm()

    if request.method == "POST":
        form = StoreItemForm(request.POST, request.FILES)
        if form.is_valid():
            item = form.save(commit=False)
            item.created_by = request.user
            item.save()
            messages.success(
                request,
                f"Item “{item.name}” registered as {item.reference_code}.",
            )
            return redirect("billing:store_register_item")
        open_item_modal = True

    items = StoreItem.objects.select_related(
        "expense_category", "department_station", "created_by"
    )
    return render(
        request,
        "billing/store_register_item.html",
        _store_workspace_context(
            module=module,
            form=form,
            items=items,
            item_count=items.count(),
            open_item_modal=open_item_modal,
        ),
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def store_stock_in_out(request):
    module = STORE_MODULE_MAP["stock-in-out"]
    ensure_store_lookups()
    open_stock_modal = False
    stock_form = {
        "item_id": "",
        "direction": StoreStockMovement.Direction.IN,
        "quantity": "",
        "notes": "",
        "item_name": "",
        "on_hand": "",
        "supplier_id": "",
        "supplier_name": "",
        "supplier_phone": "",
        "payment_status": StoreStockMovement.PaymentStatus.UNPAID,
        "out_reason": "",
        "destination_station_id": "",
    }

    if request.method == "POST":
        direction = upper_input(request.POST.get("direction"))
        item_id = (request.POST.get("item_id") or "").strip()
        quantity_raw = (request.POST.get("quantity") or "").strip()
        notes = (request.POST.get("notes") or "").strip()
        supplier_id = (request.POST.get("supplier_id") or "").strip()
        supplier_name = upper_input(request.POST.get("supplier_name"))
        supplier_phone = normalize_supplier_phone(request.POST.get("supplier_phone"))
        payment_status = upper_input(request.POST.get("payment_status"))
        out_reason = upper_input(request.POST.get("out_reason"))
        destination_station_id = (request.POST.get("destination_station_id") or "").strip()
        stock_form.update(
            {
                "item_id": item_id,
                "direction": direction,
                "quantity": quantity_raw,
                "notes": notes,
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "supplier_phone": supplier_phone,
                "payment_status": payment_status,
                "out_reason": out_reason,
                "destination_station_id": destination_station_id,
            }
        )
        error = None
        item = None
        quantity = None
        supplier = None
        destination_station = None
        needs_supplier = False
        if direction not in StoreStockMovement.Direction.values:
            error = "Choose stock in or stock out."
        if error is None:
            if not item_id.isdigit():
                error = "Select an item."
            else:
                item = StoreItem.objects.filter(pk=int(item_id), is_active=True).first()
                if item is None:
                    error = "Select a registered item."
        if error is None:
            try:
                quantity = Decimal(quantity_raw)
                if quantity <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a quantity greater than zero."
        if error is None and len(notes) > 255:
            error = "Notes must be 255 characters or fewer."

        if error is None and direction == StoreStockMovement.Direction.IN:
            needs_supplier = True
            out_reason = ""
            if payment_status not in StoreStockMovement.PaymentStatus.values:
                error = "Select a payment status."
        elif error is None:
            payment_status = ""
            if out_reason not in StoreStockMovement.OutReason.values:
                error = "Choose how this stock is going out."
            elif out_reason == StoreStockMovement.OutReason.TRANSFER:
                if not destination_station_id.isdigit():
                    error = "Select the department receiving this stock."
                else:
                    destination_station = StoreDepartmentStation.objects.filter(
                        pk=int(destination_station_id), is_active=True
                    ).first()
                    if destination_station is None:
                        error = "Select a valid department station."
            elif out_reason == StoreStockMovement.OutReason.RETURN:
                needs_supplier = True
            else:
                destination_station = None

        if error is None and needs_supplier:
            if not supplier_name:
                error = "Supplier name is required."
            elif not supplier_phone:
                error = "Supplier phone number is required."
            elif len(supplier_phone) < 9:
                error = "Enter a valid supplier phone number."
            elif supplier_id.isdigit():
                supplier = StoreSupplier.objects.filter(
                    pk=int(supplier_id), is_active=True
                ).first()
                if supplier is None:
                    error = "Selected supplier was not found."
        elif error is None:
            supplier_id = ""
            supplier_name = ""
            supplier_phone = ""
            stock_form["supplier_id"] = ""
            stock_form["supplier_name"] = ""
            stock_form["supplier_phone"] = ""

        if error:
            messages.error(request, error)
            open_stock_modal = True
            if item is not None:
                stock_form["item_name"] = item.name
                stock_form["on_hand"] = str(store_item_quantity_on_hand(item))
        else:
            with transaction.atomic():
                locked = StoreItem.objects.select_for_update().get(pk=item.pk)
                on_hand = store_item_quantity_on_hand(locked)
                if (
                    direction == StoreStockMovement.Direction.OUT
                    and quantity > on_hand
                ):
                    messages.error(
                        request,
                        f"Cannot stock out {quantity} — only {on_hand} {locked.get_measure_display().lower()} on hand.",
                    )
                    open_stock_modal = True
                    stock_form["item_name"] = locked.name
                    stock_form["on_hand"] = str(on_hand)
                else:
                    if needs_supplier:
                        if supplier is None or supplier.phone_number != supplier_phone:
                            try:
                                supplier, _ = get_or_create_store_supplier(
                                    name=supplier_name,
                                    phone_number=supplier_phone,
                                    user=request.user,
                                )
                            except ValueError as exc:
                                messages.error(request, str(exc))
                                open_stock_modal = True
                                stock_form["item_name"] = locked.name
                                stock_form["on_hand"] = str(on_hand)
                                supplier = None
                        elif supplier_name and supplier.name != supplier_name:
                            supplier.name = supplier_name
                            supplier.save(update_fields=["name", "updated_at"])
                    else:
                        supplier = None

                    if not open_stock_modal and (not needs_supplier or supplier is not None):
                        movement = StoreStockMovement(
                            item=locked,
                            direction=direction,
                            quantity=quantity,
                            supplier=supplier,
                            payment_status=payment_status,
                            out_reason=out_reason,
                            destination_station=destination_station,
                            notes=notes,
                            created_by=request.user,
                        )
                        movement.save()
                        if direction == StoreStockMovement.Direction.IN:
                            messages.success(
                                request,
                                f"Stock in recorded for {locked.name}. Print the delivery receipt for the supplier.",
                            )
                            return redirect(
                                "billing:store_stock_delivery_receipt",
                                movement_id=movement.id,
                            )
                        qty_label = f"{quantity} {locked.get_measure_display().lower()}"
                        if out_reason == StoreStockMovement.OutReason.TRANSFER:
                            messages.success(
                                request,
                                f"Transferred {qty_label} of {locked.name} to {destination_station.name}.",
                            )
                        elif out_reason == StoreStockMovement.OutReason.WASTE:
                            messages.success(
                                request,
                                f"Recorded {qty_label} of {locked.name} as waste.",
                            )
                        else:
                            messages.success(
                                request,
                                f"Returned {qty_label} of {locked.name} to {supplier.name}.",
                            )
                        return redirect("billing:store_stock_in_out")

    zero = Value(
        Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2)
    )
    items = list(
        StoreItem.objects.filter(is_active=True)
        .select_related("expense_category", "department_station")
        .annotate(
            qty_in=Coalesce(
                Sum(
                    "movements__quantity",
                    filter=Q(movements__direction=StoreStockMovement.Direction.IN),
                ),
                zero,
            ),
            qty_out=Coalesce(
                Sum(
                    "movements__quantity",
                    filter=Q(movements__direction=StoreStockMovement.Direction.OUT),
                ),
                zero,
            ),
        )
        .order_by(
            "expense_category__sort_order",
            "expense_category__name",
            "name",
        )
    )
    category_groups = []
    grouped = {}
    for item in items:
        item.quantity_on_hand = item.qty_in - item.qty_out
        category = item.expense_category
        if category.id not in grouped:
            group = {"category": category, "items": []}
            grouped[category.id] = group
            category_groups.append(group)
        grouped[category.id]["items"].append(item)

    if open_stock_modal and stock_form["item_id"] and not stock_form["item_name"]:
        match = next(
            (row for row in items if str(row.id) == str(stock_form["item_id"])),
            None,
        )
        if match is not None:
            stock_form["item_name"] = match.name
            stock_form["on_hand"] = str(match.quantity_on_hand)

    return render(
        request,
        "billing/store_stock_in_out.html",
        _store_workspace_context(
            module=module,
            category_groups=category_groups,
            item_count=len(items),
            open_stock_modal=open_stock_modal,
            stock_form=stock_form,
            payment_statuses=StoreStockMovement.PaymentStatus.choices,
            out_reasons=StoreStockMovement.OutReason.choices,
            department_stations=StoreDepartmentStation.objects.filter(is_active=True),
            supplier_suggest_url=reverse("billing:store_supplier_suggest"),
        ),
    )


@portal_access_required
@require_GET
def store_suppliers(request):
    module = STORE_MODULE_MAP["suppliers"]
    suppliers = (
        StoreSupplier.objects.annotate(
            delivery_count=Count(
                "stock_movements",
                filter=Q(stock_movements__direction=StoreStockMovement.Direction.IN),
            ),
            return_count=Count(
                "stock_movements",
                filter=Q(
                    stock_movements__direction=StoreStockMovement.Direction.OUT,
                    stock_movements__out_reason=StoreStockMovement.OutReason.RETURN,
                ),
            ),
            open_payments=Count(
                "stock_movements",
                filter=Q(
                    stock_movements__direction=StoreStockMovement.Direction.IN,
                    stock_movements__payment_status__in=SUPPLIER_OPEN_PAYMENT_STATUSES,
                ),
            ),
        )
        .order_by("-is_active", "name")
    )
    active_count = sum(1 for row in suppliers if row.is_active)
    open_payment_total = sum(row.open_payments for row in suppliers)
    return render(
        request,
        "billing/store_suppliers.html",
        _store_workspace_context(
            module=module,
            suppliers=suppliers,
            supplier_count=len(suppliers),
            active_count=active_count,
            open_payment_total=open_payment_total,
        ),
    )


@portal_access_required
@require_GET
def store_supplier_detail(request, supplier_id):
    supplier = get_object_or_404(StoreSupplier, pk=supplier_id)
    movements = (
        StoreStockMovement.objects.filter(supplier=supplier)
        .select_related(
            "item",
            "item__expense_category",
            "destination_station",
            "created_by",
        )
        .order_by("-created_at")
    )
    delivery_count = movements.filter(
        direction=StoreStockMovement.Direction.IN
    ).count()
    return_count = movements.filter(
        direction=StoreStockMovement.Direction.OUT,
        out_reason=StoreStockMovement.OutReason.RETURN,
    ).count()
    open_payments = movements.filter(
        direction=StoreStockMovement.Direction.IN,
        payment_status__in=SUPPLIER_OPEN_PAYMENT_STATUSES,
    ).count()
    paid_deliveries = movements.filter(
        direction=StoreStockMovement.Direction.IN,
        payment_status=StoreStockMovement.PaymentStatus.PAID,
    ).count()
    return render(
        request,
        "billing/store_supplier_detail.html",
        _store_workspace_context(
            module=STORE_MODULE_MAP["suppliers"],
            supplier=supplier,
            movements=movements,
            delivery_count=delivery_count,
            return_count=return_count,
            open_payments=open_payments,
            paid_deliveries=paid_deliveries,
        ),
    )


@portal_access_required
@require_GET
def reports(request):
    today = timezone.localdate()
    financial_years = list(
        FinancialYear.objects.prefetch_related("terms").order_by("-start_date")
    )
    current_year = FinancialYear.current() or (
        financial_years[0] if financial_years else None
    )
    current_term = None
    if current_year is not None:
        system_current_term = FinancialTerm.current()
        if (
            system_current_term is not None
            and system_current_term.financial_year_id == current_year.id
        ):
            current_term = system_current_term
        else:
            current_term = (
                current_year.terms.filter(is_current=True).first()
                or current_year.terms.order_by("start_date").first()
            )

    category_key = (request.GET.get("category") or "").strip()
    report_type = (request.GET.get("report_type") or "").strip()
    period_mode = (request.GET.get("period_mode") or "").strip() or "academic_year"
    year_id = (request.GET.get("financial_year_id") or "").strip()
    term_id = (request.GET.get("financial_term_id") or "").strip()
    custom_from = parse_report_date(request.GET.get("date_from"))
    custom_to = parse_report_date(request.GET.get("date_to"))
    generated = "report_type" in request.GET
    error = None
    selected_report = None
    selected_category = None
    report_result = None
    selected_year = None
    selected_term = None
    date_from = None
    date_to = None
    period_label = ""

    if period_mode not in {"academic_year", "academic_term", "custom"}:
        period_mode = "academic_year"

    if year_id.isdigit():
        selected_year = next(
            (year for year in financial_years if year.id == int(year_id)), None
        )
    if selected_year is None:
        selected_year = current_year

    if selected_year is not None and term_id.isdigit():
        selected_term = selected_year.terms.filter(pk=int(term_id)).first()
    if selected_term is None and selected_year is not None:
        if (
            current_term is not None
            and current_term.financial_year_id == selected_year.id
        ):
            selected_term = current_term
        else:
            selected_term = selected_year.terms.order_by("start_date").first()

    category_map = {item["key"]: item for item in REPORT_CATEGORIES}
    if category_key in category_map:
        selected_category = category_map[category_key]

    def resolve_period():
        nonlocal date_from, date_to, period_label, selected_year, selected_term
        if period_mode == "academic_year":
            if selected_year is None:
                return "Select an academic year."
            date_from = selected_year.start_date
            date_to = selected_year.end_date
            period_label = f"Academic year {selected_year.display_name}"
            return None
        if period_mode == "academic_term":
            if selected_year is None:
                return "Select an academic year."
            if selected_term is None:
                return "Select a term for that academic year."
            date_from = selected_term.start_date
            date_to = selected_term.end_date
            period_label = (
                f"{selected_term.name} · {selected_year.display_name}"
            )
            return None
        # custom
        date_from = custom_from
        date_to = custom_to
        if date_from is None or date_to is None:
            return "Enter both custom from and to dates."
        if date_from > date_to:
            return "The start date must be on or before the end date."
        period_label = f"Custom · {date_from.isoformat()} to {date_to.isoformat()}"
        return None

    if generated:
        if not report_type:
            error = "Select the type of report to generate."
        elif report_type not in REPORT_TYPE_MAP:
            error = "Choose a valid report type."
        else:
            selected_report = REPORT_TYPE_MAP[report_type]
            selected_category = category_map.get(selected_report["category_key"])
            category_key = selected_report["category_key"]
            error = resolve_period()
            if error is None:
                try:
                    report_result = build_report(report_type, date_from, date_to)
                except ValueError:
                    error = "Choose a valid report type."
    else:
        # Defaults for the filter form before generate.
        if period_mode == "custom":
            date_from = custom_from or (
                current_year.start_date if current_year else today - timedelta(days=30)
            )
            date_to = custom_to or (current_year.end_date if current_year else today)
            period_label = "Custom date range"
        elif period_mode == "academic_term" and selected_term is not None:
            date_from = selected_term.start_date
            date_to = selected_term.end_date
            period_label = f"{selected_term.name} · {selected_year.display_name}"
        elif selected_year is not None:
            date_from = selected_year.start_date
            date_to = selected_year.end_date
            period_label = f"Academic year {selected_year.display_name}"
        else:
            date_from = today - timedelta(days=30)
            date_to = today
            period_label = "Custom date range"
            period_mode = "custom"

    if error:
        messages.error(request, error)

    available_reports = selected_category["reports"] if selected_category else []
    years_payload = [
        {
            "id": year.id,
            "label": year.display_name,
            "start": year.start_date.isoformat(),
            "end": year.end_date.isoformat(),
            "terms": [
                {
                    "id": term.id,
                    "label": term.name,
                    "start": term.start_date.isoformat(),
                    "end": term.end_date.isoformat(),
                }
                for term in year.terms.all()
            ],
        }
        for year in financial_years
    ]

    return render(
        request,
        "billing/reports.html",
        {
            "report_categories": REPORT_CATEGORIES,
            "selected_category": selected_category,
            "category_key": category_key,
            "available_reports": available_reports,
            "selected_report": selected_report,
            "report_type": report_type,
            "period_mode": period_mode,
            "financial_years": financial_years,
            "years_payload_json": json.dumps(years_payload),
            "selected_year_id": selected_year.id if selected_year else "",
            "selected_term_id": selected_term.id if selected_term else "",
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
            "period_label": period_label,
            "generated": generated and error is None and report_result is not None,
            "report_result": report_result,
            "row_count": len(report_result["rows"]) if report_result else 0,
        },
    )


@portal_access_required
@require_GET
def store_reports(request):
    return redirect("billing:reports")


@portal_access_required
@require_GET
def store_supplier_suggest(request):
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    phone_q = normalize_supplier_phone(q)
    lookup = Q(name__icontains=q) | Q(phone_number__icontains=q)
    if phone_q and phone_q != q:
        lookup |= Q(phone_number__icontains=phone_q)
    results = [
        {
            "id": supplier.id,
            "name": supplier.name,
            "phone_number": supplier.phone_number,
        }
        for supplier in StoreSupplier.objects.filter(is_active=True)
        .filter(lookup)
        .order_by("name")[:8]
    ]
    return JsonResponse({"results": results})


@portal_access_required
@require_GET
def store_stock_delivery_receipt(request, movement_id):
    movement = get_object_or_404(
        StoreStockMovement.objects.select_related(
            "item",
            "item__expense_category",
            "item__department_station",
            "supplier",
            "created_by",
        ),
        pk=movement_id,
        direction=StoreStockMovement.Direction.IN,
    )
    return render(
        request,
        "billing/store_stock_delivery_receipt.html",
        _store_workspace_context(movement=movement),
    )


PAYABLE_SUPPLIER_STATUSES = {
    StoreStockMovement.PaymentStatus.UNPAID,
    StoreStockMovement.PaymentStatus.PENDING,
    StoreStockMovement.PaymentStatus.PARTIAL,
}


def _supplier_payable_movements(supplier_id):
    return (
        StoreStockMovement.objects.filter(
            supplier_id=supplier_id,
            direction=StoreStockMovement.Direction.IN,
            payment_status__in=PAYABLE_SUPPLIER_STATUSES,
        )
        .select_related("item", "item__expense_category")
        .order_by("-created_at")
    )


@portal_access_required
@require_GET
def invoices(request):
    charges = FeeCharge.objects.select_related("category").order_by("-created_at")[:40]
    payments = Payment.objects.select_related("charge", "received_by").order_by(
        "-received_at"
    )[:40]
    student_ids = {c.student_id for c in charges} | {p.student_id for p in payments}
    student_map = {
        s.id: s for s in Student.objects.filter(id__in=student_ids)
    }
    for charge in charges:
        charge.student = student_map.get(charge.student_id)
    for payment in payments:
        payment.student = student_map.get(payment.student_id)

    totals = Payment.objects.aggregate(total=Sum("amount"))
    supplier_count = StoreSupplier.objects.filter(is_active=True).count()
    return render(
        request,
        "billing/invoices.html",
        {
            "charges": charges,
            "payments": payments,
            "payments_total": totals["total"] or Decimal("0.00"),
            "invoice_count": FeeCharge.objects.count(),
            "payment_count": Payment.objects.count(),
            "supplier_count": supplier_count,
        },
    )


@portal_access_required
@require_GET
def supplier_accounts(request):
    suppliers = []
    for supplier in StoreSupplier.objects.filter(is_active=True).order_by("name"):
        payable = _supplier_payable_movements(supplier.id)
        pending_count = payable.count()
        outstanding = Decimal("0.00")
        for movement in payable:
            if movement.invoice_amount is not None:
                outstanding += movement.amount_outstanding or Decimal("0.00")
        suppliers.append(
            {
                "supplier": supplier,
                "pending_count": pending_count,
                "outstanding": outstanding,
            }
        )
    return render(
        request,
        "billing/supplier_accounts.html",
        {"suppliers": suppliers},
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def supplier_account_detail(request, supplier_id):
    supplier = get_object_or_404(StoreSupplier, pk=supplier_id, is_active=True)
    open_pay_modal = False
    pay_form = {
        "movement_id": "",
        "account_id": "",
        "amount": "",
        "invoice_amount": "",
        "method": "",
        "item_label": "",
        "needs_invoice_amount": False,
    }
    preview_reference = generate_supplier_payment_reference_code()

    payable_movements = list(_supplier_payable_movements(supplier.id))
    for movement in payable_movements:
        movement.outstanding_display = movement.amount_outstanding

    pay_accounts = []
    for account in SchoolAccount.objects.filter(is_active=True).order_by(
        "category", "name"
    ):
        balance = school_account_available_balance(account)
        pay_accounts.append({"account": account, "balance": balance})

    if request.method == "POST":
        movement_id = (request.POST.get("movement_id") or "").strip()
        account_id = (request.POST.get("account_id") or "").strip()
        amount_raw = (request.POST.get("amount") or "").strip()
        invoice_raw = (request.POST.get("invoice_amount") or "").strip()
        method = upper_input(request.POST.get("method"))
        pay_form.update(
            {
                "movement_id": movement_id,
                "account_id": account_id,
                "amount": amount_raw,
                "invoice_amount": invoice_raw,
                "method": method,
            }
        )
        error = None
        movement = None
        account = None
        amount = None
        invoice_amount = None

        if not movement_id.isdigit():
            error = "Select a delivery to pay."
        else:
            movement = get_object_or_404(
                StoreStockMovement,
                pk=int(movement_id),
                supplier=supplier,
                direction=StoreStockMovement.Direction.IN,
            )
            pay_form["item_label"] = movement.item.name
            pay_form["needs_invoice_amount"] = movement.invoice_amount is None
            if movement.payment_status not in PAYABLE_SUPPLIER_STATUSES:
                error = "That delivery is already paid."

        if error is None and not account_id.isdigit():
            error = "Select the account to pay from."
        elif error is None:
            account = SchoolAccount.objects.filter(pk=int(account_id), is_active=True).first()
            if account is None:
                error = "Select a valid school account."

        if error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid payment amount."

        if error is None and movement.invoice_amount is None:
            if not invoice_raw:
                error = "Enter the invoice amount for this delivery."
            else:
                try:
                    invoice_amount = Decimal(invoice_raw)
                    if invoice_amount <= 0:
                        raise ValueError
                except Exception:
                    error = "Enter a valid invoice amount."
        elif error is None:
            invoice_amount = movement.invoice_amount

        if error is None and amount > (movement.amount_outstanding or invoice_amount):
            error = "Payment amount exceeds the outstanding balance for this delivery."

        valid_methods = {choice for choice, _ in AccountWithdraw.Method.choices}
        if error is None and method not in valid_methods:
            error = "Select a valid payment method."

        if error is None:
            available = school_account_available_balance(account)
            if amount > available:
                error = f"Insufficient balance in {account.name} (KES {available:,.2f} available)."

        if error:
            messages.error(request, error)
            open_pay_modal = True
            preview_reference = generate_supplier_payment_reference_code()
        else:
            with transaction.atomic():
                locked_movement = StoreStockMovement.objects.select_for_update().get(
                    pk=movement.pk
                )
                update_fields = ["amount_paid", "payment_status"]
                if locked_movement.invoice_amount is None:
                    locked_movement.invoice_amount = invoice_amount
                    update_fields.append("invoice_amount")
                locked_movement.amount_paid = (
                    locked_movement.amount_paid or Decimal("0.00")
                ) + amount
                locked_movement.refresh_payment_status(save=False)
                locked_movement.save(update_fields=update_fields)

                payment = StoreSupplierPayment(
                    movement=locked_movement,
                    account=account,
                    amount=amount,
                    method=method,
                    created_by=request.user,
                )
                payment.save()

                withdrawal = AccountWithdraw(
                    account=account,
                    amount=amount,
                    method=method,
                    payee=supplier.name,
                    description=f"Supplier payment · {locked_movement.reference_code}",
                    reference_number=payment.reference_number,
                    status=AccountWithdraw.Status.APPROVED,
                    created_by=request.user,
                )
                withdrawal.save()
                payment.withdrawal = withdrawal
                payment.save(update_fields=["withdrawal"])

            messages.success(
                request,
                f"Paid KES {amount:,.2f} to {supplier.name} ({payment.reference_code}).",
            )
            return redirect("billing:supplier_account_detail", supplier_id=supplier.id)

    if open_pay_modal and pay_form["movement_id"] and not pay_form["item_label"]:
        match = next(
            (m for m in payable_movements if str(m.id) == str(pay_form["movement_id"])),
            None,
        )
        if match:
            pay_form["item_label"] = match.item.name
            pay_form["needs_invoice_amount"] = match.invoice_amount is None

    recent_payments = (
        StoreSupplierPayment.objects.filter(movement__supplier=supplier)
        .select_related("movement", "movement__item", "account")
        .order_by("-created_at")[:15]
    )

    return render(
        request,
        "billing/supplier_account_detail.html",
        {
            "supplier": supplier,
            "payable_movements": payable_movements,
            "pay_accounts": pay_accounts,
            "pay_methods": AccountWithdraw.Method.choices,
            "open_pay_modal": open_pay_modal,
            "pay_form": pay_form,
            "preview_reference": preview_reference,
            "recent_payments": recent_payments,
        },
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def school_accounts(request):
    academic_levels = list(AcademicLevel.objects.all().order_by("order", "name"))
    level_map = {level.id: level for level in academic_levels}
    daraja = DarajaSettings.load()
    mpesa_enabled = bool(daraja.is_enabled)
    open_account_modal = False
    open_topup_modal = False
    editing_account_id = None
    form_data = {
        "category": "",
        "custom_category": "",
        "name": "",
        "description": "",
        "payment_modes": [],
        "academic_levels": [],
        "vote_fund_allocation": SchoolAccount.VoteFundAllocation.PRIORITY_ORDER,
    }
    topup_form = {
        "account_id": "",
        "amount": "",
        "method": "",
        "description": "",
        "reference_number": "",
    }

    def parse_account_form():
        return parse_school_account_form(request, level_map)

    def parse_topup_form():
        account_id = (request.POST.get("topup_account_id") or "").strip()
        amount_raw = (request.POST.get("amount") or "").strip()
        method = upper_input(request.POST.get("method"))
        description = (request.POST.get("description") or "").strip()
        reference_number = upper_input(request.POST.get("reference_number"))
        data = {
            "account_id": account_id,
            "amount": amount_raw,
            "method": method,
            "description": description,
            "reference_number": reference_number,
        }
        valid_methods = {choice for choice, _ in AccountTopUp.Method.choices}
        if not mpesa_enabled:
            valid_methods.discard(AccountTopUp.Method.STK_PUSH)

        error = None
        account = None
        amount = None
        if not account_id.isdigit():
            error = "Select an account to top up."
        else:
            account = SchoolAccount.objects.filter(pk=int(account_id), is_active=True).first()
            if account is None:
                error = "Select an active account to top up."
        if error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid top-up amount greater than zero."
        if error is None and method not in valid_methods:
            error = "Select a valid top-up method."
        if error is None and not reference_number:
            error = "Reference number is required."
        if error is None and len(description) > 255:
            error = "Description must be 255 characters or fewer."
        return data, account, amount, error

    if request.method == "POST":
        action = (request.POST.get("action") or "create").strip()
        account_id = request.POST.get("account_id")

        if action == "topup":
            topup_form, account, amount, error = parse_topup_form()
            if error:
                messages.error(request, error)
                open_topup_modal = True
            else:
                topup = AccountTopUp(
                    account=account,
                    amount=amount,
                    method=topup_form["method"],
                    description=topup_form["description"],
                    reference_number=topup_form["reference_number"],
                    status=AccountTopUp.Status.APPROVED,
                    created_by=request.user,
                )
                topup.save()
                if topup.method == AccountTopUp.Method.STK_PUSH:
                    messages.success(
                        request,
                        f"STK top-up recorded as approved for “{account.name}”. "
                        f"Reference code: {topup.reference_code}.",
                    )
                else:
                    messages.success(
                        request,
                        f"Topped up “{account.name}” with KES {topup.amount}. "
                        f"Reference code: {topup.reference_code}.",
                    )
                return redirect("billing:school_accounts")

        elif action in {"suspend", "unsuspend", "delete"}:
            account = get_object_or_404(SchoolAccount, pk=account_id)
            if action == "delete":
                label = account.name
                account.delete()
                messages.success(request, f"Account “{label}” deleted.")
            elif action == "suspend":
                account.is_active = False
                account.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"Account “{account.name}” suspended.")
            else:
                account.is_active = True
                account.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"Account “{account.name}” unsuspended.")
            return redirect("billing:school_accounts")

        else:
            form_data, error = parse_account_form()
            if error:
                messages.error(request, error)
                open_account_modal = True
                editing_account_id = int(account_id) if str(account_id or "").isdigit() else None
            elif action == "edit":
                account = get_object_or_404(SchoolAccount, pk=account_id)
                apply_school_account_form(account, form_data)
                account.save()
                messages.success(request, f"Account “{account.name}” updated.")
                return redirect("billing:school_accounts")
            else:
                SchoolAccount.objects.create(
                    category=form_data["category"],
                    custom_category=form_data["custom_category"],
                    name=form_data["name"],
                    description=form_data["description"],
                    payment_modes=form_data["payment_modes"],
                    academic_level_ids=form_data["academic_levels"],
                    vote_fund_allocation=form_data["vote_fund_allocation"],
                    created_by=request.user,
                )
                messages.success(request, f"Account “{form_data['name']}” registered.")
                return redirect("billing:school_accounts")

    balance_zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))
    accounts_qs = (
        SchoolAccount.objects.annotate(
            balance=Coalesce(
                Sum(
                    "top_ups__amount",
                    filter=Q(top_ups__status=AccountTopUp.Status.APPROVED),
                ),
                balance_zero,
            ),
            pending_topups=Count(
                "top_ups",
                filter=Q(top_ups__status=AccountTopUp.Status.PENDING),
            ),
        )
        .order_by("-is_active", "category", "name")
    )

    registered_accounts = []
    total_balance = Decimal("0.00")
    for account in accounts_qs:
        linked = [
            level_map[level_id]
            for level_id in (account.academic_level_ids or [])
            if level_id in level_map
        ]
        balance = account.balance or Decimal("0.00")
        total_balance += balance
        registered_accounts.append(
            {
                "account": account,
                "levels": linked,
                "balance": balance,
                "pending_topups": account.pending_topups or 0,
            }
        )

    active_count = SchoolAccount.objects.filter(is_active=True).count()
    active_accounts = SchoolAccount.objects.filter(is_active=True).order_by("category", "name")
    pending_topup_count = AccountTopUp.objects.filter(
        status=AccountTopUp.Status.PENDING
    ).count()
    recent_topups = (
        AccountTopUp.objects.select_related("account")
        .all()[:20]
    )
    topup_methods = [
        choice
        for choice in AccountTopUp.Method.choices
        if mpesa_enabled or choice[0] != AccountTopUp.Method.STK_PUSH
    ]

    return render(
        request,
        "billing/school_accounts.html",
        {
            "registered_accounts": registered_accounts,
            "active_account_count": active_count,
            "active_accounts": active_accounts,
            "total_balance": total_balance,
            "pending_topup_count": pending_topup_count,
            "account_categories": SchoolAccount.Category.choices,
            "payment_modes": SchoolAccount.PaymentMode.choices,
            "vote_fund_allocations": SchoolAccount.VoteFundAllocation.choices,
            "academic_levels": academic_levels,
            "open_account_modal": open_account_modal,
            "open_topup_modal": open_topup_modal,
            "editing_account_id": editing_account_id,
            "form_data": form_data,
            "topup_form": topup_form,
            "topup_methods": topup_methods,
            "mpesa_enabled": mpesa_enabled,
            "recent_topups": recent_topups,
        },
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def school_account_detail(request, account_id):
    account = get_object_or_404(SchoolAccount, pk=account_id)
    academic_levels = list(AcademicLevel.objects.all().order_by("order", "name"))
    level_map = {level.id: level for level in academic_levels}
    daraja = DarajaSettings.load()
    mpesa_enabled = bool(daraja.is_enabled)
    open_account_modal = False
    open_topup_modal = False
    open_vote_modal = False
    editing_vote_id = None
    form_data = {
        "category": account.category,
        "custom_category": account.custom_category,
        "name": account.name,
        "description": account.description,
        "payment_modes": list(account.payment_modes or []),
        "academic_levels": list(account.academic_level_ids or []),
        "vote_fund_allocation": account.vote_fund_allocation
        or SchoolAccount.VoteFundAllocation.PRIORITY_ORDER,
    }
    topup_form = {
        "account_id": str(account.id),
        "amount": "",
        "method": "",
        "description": "",
        "reference_number": "",
    }
    vote_form = {
        "name": "",
        "code": "",
        "description": "",
        "allocation_mode": AccountVote.AllocationMode.PERCENTAGE,
        "amount": "",
        "percentage": "",
        "allocation_order": "",
    }

    def current_account_balance():
        agg = AccountTopUp.objects.filter(
            account=account,
            status=AccountTopUp.Status.APPROVED,
        ).aggregate(total=Sum("amount"))
        return agg["total"] or Decimal("0.00")

    def current_votes_allocated():
        agg = AccountVote.objects.filter(
            account=account,
            status=AccountVote.Status.APPROVED,
        ).aggregate(total=Sum("amount"))
        return agg["total"] or Decimal("0.00")

    def remaining_vote_percentage(exclude_vote=None):
        """Share of 100% not yet claimed by approved votes (editable default)."""
        bal = current_account_balance()
        used = Decimal("0.00")
        qs = AccountVote.objects.filter(
            account=account,
            status=AccountVote.Status.APPROVED,
        )
        if exclude_vote is not None:
            qs = qs.exclude(pk=exclude_vote.pk)
        for vote in qs:
            if (
                vote.allocation_mode == AccountVote.AllocationMode.PERCENTAGE
                and vote.percentage is not None
            ):
                used += vote.percentage
            elif bal > 0 and vote.amount:
                used += (vote.amount * Decimal("100") / bal).quantize(Decimal("1"))
        remaining = (Decimal("100") - used).quantize(Decimal("1"))
        return remaining if remaining > 0 else Decimal("0")

    def next_allocation_order():
        from django.db.models import Max

        current = (
            AccountVote.objects.filter(account=account).aggregate(m=Max("allocation_order"))["m"]
            or 0
        )
        return current + 1

    if not vote_form["allocation_order"]:
        vote_form["allocation_order"] = str(next_allocation_order())
    next_vote_order = next_allocation_order()

    def parse_account_form():
        return parse_school_account_form(request, level_map)

    def parse_topup_form():
        amount_raw = (request.POST.get("amount") or "").strip()
        method = upper_input(request.POST.get("method"))
        description = (request.POST.get("description") or "").strip()
        reference_number = upper_input(request.POST.get("reference_number"))
        data = {
            "account_id": str(account.id),
            "amount": amount_raw,
            "method": method,
            "description": description,
            "reference_number": reference_number,
        }
        valid_methods = {choice for choice, _ in AccountTopUp.Method.choices}
        if not mpesa_enabled:
            valid_methods.discard(AccountTopUp.Method.STK_PUSH)

        error = None
        amount = None
        if not account.is_active:
            error = "This account is suspended and cannot be topped up."
        if error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid top-up amount greater than zero."
        if error is None and method not in valid_methods:
            error = "Select a valid top-up method."
        if error is None and not reference_number:
            error = "Reference number is required."
        if error is None and len(description) > 255:
            error = "Description must be 255 characters or fewer."
        return data, amount, error

    def parse_vote_form(editing_vote=None):
        name = upper_input(request.POST.get("vote_name"))
        code = upper_input(request.POST.get("vote_code"))
        description = (request.POST.get("vote_description") or "").strip()
        allocation_mode = upper_input(request.POST.get("allocation_mode"))
        amount_raw = (request.POST.get("vote_amount") or "").strip()
        percentage_raw = (request.POST.get("vote_percentage") or "").strip()
        order_raw = (request.POST.get("allocation_order") or "").strip()
        data = {
            "name": name,
            "code": code,
            "description": description,
            "allocation_mode": allocation_mode or AccountVote.AllocationMode.PERCENTAGE,
            "amount": amount_raw,
            "percentage": percentage_raw,
            "allocation_order": order_raw,
        }
        error = None
        amount = None
        percentage = None
        allocation_order = None
        balance = current_account_balance()
        allocated = current_votes_allocated()
        if editing_vote and editing_vote.status == AccountVote.Status.APPROVED:
            allocated -= editing_vote.amount or Decimal("0.00")
        unallocated = balance - allocated
        valid_modes = {choice for choice, _ in AccountVote.AllocationMode.choices}

        if not name:
            error = "Vote name is required."
        elif len(name) > 160:
            error = "Vote name must be 160 characters or fewer."
        elif len(code) > 40:
            error = "Vote code must be 40 characters or fewer."
        elif len(description) > 255:
            error = "Description must be 255 characters or fewer."
        elif allocation_mode not in valid_modes:
            error = "Select amount or percentage allocation."
        else:
            try:
                allocation_order = int(order_raw)
                if allocation_order < 1:
                    raise ValueError
            except Exception:
                error = "Enter a valid allocation order (1 or higher)."

        if error is None:
            order_qs = AccountVote.objects.filter(
                account=account, allocation_order=allocation_order
            )
            if editing_vote:
                order_qs = order_qs.exclude(pk=editing_vote.pk)
            if order_qs.exists():
                error = f"Allocation order {allocation_order} is already used on this account."

        if error is None and allocation_mode == AccountVote.AllocationMode.PERCENTAGE:
            try:
                percentage = Decimal(percentage_raw).quantize(Decimal("1"))
                if percentage < 1 or percentage > Decimal("100"):
                    raise ValueError
            except Exception:
                error = "Enter a whole percentage between 1 and 100."
            if error is None:
                # Percentage of total approved top-ups (0 when account not yet topped up).
                amount = (balance * percentage / Decimal("100")).quantize(Decimal("0.01"))
        elif error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid allocation amount greater than zero."

        # Only enforce remaining-funds cap when the account already has top-ups.
        # Votes can be registered first; balances update when money is topped up.
        if (
            error is None
            and balance > 0
            and amount is not None
            and amount > unallocated
        ):
            error = (
                f"Allocation KES {amount} exceeds remaining topped-up funds "
                f"KES {unallocated.quantize(Decimal('0.01'))} "
                f"(total topped up KES {balance.quantize(Decimal('0.01'))})."
            )
        return data, amount, percentage, allocation_order, error

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        vote_id = request.POST.get("vote_id")

        if action == "topup":
            topup_form, amount, error = parse_topup_form()
            if error:
                messages.error(request, error)
                open_topup_modal = True
            else:
                topup = AccountTopUp(
                    account=account,
                    amount=amount,
                    method=topup_form["method"],
                    description=topup_form["description"],
                    reference_number=topup_form["reference_number"],
                    status=AccountTopUp.Status.APPROVED,
                    created_by=request.user,
                )
                topup.save()
                if topup.method == AccountTopUp.Method.STK_PUSH:
                    messages.success(
                        request,
                        f"STK top-up recorded as approved. Reference code: {topup.reference_code}.",
                    )
                else:
                    messages.success(
                        request,
                        f"Topped up with KES {topup.amount}. Reference code: {topup.reference_code}.",
                    )
                return redirect("billing:school_account_detail", account_id=account.id)

        elif action in {"suspend_vote", "unsuspend_vote", "delete_vote"}:
            vote = get_object_or_404(AccountVote, pk=vote_id, account=account)
            if action == "delete_vote":
                label = vote.name
                vote.delete()
                messages.success(request, f"Vote “{label}” deleted.")
            elif action == "suspend_vote":
                vote.status = AccountVote.Status.SUSPENDED
                vote.save(update_fields=["status"])
                messages.success(request, f"Vote “{vote.name}” suspended.")
            else:
                # Restoring must still fit within unallocated capacity.
                allocated = current_votes_allocated()
                unallocated = current_account_balance() - allocated
                if vote.amount > unallocated:
                    messages.error(
                        request,
                        f"Cannot unsuspend “{vote.name}”: needs KES {vote.amount}, "
                        f"but only KES {unallocated.quantize(Decimal('0.01'))} remains "
                        f"from total topped-up funds.",
                    )
                else:
                    vote.status = AccountVote.Status.APPROVED
                    vote.save(update_fields=["status"])
                    messages.success(request, f"Vote “{vote.name}” unsuspended.")
            return redirect("billing:school_account_detail", account_id=account.id)

        elif action == "register_vote":
            vote_form, amount, percentage, allocation_order, error = parse_vote_form()
            if error:
                messages.error(request, error)
                open_vote_modal = True
            else:
                vote = AccountVote(
                    account=account,
                    name=vote_form["name"],
                    code=vote_form["code"],
                    description=vote_form["description"],
                    allocation_mode=vote_form["allocation_mode"],
                    percentage=percentage,
                    amount=amount,
                    allocation_order=allocation_order,
                    status=AccountVote.Status.APPROVED,
                    created_by=request.user,
                )
                vote.save()
                if vote.allocation_mode == AccountVote.AllocationMode.PERCENTAGE:
                    messages.success(
                        request,
                        f"Vote “{vote.name}” registered at order {vote.allocation_order} "
                        f"({vote.percentage}% · KES {vote.amount}). "
                        f"Reference code: {vote.reference_code}.",
                    )
                else:
                    messages.success(
                        request,
                        f"Vote “{vote.name}” registered at order {vote.allocation_order} "
                        f"for KES {vote.amount}. Reference code: {vote.reference_code}.",
                    )
                return redirect("billing:school_account_detail", account_id=account.id)

        elif action == "edit_vote":
            vote = get_object_or_404(AccountVote, pk=vote_id, account=account)
            vote_form, amount, percentage, allocation_order, error = parse_vote_form(
                editing_vote=vote
            )
            if error:
                messages.error(request, error)
                open_vote_modal = True
                editing_vote_id = vote.id
            else:
                vote.name = vote_form["name"]
                vote.code = vote_form["code"] or vote.reference_code
                vote.description = vote_form["description"]
                vote.allocation_mode = vote_form["allocation_mode"]
                vote.percentage = percentage
                vote.amount = amount
                vote.allocation_order = allocation_order
                vote.save()
                messages.success(request, f"Vote “{vote.name}” updated.")
                return redirect("billing:school_account_detail", account_id=account.id)

        elif action == "delete":
            label = account.name
            account.delete()
            messages.success(request, f"Account “{label}” deleted.")
            return redirect("billing:school_accounts")

        elif action == "suspend":
            account.is_active = False
            account.save(update_fields=["is_active", "updated_at"])
            messages.success(request, f"Account “{account.name}” suspended.")
            return redirect("billing:school_account_detail", account_id=account.id)

        elif action == "unsuspend":
            account.is_active = True
            account.save(update_fields=["is_active", "updated_at"])
            messages.success(request, f"Account “{account.name}” unsuspended.")
            return redirect("billing:school_account_detail", account_id=account.id)

        elif action == "edit":
            form_data, error = parse_account_form()
            if error:
                messages.error(request, error)
                open_account_modal = True
            else:
                apply_school_account_form(account, form_data)
                account.save()
                messages.success(request, f"Account “{account.name}” updated.")
                return redirect("billing:school_account_detail", account_id=account.id)

    balance_zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))
    account = (
        SchoolAccount.objects.filter(pk=account.id)
        .annotate(
            balance=Coalesce(
                Sum(
                    "top_ups__amount",
                    filter=Q(top_ups__status=AccountTopUp.Status.APPROVED),
                ),
                balance_zero,
            ),
            pending_topups=Count(
                "top_ups",
                filter=Q(top_ups__status=AccountTopUp.Status.PENDING),
            ),
            votes_allocated=Coalesce(
                Sum(
                    "votes__amount",
                    filter=Q(votes__status=AccountVote.Status.APPROVED),
                ),
                balance_zero,
            ),
        )
        .get()
    )
    linked_levels = [
        level_map[level_id]
        for level_id in (account.academic_level_ids or [])
        if level_id in level_map
    ]
    topups = AccountTopUp.objects.filter(account=account).order_by("-created_at")
    votes_qs = AccountVote.objects.filter(account=account).order_by(
        "allocation_order", "created_at"
    )
    topup_methods = [
        choice
        for choice in AccountTopUp.Method.choices
        if mpesa_enabled or choice[0] != AccountTopUp.Method.STK_PUSH
    ]
    active_accounts = SchoolAccount.objects.filter(is_active=True).order_by("category", "name")
    balance = account.balance or Decimal("0.00")
    votes_allocated = account.votes_allocated or Decimal("0.00")

    vote_rows = []
    votes_allocated_now = Decimal("0.00")
    for vote in votes_qs:
        if vote.status == AccountVote.Status.APPROVED:
            if (
                vote.allocation_mode == AccountVote.AllocationMode.PERCENTAGE
                and vote.percentage is not None
            ):
                allocated = (balance * vote.percentage / Decimal("100")).quantize(
                    Decimal("0.01")
                )
            else:
                allocated = vote.amount or Decimal("0.00")
            # Funded balance is 0 until the account has approved top-ups.
            vote_balance = allocated if balance > 0 else Decimal("0.00")
            votes_allocated_now += vote_balance
        else:
            allocated = vote.amount or Decimal("0.00")
            vote_balance = Decimal("0.00")
        vote_rows.append(
            {
                "vote": vote,
                "allocated": allocated,
                "balance": vote_balance,
            }
        )

    remaining_percentage = remaining_vote_percentage(
        exclude_vote=AccountVote.objects.filter(pk=editing_vote_id).first()
        if editing_vote_id
        else None
    )
    if (
        not editing_vote_id
        and vote_form.get("allocation_mode") == AccountVote.AllocationMode.PERCENTAGE
        and not (vote_form.get("percentage") or "").strip()
    ):
        vote_form["percentage"] = f"{int(remaining_percentage)}"

    return render(
        request,
        "billing/school_account_detail.html",
        {
            "account": account,
            "balance": balance,
            "votes_allocated": votes_allocated_now,
            "unallocated": balance - votes_allocated_now,
            "pending_topups": account.pending_topups or 0,
            "linked_levels": linked_levels,
            "topups": topups,
            "votes": votes_qs,
            "vote_rows": vote_rows,
            "account_categories": SchoolAccount.Category.choices,
            "payment_modes": SchoolAccount.PaymentMode.choices,
            "vote_fund_allocations": SchoolAccount.VoteFundAllocation.choices,
            "academic_levels": academic_levels,
            "open_account_modal": open_account_modal,
            "open_topup_modal": open_topup_modal,
            "open_vote_modal": open_vote_modal,
            "editing_vote_id": editing_vote_id,
            "form_data": form_data,
            "topup_form": topup_form,
            "vote_form": vote_form,
            "next_vote_order": next_vote_order,
            "remaining_percentage": remaining_percentage,
            "topup_methods": topup_methods,
            "mpesa_enabled": mpesa_enabled,
            "active_accounts": active_accounts,
        },
    )


@portal_access_required
@require_GET
def student_ledger(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    charges = (
        FeeCharge.objects.filter(student_id=student_id)
        .select_related("category")
        .order_by("-created_at")
    )
    payments = (
        Payment.objects.filter(student_id=student_id)
        .select_related("charge")
        .order_by("-received_at")
    )
    return render(
        request,
        "billing/student_ledger.html",
        {
            "student": student,
            "charges": charges,
            "payments": payments,
            "balance": student_balance(student_id),
        },
    )


@portal_access_required
@require_GET
def student_search(request):
    q = (request.GET.get("q") or "").strip()
    qs = Student.objects.filter(is_suspended=False).select_related("parent_guardian")
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(admission_number__icontains=q)
            | Q(assessment_number__icontains=q)
        )
    students = list(qs[:30])
    for s in students:
        s.ledger_balance = student_balance(s.id)
    return render(
        request,
        "billing/student_search.html",
        {"q": q, "students": students},
    )


@portal_access_required
@require_GET
def system_settings(request):
    return render(request, "billing/system_settings.html")


@portal_access_required
@require_http_methods(["GET", "POST"])
def accounts_settings(request):
    academic_levels = list(AcademicLevel.objects.all().order_by("order", "name"))
    level_map = {level.id: level for level in academic_levels}
    open_account_modal = False
    editing_account_id = None
    form_data = {
        "category": "",
        "custom_category": "",
        "name": "",
        "description": "",
        "payment_modes": [],
        "academic_levels": [],
        "vote_fund_allocation": SchoolAccount.VoteFundAllocation.PRIORITY_ORDER,
    }

    def parse_account_form():
        return parse_school_account_form(request, level_map)

    if request.method == "POST":
        action = (request.POST.get("action") or "edit").strip()
        account_id = request.POST.get("account_id")

        if action in {"suspend", "unsuspend", "delete"}:
            account = get_object_or_404(SchoolAccount, pk=account_id)
            if action == "delete":
                label = account.name
                account.delete()
                messages.success(request, f"Account “{label}” deleted.")
            elif action == "suspend":
                account.is_active = False
                account.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"Account “{account.name}” suspended.")
            else:
                account.is_active = True
                account.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"Account “{account.name}” unsuspended.")
            return redirect("billing:accounts_settings")

        form_data, error = parse_account_form()
        if error:
            messages.error(request, error)
            open_account_modal = True
            editing_account_id = int(account_id) if str(account_id or "").isdigit() else None
        elif action == "edit":
            account = get_object_or_404(SchoolAccount, pk=account_id)
            apply_school_account_form(account, form_data)
            account.save()
            messages.success(request, f"Account “{account.name}” updated.")
            return redirect("billing:accounts_settings")
        else:
            SchoolAccount.objects.create(
                category=form_data["category"],
                custom_category=form_data["custom_category"],
                name=form_data["name"],
                description=form_data["description"],
                payment_modes=form_data["payment_modes"],
                academic_level_ids=form_data["academic_levels"],
                vote_fund_allocation=form_data["vote_fund_allocation"],
                created_by=request.user,
            )
            messages.success(request, f"Account “{form_data['name']}” registered.")
            return redirect("billing:accounts_settings")

    registered_accounts = []
    for account in SchoolAccount.objects.all().order_by("-is_active", "category", "name"):
        linked = [
            level_map[level_id]
            for level_id in (account.academic_level_ids or [])
            if level_id in level_map
        ]
        registered_accounts.append({"account": account, "levels": linked})

    active_count = SchoolAccount.objects.filter(is_active=True).count()
    total_count = SchoolAccount.objects.count()
    categories = FeeCategory.objects.order_by("name")

    return render(
        request,
        "billing/accounts_settings.html",
        {
            "registered_accounts": registered_accounts,
            "school_account_count": total_count,
            "active_account_count": active_count,
            "suspended_account_count": total_count - active_count,
            "categories": categories,
            "active_categories": categories.filter(is_active=True).count(),
            "account_categories": SchoolAccount.Category.choices,
            "payment_modes": SchoolAccount.PaymentMode.choices,
            "vote_fund_allocations": SchoolAccount.VoteFundAllocation.choices,
            "academic_levels": academic_levels,
            "open_account_modal": open_account_modal,
            "editing_account_id": editing_account_id,
            "form_data": form_data,
        },
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def account_votes_settings(request, account_id):
    account = get_object_or_404(SchoolAccount, pk=account_id)
    open_vote_modal = False
    editing_vote_id = None
    vote_form = {
        "name": "",
        "code": "",
        "description": "",
        "allocation_mode": AccountVote.AllocationMode.AMOUNT,
        "amount": "",
        "percentage": "",
        "allocation_order": "",
    }

    def current_account_balance():
        agg = AccountTopUp.objects.filter(
            account=account,
            status=AccountTopUp.Status.APPROVED,
        ).aggregate(total=Sum("amount"))
        return agg["total"] or Decimal("0.00")

    def current_votes_allocated():
        agg = AccountVote.objects.filter(
            account=account,
            status=AccountVote.Status.APPROVED,
        ).aggregate(total=Sum("amount"))
        return agg["total"] or Decimal("0.00")

    def next_allocation_order():
        from django.db.models import Max

        current = (
            AccountVote.objects.filter(account=account).aggregate(m=Max("allocation_order"))["m"]
            or 0
        )
        return current + 1

    if not vote_form["allocation_order"]:
        vote_form["allocation_order"] = str(next_allocation_order())
    next_vote_order = next_allocation_order()

    def parse_vote_form(editing_vote=None):
        name = upper_input(request.POST.get("vote_name"))
        code = upper_input(request.POST.get("vote_code"))
        description = (request.POST.get("vote_description") or "").strip()
        allocation_mode = upper_input(request.POST.get("allocation_mode"))
        amount_raw = (request.POST.get("vote_amount") or "").strip()
        percentage_raw = (request.POST.get("vote_percentage") or "").strip()
        order_raw = (request.POST.get("allocation_order") or "").strip()
        data = {
            "name": name,
            "code": code,
            "description": description,
            "allocation_mode": allocation_mode or AccountVote.AllocationMode.PERCENTAGE,
            "amount": amount_raw,
            "percentage": percentage_raw,
            "allocation_order": order_raw,
        }
        error = None
        amount = None
        percentage = None
        allocation_order = None
        balance = current_account_balance()
        allocated = current_votes_allocated()
        if editing_vote and editing_vote.status == AccountVote.Status.APPROVED:
            allocated -= editing_vote.amount or Decimal("0.00")
        unallocated = balance - allocated
        valid_modes = {choice for choice, _ in AccountVote.AllocationMode.choices}

        if not name:
            error = "Vote name is required."
        elif len(name) > 160:
            error = "Vote name must be 160 characters or fewer."
        elif len(code) > 40:
            error = "Vote code must be 40 characters or fewer."
        elif len(description) > 255:
            error = "Description must be 255 characters or fewer."
        elif allocation_mode not in valid_modes:
            error = "Select amount or percentage allocation."
        else:
            try:
                allocation_order = int(order_raw)
                if allocation_order < 1:
                    raise ValueError
            except Exception:
                error = "Enter a valid allocation order (1 or higher)."

        if error is None:
            order_qs = AccountVote.objects.filter(
                account=account, allocation_order=allocation_order
            )
            if editing_vote:
                order_qs = order_qs.exclude(pk=editing_vote.pk)
            if order_qs.exists():
                error = f"Allocation order {allocation_order} is already used on this account."

        if error is None and allocation_mode == AccountVote.AllocationMode.PERCENTAGE:
            try:
                percentage = Decimal(percentage_raw).quantize(Decimal("1"))
                if percentage < 1 or percentage > Decimal("100"):
                    raise ValueError
            except Exception:
                error = "Enter a whole percentage between 1 and 100."
            if error is None:
                amount = (balance * percentage / Decimal("100")).quantize(Decimal("0.01"))
        elif error is None:
            try:
                amount = Decimal(amount_raw)
                if amount <= 0:
                    raise ValueError
            except Exception:
                error = "Enter a valid allocation amount greater than zero."

        if (
            error is None
            and balance > 0
            and amount is not None
            and amount > unallocated
        ):
            error = (
                f"Allocation KES {amount} exceeds remaining topped-up funds "
                f"KES {unallocated.quantize(Decimal('0.01'))} "
                f"(total topped up KES {balance.quantize(Decimal('0.01'))})."
            )
        return data, amount, percentage, allocation_order, error

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        vote_id = request.POST.get("vote_id")

        if action in {"suspend_vote", "unsuspend_vote", "delete_vote"}:
            vote = get_object_or_404(AccountVote, pk=vote_id, account=account)
            if action == "delete_vote":
                label = vote.name
                vote.delete()
                messages.success(request, f"Vote “{label}” deleted.")
            elif action == "suspend_vote":
                vote.status = AccountVote.Status.SUSPENDED
                vote.save(update_fields=["status"])
                messages.success(request, f"Vote “{vote.name}” suspended.")
            else:
                allocated = current_votes_allocated()
                unallocated = current_account_balance() - allocated
                if vote.amount > unallocated:
                    messages.error(
                        request,
                        f"Cannot unsuspend “{vote.name}”: needs KES {vote.amount}, "
                        f"but only KES {unallocated.quantize(Decimal('0.01'))} remains "
                        f"from total topped-up funds.",
                    )
                else:
                    vote.status = AccountVote.Status.APPROVED
                    vote.save(update_fields=["status"])
                    messages.success(request, f"Vote “{vote.name}” unsuspended.")
            return redirect("billing:account_votes_settings", account_id=account.id)

        elif action == "register_vote":
            vote_form, amount, percentage, allocation_order, error = parse_vote_form()
            if error:
                messages.error(request, error)
                open_vote_modal = True
            else:
                vote = AccountVote(
                    account=account,
                    name=vote_form["name"],
                    code=vote_form["code"],
                    description=vote_form["description"],
                    allocation_mode=vote_form["allocation_mode"],
                    percentage=percentage,
                    amount=amount,
                    allocation_order=allocation_order,
                    status=AccountVote.Status.APPROVED,
                    created_by=request.user,
                )
                vote.save()
                messages.success(
                    request,
                    f"Vote “{vote.name}” registered at order {vote.allocation_order}.",
                )
                return redirect("billing:account_votes_settings", account_id=account.id)

        elif action == "edit_vote":
            vote = get_object_or_404(AccountVote, pk=vote_id, account=account)
            vote_form, amount, percentage, allocation_order, error = parse_vote_form(
                editing_vote=vote
            )
            if error:
                messages.error(request, error)
                open_vote_modal = True
                editing_vote_id = vote.id
            else:
                vote.name = vote_form["name"]
                vote.code = vote_form["code"] or vote.reference_code
                vote.description = vote_form["description"]
                vote.allocation_mode = vote_form["allocation_mode"]
                vote.percentage = percentage
                vote.amount = amount
                vote.allocation_order = allocation_order
                vote.save()
                messages.success(request, f"Vote “{vote.name}” updated.")
                return redirect("billing:account_votes_settings", account_id=account.id)

    balance = current_account_balance()
    votes_allocated = current_votes_allocated()
    votes = AccountVote.objects.filter(account=account).order_by(
        "allocation_order", "created_at"
    )

    return render(
        request,
        "billing/account_votes_settings.html",
        {
            "account": account,
            "votes": votes,
            "balance": balance,
            "votes_allocated": votes_allocated,
            "unallocated": balance - votes_allocated,
            "vote_form": vote_form,
            "open_vote_modal": open_vote_modal,
            "editing_vote_id": editing_vote_id,
            "next_vote_order": next_vote_order,
            "approved_vote_count": votes.filter(status=AccountVote.Status.APPROVED).count(),
            "suspended_vote_count": votes.filter(status=AccountVote.Status.SUSPENDED).count(),
        },
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def financial_year_settings(request):
    year_id = request.GET.get("year") or request.POST.get("year_id")
    selected_year = None
    if str(year_id or "").isdigit():
        selected_year = FinancialYear.objects.filter(pk=int(year_id)).first()
    if selected_year is None:
        selected_year = FinancialYear.current() or FinancialYear.objects.first()

    year_form = FinancialYearForm()
    editing_year = False

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "save_year":
            instance = None
            edit_id = request.POST.get("edit_year_id")
            if str(edit_id or "").isdigit():
                instance = get_object_or_404(FinancialYear, pk=int(edit_id))
                editing_year = True
            year_form = FinancialYearForm(request.POST, instance=instance)
            if year_form.is_valid():
                year = year_form.save(commit=False)
                if instance is None:
                    year.created_by = request.user
                    # First year becomes current by default.
                    if not FinancialYear.objects.exists():
                        year.is_current = True
                year.save()
                ensure_default_terms(year)
                messages.success(
                    request,
                    f"Financial year “{year.display_name}” saved.",
                )
                return redirect(
                    f"{reverse('billing:financial_year_settings')}?year={year.id}"
                )
            selected_year = instance or selected_year

        elif action == "edit_year" and selected_year:
            editing_year = True
            year_form = FinancialYearForm(instance=selected_year)

        elif action == "delete_year" and selected_year:
            label = selected_year.display_name
            selected_year.delete()
            messages.success(request, f"Financial year “{label}” deleted.")
            return redirect("billing:financial_year_settings")

        elif action == "set_current_term" and selected_year:
            if not selected_year.is_current:
                messages.error(
                    request,
                    "Current term can only be set on the current financial year.",
                )
                return redirect(
                    f"{reverse('billing:financial_year_settings')}?year={selected_year.id}"
                )
            term_id = request.POST.get("term_id")
            term = get_object_or_404(
                FinancialTerm, pk=term_id, financial_year=selected_year
            )
            term.is_current = True
            term.save()
            messages.success(
                request,
                f"Current term set to “{term.name}” for {selected_year.display_name}.",
            )
            return redirect(
                f"{reverse('billing:financial_year_settings')}?year={selected_year.id}"
            )

        elif action == "use_suggested_term" and selected_year:
            if not selected_year.is_current:
                messages.error(
                    request,
                    "Current term can only be set on the current financial year.",
                )
                return redirect(
                    f"{reverse('billing:financial_year_settings')}?year={selected_year.id}"
                )
            suggested = apply_default_current_term(selected_year, force=True)
            if suggested:
                messages.success(
                    request,
                    f"Current term set to “{suggested.name}” (matches today’s date).",
                )
            else:
                messages.error(
                    request,
                    "No term date range includes today. Adjust term dates or select a term manually.",
                )
            return redirect(
                f"{reverse('billing:financial_year_settings')}?year={selected_year.id}"
            )

        elif action == "delete_term" and selected_year:
            term = get_object_or_404(
                FinancialTerm,
                pk=request.POST.get("term_id"),
                financial_year=selected_year,
            )
            label = term.name
            was_current = term.is_current
            term.delete()
            if was_current and selected_year.is_current:
                apply_default_current_term(selected_year, force=True)
            messages.success(request, f"Term “{label}” deleted.")
            return redirect(
                f"{reverse('billing:financial_year_settings')}?year={selected_year.id}"
            )

    if selected_year and not editing_year and not year_form.is_bound:
        year_form = FinancialYearForm()

    # Current term exists only on the current financial year.
    # If unset, default to the term whose dates include today.
    current_year = FinancialYear.current()
    FinancialTerm.objects.filter(is_current=True).exclude(
        financial_year__is_current=True
    ).update(is_current=False)
    if current_year and not current_year.terms.filter(is_current=True).exists():
        apply_default_current_term(current_year, force=True)

    years = FinancialYear.objects.prefetch_related("terms").all()
    if selected_year:
        selected_year.refresh_from_db()
    terms = list(selected_year.terms.all()) if selected_year else []
    suggested_term = (
        suggest_current_term(selected_year)
        if selected_year and selected_year.is_current
        else None
    )
    system_current_term = FinancialTerm.current()
    current_term = system_current_term if selected_year and selected_year.is_current else None

    return render(
        request,
        "billing/financial_year_settings.html",
        {
            "years": years,
            "selected_year": selected_year,
            "year_form": year_form,
            "terms": terms,
            "editing_year": editing_year,
            "suggested_term": suggested_term,
            "current_term": current_term,
            "system_current_term": system_current_term,
            "system_current_year": current_year,
        },
    )


@portal_access_required
@require_http_methods(["GET", "POST"])
def payments_settings(request):
    settings_obj = DarajaSettings.load()
    if request.method == "POST":
        form = DarajaSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            daraja = form.save(commit=False)
            daraja.updated_by = request.user
            daraja.save()
            messages.success(request, "Daraja API settings saved.")
            return redirect("billing:payments_settings")
    else:
        form = DarajaSettingsForm(instance=settings_obj)

    callback_path = reverse("billing:mpesa_callback")
    recent_callbacks = MpesaCallbackLog.objects.all()[:12]
    stk_missing = settings_obj.stk_missing_fields()

    return render(
        request,
        "billing/payments_settings.html",
        {
            "form": form,
            "daraja": settings_obj,
            "sandbox_secret_saved": bool(settings_obj.sandbox_consumer_secret),
            "sandbox_passkey_saved": bool(settings_obj.sandbox_passkey),
            "production_secret_saved": bool(settings_obj.production_consumer_secret),
            "production_passkey_saved": bool(settings_obj.production_passkey),
            "callback_path": callback_path,
            "callback_example": f"https://YOUR-NGROK-HOST{callback_path}",
            "recent_callbacks": recent_callbacks,
            "stk_ready": settings_obj.stk_ready(),
            "stk_missing_fields": stk_missing,
        },
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def mpesa_callback(request):
    """
    Public Daraja STK Push CallBackURL.

    Local testing: tunnel localhost (ngrok / cloudflared) to this path, e.g.
    https://abc123.ngrok-free.app/accounts-dashboard/mpesa/callback/
    """
    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "service": "accounts-mpesa-callback",
                "hint": "POST Daraja STK Push callbacks to this URL.",
            }
        )

    try:
        payload = decode_callback_body(request.body)
    except Exception:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid JSON"}, status=400)

    try:
        log = process_stk_callback(payload)
    except Exception:
        # Always acknowledge Safaricom so they do not retry endlessly during outages.
        return JsonResponse(
            {"ResultCode": 0, "ResultDesc": "Accepted with processing error"},
            status=200,
        )

    return JsonResponse(
        {
            "ResultCode": 0,
            "ResultDesc": "Accepted",
            "CheckoutRequestID": log.checkout_request_id,
            "Status": log.status,
        }
    )
