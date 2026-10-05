from functools import wraps

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.directory.models import Employee
from apps.staff.models import AccountsUser

PORTAL_DENIED_MESSAGE = "Not authorised."
ALLOWED_EMPLOYEE_ROLES = Employee.PORTAL_ROLES


def portal_access_required(view_func):
    """Allow only Accountant / Store Manager / Support portal sessions."""

    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not getattr(user, "can_access_portal", False):
            logout(request)
            return redirect("staff:login")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _extract_employee_code(post):
    raw = (
        post.get("accounts_staff_code")
        or post.get("staff_code")
        or post.get("employee_code")
        or post.get("username")
        or ""
    )
    return "".join(ch for ch in str(raw) if ch.isdigit())[:6]


def sync_accounts_user_from_employee(employee: Employee) -> AccountsUser:
    """Keep a local AccountsUser for sessions/FKs, sourced from employees_employee."""
    if employee.role == "STORE_MANAGER":
        role = AccountsUser.Role.STORE_MANAGER
    elif employee.role == "SUPPORT":
        role = AccountsUser.Role.SUPPORT
    else:
        role = AccountsUser.Role.ACCOUNTANT
    user, _ = AccountsUser.objects.get_or_create(
        staff_code=employee.employee_code,
        defaults={
            "email": employee.email or f"{employee.employee_code}@school.local",
            "first_name": employee.first_name,
            "last_name": employee.last_name,
            "phone_number": employee.phone_number or "",
            "role": role,
            "is_staff": True,
            "is_active": True,
            "employee_id": employee.id,
        },
    )
    user.email = employee.email or user.email
    user.first_name = employee.first_name
    user.last_name = employee.last_name
    user.phone_number = employee.phone_number or ""
    user.role = role
    user.is_active = True
    user.is_staff = True
    user.employee_id = employee.id
    # Keep password hash in sync so session user matches employee credentials.
    user.password = employee.password
    user.save()
    return user


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        if getattr(request.user, "can_access_portal", False):
            return redirect("billing:dashboard")
        logout(request)

    error = None
    staff_code = ""
    if request.method == "POST":
        staff_code = _extract_employee_code(request.POST)
        password = (request.POST.get("password") or "").strip()

        if len(staff_code) != 6:
            error = "Enter your 6-digit employee code (numbers only)."
        elif not password:
            error = "Enter your password."
        else:
            employee = Employee.objects.filter(employee_code=staff_code).first()
            if employee is None or not employee.check_password(password):
                error = "Invalid employee code or password."
            elif employee.role not in ALLOWED_EMPLOYEE_ROLES:
                error = PORTAL_DENIED_MESSAGE
            elif employee.approval_status != "APPROVED":
                error = "Your employee account is not approved yet."
            elif employee.is_suspended:
                error = "Your employee account is suspended."
            elif not employee.is_active:
                error = "Your employee account is inactive."
            else:
                user = sync_accounts_user_from_employee(employee)
                login(
                    request,
                    user,
                    backend="django.contrib.auth.backends.ModelBackend",
                )
                return redirect("billing:dashboard")

    return render(
        request,
        "staff/login.html",
        {
            "error": error,
            "staff_code": staff_code,
        },
    )


@require_POST
@login_required
def logout_view(request):
    logout(request)
    return redirect("staff:login")
