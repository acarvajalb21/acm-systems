"""Reinicio seguro de la demostración: solo borra la base local de la carpeta de datos y sus adjuntos.

También guarda, al terminar cada reinicio, una copia íntegra del escenario recién generado y la restaura en segundos
con las mismas guardas (`save_snapshot`, `restore`)."""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from django.conf import settings
from django.core.management import call_command
from django.db import connections

ALLOWED_NAMES = ('feria.sqlite3', 'test.sqlite3')
ACCESS_FILE = 'ACCESOS_DEMO.md'


class Unsafe(Exception):
    """El destino configurado no es la base local de la demostración."""


def check_target(database=None, private_dir=None, base_dir=None, env=None):
    """Devuelve la ruta de la base que se puede reiniciar, o lanza `Unsafe` con el motivo.

    Solo se admite SQLite, con el archivo `feria.sqlite3` (o `test.sqlite3` en pruebas) directamente dentro de
    la carpeta de datos, y esa carpeta debe ser `<proyecto>/datos` o la indicada en `FERIA_PRIVATE_DIR`.
    """
    database = database if database is not None else settings.DATABASES['default']
    private = Path(private_dir if private_dir is not None else settings.PRIVATE_DIR).resolve()
    base = Path(base_dir if base_dir is not None else settings.BASE_DIR).resolve()
    env = os.environ if env is None else env
    if database.get('ENGINE') != 'django.db.backends.sqlite3':
        raise Unsafe('La base configurada no es SQLite: el reinicio solo actúa sobre la base local de la demostración.')
    allowed = {base / 'datos'}
    if env.get('FERIA_PRIVATE_DIR'):
        allowed.add(Path(env['FERIA_PRIVATE_DIR']).resolve())
    if private not in allowed:
        raise Unsafe('La carpeta de datos no es «datos» del proyecto ni la indicada en FERIA_PRIVATE_DIR.')
    name = str(database.get('NAME') or '')
    if not name or name == ':memory:' or name.startswith('file:'):
        raise Unsafe('La base configurada no es un archivo de la carpeta de datos.')
    path = Path(name).resolve()
    if path.parent != private or path.name not in ALLOWED_NAMES:
        raise Unsafe('El archivo de la base no es «feria.sqlite3» dentro de la carpeta de datos.')
    return path


def reset(path):
    """Borra y vuelve a crear la base y la carpeta de adjuntos de esa misma carpeta de datos. Nada más."""
    connections.close_all()
    for suffix in ('', '-wal', '-shm', '-journal'):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()
    attachments = path.parent / 'adjuntos'
    if attachments.exists():
        shutil.rmtree(attachments)
    attachments.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_path(path)   # la copia del escenario anterior deja de corresponder a esta base
    for stale in (snapshot, _meta_path(snapshot)):
        if stale.exists():
            stale.unlink()
    call_command('migrate', verbosity=0, interactive=False)


def write_access(state, passwords):
    """Deja en la carpeta de datos las cuentas de demostración con las claves generadas en este reinicio."""
    from inventory.models import User
    from . import datos_base as B
    labels = dict(User.Role.choices)
    lines = ['# Accesos de la demostración', '',
             f'Escenario del {state.reference_date:%d/%m/%Y} · semilla {state.seed}. Las claves se generaron en este reinicio y cambian en el siguiente.',
             'Este archivo no sale de este equipo. En la pantalla de ingreso también se puede entrar a cada perfil con un clic.', '',
             '| Perfil | Área | Persona | Usuario | Clave |', '|---|---|---|---|---|']
    for role, username, first, last, area, _ in B.TEAM:
        lines.append(f'| {labels[role]} | {area} | {first} {last} | {username} | {passwords.get(username, "")} |')
    lines += ['', 'Dirección local: http://127.0.0.1:8790/', '']
    path = Path(settings.PRIVATE_DIR) / ACCESS_FILE
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


# ------------------------------------------------------------------------------------------------------------------
# Copia del escenario inicial y restauración rápida (segundos) entre una exposición y la siguiente.

SNAPSHOTS = {'feria.sqlite3': 'escenario_inicial.sqlite3', 'test.sqlite3': 'escenario_inicial.test.sqlite3'}
FILE_FIELDS = (('inventory', 'InvoiceFile', 'file'), ('inventory', 'PaymentFile', 'file'), ('cartera', 'Sale', 'pdf'), ('cartera', 'StatementImport', 'file'))


class NoSnapshot(Exception):
    """No hay copia del escenario inicial para esa base."""


