"""Datos maestros del escenario sintético: equipo, proveedores, catálogo y clientes.

Todo es inventado para la demostración: empresas y personas ficticias, NIT en el rango 900.555.0xx, cédulas en
1.000.555.xxx, correos en dominios `.example` y teléfonos 300 555 01xx. Las descripciones de producto son
genéricas (sin marcas comerciales).
"""
from decimal import Decimal

COMPANY_NIT = '900555010'
LOCATION = 'Bodega principal'
LOCATION_CONTROLLED = 'Almacén ventilado'
BANK = {'name': 'Cuenta corriente principal', 'bank': 'Banco Ejemplo', 'last4': '4821', 'contable_payment_id': '301'}
OPENING_BALANCE = Decimal('164500000')

# rol → (usuario, nombres, apellidos, área, qué puede probar)
TEAM = [
    ('owner', 'direccion.demo', 'Beatriz', 'Londoño Arango', 'Dirección',
     'Ve todo: resultados, descuentos por confirmar, agentes y auditoría.'),
    ('admin', 'administracion.demo', 'Rodrigo', 'Santamaría Peña', 'Administración',
     'Opera todos los módulos y concede descuentos con su cupo mensual.'),
    ('inventory', 'inventario.demo', 'Leidy Johana', 'Cortés Vargas', 'Inventario',
     'Existencias por lote, solicitudes, reposición, pedidos y recepción.'),
    ('reception', 'recepcion.demo', 'Wilmer Andrés', 'Tapias Roa', 'Recepción',
     'Recibe la mercancía de una factura con el lector de códigos.'),
    ('finance', 'contabilidad.demo', 'Gloria Inés', 'Betancur Mesa', 'Contabilidad',
     'Cuentas por pagar, cartera, banco y conciliación; aprueba propuestas financieras.'),
    ('billing', 'facturacion.demo', 'Sebastián', 'Mora Giraldo', 'Facturación',
     'Registra ventas a precio de lista y las emite en el sistema contable simulado.'),
    ('requester', 'solicitante.demo', 'Fabián', 'Castaño Ruiz', 'Servicio técnico',
     'Pide insumos para su área y sigue el estado de sus solicitudes.'),
    ('requester', 'proyectos.demo', 'Natalia', 'Urrego Salazar', 'Proyectos',
     'Segundo solicitante: pide insumos para obras y adecuaciones internas.'),
]
ROLE_ORDER = ['owner', 'admin', 'inventory', 'reception', 'finance', 'billing', 'requester']

# clave → proveedor. lead = (días de entrega, variación); doc = formato del soporte de sus facturas.
SUPPLIERS = [
    {'key': 'quimicos', 'name': 'Químicos y Lubricantes del Valle S.A.S.', 'nit': '900555021', 'email': 'pedidos@quimicosdelvalle.example', 'phone': '300 555 0121',
     'payment_days': 30, 'base': 'subtotal', 'tiers': [(10, '3')], 'lead': (5, 1), 'doc': 'xml', 'prefix': 'QV'},
    {'key': 'adhesivos', 'name': 'Adhesivos Técnicos Andinos S.A.S.', 'nit': '900555022', 'email': 'ventas@adhesivosandinos.example', 'phone': '300 555 0122',
     'payment_days': 45, 'base': 'subtotal', 'tiers': [(15, '2')], 'lead': (8, 2), 'doc': 'xml', 'prefix': 'AT'},
    {'key': 'sellantes', 'name': 'Sellantes y Espumas de Occidente S.A.S.', 'nit': '900555023', 'email': 'pedidos@sellantesoccidente.example', 'phone': '300 555 0123',
     'payment_days': 30, 'base': 'subtotal', 'tiers': [(10, '3'), (20, '1.5')], 'lead': (6, 2), 'doc': 'pdf', 'prefix': 'SO'},
    {'key': 'recubrimientos', 'name': 'Recubrimientos Cordillera S.A.', 'nit': '900555024', 'email': 'comercial@recubrimientoscordillera.example', 'phone': '300 555 0124',
     'payment_days': 60, 'base': 'total', 'tiers': [(15, '2.5')], 'lead': (12, 2), 'doc': 'pdf', 'prefix': 'RC'},
    {'key': 'cintas', 'name': 'Cintas y Empaques Industriales Ltda.', 'nit': '900555025', 'email': 'pedidos@cintasyempaques.example', 'phone': '300 555 0125',
     'payment_days': 30, 'base': 'subtotal', 'tiers': [], 'lead': (4, 1), 'doc': 'xml', 'prefix': 'CE'},
    {'key': 'importadora', 'name': 'Importadora Ferrotécnica S.A.S.', 'nit': '900555026', 'email': 'ordenes@ferrotecnica.example', 'phone': '300 555 0126',
     'payment_days': 45, 'base': 'subtotal', 'tiers': [(10, '2')], 'lead': (14, 3), 'doc': 'manual', 'prefix': 'IF'},
]

