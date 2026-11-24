from datetime import timedelta
from decimal import Decimal
from django import forms
from django.utils import timezone
from inventory.forms import TokenForm, StyledForm, LabeledInputs, BaseLineFormSet, date_input, TAX_RATES
from inventory.models import Product, Movement
from contable import services as contable
from .models import Party, PriceList, Sale, BankAccount, Context, PartyContact


class PartyForm(TokenForm):
    id_type = forms.ChoiceField(choices=Party.ID_TYPES, label='Tipo de identificación')
    identification = forms.CharField(max_length=25, label='Número de identificación', help_text='Para NIT puede incluir el dígito de verificación.')
    name = forms.CharField(max_length=160, label='Nombre o razón social', help_text='Tal como figura en el sistema contable y en la factura electrónica.')
    kind = forms.ChoiceField(choices=Party.KINDS, label='Tipo de persona')
    default_context = forms.ChoiceField(choices=Context.choices, label='Contexto habitual', help_text='Se propone al facturar; puede cambiarse en cada venta.')
    price_list = forms.ModelChoiceField(queryset=PriceList.objects.filter(active=True), required=False, label='Lista de precios acordada')
    credit_days = forms.IntegerField(min_value=0, max_value=365, initial=30, label='Plazo de crédito (días)')
    requires_po = forms.BooleanField(required=False, label='Exige orden de compra en sus facturas')
    einvoice_email = forms.EmailField(required=False, label='Correo de facturación electrónica')
    address = forms.CharField(max_length=200, required=False, label='Dirección')
    city = forms.CharField(max_length=80, required=False, label='Ciudad')
    notes = forms.CharField(max_length=300, required=False, label='Notas internas')
    active = forms.BooleanField(required=False, initial=True, label='Activo')


class ContactForm(TokenForm):
    name = forms.CharField(max_length=120, label='Persona')
    role = forms.ChoiceField(choices=PartyContact.ROLES, label='Uso')
    channel = forms.ChoiceField(choices=PartyContact.CHANNELS, label='Canal')
    value = forms.CharField(max_length=160, label='Número o correo')
    preferred = forms.BooleanField(required=False, label='Canal preferido de cobranza')
    notes = forms.CharField(max_length=200, required=False, label='Notas', help_text='Por ejemplo, horario o persona que responde.')


class PriceForm(TokenForm):
    price_list = forms.ModelChoiceField(queryset=PriceList.objects.filter(active=True), label='Lista de precios')
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True), label='Referencia')
    price = forms.DecimalField(min_value=0, max_digits=16, decimal_places=2, label='Precio de venta (COP)')
    valid_from = forms.DateField(widget=date_input(), initial=timezone.localdate, label='Vigente desde')


def _choices(kind, blank=True):
    rows = [(c.contable_id, c.name) for c in contable.catalog(kind)]
    return ([('', 'Seleccione…')] if blank else []) + rows


class SaleForm(TokenForm):
    party = forms.ModelChoiceField(queryset=Party.objects.filter(active=True), label='Tercero facturado')
    context = forms.ChoiceField(choices=Context.choices, label='Contexto de esta venta')
    price_list = forms.ModelChoiceField(queryset=PriceList.objects.filter(active=True), label='Lista de precios')
    issue_date = forms.DateField(widget=date_input(), initial=timezone.localdate, label='Fecha de la factura')
    seller_id = forms.ChoiceField(label='Vendedor en el sistema contable')
    condition = forms.ChoiceField(choices=Sale.CONDITIONS, label='Condición de pago')
    payment_type_id = forms.ChoiceField(label='Forma de pago en el sistema contable')
    due_date = forms.DateField(widget=date_input(), required=False, label='Vencimiento', help_text='Obligatorio en crédito.')
    cash_method = forms.ChoiceField(choices=[('', '—')] + Sale.CASH_METHODS, required=False, label='Medio del recaudo', help_text='Obligatorio en contado.')
    cash_reference = forms.CharField(max_length=80, required=False, label='Referencia del recaudo', help_text='Aprobación del datáfono o comprobante de la transferencia.')
    purchase_order = forms.CharField(max_length=20, required=False, label='Orden de compra')
    observations = forms.CharField(max_length=1000, required=False, widget=forms.Textarea(attrs={'rows': 2}), label='Observaciones de la factura',
                                   help_text='Se imprimen en la factura: no incluya datos personales.')
    beneficiary_name = forms.CharField(max_length=160, required=False, label='Persona que recibe')
    beneficiary_document = forms.CharField(max_length=30, required=False, label='Documento de quien recibe')
    beneficiary_relation = forms.CharField(max_length=60, required=False, label='Relación con el cliente', help_text='Por ejemplo, conductor o encargado de obra.')
    internal_note = forms.CharField(max_length=300, required=False, label='Nota interna', help_text='Uso interno; no se envía al sistema contable.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['seller_id'].choices = _choices('seller', blank=False)
        self.fields['payment_type_id'].choices = _choices('payment_fv')

    def clean(self):
        data = super().clean()
        if data.get('condition') == 'credit' and data.get('party') and not data.get('due_date') and data.get('issue_date'):
            data['due_date'] = data['issue_date'] + timedelta(days=data['party'].credit_days)
        return data


class SaleLineForm(LabeledInputs, StyledForm):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True), label='Referencia')
    quantity = forms.IntegerField(min_value=1, max_value=100000, label='Cantidad')
    unit_price = forms.DecimalField(min_value=0, max_digits=16, decimal_places=2, required=False, label='Precio unitario',
                                    widget=forms.NumberInput(attrs={'placeholder': 'De la lista', 'step': '0.01'}))
    price_reason = forms.CharField(max_length=200, required=False, label='Motivo si cambia el precio')
    tax_rate = forms.TypedChoiceField(choices=TAX_RATES, coerce=Decimal, initial='0', label='IVA')
    prior_movement = forms.ModelChoiceField(queryset=Movement.objects.none(), required=False, label='Entrega ya registrada')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        since = timezone.now() - timedelta(days=120)
        self.fields['prior_movement'].queryset = (Movement.objects.select_related('batch__product').filter(kind__in=('exit', 'dispatch'), created_at__gte=since,
                                                                                                           sale_stock__isnull=True).order_by('-created_at'))
        self.fields['prior_movement'].label_from_instance = lambda m: (f'{m.batch.product.name} · {-m.quantity} · lote {m.batch.lot} · '
                                                                       f'{timezone.localtime(m.created_at):%d/%m} · {m.get_kind_display()}')
        self.fields['prior_movement'].empty_label = 'Descontar del inventario al emitir'
        self.label_inputs()


