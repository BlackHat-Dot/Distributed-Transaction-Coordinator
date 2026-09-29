from django.apps import AppConfig


class CoordinatorConfig(AppConfig):
    name = 'coordinator'

    def ready(self):
        from . import checks  # noqa: F401