# Patrones de salida semanal. `mean` = unidades por semana en régimen normal.
#   estable_alto / estable_medio / bajo: nivel con ruido
#   lluvias: sube en los dos periodos de lluvias (abril-mayo y octubre-noviembre)
#   fin_de_ano: sube de octubre a diciembre
#   creciente / decreciente: tendencia lineal entre `mean` y `end`
#   intermitente: muchas semanas en cero y pedidos por obra (`size` = tamaño típico)
#   excepciones: nivel estable con uno o dos pedidos extraordinarios por proyecto
#   agotados: nivel estable con entregas del proveedor retrasadas (semanas sin existencias)
#   sin_rotacion: deja de rotar unos cinco meses antes de la fecha de referencia
#   lanzamiento: se lanza unas cuatro semanas antes de la fecha de referencia
#   nueva: recién creada, sin movimientos
# (código, nombre, categoría, unidad, empaque, vida útil en meses, almacenamiento controlado, proveedor, costo, patrón, media, extras)
PRODUCTS = [
    ('MNT-001', 'Aflojatodo multiuso 400 ml', 'mantenimiento', 'unidad', 12, 24, True, 'quimicos', 11800, 'estable_alto', 46, {}),
    ('MNT-002', 'Desengrasante industrial 1 gal', 'mantenimiento', 'galón', 4, 24, False, 'quimicos', 28500, 'estable_medio', 14, {}),
    ('MNT-003', 'Limpiador de contactos 300 ml', 'mantenimiento', 'unidad', 12, 24, True, 'quimicos', 13900, 'estable_medio', 12, {}),
    ('MNT-004', 'Grasa de litio EP2 cartucho 400 g', 'mantenimiento', 'cartucho', 12, 30, False, 'quimicos', 9600, 'excepciones', 18, {'spikes': [(0.36, 96), (0.78, 120)]}),
    ('MNT-005', 'Lubricante de cadena 500 ml', 'mantenimiento', 'unidad', 12, 24, True, 'quimicos', 15200, 'decreciente', 17, {'end': 3}),
    ('MNT-006', 'Removedor de óxido 1 L', 'mantenimiento', 'unidad', 6, 18, False, 'quimicos', 17400, 'bajo', 4, {}),
    ('MNT-007', 'Cinta de teflón 12 m', 'mantenimiento', 'rollo', 50, 30, False, 'cintas', 900, 'estable_alto', 70, {}),
    ('MNT-008', 'Paños industriales x 50', 'mantenimiento', 'paquete', 10, 30, False, 'cintas', 14500, 'estable_medio', 10, {}),
    ('ADH-001', 'Epóxico bicomponente 50 ml', 'adhesivos', 'unidad', 12, 18, False, 'importadora', 12400, 'estable_medio', 15, {}),
    ('ADH-002', 'Adhesivo de contacto 1 gal', 'adhesivos', 'galón', 4, 12, True, 'adhesivos', 46000, 'agotados', 9, {'delays': [0.19, 0.62]}),
    ('ADH-003', 'Cianoacrilato 20 g', 'adhesivos', 'unidad', 24, 12, False, 'adhesivos', 4300, 'estable_alto', 40, {}),
    ('ADH-004', 'Adhesivo estructural acrílico 400 ml', 'adhesivos', 'cartucho', 6, 12, False, 'importadora', 38500, 'excepciones', 3, {'spikes': [(0.55, 30)], 'low': True}),
    ('ADH-005', 'Soldadura líquida para PVC 1/4 gal', 'adhesivos', 'unidad', 12, 18, True, 'adhesivos', 16800, 'estable_medio', 20, {}),
    ('ADH-006', 'Cinta doble faz 19 mm x 25 m', 'adhesivos', 'rollo', 24, 24, False, 'cintas', 7900, 'estable_medio', 16, {}),
    ('ADH-007', 'Adhesivo cerámico 25 kg', 'adhesivos', 'saco', 10, 9, False, 'adhesivos', 21500, 'intermitente', 6, {'size': 30, 'prob': 0.2}),
    ('ADH-008', 'Fijador de roscas 50 ml', 'adhesivos', 'unidad', 10, 24, False, 'importadora', 23900, 'sin_rotacion', 3, {}),
    ('SEL-001', 'Silicona neutra transparente 300 ml', 'sellantes', 'cartucho', 24, 18, False, 'sellantes', 9800, 'estable_alto', 55, {}),
    ('SEL-002', 'Silicona acética blanca 300 ml', 'sellantes', 'cartucho', 24, 18, False, 'sellantes', 7600, 'estable_alto', 38, {}),
    ('SEL-003', 'Sellante de poliuretano gris 310 ml', 'sellantes', 'cartucho', 12, 12, False, 'sellantes', 15900, 'lluvias', 13, {'peak': 2.1}),
    ('SEL-004', 'Sellador acrílico pintable 300 ml', 'sellantes', 'cartucho', 24, 24, False, 'sellantes', 5400, 'lluvias', 20, {'peak': 1.5}),
    ('SEL-005', 'Espuma expansiva 750 ml', 'sellantes', 'unidad', 12, 12, True, 'sellantes', 19800, 'creciente', 6, {'end': 30}),
    ('SEL-006', 'Formador de empaques 85 g', 'sellantes', 'unidad', 12, 24, False, 'importadora', 8900, 'bajo', 5, {}),
    ('SEL-007', 'Cinta butílica 10 m', 'sellantes', 'rollo', 12, 24, False, 'cintas', 26500, 'intermitente', 4, {'size': 18, 'prob': 0.22}),
    ('SEL-008', 'Masilla epóxica 100 g', 'sellantes', 'unidad', 24, 24, False, 'importadora', 6200, 'agotados', 12, {'delays': [0.33, 0.7]}),
    ('REC-001', 'Anticorrosivo gris 1 gal', 'recubrimientos', 'galón', 4, 24, True, 'recubrimientos', 52000, 'estable_medio', 10, {}),
    ('REC-002', 'Esmalte industrial azul 1 gal', 'recubrimientos', 'galón', 4, 24, True, 'recubrimientos', 58000, 'agotados', 6, {'delays': [0.47]}),
    ('REC-003', 'Pintura epóxica para piso (kit 1 gal)', 'recubrimientos', 'kit', 2, 12, True, 'recubrimientos', 138000, 'intermitente', 2, {'size': 10, 'prob': 0.18}),
    ('REC-004', 'Impermeabilizante acrílico (cuñete 5 gal)', 'recubrimientos', 'cuñete', 1, 18, False, 'recubrimientos', 185000, 'lluvias', 5, {'peak': 2.6}),
    ('REC-005', 'Barniz poliuretano 1/4 gal', 'recubrimientos', 'unidad', 6, 18, True, 'recubrimientos', 31000, 'bajo', 5, {}),
    ('REC-006', 'Imprimante universal 1 gal', 'recubrimientos', 'galón', 4, 24, False, 'recubrimientos', 39500, 'lanzamiento', 7, {}),
    ('REC-007', 'Pintura de demarcación amarilla 1 gal', 'recubrimientos', 'galón', 4, 18, True, 'recubrimientos', 64000, 'fin_de_ano', 5, {'peak': 3.0}),
    ('REC-008', 'Zinc en aerosol 400 ml', 'recubrimientos', 'unidad', 12, 24, True, 'importadora', 18600, 'nueva', 0, {}),
]
PRESENTATION = {'unidad': 'Caja x {n}', 'galón': 'Caja x {n} galones', 'cartucho': 'Caja x {n} cartuchos', 'rollo': 'Caja x {n} rollos', 'paquete': 'Bulto x {n} paquetes',
                'saco': 'Estiba parcial x {n} sacos', 'kit': 'Caja x {n} kits', 'cuñete': 'Cuñete individual'}
