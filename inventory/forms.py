import uuid
from decimal import Decimal
from django import forms
from django.db.models import F
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm, AuthenticationForm, PasswordChangeForm
from django.utils import timezone
from .models import User, Product, Batch, Supplier, Movement, Invoice, PayableAdjustment, PAYMENT_METHODS

DATE = {'type': 'date'}
TAX_RATES = [('0', '0 %'), ('5', '5 %'), ('19', '19 %')]


def date_input(**attrs):
    return forms.DateInput(attrs={**DATE, **attrs}, format='%Y-%m-%d')


class Styled:
    """Clases CSS uniformes para todos los campos."""
    def style(self):
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(w, forms.HiddenInput):
                continue
            kind = 'check' if isinstance(w, forms.CheckboxInput) else 'input'
            w.attrs['class'] = (w.attrs.get('class', '') + ' ' + kind).strip()
            if field.required and kind == 'input':
                w.attrs.setdefault('required', True)


class StyledForm(Styled, forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


class StyledModelForm(Styled, forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


class TokenForm(StyledForm):
    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput(attrs={'accept': '.pdf,.xml,.zip,.png,.jpg,.jpeg'}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        one = super().clean
        if isinstance(data, (list, tuple)):
            return [one(d, initial) for d in data]
        return [one(data, initial)] if data else []


def uploads(files):
    return [(f.name, f.read()) for f in files or []]


class SignInForm(Styled, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Usuario'
        self.fields['username'].widget.attrs.update({'autocomplete': 'username', 'placeholder': 'nombre.apellido'})
        self.fields['password'].widget.attrs.update({'autocomplete': 'current-password'})
        self.style()


class ChangePasswordForm(Styled, PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


class LotChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, b):
        state = f'vencido · saldo {b.physical}' if b.expired else f'disponible {b.available}'
        return f'{b.product.name} · lote {b.lot} · {b.location} · vence {b.expiry:%d/%m/%Y} · {state}'


class LabeledInputs:
    """Filas de tabla sin etiqueta visible: el nombre accesible viene de la etiqueta del campo."""
    def label_inputs(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault('aria-label', field.label)


# ---------------------------------------------------------- inventario

class RequestForm(TokenForm):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True), label='Referencia')
    quantity = forms.IntegerField(min_value=1, max_value=100000, label='Cantidad')
    needed_by = forms.DateField(required=False, widget=date_input(), label='Fecha en que la necesita')
    purpose = forms.CharField(max_length=300, label='Destino o área', help_text='Área, obra o punto de venta que lo necesita.')


class ReserveForm(TokenForm):
    batch = LotChoiceField(queryset=Batch.objects.none(), label='Lote')
    quantity = forms.IntegerField(min_value=1, max_value=100000, label='Cantidad')
    note = forms.CharField(max_length=300, label='Destino de la reserva')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['batch'].queryset = Batch.objects.select_related('product').filter(expiry__gte=timezone.localdate(), physical__gt=0).order_by('product__name', 'expiry')


class ExitForm(TokenForm):
    batch = LotChoiceField(queryset=Batch.objects.none(), label='Lote')
    quantity = forms.IntegerField(min_value=1, max_value=100000, label='Cantidad')
    reference = forms.CharField(max_length=80, required=False, label='Documento soporte', help_text='Número de factura de venta, remisión u orden.')
    note = forms.CharField(max_length=300, required=False, label='Destino u observación')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['batch'].queryset = Batch.objects.select_related('product').filter(expiry__gte=timezone.localdate(), physical__gt=0).order_by('product__name', 'expiry')


class AdjustInForm(TokenForm):
    kind = forms.ChoiceField(choices=[('initial', 'Saldo inicial (conteo de apertura)'), ('adjust_in', 'Ajuste positivo por conteo')], label='Tipo de entrada')
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True), label='Referencia')
    lot = forms.CharField(max_length=80, label='Lote')
    expiry = forms.DateField(widget=date_input(), label='Vencimiento')
    location = forms.CharField(max_length=120, label='Ubicación', widget=forms.TextInput(attrs={'list': 'ubicaciones'}))
    quantity = forms.IntegerField(min_value=1, max_value=1000000, label='Cantidad')
    note = forms.CharField(max_length=300, required=False, label='Observación')


class AdjustOutForm(TokenForm):
    kind = forms.ChoiceField(choices=[('writeoff', 'Baja de inventario'), ('adjust_out', 'Ajuste negativo por conteo')], label='Tipo de salida')
    batch = LotChoiceField(queryset=Batch.objects.none(), label='Lote')
    quantity = forms.IntegerField(min_value=1, max_value=1000000, label='Cantidad')
    loss_reason = forms.ChoiceField(choices=[('', 'Seleccione…')] + Movement.LOSS_REASONS, required=False, label='Causa de la baja')
    note = forms.CharField(max_length=300, label='Justificación')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['batch'].queryset = Batch.objects.select_related('product').filter(physical__gt=0).order_by('expiry')


class ApproveForm(TokenForm):
    batch = LotChoiceField(queryset=Batch.objects.none(), label='Lote que se reserva')

    def __init__(self, *args, supply_request, suggested=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Solo lotes vigentes con unidades disponibles: los agotados o reservados por completo no se ofrecen.
        self.fields['batch'].queryset = (Batch.objects.select_related('product')
                                         .filter(product=supply_request.product, expiry__gte=timezone.localdate(), physical__gt=F('reserved')).order_by('expiry'))
        if suggested:
            self.fields['batch'].initial = suggested.pk
            self.fields['batch'].help_text = 'Sugerido: el lote vigente más próximo a vencer que cubre la cantidad.'


class ReasonForm(TokenForm):
    reason = forms.CharField(max_length=300, label='Motivo')


class ProductForm(StyledModelForm):
    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    pack_size = forms.IntegerField(min_value=1, max_value=10000, required=False, label='Unidades por empaque de compra',
                                   help_text='Los pedidos sugeridos se redondean a este múltiplo. Vacío: de a una unidad.')

    class Meta:
        model = Product
        fields = ['code', 'name', 'category', 'generic', 'manufacturer', 'presentation', 'unit', 'minimum', 'pack_size', 'controlled_storage', 'active']
        help_texts = {'minimum': 'Se alerta cuando el disponible llega a este valor. Vacío: sin alerta por mínimo.',
                      'code': 'El mismo código del sistema contable cuando exista.'}

    def clean_pack_size(self):
        return self.cleaned_data.get('pack_size') or 1


class SettingsForm(TokenForm):
    expiry_days = forms.IntegerField(min_value=0, max_value=3650, label='Alertar vencimiento de lotes con anticipación de (días)')
    discount_alert_days = forms.IntegerField(min_value=0, max_value=90, label='Avisar cierre de descuentos por pronto pago con (días)')
    due_alert_days = forms.IntegerField(min_value=0, max_value=90, label='Avisar vencimiento de facturas con (días)')


# ------------------------------------------------------- proveedores y facturas

class SupplierForm(TokenForm):
    name = forms.CharField(max_length=160, label='Razón social')
    nit = forms.CharField(max_length=20, label='NIT', help_text='Con o sin dígito de verificación, por ejemplo 900123456-7.')
    email = forms.EmailField(required=False, label='Correo de facturación')
    phone = forms.CharField(max_length=40, required=False, label='Teléfono')
    payment_days = forms.IntegerField(min_value=0, max_value=365, initial=30, label='Plazo de pago (días)')
    discount_base = forms.ChoiceField(choices=Supplier.Base.choices, label='Base del descuento por pronto pago')
    active = forms.BooleanField(required=False, initial=True, label='Proveedor activo')


class TierForm(LabeledInputs, StyledForm):
    days = forms.IntegerField(min_value=1, max_value=365, required=False, label='Pago dentro de (días)')
    percent = forms.DecimalField(min_value=Decimal('0.01'), max_value=100, decimal_places=2, required=False, label='Descuento (%)')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.label_inputs()

    def clean(self):
        data = super().clean()
        if (data.get('days') is None) != (data.get('percent') is None):
            raise forms.ValidationError('Complete días y porcentaje, o deje ambos vacíos.')
        return data


TierFormSet = forms.formset_factory(TierForm, extra=4, max_num=4)


class InvoiceForm(TokenForm):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.filter(active=True), label='Proveedor')
    number = forms.CharField(max_length=40, label='Número de factura')
    issue_date = forms.DateField(widget=date_input(), label='Fecha de emisión')
    due_date = forms.DateField(required=False, widget=date_input(), label='Fecha de vencimiento', help_text='Vacío: se calcula con el plazo del proveedor.')
    ordered_on = forms.DateField(required=False, widget=date_input(), label='Fecha del pedido')
    order_number = forms.CharField(max_length=40, required=False, label='Orden de compra')
    cufe = forms.CharField(max_length=120, required=False, label='CUFE')
    global_discount = forms.DecimalField(min_value=0, decimal_places=2, max_digits=16, required=False, initial=0, label='Descuento global')
    retention = forms.DecimalField(min_value=0, decimal_places=2, max_digits=16, required=False, initial=0, label='Retenciones')
    notes = forms.CharField(max_length=300, required=False, label='Observaciones')
    files = MultipleFileField(required=False, label='Soportes (PDF, XML o ZIP)')

    def clean(self):
        data = super().clean()
        data['global_discount'] = data.get('global_discount') or Decimal(0)
        data['retention'] = data.get('retention') or Decimal(0)
        return data


class PDFInvoiceForm(InvoiceForm):
    """La lectura del PDF propone valores; la persona confirma proveedor y total antes de guardar."""
    supplier_name = forms.CharField(max_length=160, required=False, label='Razón social del proveedor en el PDF')
    supplier_nit = forms.CharField(max_length=20, required=False, label='NIT del proveedor en el PDF')
    pdf_currency = forms.ChoiceField(choices=[('', 'Confirme la moneda'), ('COP', 'Pesos colombianos (COP)')],
                                     label='Moneda del documento')
    pdf_total = forms.DecimalField(min_value=0, decimal_places=2, max_digits=16,
                                  label='Total a pagar según el PDF',
                                  help_text='Compare con el original. Corrija este valor si la lectura no coincide; no use el importe con descuento por pronto pago.')
    pdf_reviewed = forms.BooleanField(label='Confirmo que es una factura de compra y revisé proveedor, fechas, productos, cantidades, impuestos, total y condiciones de pago contra el PDF.',
                                      error_messages={'required': 'Falta esta confirmación: compare los datos con el PDF, marque la casilla y guarde de nuevo. Lo que escribió se conserva.'})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].required = False
        self.fields['supplier'].widget.attrs.pop('required', None)
        self.fields['supplier'].help_text = 'Seleccione uno existente o complete el NIT y la razón social para crearlo al guardar.'
        self.fields['due_date'].required = True
        self.fields['due_date'].widget.attrs['required'] = True
        self.fields['due_date'].help_text = 'Si no aparece en el PDF, confírmela con las condiciones de pago del proveedor.'

    def clean(self):
        from .services import normalize_nit
        data = super().clean()
        data['currency'] = data.get('pdf_currency')
        if not data.get('supplier'):
            if not data.get('supplier_name'):
                self.add_error('supplier_name', 'Indique la razón social del proveedor o seleccione uno existente.')
            try:
                data['supplier_nit'] = normalize_nit(data.get('supplier_nit', ''))
            except forms.ValidationError as err:
                self.add_error('supplier_nit', err)
        return data


