"""Tests for ``main.utils``."""

from django.test import RequestFactory, TestCase, override_settings

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
        # address (172.68.0.1 is in 172.64.0.0/13) shared by many visitors;
        # CF-Connecting-IP is the visitor.
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP=" 198.51.100.4 ",
            HTTP_X_FORWARDED_FOR="172.68.0.1, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "198.51.100.4")

    def test_cloudflare_ipv6_edge_is_trusted_too(self):
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="2001:db8::5",
            HTTP_X_FORWARDED_FOR="2606:4700::1, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "2001:db8::5")

    def test_connecting_ip_from_a_non_cloudflare_peer_is_ignored(self):
        # Anyone who reaches the ingress directly can send CF-Connecting-IP;
        # believing it would let them pick a fresh throttle bucket per
        # request, or spend someone else's.
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="198.51.100.4",
            HTTP_X_FORWARDED_FOR="203.0.113.9, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "203.0.113.9")

    def test_connecting_ip_is_ignored_without_a_forwarded_peer(self):
        # No X-Forwarded-For: the peer is REMOTE_ADDR, which is not Cloudflare.
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="198.51.100.4")
        self.assertEqual(get_client_ip(request), "127.0.0.1")

    def test_malformed_peer_does_not_raise_or_unlock_the_header(self):
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="198.51.100.4",
            HTTP_X_FORWARDED_FOR="not-an-address, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "not-an-address")

    def test_blank_connecting_ip_falls_back_to_the_peer(self):
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="  ",
            HTTP_X_FORWARDED_FOR="172.68.0.1, 10.244.0.9")
        self.assertEqual(get_client_ip(request), "172.68.0.1")

    @override_settings(CLOUDFLARE_IP_RANGES=["192.0.2.0/24"])
    def test_configured_ranges_replace_the_defaults(self):
        trusted = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="198.51.100.4",
            HTTP_X_FORWARDED_FOR="192.0.2.10, 10.244.0.9")
        self.assertEqual(get_client_ip(trusted), "198.51.100.4")

        # A default Cloudflare range that the setting no longer lists.
        untrusted = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="198.51.100.4",
            HTTP_X_FORWARDED_FOR="172.68.0.1, 10.244.0.9")
        self.assertEqual(get_client_ip(untrusted), "172.68.0.1")

    @override_settings(
        CLOUDFLARE_IP_RANGES=["not-a-network", "", "192.0.2.0/24"])
    def test_malformed_range_entries_are_skipped(self):
        request = RequestFactory().get(
            "/", HTTP_CF_CONNECTING_IP="198.51.100.4",
            HTTP_X_FORWARDED_FOR="192.0.2.10, 10.244.0.9")
        with self.assertLogs("main.utils", level="WARNING"):
            self.assertEqual(get_client_ip(request), "198.51.100.4")
