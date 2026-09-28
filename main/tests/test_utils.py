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

    def test_cloudflares_connecting_ip_beats_the_edge_address(self):
        # Behind Cloudflare the first X-Forwarded-For entry is an edge
        # address shared by many visitors; CF-Connecting-IP is the visitor.
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP=" 198.51.100.4 ",
            HTTP_X_FORWARDED_FOR="172.68.0.1, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "198.51.100.4")
