import sys
from unittest import TestCase
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings



class TestG5StartupGuard(TestCase):
    def setUp(self):
        self.config = apps.get_app_config("inventory")
        self.old_argv = sys.argv.copy()

    def tearDown(self):
        sys.argv = self.old_argv

    @override_settings(DEBUG=False, IS_TESTING=True)
    def test_g5_raises_for_celery_worker(self):
        """G5: Celery worker startup under DEBUG=False must raise ImproperlyConfigured with bypass flags."""
        sys.argv = ["celery", "-A", "inventory_service", "worker"]
        with self.assertRaises(ImproperlyConfigured):
            self.config.ready()

    @override_settings(DEBUG=False, ALLOW_UNVERIFIED_IDENTITY_HEADERS=True)
    def test_g5_raises_for_uvicorn(self):
        """G5: Uvicorn startup under DEBUG=False must raise ImproperlyConfigured with bypass flags."""
        sys.argv = ["uvicorn", "inventory_service.asgi:application"]
        with self.assertRaises(ImproperlyConfigured):
            self.config.ready()

    @override_settings(DEBUG=False, ALLOW_UNAUTHENTICATED_RESET=True)
    def test_g5_raises_for_daphne(self):
        """G5: Daphne startup under DEBUG=False must raise ImproperlyConfigured with bypass flags."""
        sys.argv = ["daphne", "-b", "0.0.0.0", "-p", "8000", "inventory_service.asgi:application"]
        with self.assertRaises(ImproperlyConfigured):
            self.config.ready()

    @override_settings(DEBUG=False, IS_TESTING=True)
    def test_g5_raises_for_management_command_migrate(self):
        """G5: Management commands (e.g. migrate) under DEBUG=False must raise with bypass flags."""
        sys.argv = ["manage.py", "migrate"]
        with self.assertRaises(ImproperlyConfigured):
            self.config.ready()

    @override_settings(DEBUG=False, IS_TESTING=True)
    def test_g5_allows_django_test_runner(self):
        """G5: Django test runner (manage.py test) is exempt from startup raise."""
        sys.argv = ["manage.py", "test"]
        # Should not raise
        self.config.ready()

    @override_settings(DEBUG=True, IS_TESTING=True, ALLOW_UNVERIFIED_IDENTITY_HEADERS=True)
    def test_g5_allows_when_debug_true(self):
        """G5: Under DEBUG=True, development bypass flags are permitted."""
        sys.argv = ["manage.py", "runserver"]
        # Should not raise
        self.config.ready()
