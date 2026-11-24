"""Estado de los modelos que aprenden con los datos de la plataforma.

Tres aprendizajes, todos con datos propios y sin servicios externos:
- Reposición: para cada referencia se elige un método de pronóstico con una ventana de selección y se guarda el
  pronóstico semanal, con su intervalo, antes de conocer la semana, para compararlo después con lo observado
  (`ForecastRecord`).
- Pagos: un modelo en línea aprende de cada ingreso bancario identificado por contabilidad qué señales (menciones,
  titulares, valores, palabras del extracto) llevan a cada cliente (`LearnedModel`, `MatchFeedback`). Si una
  identificación se corrige, su ejemplo se anula y el modelo se reconstruye sin él.
- Cobranza: cada recordatorio aprobado queda como un intento con su estrategia (tono y momento). Solo los que
  tuvieron envío registrado (simulado) se evalúan, y únicamente al cumplirse la ventana (`CollectionAttempt`).
"""
from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL


class LearnedModel(models.Model):
    """Pesos y metadatos de un modelo en línea (hoy, el de pagos)."""
    key = models.CharField(max_length=40, unique=True)
    weights = models.JSONField(default=dict, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class ForecastRecord(models.Model):
    """Pronóstico de consumo de una referencia para una semana, hecho antes de que la semana termine."""
    product = models.ForeignKey('inventory.Product', on_delete=models.CASCADE, related_name='forecasts')
    week_start = models.DateField('semana (lunes)')
    forecast = models.FloatField('unidades previstas')
    sigma = models.FloatField('error típico medido a una semana', default=0)
    low = models.FloatField('límite inferior del intervalo del 80 %', null=True, blank=True)
    high = models.FloatField('límite superior del intervalo del 80 %', null=True, blank=True)
    interval_kind = models.CharField('origen del intervalo', max_length=12, blank=True)   # 'empirico' | 'aproximado'
    method = models.CharField('método', max_length=30)
    made_on = models.DateField('hecho el')
    actual = models.IntegerField('unidades reales', null=True, blank=True)
    stockout = models.BooleanField('semana sin existencias', default=False)

    class Meta:
        ordering = ['-week_start']
        constraints = [models.UniqueConstraint(fields=['product', 'week_start'], name='one_forecast_per_week')]

    @property
    def error(self):
        return None if self.actual is None else self.actual - self.forecast

    @property
    def covered(self):
        """Si lo observado cayó dentro del intervalo guardado (None: semana abierta, censurada o sin intervalo)."""
        if self.actual is None or self.stockout or self.low is None or self.high is None:
            return None
        return self.low - 1e-9 <= self.actual <= self.high + 1e-9


class MatchFeedback(models.Model):
    """Lo que el modelo de pagos predijo antes de aprender de cada identificación (evaluación secuencial).

    `score` es el puntaje del modelo para su primer candidato (0 a 1, sin calibrar: no es una probabilidad).
    `rules_*` guarda lo que habrían sugerido las reglas solas con la misma información, para comparar.
    Un ejemplo anulado (`voided_at`) dejó de ser verdadero: no entra al modelo ni a la evaluación."""
    transaction = models.ForeignKey('cartera.BankTransaction', on_delete=models.CASCADE, related_name='match_feedback')
    predicted_party = models.ForeignKey('cartera.Party', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    score = models.FloatField('puntaje del modelo (sin calibrar)', default=0)
    chosen_party = models.ForeignKey('cartera.Party', on_delete=models.CASCADE, related_name='+')
    correct = models.BooleanField(default=False)
    candidates = models.PositiveIntegerField(default=0)
    trained = models.BooleanField('el modelo ya tenía suficientes ejemplos', default=False)
    rules_party = models.ForeignKey('cartera.Party', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    rules_correct = models.BooleanField('las reglas solas habrían sugerido al cliente correcto', default=False)
    voided_at = models.DateTimeField('anulado el', null=True, blank=True)
    void_reason = models.CharField('motivo de la anulación', max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class CollectionAttempt(models.Model):
    """Recordatorio de cobro preparado con una estrategia.

    `sent` distingue el mensaje solo preparado del envío registrado (en la demostración, un envío simulado).
    Solo los intentos con envío registrado se evalúan, y solo al cumplirse la ventana: hubo pago, abono o no
    hubo pago dentro de ella. Es una coincidencia temporal, no una prueba de que el recordatorio causara el pago."""
    TONES = [('cordial', 'Cordial'), ('formal', 'Formal'), ('firme', 'Firme')]
    OUTCOMES = [('pending', 'Ventana abierta'), ('paid', 'Pagó en la ventana'), ('partial', 'Abonó en la ventana'), ('unpaid', 'Sin pago en la ventana'),
                ('skipped', 'Sin envío: no se evalúa')]
    party = models.ForeignKey('cartera.Party', on_delete=models.CASCADE, related_name='collection_attempts')
    action = models.ForeignKey('agentes.AgentAction', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    segment = models.CharField('tipo de cliente', max_length=40)
    tone = models.CharField('tono', max_length=10, choices=TONES)
    timing_days = models.PositiveIntegerField('días de mora al recordar')
    channel = models.CharField('canal', max_length=10, blank=True)
    receivables = models.JSONField(default=list)
    amount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    sent_at = models.DateTimeField()
    sent = models.BooleanField('envío registrado (simulado)', default=True)
    outcome = models.CharField(max_length=8, choices=OUTCOMES, default='pending')
    resolved_at = models.DateTimeField(null=True, blank=True)
    days_to_pay = models.IntegerField(null=True, blank=True)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ['-sent_at']
        indexes = [models.Index(fields=['segment', 'outcome'])]

    @property
    def success(self):
        return self.outcome in ('paid', 'partial')
