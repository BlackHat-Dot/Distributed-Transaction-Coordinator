from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from coordinator.views import _get_caller, trigger_recovery_sweep


class StaffBypassB2Tests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.client = APIClient()

    @override_settings(IS_TESTING=False, DEBUG=True, ALLOW_DEV_STAFF_BYPASS=False)
    def test_dev_mode_without_flag_does_not_grant_staff(self):
        """F-17: When DEBUG=True but ALLOW_DEV_STAFF_BYPASS is False, caller is NOT staff."""
        request = self.factory.get("/transactions/")
        is_auth, uid, is_staff = _get_caller(request)
        self.assertTrue(is_auth)
        self.assertFalse(is_staff, "DEBUG=True should NOT grant is_staff=True when ALLOW_DEV_STAFF_BYPASS=False!")

    @override_settings(IS_TESTING=False, DEBUG=True, ALLOW_DEV_STAFF_BYPASS=True)
    def test_dev_mode_with_flag_grants_staff(self):
        """F-17: When DEBUG=True and ALLOW_DEV_STAFF_BYPASS is True, dev caller is granted staff."""
        request = self.factory.get("/transactions/")
        is_auth, uid, is_staff = _get_caller(request)
        self.assertTrue(is_auth)
        self.assertTrue(is_staff, "DEBUG=True with ALLOW_DEV_STAFF_BYPASS=True should grant is_staff=True.")

    @override_settings(IS_TESTING=False, DEBUG=False, ALLOW_DEV_STAFF_BYPASS=True)
    def test_prod_mode_never_grants_dev_staff_even_with_flag(self):
        """F-17: When DEBUG=False, ALLOW_DEV_STAFF_BYPASS must NEVER grant staff privileges."""
        request = self.factory.get("/transactions/")
        is_auth, uid, is_staff = _get_caller(request)
        self.assertFalse(is_staff, "ALLOW_DEV_STAFF_BYPASS must NEVER grant staff when DEBUG=False!")

    @override_settings(IS_TESTING=False, DEBUG=True, ALLOW_DEV_STAFF_BYPASS=False)
    def test_trigger_recovery_sweep_denied_in_dev_by_default(self):
        """F-17: trigger_recovery_sweep requires staff privileges even when DEBUG=True."""
        request = self.factory.post("/trigger-recovery-sweep/")
        resp = trigger_recovery_sweep(request)
        self.assertEqual(resp.status_code, 403, "trigger_recovery_sweep must require staff even in dev mode by default!")
