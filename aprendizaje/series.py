"""Pronóstico semanal para series cortas e intermitentes, en Python puro.

Qué hay aquí y cómo se clasifica cada pieza:

- **Métodos estadísticos** (no se entrenan): último valor, promedio, medias móviles, suavizado exponencial,
  Croston-SBA para consumo intermitente y el valor de la misma semana del año anterior.
- **Modelo entrenado**: UNA regresión ridge global, la misma para todas las referencias (`GlobalRidge`).
- La regla de reposición está en `reposicion.py`.

Diseño de validación (por referencia, en orden temporal, sobre las semanas utilizables):

    [ entrenamiento ........ | selección (origen móvil) | prueba final ]
      inicio del historial     hasta 26 semanas           últimas 8 semanas

- En cada origen `t` el pronóstico usa únicamente `y[:t]`; lo que ocurre después no interviene.
- El método se elige con el error de la ventana de **selección**. La ventana de **prueba** no se mira al elegir:
  su error es el que se informa como desempeño, con su `n`.
- Se evalúan dos horizontes: una semana y el acumulado de `H` semanas (el periodo de protección de la decisión).
- Con historial corto las ventanas se reducen de forma explícita (`design`); por debajo de `MIN_WEEKS` no se
  elige nada: se usa el método de respaldo y se marca «historial insuficiente».
- El intervalo del 80 % sale de los cuantiles empíricos 10 % y 90 % de los errores de selección del método
  elegido, para el mismo horizonte, y su cobertura se mide en la ventana de prueba. El intervalo siempre contiene
  al pronóstico: si los errores de selección quedaron todos de un lado (método sesgado), el límite de ese lado es
  el propio pronóstico. La cobertura se mide con esa misma regla.

Los errores de orígenes consecutivos a varias semanas se solapan: no son observaciones independientes y los `n`
que se muestran son conteos de orígenes, no tamaños de muestra efectivos.
"""
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from math import cos, pi, sin, sqrt
from statistics import fmean, pstdev

WEEK = timedelta(days=7)
YEAR = timedelta(days=364)       # 52 semanas exactas: la misma semana del año anterior
WARMUP = 4                       # semanas mínimas de entrenamiento antes del primer origen
TEST_WEEKS = 8                   # prueba final: últimas semanas utilizables
TEST_WEEKS_REDUCED = 4           # prueba reducida con historial corto
SELECT_MAX = 26                  # ventana de selección: hasta medio año antes de la prueba
MIN_WEEKS = 12                   # 4 de entrenamiento + 4 de selección + 4 de prueba
FULL_WEEKS = WARMUP + TEST_WEEKS + TEST_WEEKS   # desde aquí la prueba es de 8 semanas
MIN_SELECT_ORIGINS = 4           # orígenes mínimos para elegir con el horizonte acumulado
MIN_SEASONAL = 60                # semanas utilizables para considerar la misma semana del año anterior
MIN_QUANTILE = 12                # errores mínimos para usar un cuantil empírico en el inventario de seguridad
INTERVAL = (0.10, 0.90)          # intervalo del 80 %
INTERMITTENT = 0.3               # proporción de semanas en cero desde la que entra Croston-SBA
TOLERANCE = 1e-9


# ------------------------------------------------------------------------------ métodos estadísticos

def promedio(h):
    return fmean(h) if h else 0.0


def ingenuo(h):
    return float(h[-1]) if h else 0.0


def media_movil(k):
    def f(h):
        w = h[-k:]
        return fmean(w) if w else 0.0
    f.__name__ = f'media_movil_{k}'
    return f


def suavizado(alpha):
    def f(h):
        if not h:
            return 0.0
        start = min(4, len(h))
        level = fmean(h[:start])
        for x in h[start:]:
            level = alpha * x + (1 - alpha) * level
        return level
    f.__name__ = f'suavizado_{alpha}'
    return f