GENERIC = {'mantenimiento': 'Insumo de mantenimiento industrial', 'adhesivos': 'Adhesivo de uso profesional', 'sellantes': 'Sellante para juntas y acabados',
           'recubrimientos': 'Recubrimiento de protección y acabado'}

# Listas de precios por contexto: margen sobre el costo de compra.
PRICE_LISTS = [('MOSTRADOR', 'Mostrador', 'counter', Decimal('1.46')), ('EMPRESA', 'Empresa', 'company', Decimal('1.33')),
               ('CONTRATISTA', 'Contratista', 'contractor', Decimal('1.39'))]

# Clientes. behaviour: puntual | retraso | moroso | contado | nuevo. weekday 0 = lunes. every = cada cuántas semanas compra.
# cats = peso de compra por categoría (mnt, adh, sel, rec). payer = cómo llegan sus pagos al banco:
#   mention (cita la factura), holder (trae su nombre e identificación), third (transfiere otra persona), bare (solo la cuenta de origen).
CLIENTS = [
    {'key': 'piston', 'name': 'Taller Mecánico El Pistón S.A.S.', 'id_type': '31', 'ident': '900555031', 'context': 'company', 'credit': 30, 'behaviour': 'puntual',
     'weekday': 0, 'every': 1, 'cats': (5, 2, 1, 1), 'payer': 'mention', 'city': 'Pereira', 'contact': ('Andrés Felipe Rincón', 'email', 'pagos@tallerelpiston.example')},
    {'key': 'altos', 'name': 'Constructora Altos del Parque S.A.S.', 'id_type': '31', 'ident': '900555032', 'context': 'company', 'credit': 45, 'behaviour': 'retraso',
     'weekday': 1, 'every': 1, 'cats': (1, 3, 4, 4), 'payer': 'holder', 'po': True, 'city': 'Pereira', 'contact': ('Lina Marcela Guzmán', 'email', 'tesoreria@altosdelparque.example')},
    {'key': 'esquina', 'name': 'Ferretería La Esquina del Barrio', 'id_type': '31', 'ident': '900555033', 'context': 'company', 'credit': 30, 'behaviour': 'puntual',
     'weekday': 2, 'every': 1, 'cats': (3, 3, 3, 2), 'payer': 'third', 'third': ('LUZ DARY PATERNINA', '1000555201'), 'city': 'Dosquebradas',
     'contact': ('Luz Dary Paternina', 'chat', '300 555 0133')},
    {'key': 'sanrafael', 'name': 'Ferretería y Depósito San Rafael S.A.S.', 'id_type': '31', 'ident': '900555034', 'context': 'company', 'credit': 30, 'behaviour': 'retraso',
     'weekday': 3, 'every': 2, 'cats': (3, 2, 3, 3), 'payer': 'bare', 'city': 'Santa Rosa de Cabal', 'contact': ('Jairo Enrique Sarmiento', 'chat', '300 555 0134')},
    {'key': 'mirador', 'name': 'Conjunto Residencial Mirador del Río P.H.', 'id_type': '31', 'ident': '900555035', 'context': 'company', 'credit': 30, 'behaviour': 'puntual',
     'weekday': 4, 'every': 2, 'cats': (2, 1, 3, 3), 'payer': 'mention', 'city': 'Pereira', 'contact': ('Administración del conjunto', 'email', 'administracion@miradordelrio.example')},
    {'key': 'sabana', 'name': 'Conjunto Residencial Balcones de la Sabana P.H.', 'id_type': '31', 'ident': '900555036', 'context': 'company', 'credit': 30, 'behaviour': 'moroso',
     'weekday': 2, 'every': 2, 'cats': (2, 1, 3, 3), 'payer': 'holder', 'city': 'La Virginia', 'contact': ('Consejo de administración', 'email', 'consejo@balconesdelasabana.example')},
    {'key': 'pradera', 'name': 'Planta Procesadora La Pradera S.A.', 'id_type': '31', 'ident': '900555037', 'context': 'company', 'credit': 60, 'behaviour': 'puntual',
     'weekday': 1, 'every': 1, 'cats': (5, 2, 2, 3), 'payer': 'mention', 'po': True, 'city': 'Cartago', 'contact': ('Cuentas por pagar', 'email', 'proveedores@plantalapradera.example')},
    {'key': 'metal', 'name': 'Metalmecánica Industrial La Fragua S.A.S.', 'id_type': '31', 'ident': '900555038', 'context': 'company', 'credit': 45, 'behaviour': 'retraso',
     'weekday': 3, 'every': 2, 'cats': (4, 3, 1, 3), 'payer': 'holder', 'city': 'Pereira', 'contact': ('Mónica Tatiana Illera', 'email', 'contabilidad@metalfragua.example')},
    {'key': 'rueda', 'name': 'Taller de Latonería y Pintura Rueda Libre', 'id_type': '31', 'ident': '900555039', 'context': 'company', 'credit': 30, 'behaviour': 'moroso',
     'weekday': 4, 'every': 2, 'cats': (2, 2, 1, 4), 'payer': 'bare', 'city': 'Dosquebradas', 'contact': ('Édgar Alonso Camargo', 'chat', '300 555 0139')},
    {'key': 'vias', 'name': 'Constructora Obras y Vías del Café S.A.S.', 'id_type': '31', 'ident': '900555040', 'context': 'company', 'credit': 45, 'behaviour': 'puntual',
     'weekday': 0, 'every': 2, 'cats': (1, 3, 3, 5), 'payer': 'mention', 'po': True, 'city': 'Pereira', 'contact': ('Tesorería de obra', 'email', 'tesoreria@obrasyviasdelcafe.example')},
    {'key': 'edificios', 'name': 'Mantenimiento Integral de Edificios S.A.S.', 'id_type': '31', 'ident': '900555041', 'context': 'company', 'credit': 30, 'behaviour': 'retraso',
     'weekday': 4, 'every': 2, 'cats': (3, 2, 4, 2), 'payer': 'third', 'third': ('RUBEN DARIO ZULETA', '1000555202'), 'city': 'Pereira',
     'contact': ('Rubén Darío Zuleta', 'chat', '300 555 0141')},
    {'key': 'remache', 'name': 'Ferretería El Remache', 'id_type': '31', 'ident': '900555042', 'context': 'company', 'credit': 30, 'behaviour': 'nuevo',
     'weekday': 1, 'every': 1, 'cats': (3, 3, 3, 1), 'payer': 'holder', 'city': 'Manizales', 'contact': ('Yesid Alberto Polo', 'chat', '300 555 0142')},
    {'key': 'ospina', 'name': 'Julián Ospina Cárdenas', 'id_type': '13', 'ident': '1000555101', 'context': 'contractor', 'credit': 15, 'behaviour': 'puntual',
     'weekday': 0, 'every': 2, 'cats': (1, 3, 4, 2), 'payer': 'holder', 'city': 'Pereira', 'contact': ('Julián Ospina', 'chat', '300 555 0151')},
    {'key': 'quintero', 'name': 'Marcela Quintero Ríos', 'id_type': '13', 'ident': '1000555102', 'context': 'contractor', 'credit': 0, 'behaviour': 'contado',
     'weekday': 2, 'every': 2, 'cats': (1, 2, 4, 3), 'payer': 'holder', 'city': 'Dosquebradas', 'contact': ('Marcela Quintero', 'chat', '300 555 0152')},
    {'key': 'pulgarin', 'name': 'Hernán Darío Pulgarín Soto', 'id_type': '13', 'ident': '1000555103', 'context': 'contractor', 'credit': 15, 'behaviour': 'retraso',
     'weekday': 3, 'every': 2, 'cats': (2, 3, 3, 2), 'payer': 'bare', 'city': 'Pereira', 'contact': ('Hernán Pulgarín', 'chat', '300 555 0153')},
    {'key': 'becerra', 'name': 'Yolanda Becerra Niño', 'id_type': '13', 'ident': '1000555104', 'context': 'contractor', 'credit': 15, 'behaviour': 'moroso',
     'weekday': 1, 'every': 2, 'cats': (1, 2, 3, 3), 'payer': 'holder', 'city': 'Santa Rosa de Cabal', 'contact': ('Yolanda Becerra', 'chat', '300 555 0154')},
    {'key': 'montealegre', 'name': 'Óscar Iván Montealegre Duarte', 'id_type': '13', 'ident': '1000555105', 'context': 'counter', 'credit': 0, 'behaviour': 'contado',
     'weekday': 5, 'every': 2, 'cats': (3, 2, 2, 1), 'payer': 'holder', 'city': 'Pereira', 'contact': ('Óscar Montealegre', 'chat', '300 555 0155')},
    {'key': 'salgado', 'name': 'Diana Carolina Salgado Pinto', 'id_type': '13', 'ident': '1000555106', 'context': 'contractor', 'credit': 15, 'behaviour': 'nuevo',
     'weekday': 4, 'every': 1, 'cats': (1, 2, 3, 2), 'payer': 'holder', 'city': 'Pereira', 'contact': ('Diana Salgado', 'chat', '300 555 0156')},
    {'key': 'mostrador', 'name': 'Clientes varios de mostrador', 'id_type': '13', 'ident': '1000555100', 'context': 'counter', 'credit': 0, 'behaviour': 'contado',
     'weekday': 5, 'every': 1, 'cats': (3, 3, 3, 1), 'payer': 'holder', 'city': 'Pereira', 'contact': None},
]