class InvoicePaymentTermsForm(StyledForm):
    payment_days = forms.IntegerField(min_value=0, max_value=365, label='Plazo de pago (días desde emisión)')
    discount_base = forms.ChoiceField(required=False, choices=[('', 'Confirme la base si hay descuento')] + list(Supplier.Base.choices),
                                      label='Base del descuento por pronto pago')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for index in range(1, 5):
            self.fields[f'days_{index}'] = forms.IntegerField(min_value=1, max_value=365, required=False, label=f'Plazo de descuento {index} (días)')
            self.fields[f'percent_{index}'] = forms.DecimalField(min_value=Decimal('0.01'), max_value=100, decimal_places=2, required=False,
                                                                label=f'Descuento {index} (%)')
        self.style()

    def tier_fields(self):
        return [(self[f'days_{i}'], self[f'percent_{i}']) for i in range(1, 5)]

    def clean(self):
        data = super().clean()
        tiers = []
        for index in range(1, 5):
            day, percent = data.get(f'days_{index}'), data.get(f'percent_{index}')
            if (day is None) != (percent is None):
                self.add_error(f'days_{index}', 'Complete plazo y porcentaje, o deje ambos vacíos.')
            elif day is not None:
                tiers.append([day, str(percent)])
        if tiers and not data.get('discount_base'):
            self.add_error('discount_base', 'Confirme sobre qué valor se aplica el descuento del proveedor.')
        if len({d for d, _ in tiers}) != len(tiers):
            raise forms.ValidationError('Los plazos de descuento no deben repetirse.')
        if data.get('payment_days') is not None and any(d > data['payment_days'] for d, _ in tiers):
            raise forms.ValidationError('El plazo de descuento no puede superar el plazo de pago de la factura.')
        data['document_terms'] = {'payment_days': data.get('payment_days'), 'base': data.get('discount_base') or 'subtotal',
                                  'tiers': sorted(tiers)}
        return data