def sba(alpha):
    """Croston con la corrección de Syntetos-Boylan: separa tamaño e intervalo de las semanas con consumo."""
    def f(h):
        nz = [(i, x) for i, x in enumerate(h) if x > 0]
        if not nz:
            return 0.0
        size, interval, last = float(nz[0][1]), float(nz[0][0] + 1), nz[0][0]
        for i, x in nz[1:]:
            size = alpha * x + (1 - alpha) * size
            interval = alpha * (i - last) + (1 - alpha) * interval
            last = i
        return (1 - alpha / 2) * size / max(interval, 1.0)
    f.__name__ = f'sba_{alpha}'
    return f


@dataclass(frozen=True)
class Candidate:
    name: str
    label: str
    kind: str                 # 'estadistico' | 'entrenado'
    rate: object = None       # función de tasa semanal para los métodos de pronóstico plano

    @property
    def kind_label(self):
        return KINDS[self.kind]


KINDS = {'estadistico': 'Método estadístico', 'entrenado': 'Modelo entrenado'}
SEASONAL = Candidate('estacional_52', 'Misma semana del año anterior', 'estadistico')
RIDGE = Candidate('ridge_global', 'Regresión ridge global (modelo entrenado)', 'entrenado')
# El orden es de menor a mayor complejidad: en un empate gana el que está antes.
FLAT = [
    Candidate('promedio', 'Promedio de todo el historial', 'estadistico', promedio),
    Candidate('ingenuo', 'Último valor observado', 'estadistico', ingenuo),
    Candidate('media_movil_4', 'Media móvil de 4 semanas', 'estadistico', media_movil(4)),
    Candidate('media_movil_8', 'Media móvil de 8 semanas', 'estadistico', media_movil(8)),
    Candidate('suavizado_0.2', 'Suavizado exponencial suave (alfa 0,2)', 'estadistico', suavizado(0.2)),
    Candidate('suavizado_0.35', 'Suavizado exponencial (alfa 0,35)', 'estadistico', suavizado(0.35)),
    Candidate('suavizado_0.5', 'Suavizado exponencial reactivo (alfa 0,5)', 'estadistico', suavizado(0.5)),
    Candidate('sba_0.1', 'Croston-SBA (alfa 0,1), consumo intermitente', 'estadistico', sba(0.1)),
    Candidate('sba_0.2', 'Croston-SBA (alfa 0,2), consumo intermitente', 'estadistico', sba(0.2)),
    Candidate('sba_0.3', 'Croston-SBA (alfa 0,3), consumo intermitente', 'estadistico', sba(0.3)),
]
CANDIDATES = FLAT + [SEASONAL, RIDGE]
ORDER = [c.name for c in CANDIDATES]
LABELS = {c.name: c.label for c in CANDIDATES}
BY_NAME = {c.name: c for c in CANDIDATES}
METHODS = [c.rate for c in FLAT]     # compatibilidad: funciones de tasa de los métodos planos
FALLBACK = 'promedio'


# ---------------------------------------------------------------------------------------- métricas

@dataclass
class Metrics:
    """Error de un método en una ventana y un horizonte. Error = observado − pronóstico."""
    n: int = 0
    mae: float = None
    bias: float = None        # positivo: se pronosticó de menos
    rmse: float = None
    wape: float = None        # None = no definido (sin salidas en la ventana); no se promedia
    observed: float = 0.0
    abs_error: float = 0.0
    pairs: list = field(default_factory=list)   # (pronóstico, observado) por origen

    @property
    def errors(self):
        return [a - f for f, a in self.pairs]

    @property
    def wape_defined(self):
        return self.wape is not None

    @property
    def wape_pct(self):
        return self.wape * 100 if self.wape is not None else None