def snapshot_path(db_path):
    """La copia vive junto a la base, en la misma carpeta de datos, con un nombre fijo por base."""
    db_path = Path(db_path)
    return db_path.parent / SNAPSHOTS[db_path.name]


def _meta_path(snapshot):
    return Path(snapshot).with_suffix('.json')


def _backup(source, target):
    """Copia íntegra con la API de respaldo de SQLite: consistente aunque otra conexión tenga abierta la base."""
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(target), timeout=30)
        try:
            src.backup(dst, sleep=0.1)
        finally:
            dst.close()
    finally:
        src.close()


def save_snapshot(db_path, state=None, snapshot=None):
    """Guarda la copia del escenario inicial con su huella y su fecha de referencia. Devuelve esos datos.

    Se llama con la base recién generada (o comprobando antes que su huella sigue siendo la de la generación)."""
    from .huella import table_summary
    from .models import DemoState
    db_path = Path(db_path)
    snapshot = Path(snapshot) if snapshot else snapshot_path(db_path)
    state = state if state is not None else DemoState.current()
    summary = table_summary()
    connections.close_all()
    partial = snapshot.with_name(snapshot.name + '.parcial')
    if partial.exists():
        partial.unlink()
    _backup(db_path, partial)
    os.replace(partial, snapshot)
    meta = {'huella': summary['huella'], 'filas': summary['filas'], 'saved_at': time.time(),
            'reference_date': state.reference_date.isoformat() if state.ready else None, 'seed': state.seed if state.ready else None,
            'months': state.months if state.ready else None, 'database': db_path.name}
    _meta_path(snapshot).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    return meta


def read_snapshot(db_path, snapshot=None):
    """Datos de la copia guardada; lanza `NoSnapshot` si falta la copia o su ficha."""
    snapshot = Path(snapshot) if snapshot else snapshot_path(db_path)
    meta_file = _meta_path(snapshot)
    if not snapshot.is_file() or not meta_file.is_file():
        raise NoSnapshot(f'No hay copia del escenario inicial ({snapshot.name}). Se crea al terminar «manage.py reiniciar_feria».')
    try:
        meta = json.loads(meta_file.read_text(encoding='utf-8'))
    except ValueError:
        raise NoSnapshot(f'La ficha de la copia ({meta_file.name}) no se puede leer. Genere de nuevo el escenario con «manage.py reiniciar_feria».')
    if not meta.get('huella') or 'saved_at' not in meta:
        raise NoSnapshot(f'La ficha de la copia ({meta_file.name}) está incompleta. Genere de nuevo el escenario con «manage.py reiniciar_feria».')
    return snapshot, meta


def _referenced_files():
    from django.apps import apps
    names = set()
    for app, model, field in FILE_FIELDS:
        names.update(n for n in apps.get_model(app, model).objects.exclude(**{field: ''}).values_list(field, flat=True) if n)
    return {n.replace('\\', '/') for n in names}


def clean_attachments(folder, saved_at):
    """Borra de `adjuntos/` los archivos creados después de la copia que la base restaurada ya no referencia."""
    folder = Path(folder)
    if folder.name != 'adjuntos' or not folder.is_dir():
        return 0
    keep, removed = _referenced_files(), 0
    for path in sorted(folder.rglob('*')):
        if path.is_file() and path.relative_to(folder).as_posix() not in keep and path.stat().st_mtime > saved_at:
            path.unlink()
            removed += 1
    return removed


def restore(db_path, snapshot=None, attachments=None):
    """Devuelve la base viva al escenario inicial. Funciona con el servidor encendido: no reemplaza el archivo, copia
    su contenido con la API de respaldo de SQLite, y las demás conexiones ven los datos restaurados en su siguiente
    consulta. Devuelve {'huella', 'expected', 'matches', 'removed', 'meta'}."""
    from .huella import table_summary
    db_path = Path(db_path)
    snapshot, meta = read_snapshot(db_path, snapshot)
    connections.close_all()
    _backup(snapshot, db_path)
    removed = clean_attachments(attachments if attachments is not None else db_path.parent / 'adjuntos', meta['saved_at'])
    invalidate_memory()
    huella = table_summary()['huella']
    return {'huella': huella, 'expected': meta['huella'], 'matches': huella == meta['huella'], 'removed': removed, 'meta': meta}


def invalidate_memory():
    """Olvida lo memorizado en este proceso que dependa de los datos. El servidor es otro proceso: su memoria de
    reposición se identifica con una huella de los datos que recalcula en cada consulta, así que se corrige sola."""
    try:
        from aprendizaje import reposicion
        reposicion.invalidate()
    except Exception:   # la restauración no depende de esa memoria
        pass
