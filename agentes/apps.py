from django.apps import AppConfig


class AgentesConfig(AppConfig):
    name = 'agentes'
    verbose_name = 'Agentes de la plataforma'

    def ready(self):
        from . import signals  # noqa: F401  (disparadores por evento)