def metrics(pairs):
    if not pairs:
        return Metrics()
    errs = [a - f for f, a in pairs]
    total = sum(a for _, a in pairs)
    abs_sum = sum(abs(e) for e in errs)
    return Metrics(len(errs), abs_sum / len(errs), fmean(errs), sqrt(fmean(e * e for e in errs)), (abs_sum / total) if total > 0 else None,
                   total, abs_sum, list(pairs))


def quantile(values, q):
    """Cuantil empírico con interpolación lineal entre estadísticos de orden."""
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def coverage(pairs, low, high):
    """Cuántos observados cayeron dentro de pronóstico + [low, high]. Devuelve (dentro, n)."""
    inside = sum(1 for f, a in pairs if f + low - TOLERANCE <= a <= f + high + TOLERANCE)
    return inside, len(pairs)


def design(n):
    """Reparto temporal de `n` semanas utilizables, o None si no alcanza para elegir método."""
    if n < MIN_WEEKS:
        return None
    test = TEST_WEEKS if n >= FULL_WEEKS else TEST_WEEKS_REDUCED
    selection = min(SELECT_MAX, n - test - WARMUP)
    return {'train': n - test - selection, 'selection': selection, 'test': test, 'reduced': n < FULL_WEEKS, 'weeks': n}


def _floor(sigma, forecast):
    # Con pocas semanas o series planas el error medido puede ser cero: en la aproximación normal se conserva al
    # menos la variabilidad de un conteo con esa media (√media), para que la seguridad no sea nula con consumo.
    return max(sigma or 0.0, sqrt(max(forecast, 0.5)))


# ------------------------------------------------------------------------------- modelo entrenado

