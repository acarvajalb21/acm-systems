from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from inventory.models import Invoice, with_payable_balance


class Command(BaseCommand):
    help = 'Verifica que ninguna factura de proveedor tenga saldo negativo y muestra el saldo abierto total.'

    def handle(self, *args, **options):
        validated = with_payable_balance(Invoice.objects.filter(review=Invoice.Review.VALIDATED).select_related('supplier'))
        problems = [f'Factura {inv.number} de {inv.supplier.name}: saldo negativo {inv.balance_due}.' for inv in validated.filter(balance_due__lt=0)]
        open_total = sum((i.balance_due for i in validated.filter(balance_due__gt=0)), Decimal(0))
        self.stdout.write(f'Saldo abierto con proveedores: {open_total:,.2f}')
        if problems:
            for line in problems:
                self.stdout.write(self.style.ERROR(line))
            raise CommandError(f'{len(problems)} factura(s) con saldo negativo.')
        self.stdout.write(self.style.SUCCESS('Sin diferencias: no hay saldos negativos.'))
