"""Tests for ``main.utils``."""

from django.test import RequestFactory, TestCase

from main.utils import get_client_ip


class ClientIpTest(TestCase):
    def test_first_forwarded_for_entry_wins(self):
        request = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_falls_back_to_remote_addr(self):
        request = RequestFactory().get("/")
        self.assertEqual(get_client_ip(request), "127.0.0.1")
