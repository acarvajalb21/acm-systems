"""Estado de la demostración: qué escenario sintético está cargado y dónde están sus casos preparados."""
from django.db import models


class DemoState(models.Model):
    """Fila única con el escenario vigente. La escribe `manage.py reiniciar_feria`; el resto solo la lee.

    `DemoState.current()` devuelve siempre un objeto (vacío si todavía no se generó el escenario), de modo que
    `DemoState.current().cases.get('faltante', {})` nunca falla.

    `cases` es un diccionario con claves estables. Cada sección trae identificadores y la URL lista para abrir.
    Una clave `action_id` apunta a la propuesta del agente (`agentes.AgentAction`) y puede ser None si el agente
    no dejó propuesta; las URL son rutas locales de la plataforma.

        reference_date            'aaaa-mm-dd' para la que se prepararon los casos
        accounts                  {rol: usuario} de las cuentas de demostración: owner, admin, inventory, reception,
                                  finance, billing, requester; y `requester_2` (segundo solicitante)
        account_names             {usuario: nombre para mostrar}
        faltante                  product_id, product_code, product_name, available, supplier_id, forecast_url,
                                  action_id, agents_url          (referencia de alta rotación bajo su punto de pedido)
        reposicion                action_id, supplier_id, product_ids, url   (propuesta de pedido lista para aprobar)
        vencimiento.por_vencer    batch_id, product_id, lot, days_left, units, url      (lote que vence en ≤ 30 días)
        vencimiento.vencido       batch_id, product_id, lot, units, action_id, url      (lote vencido con saldo)
        recepcion                 invoice_id, invoice_number, order_id, supplier_id, url, codes, codes_plain
                                  (factura validada por recibir; `codes` son los códigos de las cajas para pegar
                                  o teclear en el campo del lector, `codes_plain` los mismos con su descripción)
        recepcion.parcial         invoice_id, invoice_number, line_id, quantity, received, order_id, url
        pronto_pago               invoice_id, invoice_number, supplier_id, deadline, discount, action_id, url
        banco                     account_id, import_url, y tres ingresos por identificar:
        banco.claro               txn_id, party_id, receivable_id, action_id, url   (menciona la factura)
        banco.ambiguo             txn_id, party_ids, receivable_ids, url             (dos clientes con ese saldo)
        banco.sin_coincidencia    txn_id, url
        cartera.d15|d45|d90       party_id, receivable_id, days, action_id, url      (tres tramos de mora)
        sin_historial.lanzada     product_id, product_code, forecast_url             (unas cuatro semanas de historia)
        sin_historial.nueva       product_id, product_code, forecast_url             (sin movimientos)
        sin_rotacion              product_id, product_code, forecast_url             (sin salidas recientes y con existencias)
        solicitudes.con_reserva   request_id, product_id, action_id, url
        solicitudes.sin_existencias   request_id, product_id, url
        solicitudes.reserva_por_entregar   reservation_id, request_id, url
        descuentos                request_id, sale_id, admin_user_id, quota, url
        ventas.borrador           sale_id, url
        ventas.entrega_previa     sale_id, movement_id, url
        pedidos.en_transito       order_id, number, url
        pedidos.parcial           order_id, number, invoice_id, url
        pedidos.abiertos          [{order_id, number, supplier_id}]: pedidos enviados cuya factura está en los documentos
        documentos                url, folder, files: [{name, kind, title, detail}] y, en las facturas, además
                                  order_number, order_id, number y supplier_nit
                                  kind ∈ factura_pdf | factura_xml | extracto | codigos
        patrones                  {código de referencia: patrón de salidas simulado}

    `summary` guarda conteos y sumas por tabla (y una huella) para comprobar que dos generaciones con la misma
    semilla y fecha producen los mismos datos.
    """
    reference_date = models.DateField('fecha de referencia')
    seed = models.BigIntegerField('semilla')
    months = models.PositiveIntegerField('meses simulados', default=24)
    generated_at = models.DateTimeField('generado el')
    duration_seconds = models.FloatField('segundos de generación', default=0)
    cases = models.JSONField('casos preparados', default=dict, blank=True)
    summary = models.JSONField('resumen de conteos y sumas', default=dict, blank=True)

    class Meta:
        verbose_name = 'estado de la demostración'

    def __str__(self):
        return f'Escenario del {self.reference_date:%d/%m/%Y} · semilla {self.seed}'

    @classmethod
    def current(cls):
        """El escenario vigente; un objeto vacío (sin guardar) si aún no se ha generado."""
        return cls.objects.order_by('-pk').first() or cls(cases={}, summary={})

    @property
    def ready(self):
        return self.pk is not None

    def case(self, path, default=None):
        """Lectura con ruta de puntos: `state.case('recepcion.parcial.invoice_id')`."""
        node = self.cases or {}
        for part in path.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def demo_usernames(self):
        return sorted(set((self.cases or {}).get('accounts', {}).values()))
