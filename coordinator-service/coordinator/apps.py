# Imports Django's base class used to configure application metadata and manage startup lifecycles.
from django.apps import AppConfig

# Defines the custom configuration object specifically for this coordinator service application.
class CoordinatorConfig(AppConfig):
    # Specifies the Python import path name that uniquely identifies this app within the Django project.
    # this value should match the exact app folder name
    # ex: coordinator-service/coordinator
    name = 'coordinator'

    # Lifecycle hook executed automatically by Django once the entire application registry is fully loaded.
    def ready(self):

        # Imports system check rules at startup to validate configurations,
        # "noqa: F401": It tells linters (like Flake8) to ignore warning F401 ("imported but unused"),
        # since checks is imported purely to trigger its side-effect code on startup.
        from . import checks  # noqa: F401
