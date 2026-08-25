"""Safaricom Daraja (M-Pesa) helpers — STK Push, query, and callback handling."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    DarajaSettings,
    MpesaCallbackLog,
    Payment,
    StkPushRequest,
    allocate_payment_to_charges,
)

logger = logging.getLogger(__name__)

# Daraja STK Push result codes (see Safaricom docs).
STK_PENDING_CODES = {None, 4999, 1037}
STK_CANCEL_CODES = {1032, 1031}
STK_WRONG_PIN_CODES = {2001, 17}

# In-process OAuth token cache (Daraja tokens last ~1 hour).
_TOKEN_CACHE: dict = {"key": "", "token": "", "expires_at": None}
# Minimum gap between Daraja STK query calls for the same checkout id.
_QUERY_THROTTLE_SECONDS = 2.5
_QUERY_LAST_AT: dict[str, float] = {}
_QUERY_THROTTLE_MAX_KEYS = 500


def _cache_token_key(settings_obj: DarajaSettings) -> str:
    cfg = settings_obj.active_config()
    return (
        f"{settings_obj.active_environment}:"
        f"{(cfg.get('consumer_key') or '')[:24]}"
    )


def clear_mpesa_runtime_caches() -> None:
    """Test helper — reset OAuth and query throttle caches."""
    _TOKEN_CACHE["key"] = ""
    _TOKEN_CACHE["token"] = ""
    _TOKEN_CACHE["expires_at"] = None
    _QUERY_LAST_AT.clear()


def _should_throttle_query(checkout_request_id: str) -> bool:
    checkout = (checkout_request_id or "").strip()
    if not checkout:
        return False
    last = _QUERY_LAST_AT.get(checkout)
    if last is None:
        return False
    return (timezone.now().timestamp() - last) < _QUERY_THROTTLE_SECONDS


def _mark_query_ran(checkout_request_id: str) -> None:
    checkout = (checkout_request_id or "").strip()
    if not checkout:
        return
    if len(_QUERY_LAST_AT) >= _QUERY_THROTTLE_MAX_KEYS:
        # Drop oldest half to keep memory bounded in long-running workers.
        for key in list(_QUERY_LAST_AT.keys())[: _QUERY_THROTTLE_MAX_KEYS // 2]:
            _QUERY_LAST_AT.pop(key, None)
    _QUERY_LAST_AT[checkout] = timezone.now().timestamp()


def _normalize_result_code(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _stk_is_cancelled(result_code: int | None) -> bool:
    return result_code in STK_CANCEL_CODES


def _stk_is_pending(result_code: int | None) -> bool:
    return result_code in STK_PENDING_CODES


def _stk_is_failure(result_code: int | None) -> bool:
    if result_code is None:
        return False
    if result_code == 0:
        return False
    return not _stk_is_pending(result_code)


def _humanize_stk_failure(result_code: int | None, result_desc: str = "") -> str:
    desc = (result_desc or "").strip()
    if result_code in {1032}:
        return desc or "Payment cancelled on the phone."
    if result_code in {1031}:
        return desc or "STK prompt timed out or was cancelled."
    if result_code in STK_WRONG_PIN_CODES:
        return desc or "Wrong M-Pesa PIN entered. Please try again."
    if result_code == 1:
        return desc or "Insufficient M-Pesa balance."
    if result_code == 1001:
        return desc or "Could not reach the phone. Try again."
    if result_code == 1037:
        return desc or "Waiting for customer to enter M-Pesa PIN."
    if desc:
        return desc
    if result_code is not None:
        return f"Payment failed (M-Pesa code {result_code})."
    return "Payment was not completed."


def _find_failed_callback(stk_request: StkPushRequest):
    qs = MpesaCallbackLog.objects.filter(status=MpesaCallbackLog.Status.FAILED)
    if stk_request.checkout_request_id:
        found = qs.filter(checkout_request_id=stk_request.checkout_request_id).order_by(
            "-created_at"
        ).first()
        if found:
            return found
    if stk_request.merchant_request_id:
        return qs.filter(merchant_request_id=stk_request.merchant_request_id).order_by(
            "-created_at"
        ).first()
    return None


def _find_success_callback(stk_request: StkPushRequest, *, require_receipt: bool = False):
    qs = MpesaCallbackLog.objects.filter(status=MpesaCallbackLog.Status.SUCCESS)
    if require_receipt:
        qs = qs.filter(mpesa_receipt__gt="")
    if stk_request.checkout_request_id:
        found = qs.filter(checkout_request_id=stk_request.checkout_request_id).order_by(
            "-created_at"
        ).first()
        if found:
            return found
    if stk_request.merchant_request_id:
        return qs.filter(merchant_request_id=stk_request.merchant_request_id).order_by(
            "-created_at"
        ).first()
    return None


class MpesaApiError(Exception):
    """Raised when a Daraja API call fails or credentials are incomplete."""

    def __init__(self, message: str, *, payload=None, status_code: int | None = None):
        super().__init__(message)
        self.payload = payload or {}
        self.status_code = status_code


def _items_to_map(items) -> dict:
    mapping = {}
    if not isinstance(items, list):
        return mapping
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name")
        if name:
            mapping[name] = item.get("Value")
    return mapping


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _http_json(method: str, url: str, *, headers=None, data=None, auth=None, timeout=30):
    try:
        import requests
    except ImportError as exc:
        raise MpesaApiError(
            "The requests package is required for M-Pesa STK Push. "
            "Install it with: pip install requests"
        ) from exc

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=data,
            auth=auth,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MpesaApiError(f"Could not reach Safaricom Daraja: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": (response.text or "")[:500]}

    if response.status_code >= 400:
        message = (
            payload.get("errorMessage")
            or payload.get("error_description")
            or payload.get("ResponseDescription")
            or payload.get("ResultDesc")
            or payload.get("CustomerMessage")
            or ""
        )
        if not message:
            raw = (payload.get("raw") or response.text or "").strip()
            message = (
                f"Daraja HTTP {response.status_code}"
                + (f": {raw[:180]}" if raw else " (no error details from Safaricom)")
            )
        else:
            message = f"{message} (HTTP {response.status_code})"
        logger.warning(
            "Daraja %s %s failed status=%s body=%s",
            method,
            url,
            response.status_code,
            payload,
        )
        raise MpesaApiError(
            str(message),
            payload=payload,
            status_code=response.status_code,
        )

    return payload


def get_access_token(settings_obj: DarajaSettings | None = None) -> str:
    settings_obj = settings_obj or DarajaSettings.load()
    if not settings_obj.is_enabled:
        raise MpesaApiError("M-Pesa is disabled in System settings.")
    cfg = settings_obj.active_config()
    if not cfg["consumer_key"] or not cfg["consumer_secret"]:
        raise MpesaApiError("Daraja consumer key/secret are not configured.")

    cache_key = _cache_token_key(settings_obj)
    now = timezone.now()
    if (
        _TOKEN_CACHE.get("key") == cache_key
        and _TOKEN_CACHE.get("token")
        and _TOKEN_CACHE.get("expires_at")
        and _TOKEN_CACHE["expires_at"] > now
    ):
        return _TOKEN_CACHE["token"]

    url = f"{settings_obj.api_base_url}/oauth/v1/generate?grant_type=client_credentials"
    payload = _http_json(
        "GET",
        url,
        auth=(cfg["consumer_key"], cfg["consumer_secret"]),
    )
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise MpesaApiError("Daraja did not return an access token.", payload=payload)

    expires_in = 3300
    try:
        expires_in = max(60, int(payload.get("expires_in") or 3599) - 300)
    except (TypeError, ValueError):
        expires_in = 3300
    _TOKEN_CACHE["key"] = cache_key
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + timedelta(seconds=expires_in)
    return token


def _stk_password(shortcode: str, passkey: str, timestamp: str) -> str:
    raw = f"{shortcode}{passkey}{timestamp}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def initiate_stk_push(
    *,
    phone_number: str,
    amount: Decimal,
    account_reference: str,
    transaction_desc: str,
    settings_obj: DarajaSettings | None = None,
) -> dict:
    """
    Send a Lipa Na M-Pesa Online STK Push prompt.
    Returns merchant/checkout request ids from Daraja.
    """
    settings_obj = settings_obj or DarajaSettings.load()
    if not settings_obj.stk_ready():
        missing = ", ".join(settings_obj.stk_missing_fields())
        raise MpesaApiError(
            "M-Pesa STK is not fully configured. Missing: "
            f"{missing}. Open System settings → Payments and save all fields."
        )

    cfg = settings_obj.active_config()
    phone = "".join(ch for ch in (phone_number or "") if ch.isdigit())
    if len(phone) < 12:
        raise MpesaApiError("Enter a valid M-Pesa phone number (2547…).")

    amount_int = int(Decimal(amount).quantize(Decimal("1"), rounding=ROUND_DOWN))
    if amount_int < 1:
        raise MpesaApiError("STK Push amount must be at least KES 1.")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    token = get_access_token(settings_obj)
    payload = {
        "BusinessShortCode": cfg["shortcode"],
        "Password": _stk_password(cfg["shortcode"], cfg["passkey"], timestamp),
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount_int,
        "PartyA": phone,
        "PartyB": cfg["shortcode"],
        "PhoneNumber": phone,
        "CallBackURL": cfg["callback_url"],
        "AccountReference": (account_reference or "FEES")[:12],
        "TransactionDesc": "".join(
            ch for ch in (transaction_desc or "SchoolFees") if ch.isalnum()
        )[:13]
        or "SchoolFees",
    }
    url = f"{settings_obj.api_base_url}/mpesa/stkpush/v1/processrequest"
    response = _http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=payload,
    )

    response_code = str(response.get("ResponseCode", ""))
    if response_code not in {"0", "00"}:
        raise MpesaApiError(
            response.get("ResponseDescription")
            or response.get("CustomerMessage")
            or "STK Push was rejected by Daraja.",
            payload=response,
        )

    return {
        "merchant_request_id": str(response.get("MerchantRequestID") or ""),
        "checkout_request_id": str(response.get("CheckoutRequestID") or ""),
        "customer_message": str(response.get("CustomerMessage") or ""),
        "response_description": str(response.get("ResponseDescription") or ""),
        "raw": response,
    }


def query_stk_push(
    checkout_request_id: str,
    settings_obj: DarajaSettings | None = None,
) -> dict:
    """Query Lipa Na M-Pesa STK Push status for a CheckoutRequestID."""
    settings_obj = settings_obj or DarajaSettings.load()
    if not settings_obj.stk_ready():
        raise MpesaApiError("M-Pesa STK is not fully configured.")

    cfg = settings_obj.active_config()
    checkout_request_id = (checkout_request_id or "").strip()
    if not checkout_request_id:
        raise MpesaApiError("Missing CheckoutRequestID.")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    token = get_access_token(settings_obj)
    payload = {
        "BusinessShortCode": cfg["shortcode"],
        "Password": _stk_password(cfg["shortcode"], cfg["passkey"], timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    url = f"{settings_obj.api_base_url}/mpesa/stkpushquery/v1/query"
    try:
        response = _http_json(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=payload,
        )
    except MpesaApiError as exc:
        payload = exc.payload or {}
        result_code = _normalize_result_code(
            payload.get("ResultCode") if payload.get("ResultCode") is not None else payload.get("resultCode")
        )
        message = str(
            payload.get("errorMessage")
            or payload.get("ResultDesc")
            or payload.get("ResponseDescription")
            or ""
        )
        if result_code is not None and _stk_is_failure(result_code):
            return {
                "result_code": result_code,
                "result_desc": _humanize_stk_failure(result_code, message),
                "merchant_request_id": "",
                "checkout_request_id": checkout_request_id,
                "query_unavailable": False,
                "raw": payload,
            }
        if exc.status_code == 500 and "not exist" in message.lower():
            return {
                "result_code": 1032,
                "result_desc": "STK prompt expired or was cancelled on the phone.",
                "merchant_request_id": "",
                "checkout_request_id": checkout_request_id,
                "query_unavailable": False,
                "raw": payload,
            }
        # Sandbox apps often return 403/429 on STK Query even when the push succeeded.
        # Treat that as "still waiting" and rely on the callback URL instead.
        if exc.status_code in {403, 404, 429, 502, 503}:
            return {
                "result_code": None,
                "result_desc": "Waiting for customer confirmation on the phone.",
                "merchant_request_id": "",
                "checkout_request_id": checkout_request_id,
                "query_unavailable": True,
                "raw": payload,
            }
        raise

    result_code = _normalize_result_code(
        response.get("ResultCode")
        if response.get("ResultCode") is not None
        else response.get("resultCode")
    )

    result_desc = str(
        response.get("ResultDesc")
        or response.get("ResponseDescription")
        or ""
    )
    return {
        "result_code": result_code,
        "result_desc": result_desc,
        "merchant_request_id": str(response.get("MerchantRequestID") or ""),
        "checkout_request_id": str(
            response.get("CheckoutRequestID") or checkout_request_id
        ),
        "query_unavailable": False,
        "raw": response,
    }


def parse_stk_callback(payload: dict) -> dict:
    """Normalize a Daraja STK Push callback body into flat fields."""
    body = payload.get("Body") if isinstance(payload, dict) else None
    stk = body.get("stkCallback") if isinstance(body, dict) else None
    if not isinstance(stk, dict):
        return {
            "merchant_request_id": "",
            "checkout_request_id": "",
            "result_code": None,
            "result_desc": "Unrecognized callback payload",
            "amount": None,
            "mpesa_receipt": "",
            "phone_number": "",
            "transaction_date": "",
            "account_reference": "",
            "recognized": False,
        }

    meta = stk.get("CallbackMetadata") or {}
    values = _items_to_map(meta.get("Item") if isinstance(meta, dict) else None)
    return {
        "merchant_request_id": str(stk.get("MerchantRequestID") or ""),
        "checkout_request_id": str(stk.get("CheckoutRequestID") or ""),
        "result_code": _normalize_result_code(stk.get("ResultCode")),
        "result_desc": str(stk.get("ResultDesc") or "")[:255],
        "amount": _to_decimal(values.get("Amount")),
        "mpesa_receipt": str(values.get("MpesaReceiptNumber") or ""),
        "phone_number": str(values.get("PhoneNumber") or ""),
        "transaction_date": str(values.get("TransactionDate") or ""),
        "account_reference": str(
            values.get("AccountReference")
            or stk.get("AccountReference")
            or ""
        ),
        "recognized": True,
    }


def _resolve_student_id(account_reference: str) -> int | None:
    raw = (account_reference or "").strip().upper()
    if not raw:
        return None
    if raw.startswith("STU"):
        raw = raw[3:]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.isdigit() and digits:
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _find_stk_request(checkout_request_id: str = "", merchant_request_id: str = ""):
    qs = StkPushRequest.objects.all()
    if checkout_request_id:
        found = qs.filter(checkout_request_id=checkout_request_id).first()
        if found:
            return found
    if merchant_request_id:
        return qs.filter(merchant_request_id=merchant_request_id).first()
    return None


@transaction.atomic
def complete_stk_request(
    stk_request: StkPushRequest,
    *,
    mpesa_receipt: str = "",
    result_code: int | None = None,
    result_desc: str = "",
    phone_number: str = "",
    received_by=None,
    require_receipt: bool = True,
) -> StkPushRequest:
    """
    Mark STK request successful and allocate payment to learner charges once.
    Prefers the real M-Pesa SMS receipt (MpesaReceiptNumber) as the payment reference.
    """
    stk_request = StkPushRequest.objects.select_for_update().get(pk=stk_request.pk)
    receipt = (mpesa_receipt or stk_request.mpesa_receipt or "").strip().upper()

    # Late callback: payment already posted with checkout id — backfill real receipt.
    if stk_request.status == StkPushRequest.Status.SUCCESS and stk_request.payment_id:
        if receipt and stk_request.mpesa_receipt != receipt:
            _backfill_mpesa_receipt(stk_request, receipt)
        return stk_request

    if require_receipt and not receipt:
        stk_request.result_code = 0 if result_code is None else result_code
        stk_request.result_desc = (
            result_desc or "Payment accepted. Waiting for M-Pesa receipt…"
        )[:255]
        stk_request.save(update_fields=["result_code", "result_desc", "updated_at"])
        return stk_request

    if receipt and MpesaCallbackLog.objects.filter(
        mpesa_receipt=receipt,
        payment__isnull=False,
    ).exists():
        existing_payment = (
            MpesaCallbackLog.objects.filter(mpesa_receipt=receipt, payment__isnull=False)
            .select_related("payment")
            .first()
        )
        stk_request.status = StkPushRequest.Status.SUCCESS
        stk_request.mpesa_receipt = receipt
        stk_request.result_code = 0 if result_code is None else result_code
        stk_request.result_desc = (result_desc or "Already recorded")[:255]
        if existing_payment and existing_payment.payment_id:
            stk_request.payment = existing_payment.payment
        stk_request.save(
            update_fields=[
                "status",
                "mpesa_receipt",
                "result_code",
                "result_desc",
                "payment",
                "updated_at",
            ]
        )
        return stk_request

    reference = receipt or stk_request.checkout_request_id or f"STK-{stk_request.pk}"
    result = allocate_payment_to_charges(
        student_id=stk_request.student_id,
        amount=stk_request.amount,
        method=Payment.Method.MPESA,
        reference=reference,
        received_by=received_by or stk_request.created_by,
        notes=(
            f"STK Push · M-Pesa receipt {receipt or 'pending'} · "
            f"phone {phone_number or stk_request.phone_number} · "
            f"checkout {stk_request.checkout_request_id}"
        ),
    )
    payment = result["payments"][0] if result["payments"] else None
    stk_request.status = StkPushRequest.Status.SUCCESS
    stk_request.mpesa_receipt = receipt
    stk_request.result_code = 0 if result_code is None else result_code
    stk_request.result_desc = (
        result_desc or "The service request is processed successfully."
    )[:255]
    stk_request.payment = payment
    stk_request.notes = (
        f"Allocated KES {result['allocated']}"
        + (f"; credit KES {result['unallocated']}" if result["unallocated"] else "")
        + (f"; receipt {receipt}" if receipt else "")
    )
    stk_request.save(
        update_fields=[
            "status",
            "mpesa_receipt",
            "result_code",
            "result_desc",
            "payment",
            "notes",
            "updated_at",
        ]
    )
    return stk_request


def _backfill_mpesa_receipt(stk_request: StkPushRequest, receipt: str) -> None:
    """Replace checkout-based payment references with the real M-Pesa receipt."""
    receipt = (receipt or "").strip().upper()
    if not receipt:
        return
    checkout = (stk_request.checkout_request_id or "").strip()
    payments = Payment.objects.filter(
        student_id=stk_request.student_id,
        method=Payment.Method.MPESA,
    )
    if stk_request.payment_id:
        payments.filter(pk=stk_request.payment_id).update(reference=receipt)
    if checkout:
        payments.filter(
            Q(reference=checkout)
            | Q(reference__startswith="ws_CO_")
            | Q(notes__icontains=checkout)
        ).update(reference=receipt)
    stk_request.mpesa_receipt = receipt
    stk_request.result_desc = f"M-Pesa receipt {receipt}"[:255]
    stk_request.save(update_fields=["mpesa_receipt", "result_desc", "updated_at"])


def fail_stk_request(
    stk_request: StkPushRequest,
    *,
    result_code: int | None = None,
    result_desc: str = "",
    cancelled: bool = False,
) -> StkPushRequest:
    normalized_code = _normalize_result_code(result_code)
    human_desc = _humanize_stk_failure(normalized_code, result_desc)
    stk_request.status = (
        StkPushRequest.Status.CANCELLED
        if cancelled or _stk_is_cancelled(normalized_code)
        else StkPushRequest.Status.FAILED
    )
    stk_request.result_code = normalized_code
    stk_request.result_desc = human_desc[:255]
    stk_request.save(
        update_fields=["status", "result_code", "result_desc", "updated_at"]
    )
    return stk_request


def _apply_failed_callback(
    stk_request: StkPushRequest,
    callback: MpesaCallbackLog,
) -> StkPushRequest:
    code = _normalize_result_code(callback.result_code)
    return fail_stk_request(
        stk_request,
        result_code=code,
        result_desc=callback.result_desc,
        cancelled=_stk_is_cancelled(code),
    )


@transaction.atomic
def process_stk_callback(payload: dict) -> MpesaCallbackLog:
    """Persist callback and, on success, complete any matching STK fee payment."""
    parsed = parse_stk_callback(payload)
    result_code = parsed["result_code"]

    if not parsed["recognized"]:
        status = MpesaCallbackLog.Status.IGNORED
    elif result_code == 0:
        status = MpesaCallbackLog.Status.SUCCESS
    else:
        status = MpesaCallbackLog.Status.FAILED

    log = MpesaCallbackLog.objects.create(
        merchant_request_id=parsed["merchant_request_id"],
        checkout_request_id=parsed["checkout_request_id"],
        result_code=result_code,
        result_desc=parsed["result_desc"],
        amount=parsed["amount"],
        mpesa_receipt=parsed["mpesa_receipt"],
        phone_number=parsed["phone_number"],
        transaction_date=parsed["transaction_date"],
        account_reference=parsed["account_reference"],
        status=status,
        raw_payload=payload if isinstance(payload, dict) else {"raw": payload},
    )

    stk_request = _find_stk_request(
        checkout_request_id=parsed["checkout_request_id"],
        merchant_request_id=parsed["merchant_request_id"],
    )

    if status != MpesaCallbackLog.Status.SUCCESS:
        if stk_request and stk_request.status == StkPushRequest.Status.PENDING:
            fail_stk_request(
                stk_request,
                result_code=result_code,
                result_desc=parsed["result_desc"],
                cancelled=_stk_is_cancelled(result_code),
            )
        return log

    if parsed["mpesa_receipt"] and MpesaCallbackLog.objects.filter(
        mpesa_receipt=parsed["mpesa_receipt"],
        payment__isnull=False,
    ).exclude(pk=log.pk).exists():
        log.notes = "Duplicate receipt — payment already recorded."
        log.save(update_fields=["notes"])
        if stk_request:
            complete_stk_request(
                stk_request,
                mpesa_receipt=parsed["mpesa_receipt"],
                result_code=0,
                result_desc=parsed["result_desc"],
                phone_number=parsed["phone_number"],
            )
            if stk_request.payment_id:
                log.payment_id = stk_request.payment_id
                log.save(update_fields=["payment"])
        return log

    if stk_request:
        stk_request = complete_stk_request(
            stk_request,
            mpesa_receipt=parsed["mpesa_receipt"],
            result_code=0,
            result_desc=parsed["result_desc"],
            phone_number=parsed["phone_number"],
        )
        log.payment = stk_request.payment
        log.notes = stk_request.notes or "Completed via tracked STK request."
        log.save(update_fields=["payment", "notes"])
        return log

    student_id = _resolve_student_id(parsed["account_reference"])
    amount = parsed["amount"]
    if not student_id or amount is None or amount <= 0:
        log.notes = (
            "Callback saved, but no Payment created "
            "(need matching STK request or AccountReference student id and Amount)."
        )
        log.save(update_fields=["notes"])
        return log

    result = allocate_payment_to_charges(
        student_id=student_id,
        amount=amount,
        method=Payment.Method.MPESA,
        reference=parsed["mpesa_receipt"] or parsed["checkout_request_id"],
        notes=(
            f"Daraja STK callback · phone {parsed['phone_number']} · "
            f"checkout {parsed['checkout_request_id']}"
        ),
    )
    payment = result["payments"][0] if result["payments"] else None
    log.payment = payment
    log.notes = (
        f"Payment(s) recorded ({result['payment_count']}); "
        f"allocated KES {result['allocated']}."
    )
    log.save(update_fields=["payment", "notes"])
    logger.info(
        "M-Pesa payment created from callback receipt=%s student_id=%s amount=%s",
        parsed["mpesa_receipt"],
        student_id,
        amount,
    )
    return log


def refresh_stk_request_status(
    stk_request: StkPushRequest,
    *,
    received_by=None,
) -> StkPushRequest:
    """
    Refresh a pending STK request from callback logs and/or Daraja query API.
    Completes payment only after the real M-Pesa receipt code is available.
    """
    stk_request.refresh_from_db()

    # Already successful but missing receipt — try to backfill from callback.
    if stk_request.status == StkPushRequest.Status.SUCCESS:
        if not stk_request.mpesa_receipt and stk_request.checkout_request_id:
            callback = _find_success_callback(stk_request, require_receipt=True)
            if callback:
                return complete_stk_request(
                    stk_request,
                    mpesa_receipt=callback.mpesa_receipt,
                    result_code=0,
                    result_desc=callback.result_desc,
                    phone_number=callback.phone_number,
                    received_by=received_by,
                    require_receipt=True,
                )
        return stk_request

    if stk_request.status != StkPushRequest.Status.PENDING:
        return stk_request

    failed_callback = _find_failed_callback(stk_request)
    if failed_callback:
        return _apply_failed_callback(stk_request, failed_callback)

    callback = None
    if stk_request.checkout_request_id:
        callback = (
            MpesaCallbackLog.objects.filter(
                checkout_request_id=stk_request.checkout_request_id
            )
            .order_by("-created_at")
            .first()
        )
    if callback is None and stk_request.merchant_request_id:
        callback = (
            MpesaCallbackLog.objects.filter(
                merchant_request_id=stk_request.merchant_request_id
            )
            .order_by("-created_at")
            .first()
        )

    if callback and callback.status == MpesaCallbackLog.Status.SUCCESS:
        return complete_stk_request(
            stk_request,
            mpesa_receipt=callback.mpesa_receipt,
            result_code=_normalize_result_code(callback.result_code),
            result_desc=callback.result_desc or "M-Pesa receipt received.",
            phone_number=callback.phone_number,
            received_by=received_by,
            require_receipt=True,
        )

    if callback and callback.status == MpesaCallbackLog.Status.FAILED:
        return _apply_failed_callback(stk_request, callback)

    # Fallback: recent successful callback for same phone (if checkout id mismatched).
    recent_receipt_callback = (
        MpesaCallbackLog.objects.filter(
            status=MpesaCallbackLog.Status.SUCCESS,
            phone_number__endswith=(stk_request.phone_number or "")[-9:],
            mpesa_receipt__gt="",
            created_at__gte=timezone.now() - timedelta(minutes=20),
        )
        .order_by("-created_at")
        .first()
    )
    if recent_receipt_callback and not stk_request.mpesa_receipt:
        # Only claim it if no other pending/success STK already owns this receipt.
        owned = StkPushRequest.objects.filter(
            mpesa_receipt=recent_receipt_callback.mpesa_receipt
        ).exclude(pk=stk_request.pk).exists()
        if not owned and (
            not recent_receipt_callback.checkout_request_id
            or recent_receipt_callback.checkout_request_id
            == stk_request.checkout_request_id
        ):
            return complete_stk_request(
                stk_request,
                mpesa_receipt=recent_receipt_callback.mpesa_receipt,
                result_code=0,
                result_desc=recent_receipt_callback.result_desc,
                phone_number=recent_receipt_callback.phone_number,
                received_by=received_by,
                require_receipt=True,
            )

    # Payment accepted — wait for callback receipt only (skip slow Daraja query).
    if stk_request.result_code == 0 and not stk_request.mpesa_receipt:
        stk_request.result_desc = (
            "Payment accepted. Fetching M-Pesa receipt code…"
        )[:255]
        stk_request.save(update_fields=["result_desc", "updated_at"])
        return stk_request

    # Throttle query API: callbacks already checked; avoid rate-limit / latency.
    if _should_throttle_query(stk_request.checkout_request_id):
        if not stk_request.result_desc:
            stk_request.result_desc = "Waiting for customer confirmation on the phone."
            stk_request.save(update_fields=["result_desc", "updated_at"])
        return stk_request

    queried = query_stk_push(stk_request.checkout_request_id)
    _mark_query_ran(stk_request.checkout_request_id)
    result_code = _normalize_result_code(queried.get("result_code"))
    result_desc = queried.get("result_desc") or (
        "Waiting for customer confirmation on the phone."
    )

    failed_callback = _find_failed_callback(stk_request)
    if failed_callback:
        return _apply_failed_callback(stk_request, failed_callback)

    # Still processing / waiting for user (or query endpoint temporarily unavailable).
    if queried.get("query_unavailable") or _stk_is_pending(result_code):
        stk_request.result_code = result_code
        stk_request.result_desc = result_desc[:255]
        stk_request.save(update_fields=["result_code", "result_desc", "updated_at"])
        return stk_request

    # Daraja query confirmed success — wait for callback receipt before posting.
    if result_code == 0:
        callback = _find_success_callback(stk_request, require_receipt=True)
        receipt = (callback.mpesa_receipt if callback else "") or ""
        if receipt:
            return complete_stk_request(
                stk_request,
                mpesa_receipt=receipt,
                result_code=0,
                result_desc=callback.result_desc if callback else result_desc,
                phone_number=(callback.phone_number if callback else "")
                or stk_request.phone_number,
                received_by=received_by,
                require_receipt=True,
            )
        stk_request.result_code = 0
        stk_request.result_desc = (
            "Payment accepted on phone. Fetching M-Pesa receipt code…"
        )[:255]
        stk_request.notes = "query_confirmed_awaiting_receipt"
        stk_request.save(
            update_fields=["result_code", "result_desc", "notes", "updated_at"]
        )
        return stk_request

    return fail_stk_request(
        stk_request,
        result_code=result_code,
        result_desc=result_desc,
        cancelled=_stk_is_cancelled(result_code),
    )


def stk_request_payload(stk_request: StkPushRequest, *, include_finance: bool | None = None) -> dict:
    from django.db.models import DecimalField, Sum, Value
    from django.db.models.functions import Coalesce

    from .models import FeeCharge, student_balance

    failed = stk_request.status in {
        StkPushRequest.Status.FAILED,
        StkPushRequest.Status.CANCELLED,
    }
    success = (
        stk_request.status == StkPushRequest.Status.SUCCESS
        and bool(stk_request.mpesa_receipt)
    )
    # Skip ledger aggregation on pending polls — only needed after success.
    if include_finance is None:
        include_finance = success

    charged = Decimal("0.00")
    paid = Decimal("0.00")
    balance = Decimal("0.00")
    if include_finance:
        try:
            row = (
                FeeCharge.objects.filter(student_id=stk_request.student_id)
                .exclude(
                    status__in=[FeeCharge.Status.CANCELLED, FeeCharge.Status.WAIVED]
                )
                .aggregate(
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
            )
            charged = row["charged"] or Decimal("0.00")
            paid = row["paid"] or Decimal("0.00")
            balance = charged - paid
        except Exception:
            balance = student_balance(stk_request.student_id)
            charged = Decimal("0.00")
            paid = Decimal("0.00")

    return {
        "id": stk_request.id,
        "status": stk_request.status,
        "status_label": stk_request.get_status_display(),
        "amount": f"{stk_request.amount:.2f}",
        "phone_number": stk_request.phone_number,
        "merchant_request_id": stk_request.merchant_request_id,
        "checkout_request_id": stk_request.checkout_request_id,
        "mpesa_receipt": stk_request.mpesa_receipt,
        "result_code": stk_request.result_code,
        "result_desc": stk_request.result_desc,
        "failed": failed,
        "cancelled": stk_request.status == StkPushRequest.Status.CANCELLED,
        "failure_message": stk_request.result_desc if failed else "",
        "awaiting_receipt": bool(
            stk_request.status == StkPushRequest.Status.PENDING
            and stk_request.result_code == 0
            and not stk_request.mpesa_receipt
        ),
        "student_id": stk_request.student_id,
        "student_charged": f"{charged:.2f}" if include_finance else None,
        "student_paid": f"{paid:.2f}" if include_finance else None,
        "student_balance": f"{balance:.2f}" if include_finance else None,
        "payment_id": stk_request.payment_id,
        "updated_at": timezone.localtime(stk_request.updated_at).isoformat(),
    }


def decode_callback_body(body: bytes | str) -> dict:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    body = (body or "").strip()
    if not body:
        return {}
    return json.loads(body)