class InvoiceLineForm(LabeledInputs, StyledForm):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True), label='Producto')
    supplier_code = forms.CharField(max_length=60, required=False, label='Código proveedor',
                                    widget=forms.TextInput(attrs={'placeholder': 'Código proveedor', 'aria-label': 'Código del proveedor'}))
    description = forms.CharField(max_length=200, required=False, label='Descripción en factura',
                                  widget=forms.TextInput(attrs={'placeholder': 'Descripción en la factura', 'aria-label': 'Descripción en la factura'}))
    quantity = forms.IntegerField(min_value=1, max_value=1000000, label='Cantidad')
    unit_price = forms.DecimalField(min_value=0, decimal_places=2, max_digits=16, label='Valor unitario', help_text='0 para bonificados.')
    discount = forms.DecimalField(min_value=0, decimal_places=2, max_digits=16, required=False, initial=0, label='Descuento')
    tax_rate = forms.TypedChoiceField(choices=[('', 'Revise el IVA')] + TAX_RATES, coerce=Decimal, initial='0', label='IVA')
    expected_lot = forms.CharField(max_length=80, required=False, label='Lote facturado')
    expected_expiry = forms.DateField(required=False, widget=date_input(), label='Vencimiento')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.label_inputs()

    def has_changed(self):
        # Una fila extra sin producto, cantidad ni valor se ignora aunque se haya borrado el descuento.
        typed = [self.data.get(self.add_prefix(k)) for k in ('product', 'quantity', 'unit_price')]
        return any(v not in (None, '') for v in typed) and super().has_changed()

    def clean(self):
        data = super().clean()
        data['discount'] = data.get('discount') or Decimal(0)
        return data


