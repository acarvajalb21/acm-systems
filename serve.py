"""Servidor local de la demostración; no expone el directorio del proyecto.

    python serve.py        escucha en 127.0.0.1, puerto 8790

FERIA_HOST y FERIA_PORT cambian la dirección de escucha (por ejemplo FERIA_HOST=0.0.0.0 junto con FERIA_HOSTS para
abrirla desde un celular de la misma red). Mientras el servidor está encendido, los agentes revisan su turno cada
minuto; se desactivan con FERIA_AGENTS=0.
"""
import os
import threading
import time

HOST = os.environ.get('FERIA_HOST', '127.0.0.1')
PORT = int(os.environ.get('FERIA_PORT', 8790))
THREADS = int(os.environ.get('FERIA_THREADS', 4))

from waitress import serve  # noqa: E402
from config.wsgi import application  # noqa: E402


def agents_loop(every=60, first_delay=20):
    """Despachador interno: corre los agentes con turno pendiente. El estado vive en la base, así que un reinicio
    no repite turnos ya hechos."""
    from django.db import close_old_connections
    from agentes.runner import run_due
    time.sleep(first_delay)
    while True:
        try:
            for run in run_due():
                if run.agent != 'avisos' or run.metrics.get('new'):
                    print(f'[agentes] {run.agent}: {run.summary or run.error}', flush=True)
        except Exception as e:  # el servidor sigue atendiendo aunque un turno falle
            print(f'[agentes] error en el despachador: {type(e).__name__}: {e}', flush=True)
        finally:
            close_old_connections()
        time.sleep(every)


if __name__ == '__main__':
    from django.conf import settings
    shown = 'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST
    print(f'{settings.APP_NAME} · demostración con datos sintéticos: abra la dirección local {shown}:{PORT} en el navegador.', flush=True)
    if os.environ.get('FERIA_AGENTS', '1') != '0':
        threading.Thread(target=agents_loop, name='agentes', daemon=True).start()
        print('Agentes: revisión de turnos cada minuto.', flush=True)
    serve(application, host=HOST, port=PORT, threads=THREADS, max_request_body_size=60 * 1024 * 1024)