def sale_line_formset(extra=2):
    return forms.formset_factory(SaleLineForm, formset=BaseLineFormSet, extra=extra, max_num=50, validate_max=True, can_delete=True)


class MailForm(TokenForm):
    to = forms.EmailField(label='Enviar a')


class OpeningForm(TokenForm):
    party = forms.ModelChoiceField(queryset=Party.objects.filter(active=True), label='Cliente')
    prefix = forms.CharField(max_length=10, label='Prefijo en el sistema contable', help_text='Por ejemplo FE.')
    number = forms.IntegerField(min_value=1, label='Número de la factura')
    quote = forms.IntegerField(min_value=1, initial=1, label='Cuota')
    issue_date = forms.DateField(widget=date_input(), label='Fecha de emisión')
    due_date = forms.DateField(widget=date_input(), label='Vencimiento')
    balance = forms.DecimalField(min_value=Decimal('0.01'), max_digits=16, decimal_places=2, label='Saldo a la fecha de corte')
    note = forms.CharField(max_length=300, required=False, label='Procedencia', help_text='Por ejemplo: informe de cartera del sistema contable a la fecha de corte.')


class BankAccountForm(TokenForm):
    name = forms.CharField(max_length=80, label='Nombre de la cuenta')
    bank = forms.CharField(max_length=80, label='Banco')
    last4 = forms.RegexField(regex=r'^\d{4}$', label='Últimos 4 dígitos', error_messages={'invalid': 'Escriba solo los últimos 4 dígitos.'})
    contable_payment_id = forms.ChoiceField(label='Forma de pago en recibos de caja del sistema contable')
    active = forms.BooleanField(required=False, initial=True, label='Activa')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['contable_payment_id'].choices = _choices('payment_rc')


class StatementForm(TokenForm):
    account = forms.ModelChoiceField(queryset=BankAccount.objects.filter(active=True), label='Cuenta receptora')
    file = forms.FileField(label='Extracto del banco (CSV o XLSX)', widget=forms.ClearableFileInput(attrs={'accept': '.csv,.txt,.xlsx'}),
                           error_messages={'required': 'Elija el archivo del extracto (CSV o XLSX) y vuelva a pulsar «Revisar archivo».'})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = list(self.fields['account'].queryset[:2])
        if len(accounts) == 1:
            # Con una sola cuenta activa no hay nada que elegir: llega seleccionada y sin la opción vacía.
            self.fields['account'].initial = accounts[0].pk
            self.fields['account'].empty_label = None
        self.fields['account'].error_messages['required'] = 'Elija la cuenta bancaria que recibió estos movimientos.'


class NoteForm(TokenForm):
    note = forms.CharField(max_length=300, label='Motivo')


class PayerForm(TokenForm):
    party = forms.ModelChoiceField(queryset=Party.objects.filter(active=True), label='Cliente')
    relation = forms.CharField(max_length=80, required=False, label='Relación', help_text='Por ejemplo: socio, tesorería o empresa del mismo grupo.')
