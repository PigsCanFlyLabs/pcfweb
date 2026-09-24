"""The admin's guard rails: which Order status moves are allowed by hand, and
which models are records that the admin shows but never edits."""

from django.contrib.auth.models import User
from django.test import override_settings

from main.models import MailingListDelivery, Order, OrderItem
from main.tests.base import OrderTestBase


@override_settings(THUMBNAIL_DEBUG=False)
class OrderStatusGuardTest(OrderTestBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            "staff", "staff@example.com", "hunter2hunter2",
            is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def change_status(self, order, status):
        return self.client.post(
            f"/timbit/admin/main/order/{order.pk}/change/",
            {"status": status,
             "items-TOTAL_FORMS": "0", "items-INITIAL_FORMS": "0",
             "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000"})

    def order_in(self, status):
        order = self.place_order()
        Order.objects.filter(pk=order.pk).update(status=status)
        order.refresh_from_db()
        return order

    def test_a_paid_order_can_be_marked_fulfilled(self):
        order = self.order_in(Order.Status.PAID)

        response = self.change_status(order, Order.Status.FULFILLED)

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FULFILLED)

    def test_a_pending_order_cannot_be_marked_paid_by_hand(self):
        # That records a payment Stripe never reported and skips fulfilment.
        order = self.order_in(Order.Status.PENDING)

        response = self.change_status(order, Order.Status.PAID)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "by hand")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_nothing_goes_back_to_pending(self):
        order = self.order_in(Order.Status.FULFILLED)

        self.change_status(order, Order.Status.PENDING)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FULFILLED)

    def test_order_items_are_shown_but_not_editable(self):
        order = self.place_order()
        item = OrderItem.objects.filter(order=order).first()

        self.assertEqual(
            self.client.get("/timbit/admin/main/orderitem/").status_code, 200)
        response = self.client.post(
            f"/timbit/admin/main/orderitem/{item.pk}/delete/", {"post": "yes"})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(OrderItem.objects.filter(pk=item.pk).exists())

    def test_a_delivery_record_cannot_be_deleted(self):
        # Deleting one would queue that address for a second copy.
        self.assertEqual(
            self.client.get(
                "/timbit/admin/main/mailinglistdelivery/add/").status_code,
            403)
        self.assertFalse(MailingListDelivery.objects.exists())