class BaseLineFormSet(forms.BaseFormSet):
    def lines(self):
        return [f.cleaned_data for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]


def invoice_line_formset(extra=1):
    return forms.formset_factory(InvoiceLineForm, formset=BaseLineFormSet, extra=extra, max_num=60, validate_max=True, can_delete=True)


class ImportForm(StyledForm):
    file = forms.FileField(label='Factura de proveedor (PDF de demostración, XML o ZIP)', widget=forms.ClearableFileInput(attrs={'accept': '.pdf,.xml,.zip'}))


class AttachForm(TokenForm):
    files = MultipleFileField(label='Soportes')

    def clean_files(self):
        files = self.cleaned_data['files']
        if not files:
            raise forms.ValidationError('Seleccione al menos un archivo.')
        return files


class ReceiptForm(TokenForm):
    received_on = forms.DateField(widget=date_input(), label='Fecha de llegada', initial=timezone.localdate)
    note = forms.CharField(max_length=300, required=False, label='Observaciones o diferencias')


class ReceiptRowForm(LabeledInputs, StyledForm):
    line = forms.TypedChoiceField(coerce=int, label='Producto')
    quantity = forms.IntegerField(min_value=1, max_value=1000000, label='Cantidad')
    lot = forms.CharField(max_length=80, label='Lote')
    expiry = forms.DateField(widget=date_input(), label='Vencimiento')
    location = forms.CharField(max_length=120, label='Ubicación', widget=forms.TextInput(attrs={'list': 'ubicaciones'}))

    def __init__(self, *args, line_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['line'].choices = line_choices
        self.label_inputs()


ReceiptRowFormSet = forms.formset_factory(ReceiptRowForm, formset=BaseLineFormSet, extra=0, max_num=40, validate_max=True, can_delete=True)


class PaymentForm(TokenForm):
    paid_on = forms.DateField(widget=date_input(), label='Fecha del pago', initial=timezone.localdate)
    method = forms.ChoiceField(choices=PAYMENT_METHODS, label='Medio de pago')
    reference = forms.CharField(max_length=80, required=False, label='Referencia o comprobante')


# ------------------------------------------------------------- cuentas

class AccountForm(Styled, UserCreationForm):
    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'role')
        labels = {'username': 'Usuario', 'first_name': 'Nombres', 'last_name': 'Apellidos', 'email': 'Correo', 'role': 'Perfil'}

    def __init__(self, *args, allowed_roles=None, **kwargs):
        super().__init__(*args, **kwargs)
        if allowed_roles is not None:
            self.fields['role'].choices = [c for c in self.fields['role'].choices if c[0] in allowed_roles]
        self.style()


