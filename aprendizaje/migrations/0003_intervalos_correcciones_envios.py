"""Intervalo en el registro de pronósticos, correcciones del modelo de pagos y envío registrado en cobranza."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aprendizaje', '0002_initial'),
        ('cartera', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='forecastrecord',
            name='sigma',
            field=models.FloatField(default=0, verbose_name='error típico medido a una semana'),
        ),
        migrations.AddField(
            model_name='forecastrecord',
            name='low',
            field=models.FloatField(blank=True, null=True, verbose_name='límite inferior del intervalo del 80 %'),
        ),
        migrations.AddField(
            model_name='forecastrecord',
            name='high',
            field=models.FloatField(blank=True, null=True, verbose_name='límite superior del intervalo del 80 %'),
        ),
        migrations.AddField(
            model_name='forecastrecord',
            name='interval_kind',
            field=models.CharField(blank=True, max_length=12, verbose_name='origen del intervalo'),
        ),
        migrations.RenameField(
            model_name='matchfeedback',
            old_name='probability',
            new_name='score',
        ),
        migrations.AlterField(
            model_name='matchfeedback',
            name='score',
            field=models.FloatField(default=0, verbose_name='puntaje del modelo (sin calibrar)'),
        ),
        migrations.AddField(
            model_name='matchfeedback',
            name='rules_party',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='cartera.party'),
        ),
        migrations.AddField(
            model_name='matchfeedback',
            name='rules_correct',
            field=models.BooleanField(default=False, verbose_name='las reglas solas habrían sugerido al cliente correcto'),
        ),
        migrations.AddField(
            model_name='matchfeedback',
            name='voided_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='anulado el'),
        ),
        migrations.AddField(
            model_name='matchfeedback',
            name='void_reason',
            field=models.CharField(blank=True, max_length=120, verbose_name='motivo de la anulación'),
        ),
        migrations.AddField(
            model_name='collectionattempt',
            name='sent',
            field=models.BooleanField(default=True, verbose_name='envío registrado (simulado)'),
        ),
        migrations.AlterField(
            model_name='collectionattempt',
            name='outcome',
            field=models.CharField(choices=[('pending', 'Ventana abierta'), ('paid', 'Pagó en la ventana'), ('partial', 'Abonó en la ventana'),
                                            ('unpaid', 'Sin pago en la ventana'), ('skipped', 'Sin envío: no se evalúa')], default='pending', max_length=8),
        ),
    ]
