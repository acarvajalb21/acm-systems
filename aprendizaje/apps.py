from django.apps import AppConfig


class AprendizajeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'aprendizaje'
    verbose_name = 'Modelos que aprenden'

    def ready(self):
        from . import signals  # noqa: F401
