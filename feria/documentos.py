"""Documentos de demostración: facturas de proveedor por importar, un extracto bancario nuevo y los códigos de las
cajas para la recepción. Se regeneran en cada reinicio dentro de `documentos_demo/`.

Las facturas usan el formato sintético de `inventory/demo_documents.py` y corresponden a pedidos abiertos del
escenario. La hoja de códigos es un HTML autónomo (sin recursos externos) con cada código en barras y en texto.
"""
import html
from pathlib import Path
from django.conf import settings
from inventory import demo_documents as demo

FOLDER = 'documentos_demo'

# Code 128: anchos de barras y espacios de cada símbolo (0–105) y del símbolo de parada.
C128 = ('212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 '
        '221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 '
        '231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 '
        '314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 '
        '111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 '
        '114131 311141 411131 211412 211214 211232').split()
C128_STOP = '2331112'
START_B, START_C, CODE_B, FNC1 = 104, 105, 100, 102


def folder():
    """La carpeta del proyecto en la instalación normal; junto a los datos cuando se usa otra carpeta de datos."""
    base, private = Path(settings.BASE_DIR), Path(settings.PRIVATE_DIR)
    return base / FOLDER if private.resolve() == (base / 'datos').resolve() else private / FOLDER


def gs1_128_values(elements):
    """Símbolos de un GS1-128 para [(AI, valor, longitud fija)]: tramo numérico inicial en el juego C y el resto en el B."""
    values, in_c = [START_C, FNC1], True
    for n, (ai, value, fixed) in enumerate(elements):
        text = ai + value
        if in_c and text.isdigit() and len(text) % 2 == 0 and fixed:
            values += [int(text[i:i + 2]) for i in range(0, len(text), 2)]
            continue
        if in_c:
            values.append(CODE_B)
            in_c = False
        values += [ord(ch) - 32 for ch in text]
        if not fixed and n < len(elements) - 1:
            values.append(FNC1)
    check = (values[0] + sum(i * v for i, v in enumerate(values[1:], 1))) % 103
    return values + [check]


def barcode_svg(values, module=1.1, height=54):
    """SVG en línea con el símbolo Code 128 (geometría en atributos; sin estilos)."""
    x, quiet, bars = 10 * module, 10 * module, []
    for pattern in [C128[v] for v in values] + [C128_STOP]:
        for i, width in enumerate(pattern):
            w = int(width) * module
            if i % 2 == 0:
                bars.append(f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="{height}"/>')
            x += w
    total = x + quiet
    return (f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Código de barras de la caja" width="{total:.0f}" height="{height}" '
            f'viewBox="0 0 {total:.2f} {height}" fill="#000">' + ''.join(bars) + '</svg>')


def codes_html(payload):
    codes, day = payload['codes'], payload['reference_date']
    rows = []
    for n, c in enumerate(codes, 1):
        elements = [('01', c['gtin'], True), ('17', c['code'].split('(17)')[1][:6], True), ('10', c['lot'], False), ('21', c['code'].split('(21)')[1], False)]
        rows.append(
            '<section class="caja"><h2>Caja {n} de {total} · {product}</h2>'
            '<p>{units} unidades · lote {lot} · vence el {expiry}</p>{svg}'
            '<p class="codigo">{code}</p></section>'.format(
                n=n, total=len(codes), product=html.escape(f'{c["product"]} ({c["product_code"]})'), units=c['units'], lot=html.escape(c['lot']),
                expiry='/'.join(reversed(c['expiry'].split('-'))), svg=barcode_svg(gs1_128_values(elements)), code=html.escape(c['code'])))
    listing = '\n'.join(html.escape(c['code']) for c in codes)
    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Códigos de prueba para la recepción</title>
<style>
body {{ font-family: Arial, Helvetica, sans-serif; color: #1F2933; margin: 24px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
h2 {{ font-size: 15px; margin: 0 0 2px; }}
p {{ margin: 2px 0 8px; font-size: 13px; }}
.aviso {{ border: 1px solid #D9E0E6; padding: 8px 12px; margin: 12px 0 18px; }}
.caja {{ border-top: 1px solid #D9E0E6; padding: 14px 0; page-break-inside: avoid; }}
.codigo {{ font-size: 15px; letter-spacing: 0.5px; margin-top: 6px; }}
pre {{ font-size: 13px; border: 1px solid #D9E0E6; padding: 10px 12px; white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>Códigos de prueba para la recepción</h1>
<p>Factura {html.escape(payload['reception_invoice'])} de {html.escape(payload['reception_supplier'])} · preparada para el {day:%d/%m/%Y}</p>
<p class="aviso">Demostración con datos sintéticos. Cada bloque representa una caja de la factura. Con lector o cámara, lea el código de barras;
sin lector, copie el texto del código y péguelo en el campo «Código» de la pantalla de recepción. Cada caja se recibe una sola vez:
si se repite, la plataforma avisa y no la suma.</p>
{''.join(rows)}
<h2>Lista para copiar y pegar (un código por línea)</h2>
<pre>{listing}</pre>
</body>
</html>
'''


def write(payload):
    """Escribe los documentos y devuelve su índice (el que guarda `DemoState.cases['documentos']`)."""
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    for old in target.iterdir():
        if old.is_file():
            old.unlink()
    files = []
    for doc in payload.get('invoices', []):
        data = doc['data']
        name = f'factura_{data["number"]}.{doc["kind"]}'
        raw = demo.invoice_pdf(data) if doc['kind'] == 'pdf' else demo.invoice_xml(data)
        (target / name).write_bytes(raw)
        files.append({'name': name, 'kind': f'factura_{doc["kind"]}', 'title': f'Factura {data["number"]} de {data["supplier_name"]}',
                      'detail': f'Corresponde al pedido {doc["order_number"]}. Se carga en Compras → Facturas → Importar.',
                      'order_number': doc['order_number'], 'order_id': doc['order'], 'number': data['number'], 'supplier_nit': data['supplier_nit']})
    if payload.get('statement'):
        name = f'extracto_banco_{payload["reference_date"]:%Y-%m-%d}.csv'
        (target / name).write_bytes(payload['statement'])
        files.append({'name': name, 'kind': 'extracto', 'title': 'Extracto bancario del último día',
                      'detail': f'{payload["statement_rows"]} movimientos que aún no se han importado. Se carga en Finanzas → Banco → Importar extracto.'})
    if payload.get('codes'):
        (target / 'codigos_recepcion.html').write_text(codes_html(payload), encoding='utf-8')
        files.append({'name': 'codigos_recepcion.html', 'kind': 'codigos', 'title': 'Hoja imprimible de códigos de las cajas',
                      'detail': f'Códigos de las {len(payload["codes"])} cajas de la factura {payload["reception_invoice"]}, en barras y en texto.'})
        (target / 'codigos_recepcion.txt').write_text('\n'.join(c['code'] for c in payload['codes']) + '\n', encoding='utf-8')
        files.append({'name': 'codigos_recepcion.txt', 'kind': 'codigos', 'title': 'Códigos de las cajas en texto',
                      'detail': 'Un código por línea, para copiar y pegar en el campo del lector.'})
    return {'url': '/feria/documentos/', 'folder': FOLDER, 'files': files}
