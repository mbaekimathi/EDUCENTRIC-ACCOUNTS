from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("student-fees/", views.student_fees, name="student_fees"),
    path(
        "student-fees/<int:account_id>/",
        views.student_fees_account_levels,
        name="student_fees_account_levels",
    ),
    path(
        "student-fees/<int:account_id>/levels/<int:level_id>/",
        views.student_fees_level_students,
        name="student_fees_level_students",
    ),
    path(
        "student-fees/<int:account_id>/levels/<int:level_id>/stk/initiate/",
        views.student_fees_stk_initiate,
        name="student_fees_stk_initiate",
    ),
    path(
        "student-fees/<int:account_id>/levels/<int:level_id>/stk/<int:stk_id>/status/",
        views.student_fees_stk_status,
        name="student_fees_stk_status",
    ),
    path(
        "student-fees/<int:account_id>/fee-structure/",
        views.student_fees_fee_structure,
        name="student_fees_fee_structure",
    ),
    path("pocket-money/", views.pocket_money, name="pocket_money"),
    path("petty-cashbook/", views.petty_cashbook, name="petty_cashbook"),
    path(
        "petty-cashbook/<int:account_id>/",
        views.petty_cashbook_detail,
        name="petty_cashbook_detail",
    ),
    path("invoices/", views.invoices, name="invoices"),
    path("invoices/suppliers/", views.supplier_accounts, name="supplier_accounts"),
    path(
        "invoices/suppliers/<int:supplier_id>/",
        views.supplier_account_detail,
        name="supplier_account_detail",
    ),
    path("store-management/", views.store_management, name="store_management"),
    path(
        "store-management/requisitions/",
        views.store_section,
        {"slug": "requisitions"},
        name="store_requisitions",
    ),
    path(
        "store-management/lpo/",
        views.store_section,
        {"slug": "lpo"},
        name="store_lpo",
    ),
    path(
        "store-management/register-item/",
        views.store_register_item,
        name="store_register_item",
    ),
    path(
        "store-management/stock-in-out/",
        views.store_stock_in_out,
        name="store_stock_in_out",
    ),
    path(
        "store-management/suppliers/suggest/",
        views.store_supplier_suggest,
        name="store_supplier_suggest",
    ),
    path(
        "store-management/stock-in-out/<int:movement_id>/receipt/",
        views.store_stock_delivery_receipt,
        name="store_stock_delivery_receipt",
    ),
    path(
        "store-management/stock-analysis/",
        views.store_section,
        {"slug": "stock-analysis"},
        name="store_stock_analysis",
    ),
    path(
        "store-management/current-stock/",
        views.store_section,
        {"slug": "current-stock"},
        name="store_current_stock",
    ),
    path(
        "store-management/suppliers/",
        views.store_suppliers,
        name="store_suppliers",
    ),
    path(
        "store-management/suppliers/<int:supplier_id>/",
        views.store_supplier_detail,
        name="store_supplier_detail",
    ),
    path(
        "store-management/stock-audit/",
        views.store_section,
        {"slug": "stock-audit"},
        name="store_stock_audit",
    ),
    path(
        "store-management/reports/",
        views.store_reports,
        name="store_reports",
    ),
    path("reports/", views.reports, name="reports"),
    path("school-accounts/", views.school_accounts, name="school_accounts"),
    path(
        "school-accounts/<int:account_id>/",
        views.school_account_detail,
        name="school_account_detail",
    ),
    path("students/", views.student_search, name="student_search"),
    path("students/<int:student_id>/", views.student_ledger, name="student_ledger"),
    path("system-settings/", views.system_settings, name="system_settings"),
    path(
        "system-settings/accounts-settings/",
        views.accounts_settings,
        name="accounts_settings",
    ),
    path(
        "system-settings/accounts-settings/<int:account_id>/votes/",
        views.account_votes_settings,
        name="account_votes_settings",
    ),
    path(
        "system-settings/financial-year/",
        views.financial_year_settings,
        name="financial_year_settings",
    ),
    path("system-settings/payments/", views.payments_settings, name="payments_settings"),
    path("mpesa/callback/", views.mpesa_callback, name="mpesa_callback"),
]