REQUEST_PURPOSES = {
    'solicitante.demo': ['Mantenimiento preventivo de montacargas', 'Reparación de estantería en bodega 2', 'Servicio técnico en sitio a cliente', 'Ajuste de la banda del muelle de cargue',
                         'Mantenimiento de compresores', 'Reparación de puerta enrollable', 'Atención de garantía en taller'],
    'proyectos.demo': ['Adecuación de la sala de ventas', 'Sellado de cubierta del muelle', 'Demarcación de pasillos de bodega', 'Obra de ampliación del mezzanine',
                       'Impermeabilización de la terraza técnica', 'Reparación de pisos de la zona de despacho'],
}
REQUEST_CATEGORIES = {'solicitante.demo': ('mantenimiento', 'adhesivos', 'sellantes'), 'proyectos.demo': ('sellantes', 'recubrimientos', 'adhesivos')}
DISCOUNT_REASONS = ['Pedido por volumen para obra', 'Cliente con compras recurrentes: iguala cotización anterior', 'Cierre de negocio con pago anticipado',
                    'Liquidación de saldo de la referencia', 'Acuerdo comercial del trimestre']
DAMAGE_NOTES = [('damage', 'Envase roto durante el descargue'), ('damage', 'Caja aplastada en el almacenamiento: unidades con fuga'),
                ('storage', 'Cartuchos endurecidos por exposición al calor'), ('damage', 'Derrame al mover la estiba')]