class ResetAccountForm(Styled, SetPasswordForm):
    token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


# ------------------------------------------------------- cuentas por pagar

class PaymentPlanForm(TokenForm):
    on = forms.DateField(widget=date_input(), label='Fecha', initial=timezone.localdate)
    method = forms.ChoiceField(choices=PAYMENT_METHODS, label='Medio de pago')
    reference = forms.CharField(max_length=80, required=False, label='Referencia o comprobante', help_text='Obligatoria al confirmar si no adjunta soporte.')
    note = forms.CharField(max_length=300, required=False, label='Observaciones')
    files = MultipleFileField(required=False, label='Soporte del pago (PDF o imagen)', widget=MultipleFileInput(attrs={'accept': '.pdf,.png,.jpg,.jpeg'}))


class CreditNoteForm(TokenForm):
    number = forms.CharField(max_length=60, label='Número de la nota crédito')
    issue_date = forms.DateField(widget=date_input(), required=False, label='Fecha')
    amount = forms.DecimalField(min_value=Decimal('0.01'), max_digits=16, decimal_places=2, label='Valor')
    note = forms.CharField(max_length=300, required=False, label='Motivo', help_text='Por ejemplo: devolución, ajuste de precio o descuento.')
    invoice = forms.ModelChoiceField(queryset=None, required=False, label='Aplicar a la factura', help_text='Si la deja vacía, queda como saldo a favor.')
    apply_amount = forms.DecimalField(min_value=Decimal('0.01'), max_digits=16, decimal_places=2, required=False, label='Valor a aplicar',
                                      help_text='Vacío: todo lo que permita el saldo.')

    def __init__(self, *args, supplier=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['invoice'].queryset = Invoice.objects.filter(supplier=supplier, review=Invoice.Review.VALIDATED).order_by('-issue_date')


class AdjustmentForm(TokenForm):
    kind = forms.ChoiceField(choices=PayableAdjustment.KINDS, label='Tipo de ajuste')
    amount = forms.DecimalField(min_value=Decimal('0.01'), max_digits=16, decimal_places=2, label='Valor')
    reference = forms.CharField(max_length=120, label='Soporte', help_text='Comunicación del proveedor, nota débito o certificado.')


class TextForm(TokenForm):
    text = forms.CharField(max_length=300, label='Texto')