class GlobalRidge:
    """Regresión ridge global: un solo juego de coeficientes por horizonte para todas las referencias.

    Rasgos en el origen `t` de una referencia (todo escalado por la media de `y[:t]` de esa referencia): rezagos
    1 a 4, medias de 4, 8 y 13 semanas, valor de la misma semana del año anterior (y un indicador de si existe),
    seno y coseno de la época del año de la semana objetivo, y proporción de semanas en cero. Objetivo: consumo
    medio semanal de las `H` semanas siguientes, con la misma escala.

    Sin fuga temporal: los coeficientes que se usan en un origen se ajustan solo con filas (de cualquier
    referencia) cuya ventana objetivo terminó antes de la fecha de ese origen. Hiperparámetros fijos (no se
    ajustan con los datos): penalización `lam` y tope `CAP` de los valores escalados.
    """
    CAP = 6.0
    D = 13

    def __init__(self, panel, lam=1.0, min_rows=40):
        self.panel = {k: (list(map(float, y)), list(dates)) for k, (y, dates) in panel.items()}
        self.lam, self.min_rows = lam, min_rows
        self._index = {k: {d: i for i, d in enumerate(dates)} for k, (_, dates) in self.panel.items()}
        self._betas = {}     # H → [(fecha de corte, coeficientes o None, filas)], creciente en la fecha
        self._cuts = {}
        self.rows_used = {}  # H → filas de entrenamiento con todo el historial

    def features(self, key, t, target_date):
        y, dates = self.panel[key]
        if t < WARMUP:
            return None, 0.0
        past = y[:t]
        m = fmean(past)
        if m <= 0:
            return None, 0.0
        cap = self.CAP

        def sc(v):
            return min(v / m, cap)

        ly_i = self._index[key].get(target_date - YEAR)
        has_ly = ly_i is not None and ly_i < t
        angle = 2 * pi * (target_date.timetuple().tm_yday / 365.25)
        x = [1.0, sc(y[t - 1]), sc(y[t - 2]), sc(y[t - 3]), sc(y[t - 4]), sc(fmean(y[t - 4:t])), sc(fmean(y[max(0, t - 8):t])),
             sc(fmean(y[max(0, t - 13):t])), sc(y[ly_i]) if has_ly else 0.0, 1.0 if has_ly else 0.0, sin(angle), cos(angle),
             sum(1 for v in past if v == 0) / t]
        return x, m

    def _fit_all(self, H):
        """Coeficientes para cada fecha de corte posible, acumulando las filas en orden temporal (una sola pasada)."""
        rows, cuts = [], set()
        for key, (y, dates) in self.panel.items():
            for t in range(WARMUP, len(y) - H + 1):
                x, m = self.features(key, t, dates[t])
                if x is not None:
                    rows.append((dates[t + H - 1], x, min(fmean(y[t:t + H]) / m, self.CAP)))
            for d in dates:
                cuts.add(d + WEEK)
        rows.sort(key=lambda r: r[0])
        d = self.D
        xtx = [[0.0] * d for _ in range(d)]
        xty = [0.0] * d
        out, i = [], 0
        for cut in sorted(cuts):
            while i < len(rows) and rows[i][0] < cut:
                _, x, target = rows[i]
                for a in range(d):
                    xa = x[a]
                    if xa:
                        row = xtx[a]
                        for b in range(a, d):
                            row[b] += xa * x[b]
                        xty[a] += xa * target
                i += 1
            out.append((cut, self._solve(xtx, xty) if i >= self.min_rows else None, i))
        self._betas[H] = out
        self._cuts[H] = [c for c, _, _ in out]
        self.rows_used[H] = len(rows)

    def _solve(self, xtx, xty):
        d = self.D
        a = [[xtx[min(i, j)][max(i, j)] for j in range(d)] + [xty[i]] for i in range(d)]
        for i in range(1, d):          # el término independiente no se penaliza
            a[i][i] += self.lam
        for col in range(d):
            piv = max(range(col, d), key=lambda r: abs(a[r][col]))
            if abs(a[piv][col]) < 1e-12:
                return None
            a[col], a[piv] = a[piv], a[col]
            pivot = a[col]
            for r in range(col + 1, d):
                factor = a[r][col] / pivot[col]
                if factor:
                    row = a[r]
                    for c in range(col, d + 1):
                        row[c] -= factor * pivot[c]
        beta = [0.0] * d
        for i in range(d - 1, -1, -1):
            beta[i] = (a[i][d] - sum(a[i][j] * beta[j] for j in range(i + 1, d))) / a[i][i]
        return beta

    def beta(self, H, cutoff):
        """Coeficientes ajustados con las filas cuya ventana objetivo terminó antes de `cutoff` (None si no alcanzan)."""
        if H not in self._betas:
            self._fit_all(H)
        i = bisect_right(self._cuts[H], cutoff) - 1
        return self._betas[H][i][1] if i >= 0 else None

    def predict(self, key, t, H, target_date, cutoff):
        """Pronóstico acumulado de `H` semanas desde el origen `t`, o None si el modelo aún no puede opinar."""
        if key not in self.panel:
            return None
        beta = self.beta(H, cutoff)
        if beta is None:
            return None
        x, m = self.features(key, t, target_date)
        if x is None:
            return 0.0 if t >= WARMUP else None
        return max(sum(b * v for b, v in zip(beta, x)), 0.0) * m * H


# ------------------------------------------------------------------------------ evaluación y elección

def _forecaster(cand, y, dates, key, ridge):
    """f(t, h, fechas objetivo, fecha del origen) → pronóstico acumulado de h semanas usando solo y[:t]."""
    if cand.rate is not None:
        cache = {}

        def flat(t, h, targets, cutoff):
            if t not in cache:
                cache[t] = max(cand.rate(y[:t]), 0.0)
            return cache[t] * h
        return flat
    if cand is SEASONAL:
        index = {d: i for i, d in enumerate(dates)}

        def seasonal(t, h, targets, cutoff):
            total = 0.0
            for d in targets:
                found = [y[i] for i in (index.get(d - YEAR + k * WEEK) for k in (0, -1, 1)) if i is not None and i < t]
                total += found[0] if found else (fmean(y[:t]) if t else 0.0)
            return total
        return seasonal

    def trained(t, h, targets, cutoff):
        return ridge.predict(key, t, h, targets[0], cutoff)
    return trained


