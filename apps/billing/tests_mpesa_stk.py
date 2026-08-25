"""Focused tests for M-Pesa STK Push parsing, callbacks, and status refresh."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.billing import mpesa
from apps.billing.models import (
    MpesaCallbackLog,
    Payment,
    SchoolAccount,
    StkPushRequest,
)


def _success_callback_payload(
    *,
    checkout="ws_CO_TEST_OK",
    merchant="mr_TEST_OK",
    amount="100.00",
    receipt="QK123ABCDE",
    phone="254712345678",
    account_ref="STU42",
):
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": merchant,
                "CheckoutRequestID": checkout,
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": float(amount)},
                        {"Name": "MpesaReceiptNumber", "Value": receipt},
                        {"Name": "TransactionDate", "Value": "20260823101500"},
                        {"Name": "PhoneNumber", "Value": int(phone)},
                        {"Name": "AccountReference", "Value": account_ref},
                    ]
                },
            }
        }
    }


def _failed_callback_payload(
    *,
    checkout="ws_CO_TEST_FAIL",
    merchant="mr_TEST_FAIL",
    result_code=1032,
    result_desc="Request cancelled by user",
):
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": merchant,
                "CheckoutRequestID": checkout,
                "ResultCode": result_code,
                "ResultDesc": result_desc,
            }
        }
    }


class MpesaStkHelpersTests(TestCase):
    def setUp(self):
        mpesa.clear_mpesa_runtime_caches()

    def test_normalize_result_code_accepts_strings(self):
        self.assertEqual(mpesa._normalize_result_code("1032"), 1032)
        self.assertEqual(mpesa._normalize_result_code(2001), 2001)
        self.assertIsNone(mpesa._normalize_result_code(""))
        self.assertIsNone(mpesa._normalize_result_code(None))

    def test_humanize_cancel_and_wrong_pin(self):
        self.assertIn("cancelled", mpesa._humanize_stk_failure(1032, "").lower())
        self.assertIn("pin", mpesa._humanize_stk_failure(2001, "").lower())
        self.assertTrue(mpesa._stk_is_cancelled(1032))
        self.assertTrue(mpesa._stk_is_failure(2001))
        self.assertFalse(mpesa._stk_is_failure(0))
        self.assertTrue(mpesa._stk_is_pending(1037))

    def test_parse_stk_callback_success_string_code(self):
        payload = _success_callback_payload()
        payload["Body"]["stkCallback"]["ResultCode"] = "0"
        parsed = mpesa.parse_stk_callback(payload)
        self.assertTrue(parsed["recognized"])
        self.assertEqual(parsed["result_code"], 0)
        self.assertEqual(parsed["mpesa_receipt"], "QK123ABCDE")
        self.assertEqual(parsed["amount"], Decimal("100.00"))

    def test_parse_stk_callback_cancel_string_code(self):
        payload = _failed_callback_payload(result_code="1032")
        parsed = mpesa.parse_stk_callback(payload)
        self.assertEqual(parsed["result_code"], 1032)
        self.assertEqual(parsed["mpesa_receipt"], "")


class MpesaStkFlowTests(TestCase):
    def setUp(self):
        mpesa.clear_mpesa_runtime_caches()
        self.account = SchoolAccount.objects.create(
            category=SchoolAccount.Category.STUDENT_FEES,
            name="Test Fees Account",
            payment_modes=[
                SchoolAccount.PaymentMode.MPESA,
                SchoolAccount.PaymentMode.CASH,
            ],
        )

    def _pending_stk(self, **kwargs):
        defaults = {
            "student_id": 42,
            "account": self.account,
            "amount": Decimal("100.00"),
            "phone_number": "254712345678",
            "account_reference": "STU42",
            "merchant_request_id": "mr_TEST",
            "checkout_request_id": "ws_CO_TEST",
            "status": StkPushRequest.Status.PENDING,
            "result_desc": "STK prompt sent.",
        }
        defaults.update(kwargs)
        return StkPushRequest.objects.create(**defaults)

    def test_callback_cancel_ends_pending_session(self):
        stk = self._pending_stk(
            checkout_request_id="ws_CO_CANCEL",
            merchant_request_id="mr_CANCEL",
        )
        log = mpesa.process_stk_callback(
            _failed_callback_payload(
                checkout="ws_CO_CANCEL",
                merchant="mr_CANCEL",
                result_code="1032",
                result_desc="Request cancelled by user",
            )
        )
        stk.refresh_from_db()
        self.assertEqual(log.status, MpesaCallbackLog.Status.FAILED)
        self.assertEqual(stk.status, StkPushRequest.Status.CANCELLED)
        self.assertEqual(stk.result_code, 1032)
        self.assertIn("cancel", (stk.result_desc or "").lower())

    def test_callback_wrong_pin_marks_failed(self):
        stk = self._pending_stk(
            checkout_request_id="ws_CO_PIN",
            merchant_request_id="mr_PIN",
        )
        mpesa.process_stk_callback(
            _failed_callback_payload(
                checkout="ws_CO_PIN",
                merchant="mr_PIN",
                result_code=2001,
                result_desc="Wrong PIN",
            )
        )
        stk.refresh_from_db()
        self.assertEqual(stk.status, StkPushRequest.Status.FAILED)
        self.assertEqual(stk.result_code, 2001)
        self.assertIn("pin", (stk.result_desc or "").lower())

    def test_callback_success_posts_payment_with_receipt(self):
        stk = self._pending_stk(
            checkout_request_id="ws_CO_OK",
            merchant_request_id="mr_OK",
        )
        log = mpesa.process_stk_callback(
            _success_callback_payload(
                checkout="ws_CO_OK",
                merchant="mr_OK",
                amount="100.00",
                receipt="RCPT999",
            )
        )
        stk.refresh_from_db()
        self.assertEqual(stk.status, StkPushRequest.Status.SUCCESS)
        self.assertEqual(stk.mpesa_receipt, "RCPT999")
        self.assertIsNotNone(stk.payment_id)
        self.assertEqual(log.payment_id, stk.payment_id)
        payment = Payment.objects.get(pk=stk.payment_id)
        self.assertEqual(payment.reference, "RCPT999")
        self.assertEqual(payment.method, Payment.Method.MPESA)

    def test_refresh_uses_failed_callback_without_query(self):
        stk = self._pending_stk(
            checkout_request_id="ws_CO_REFRESH_FAIL",
            merchant_request_id="mr_REFRESH_FAIL",
        )
        MpesaCallbackLog.objects.create(
            checkout_request_id="ws_CO_REFRESH_FAIL",
            merchant_request_id="mr_REFRESH_FAIL",
            result_code=1032,
            result_desc="Request cancelled by user",
            status=MpesaCallbackLog.Status.FAILED,
            raw_payload={},
        )
        with patch("apps.billing.mpesa.query_stk_push") as query:
            refreshed = mpesa.refresh_stk_request_status(stk)
            query.assert_not_called()
        self.assertEqual(refreshed.status, StkPushRequest.Status.CANCELLED)

    def test_refresh_awaits_receipt_without_query(self):
        stk = self._pending_stk(
            checkout_request_id="ws_CO_WAIT_RCPT",
            result_code=0,
            result_desc="Accepted",
        )
        with patch("apps.billing.mpesa.query_stk_push") as query:
            refreshed = mpesa.refresh_stk_request_status(stk)
            query.assert_not_called()
        self.assertEqual(refreshed.status, StkPushRequest.Status.PENDING)
        self.assertEqual(refreshed.result_code, 0)
        self.assertIn("receipt", (refreshed.result_desc or "").lower())

    def test_refresh_completes_when_receipt_callback_arrives(self):
        stk = self._pending_stk(
            checkout_request_id="ws_CO_RCPT_LATER",
            result_code=0,
            result_desc="Accepted",
        )
        MpesaCallbackLog.objects.create(
            checkout_request_id="ws_CO_RCPT_LATER",
            merchant_request_id="mr_TEST",
            result_code=0,
            result_desc="Success",
            amount=Decimal("100.00"),
            mpesa_receipt="LATE123",
            phone_number="254712345678",
            status=MpesaCallbackLog.Status.SUCCESS,
            raw_payload={},
        )
        with patch("apps.billing.mpesa.query_stk_push") as query:
            refreshed = mpesa.refresh_stk_request_status(stk)
            query.assert_not_called()
        self.assertEqual(refreshed.status, StkPushRequest.Status.SUCCESS)
        self.assertEqual(refreshed.mpesa_receipt, "LATE123")

    def test_refresh_query_failure_ends_session(self):
        stk = self._pending_stk(checkout_request_id="ws_CO_QUERY_FAIL")
        with patch(
            "apps.billing.mpesa.query_stk_push",
            return_value={
                "result_code": 1032,
                "result_desc": "Request cancelled by user",
                "query_unavailable": False,
            },
        ):
            refreshed = mpesa.refresh_stk_request_status(stk)
        self.assertEqual(refreshed.status, StkPushRequest.Status.CANCELLED)
        self.assertEqual(refreshed.result_code, 1032)

    def test_query_throttle_skips_second_call(self):
        stk = self._pending_stk(checkout_request_id="ws_CO_THROTTLE")
        with patch(
            "apps.billing.mpesa.query_stk_push",
            return_value={
                "result_code": 1037,
                "result_desc": "Waiting for PIN",
                "query_unavailable": False,
            },
        ) as query:
            first = mpesa.refresh_stk_request_status(stk)
            second = mpesa.refresh_stk_request_status(first)
            self.assertEqual(query.call_count, 1)
        self.assertEqual(first.status, StkPushRequest.Status.PENDING)
        self.assertEqual(second.status, StkPushRequest.Status.PENDING)

    def test_stk_payload_flags_failure(self):
        stk = self._pending_stk(status=StkPushRequest.Status.FAILED, result_code=2001)
        stk.result_desc = "Wrong M-Pesa PIN entered. Please try again."
        stk.save(update_fields=["result_desc"])
        payload = mpesa.stk_request_payload(stk)
        self.assertTrue(payload["failed"])
        self.assertFalse(payload["cancelled"])
        self.assertIn("PIN", payload["failure_message"])

    def test_access_token_is_cached(self):
        from apps.billing.models import DarajaSettings

        settings_obj = DarajaSettings.load()
        settings_obj.is_enabled = True
        settings_obj.sandbox_consumer_key = "key"
        settings_obj.sandbox_consumer_secret = "secret"
        settings_obj.sandbox_shortcode = "174379"
        settings_obj.sandbox_passkey = "pass"
        settings_obj.sandbox_callback_url = (
            "https://example.ngrok-free.app/accounts-dashboard/mpesa/callback/"
        )
        settings_obj.save()

        with patch(
            "apps.billing.mpesa._http_json",
            return_value={"access_token": "tok-abc", "expires_in": 3599},
        ) as http:
            t1 = mpesa.get_access_token(settings_obj)
            t2 = mpesa.get_access_token(settings_obj)
            self.assertEqual(t1, "tok-abc")
            self.assertEqual(t2, "tok-abc")
            self.assertEqual(http.call_count, 1)
