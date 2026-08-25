from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import FinancialTerm, FinancialYear


class FinancialYearForm(forms.ModelForm):
    class Meta:
        model = FinancialYear
        fields = ["name", "start_date", "end_date", "is_current"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "field-input",
                    "placeholder": "e.g. 2025/2026 (optional)",
                    "autocomplete": "off",
                }
            ),
            "start_date": forms.DateInput(
                attrs={"class": "field-input", "type": "date"}
            ),
            "end_date": forms.DateInput(
                attrs={"class": "field-input", "type": "date"}
            ),
            "is_current": forms.CheckboxInput(
                attrs={"class": "h-4 w-4 rounded border-ink-950/20 text-brand"}
            ),
        }
        labels = {
            "name": "Year label",
            "start_date": "Start date",
            "end_date": "End date",
            "is_current": "Set as the current financial year",
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and start > end:
            self.add_error("end_date", "End date must be on or after the start date.")
        return cleaned


class FinancialTermForm(forms.ModelForm):
    class Meta:
        model = FinancialTerm
        fields = ["name", "start_date", "end_date"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "field-input",
                    "placeholder": "e.g. Term 1",
                    "autocomplete": "off",
                }
            ),
            "start_date": forms.DateInput(
                attrs={"class": "field-input", "type": "date"}
            ),
            "end_date": forms.DateInput(
                attrs={"class": "field-input", "type": "date"}
            ),
        }
        labels = {
            "name": "Term name",
            "start_date": "Term start date",
            "end_date": "Term end date",
        }

    def __init__(self, *args, financial_year=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.financial_year = financial_year

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        year = self.financial_year
        if start and end and start > end:
            self.add_error("end_date", "Term end date must be on or after the start date.")
        if year and start and end:
            if start < year.start_date or end > year.end_date:
                self.add_error(
                    "start_date",
                    "Term dates must fall within the financial year.",
                )
                self.add_error(
                    "end_date",
                    f"Use dates between {year.start_date} and {year.end_date}.",
                )
        return cleaned


def default_term_ranges(start_date, end_date, count=3):
    """Split a financial year into roughly equal term date ranges."""
    if not start_date or not end_date or start_date > end_date:
        return []
    total_days = (end_date - start_date).days + 1
    base = total_days // count
    rem = total_days % count
    ranges = []
    cursor = start_date
    for i in range(count):
        length = base + (1 if i < rem else 0)
        term_end = cursor + timedelta(days=max(length - 1, 0))
        if term_end > end_date:
            term_end = end_date
        ranges.append((cursor, term_end))
        cursor = term_end + timedelta(days=1)
        if cursor > end_date:
            break
    return ranges


def suggest_current_term(year: FinancialYear):
    """Return the term whose date range includes today, if any."""
    if year is None:
        return None
    today = timezone.localdate()
    return (
        year.terms.filter(start_date__lte=today, end_date__gte=today)
        .order_by("start_date")
        .first()
    )


def clear_current_terms(*, except_year: FinancialYear | None = None):
    """Clear is_current on all terms, optionally keeping one year's terms alone."""
    qs = FinancialTerm.objects.filter(is_current=True)
    if except_year is not None:
        qs = qs.exclude(financial_year_id=except_year.pk)
    qs.update(is_current=False)


def apply_default_current_term(year: FinancialYear, *, force: bool = False):
    """
    Ensure the current year has exactly one current term: the term containing today.

    - Only applies when the year is marked current.
    - Clears current flags on every other year.
    - If today is not inside any term, clears current on this year unless force keeps
      an existing manual selection when force=False and one already exists.
    """
    if year is None or not year.is_current:
        if year is not None:
            year.terms.filter(is_current=True).update(is_current=False)
        return None

    clear_current_terms(except_year=year)
    suggested = suggest_current_term(year)
    if suggested is None:
        if force:
            year.terms.filter(is_current=True).update(is_current=False)
        return None

    year.terms.exclude(pk=suggested.pk).filter(is_current=True).update(is_current=False)
    if not suggested.is_current:
        FinancialTerm.objects.filter(pk=suggested.pk).update(is_current=True)
        suggested.is_current = True
    return suggested


def ensure_default_terms(year: FinancialYear, created_by=None):
    """Create Term 1–3 for a new year if none exist yet."""
    if year.terms.exists():
        if year.is_current:
            if not year.terms.filter(is_current=True).exists():
                apply_default_current_term(year, force=True)
        else:
            year.terms.filter(is_current=True).update(is_current=False)
        return list(year.terms.all())

    for index, (start, end) in enumerate(
        default_term_ranges(year.start_date, year.end_date), start=1
    ):
        FinancialTerm.objects.create(
            financial_year=year,
            name=f"Term {index}",
            start_date=start,
            end_date=end,
            is_current=False,
        )

    terms = list(year.terms.all())
    if year.is_current:
        apply_default_current_term(year, force=True)
    return terms
