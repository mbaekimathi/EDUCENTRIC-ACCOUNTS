from django import forms
from django.core.exceptions import ValidationError

from .models import DarajaSettings, StoreDepartmentStation, StoreExpenseCategory, StoreItem

MAX_STORE_ITEM_IMAGE_BYTES = 2 * 1024 * 1024


class StoreItemForm(forms.ModelForm):
    class Meta:
        model = StoreItem
        fields = [
            "expense_category",
            "department_station",
            "name",
            "description",
            "measure",
            "image",
        ]
        labels = {
            "expense_category": "Expense category",
            "department_station": "Department station",
            "name": "Item name",
            "description": "Description",
            "measure": "Measure",
            "image": "Item image",
        }
        widgets = {
            "expense_category": forms.Select(attrs={"class": "field-input"}),
            "department_station": forms.Select(attrs={"class": "field-input"}),
            "name": forms.TextInput(
                attrs={
                    "class": "field-input",
                    "data-uppercase": "",
                    "autocomplete": "off",
                    "placeholder": "e.g. A4 photocopy paper",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "field-input",
                    "rows": 3,
                    "placeholder": "Optional notes",
                }
            ),
            "measure": forms.Select(attrs={"class": "field-input"}),
            "image": forms.FileInput(
                attrs={"class": "field-input", "accept": "image/jpeg,image/png,image/webp"}
            ),
        }
        help_texts = {
            "description": "Optional",
            "image": "Optional. JPEG, PNG, or WebP up to 2 MB.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].required = False
        self.fields["image"].required = False
        self.fields["expense_category"].queryset = StoreExpenseCategory.objects.filter(
            is_active=True
        )
        self.fields["department_station"].queryset = StoreDepartmentStation.objects.filter(
            is_active=True
        )
        self.fields["expense_category"].empty_label = "Select expense category"
        self.fields["department_station"].empty_label = "Select department station"
        self.fields["measure"].choices = [
            ("", "Select measure"),
            *StoreItem.Measure.choices,
        ]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip().upper()
        if not name:
            raise ValidationError("Item name is required.")
        return name

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if not image:
            return image
        if getattr(image, "size", 0) > MAX_STORE_ITEM_IMAGE_BYTES:
            raise ValidationError("Item image must be 2 MB or smaller.")
        return image

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name")
        station = cleaned.get("department_station")
        measure = cleaned.get("measure")
        if name and station and measure:
            exists = StoreItem.objects.filter(
                name=name,
                department_station=station,
                measure=measure,
            ).exists()
            if exists:
                raise ValidationError(
                    "That item is already registered for this department station and measure."
                )
        return cleaned


class DarajaSettingsForm(forms.ModelForm):
    """Daraja API credentials for sandbox and production."""

    SECRET_FIELDS = (
        "sandbox_consumer_secret",
        "sandbox_passkey",
        "production_consumer_secret",
        "production_passkey",
    )

    class Meta:
        model = DarajaSettings
        fields = [
            "is_enabled",
            "active_environment",
            "sandbox_consumer_key",
            "sandbox_consumer_secret",
            "sandbox_shortcode",
            "sandbox_passkey",
            "sandbox_callback_url",
            "production_consumer_key",
            "production_consumer_secret",
            "production_shortcode",
            "production_passkey",
            "production_callback_url",
        ]
        widgets = {
            "is_enabled": forms.CheckboxInput(
                attrs={"class": "h-4 w-4 rounded border-ink-950/20 text-brand"}
            ),
            "active_environment": forms.RadioSelect,
            "sandbox_consumer_key": forms.TextInput(
                attrs={"class": "field-input", "autocomplete": "off"}
            ),
            "sandbox_consumer_secret": forms.PasswordInput(
                attrs={
                    "class": "field-input font-mono",
                    "autocomplete": "new-password",
                    "placeholder": "Leave blank to keep saved secret",
                },
                render_value=False,
            ),
            "sandbox_shortcode": forms.TextInput(
                attrs={
                    "class": "field-input font-mono",
                    "placeholder": "e.g. 174379",
                    "autocomplete": "off",
                }
            ),
            "sandbox_passkey": forms.PasswordInput(
                attrs={
                    "class": "field-input font-mono",
                    "autocomplete": "new-password",
                    "placeholder": "Leave blank to keep saved passkey",
                },
                render_value=False,
            ),
            "sandbox_callback_url": forms.URLInput(
                attrs={
                    "class": "field-input",
                    "placeholder": "https://xxxx.ngrok-free.app/accounts-dashboard/mpesa/callback/",
                }
            ),
            "production_consumer_key": forms.TextInput(
                attrs={"class": "field-input", "autocomplete": "off"}
            ),
            "production_consumer_secret": forms.PasswordInput(
                attrs={
                    "class": "field-input font-mono",
                    "autocomplete": "new-password",
                    "placeholder": "Leave blank to keep saved secret",
                },
                render_value=False,
            ),
            "production_shortcode": forms.TextInput(
                attrs={
                    "class": "field-input font-mono",
                    "placeholder": "Paybill or Till number",
                    "autocomplete": "off",
                }
            ),
            "production_passkey": forms.PasswordInput(
                attrs={
                    "class": "field-input font-mono",
                    "autocomplete": "new-password",
                    "placeholder": "Leave blank to keep saved passkey",
                },
                render_value=False,
            ),
            "production_callback_url": forms.URLInput(
                attrs={
                    "class": "field-input",
                    "placeholder": "https://example.com/mpesa/callback/",
                }
            ),
        }
        labels = {
            "is_enabled": "Enable M-Pesa (Daraja) payments",
            "active_environment": "Active environment",
            "sandbox_consumer_key": "Consumer key",
            "sandbox_consumer_secret": "Consumer secret",
            "sandbox_shortcode": "Business shortcode",
            "sandbox_passkey": "Lipa Na M-Pesa passkey",
            "sandbox_callback_url": "Callback URL",
            "production_consumer_key": "Consumer key",
            "production_consumer_secret": "Consumer secret",
            "production_shortcode": "Business shortcode",
            "production_passkey": "Lipa Na M-Pesa passkey",
            "production_callback_url": "Callback URL",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._existing_secrets = {
            name: getattr(self.instance, name, "") or ""
            for name in self.SECRET_FIELDS
        }
        for name in self.SECRET_FIELDS:
            field = self.fields[name]
            field.required = False
            if self._existing_secrets.get(name):
                field.widget.attrs["placeholder"] = "••••••••  (saved — leave blank to keep)"

    def _normalize_callback_url(self, value: str) -> str:
        url = (value or "").strip().rstrip("/")
        if not url:
            return ""
        lower = url.lower()
        if "/mpesa/callback" in lower:
            return url if url.endswith("/") else f"{url}/"
        # Allow pasting only the tunnel host; append the Accounts callback path.
        return f"{url}/accounts-dashboard/mpesa/callback/"

    def clean_sandbox_callback_url(self):
        return self._normalize_callback_url(self.cleaned_data.get("sandbox_callback_url"))

    def clean_production_callback_url(self):
        return self._normalize_callback_url(
            self.cleaned_data.get("production_callback_url")
        )

    def save(self, commit=True):
        instance = super().save(commit=False)
        for name in self.SECRET_FIELDS:
            if not (self.cleaned_data.get(name) or "").strip():
                setattr(instance, name, self._existing_secrets.get(name, ""))
        if commit:
            instance.save()
        return instance