def _candidates(n, zeros, ridge):
    out = [c for c in FLAT if not c.name.startswith('sba') or zeros >= INTERMITTENT]
    if n >= MIN_SEASONAL:
        out.append(SEASONAL)
    if ridge is not None:
        out.append(RIDGE)
    return out


def _synthetic_dates(n):
    start = date(2001, 1, 1)   # lunes; solo importa el orden cuando no se dan fechas reales
    return [start + i * WEEK for i in range(n)]


def fit(y, dates=None, horizon=1, ridge=None, key=None, now_date=None):
    """Elige el método para una serie semanal y devuelve pronóstico, intervalo y evidencia.

    `y`: semanas utilizables, la más reciente al final. `dates`: lunes de cada semana. `horizon`: semanas del
    periodo de protección. `ridge`: modelo global ya construido con el panel (o None). `now_date`: lunes de la
    primera semana que se pronostica.
    """
    y = [float(v) for v in y]
    n = len(y)
    dates = list(dates) if dates is not None else _synthetic_dates(n)
    now_date = now_date or ((dates[-1] + WEEK) if dates else date(2001, 1, 1))
    H = max(int(horizon), 1)
    zeros = sum(1 for v in y if v == 0) / n if n else 1.0
    d = design(n)
    base = {'weeks': n, 'horizon': H, 'intermittent': zeros >= INTERMITTENT, 'zeros_share': zeros, 'design': d}
    if d is None:
        rate = max(promedio(y), 0.0)
        sigma = _floor(pstdev(y) if n > 1 else 0.0, rate)
        return {**base, 'enough': False, 'method': FALLBACK, 'label': LABELS[FALLBACK], 'kind': 'estadistico', 'forecast': rate, 'forecast_week': rate,
                'forecast_h': rate * H, 'sigma': sigma, 'selection_horizon': None, 'selection': {}, 'test': {}, 'evaluations': [],
                'interval_week': None, 'interval_h': None, 'coverage_week': None, 'coverage_h': None, 'errors_h': [], 'errors_week': [],
                'mae': None, 'mae_base': None, 'improvement': None, 'ridge_note': '',
                'note': f'Historial insuficiente ({n} semana(s) utilizable(s); se necesitan {MIN_WEEKS} para elegir y probar un método): se usa el promedio como respaldo.'}
    sel_start, test_start = d['train'], n - d['test']
    horizons = [1] if H == 1 else [1, H]
    table = []
    # Qué candidatos compiten se decide sin mirar la ventana de prueba: la proporción de ceros es la anterior a ella.
    zeros_before = sum(1 for v in y[:test_start] if v == 0) / test_start
    for cand in _candidates(n, zeros_before, ridge):
        f = _forecaster(cand, y, dates, key, ridge)
        row = {'cand': cand, 'method': cand.name, 'label': cand.label, 'kind': cand.kind, 'kind_label': cand.kind_label, 'available': True, 'note': '',
               'sel': {}, 'test': {}, 'f': f}
        for h in horizons:
            for window, origins in (('sel', range(sel_start, test_start - h + 1)), ('test', range(test_start, n - h + 1))):
                pairs = []
                for t in origins:
                    v = f(t, h, dates[t:t + h], dates[t - 1] + WEEK)
                    if v is None:
                        row['available'] = False
                        break
                    pairs.append((v, sum(y[t:t + h])))
                row[window][h] = metrics(pairs if row['available'] else [])
        if not row['available']:
            row['note'] = 'Sin datos de entrenamiento suficientes antes de la ventana de selección: no compite en esta referencia.'
        table.append(row)
    usable = [r for r in table if r['available']]
    h_sel = H if usable[0]['sel'][H].n >= MIN_SELECT_ORIGINS else 1
    ranked = sorted(usable, key=lambda r: (round(r['sel'][h_sel].mae, 6), ORDER.index(r['method'])))
    best, week_fc, h_fc = None, None, None
    for r in ranked:       # el primero que también puede pronosticar hoy (el modelo entrenado siempre debería poder)
        week_fc = r['f'](n, 1, [now_date], now_date)
        h_fc = r['f'](n, H, [now_date + k * WEEK for k in range(H)], now_date)
        if week_fc is not None and h_fc is not None:
            best = r
            break
    best['chosen'] = True
    reference = next(r for r in usable if r['method'] == FALLBACK)
    out = {**base, 'enough': True, 'method': best['method'], 'label': best['label'], 'kind': best['kind'], 'selection_horizon': h_sel,
           'forecast_week': max(week_fc, 0.0), 'forecast_h': max(h_fc, 0.0), 'forecast': max(h_fc, 0.0) / H, 'note': ''}
    for name, h in (('week', 1), ('h', H)):
        errs = best['sel'][h].errors
        out[f'errors_{name}'] = errs
        if errs:
            # Cuantiles empíricos; acotados para que el intervalo contenga al pronóstico aunque el método esté sesgado.
            lo, hi = min(quantile(errs, INTERVAL[0]), 0.0), max(quantile(errs, INTERVAL[1]), 0.0)
            fc = out['forecast_week'] if h == 1 else out['forecast_h']
            inside, m = coverage(best['test'][h].pairs, lo, hi)
            out[f'interval_{name}'] = {'low': max(fc + lo, 0.0), 'high': max(fc + hi, 0.0), 'q_low': lo, 'q_high': hi, 'n_errors': len(errs),
                                       'few': len(errs) < MIN_QUANTILE}
            out[f'coverage_{name}'] = {'inside': inside, 'n': m, 'share': (inside / m * 100) if m else None, 'few': m < TEST_WEEKS} if m else None
        else:
            out[f'interval_{name}'] = out[f'coverage_{name}'] = None
    out['selection'] = {'week': best['sel'][1], 'h': best['sel'][H]}
    out['test'] = {'week': best['test'][1], 'h': best['test'][H]}
    out['sigma'] = _floor(best['sel'][1].rmse, out['forecast_week'])
    out['mae'], out['mae_base'] = best['sel'][h_sel].mae, reference['sel'][h_sel].mae
    t_best, t_ref = best['test'][h_sel], reference['test'][h_sel]
    out['improvement'] = (1 - t_best.mae / t_ref.mae) * 100 if t_best.n and t_ref.mae else (0.0 if t_best.n else None)
    ridge_row = next((r for r in table if r['method'] == RIDGE.name), None)
    if ridge_row is None:
        out['ridge_note'] = ''
    elif not ridge_row['available']:
        out['ridge_note'] = 'El modelo entrenado no compitió: no tenía datos de entrenamiento suficientes antes de la ventana de selección.'
    elif ridge_row is best:
        out['ridge_note'] = 'El modelo entrenado tuvo el menor error de selección y es el que se usa.'
    else:
        out['ridge_note'] = 'El modelo entrenado no superó a un método sencillo en la ventana de selección: se usa el sencillo.'
    for r in table:
        r.pop('f')
        r.setdefault('chosen', False)
        r['sel_week'], r['sel_h'], r['test_week'], r['test_h'] = r['sel'].get(1), r['sel'].get(H), r['test'].get(1), r['test'].get(H)
    out['evaluations'] = sorted(table, key=lambda r: (not r['available'], round(r['sel'][h_sel].mae, 6) if r['available'] else 0, ORDER.index(r['method'])))
    return out


def choose(h, min_weeks=MIN_WEEKS):
    """Compatibilidad: elige el método de una serie sin fechas ni modelo global (horizonte de una semana)."""
    return fit(h)
