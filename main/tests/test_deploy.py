"""Tests for the things that only bite in production.

The health endpoint, the Prod configuration guards, and the Kubernetes
manifest invariants that a green test suite would otherwise say nothing
about.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import yaml
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from pigscanfly.settings import Prod, _prod_required_env


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class HealthzTest(TestCase):
    """The probes' target. It has to be able to fail."""

    def test_healthz_reports_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")

    def test_healthz_fails_when_the_database_is_unreachable(self):
        # The whole point of this endpoint over `/`: a broken database has to
        # be visible to the kubelet, not hidden behind a redirect.
        with mock.patch("main.middleware.connection.cursor",
                        side_effect=OSError("connection refused")):
            with self.assertLogs("main.middleware", level="ERROR"):
                response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)

    def test_the_health_check_runs_before_every_other_middleware(self):
        # Ordering is the whole design. Below SecurityMiddleware it gets a
        # 301; below CommonMiddleware the pod-IP Host fails ALLOWED_HOSTS;
        # above nothing, cookie_consent's process_response queries the
        # database after the response and turns a clean 503 into a 500.
        self.assertEqual(
            settings.MIDDLEWARE[0], "main.middleware.HealthCheckMiddleware")

    def test_healthz_answers_a_host_that_allowed_hosts_would_reject(self):
        # With no Host override the kubelet sends the pod IP, which cannot be
        # in ALLOWED_HOSTS. Short-circuiting above that check is what keeps
        # the probe from being a blanket 400.
        response = self.client.get("/healthz", headers={"host": "10.42.0.17"})
        self.assertEqual(response.status_code, 200)

    def test_healthz_does_not_touch_the_session_or_consent_middleware(self):
        # A probe runs on every pod every few seconds; it must not create a
        # session row or query the cookie groups each time.
        with mock.patch("cookie_consent.middleware.CleanCookiesMiddleware."
                        "process_response") as clean:
            self.client.get("/healthz")
        clean.assert_not_called()


class ProdConfigurationGuardTest(TestCase):
    """Prod refuses to boot on a misconfiguration rather than failing on a
    customer."""

    def test_secret_key_is_required(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                Prod().SECRET_KEY

    def test_stripe_live_key_is_required(self):
        # Otherwise stripe.api_key is None and the first add-to-cart 500s.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                Prod().STRIPE_API_KEY

    def test_stripe_webhook_secret_is_required(self):
        # Otherwise Stripe charges the customer and the order sits PENDING
        # forever, with nobody told to ship it.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                Prod().STRIPE_WEBHOOK_SECRET

    def test_the_guards_pass_once_the_variables_are_set(self):
        env = {
            "SECRET_KEY": "x" * 60,
            "STRIPE_LIVE_SECRET_KEY": "sk_live_x",
            "STRIPE_WEBHOOK_SECRET": "whsec_x",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            prod = Prod()
            self.assertEqual(prod.SECRET_KEY, "x" * 60)
            self.assertEqual(prod.STRIPE_API_KEY, "sk_live_x")
            self.assertEqual(prod.STRIPE_WEBHOOK_SECRET, "whsec_x")

    def test_pre_setup_names_every_missing_variable_at_once(self):
        # The guards above are evaluated in alphabetical order, so relying on
        # them alone costs one failed rollout per missing variable.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured) as caught:
                Prod.pre_setup()
        message = str(caught.exception)
        for name in ("SECRET_KEY", "STRIPE_LIVE_SECRET_KEY",
                     "STRIPE_WEBHOOK_SECRET"):
            self.assertIn(name, message)
        # Where to go and set them.
        self.assertIn("pcfweb-secret", message)

    def test_pre_setup_reports_only_what_is_missing(self):
        env = {"SECRET_KEY": "x" * 60, "STRIPE_LIVE_SECRET_KEY": "sk_live_x"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ImproperlyConfigured) as caught:
                Prod.pre_setup()
        message = str(caught.exception)
        self.assertIn("STRIPE_WEBHOOK_SECRET", message)
        self.assertNotIn("SECRET_KEY:", message)
        self.assertNotIn("STRIPE_LIVE_SECRET_KEY", message)

    def test_pre_setup_passes_once_the_variables_are_set(self):
        env = {
            "SECRET_KEY": "x" * 60,
            "STRIPE_LIVE_SECRET_KEY": "sk_live_x",
            "STRIPE_WEBHOOK_SECRET": "whsec_x",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            Prod.pre_setup()

    def test_the_database_connection_is_bounded(self):
        env = {"DBNAME": "d", "DBUSER": "u", "DBPASSWORD": "p", "DBHOST": "h"}
        with mock.patch.dict(os.environ, env, clear=True):
            options = Prod().DATABASES["default"]["OPTIONS"]
        # Without these an unreachable database is a hang, not an error, and
        # the request outlives the gunicorn worker timeout.
        self.assertIn("connect_timeout", options)
        self.assertIn("timeout", options["pool"])

    def test_the_mail_connection_is_bounded(self):
        # Same failure shape as the database: an SMTP host that drops packets
        # makes send_mail block for the OS TCP timeout. That hang happens
        # inside the Stripe webhook and during primary startup, so it must be
        # bounded well under the 60s gunicorn worker timeout.
        self.assertLess(Prod.EMAIL_TIMEOUT, 60)


class ProdSecretPreflightTest(SimpleTestCase):
    """build.sh checks pcfweb-secret before pushing, against a list it has to
    copy. A new required variable added to the settings and not to the script
    is a guard that silently stops guarding, so tie the two together."""

    def build_sh_required_keys(self):
        with open(REPO_ROOT / "build.sh") as fh:
            for line in fh:
                if line.startswith("REQUIRED_SECRET_KEYS="):
                    return set(line.split("(", 1)[1].split(")", 1)[0].split())
        self.fail("build.sh no longer declares REQUIRED_SECRET_KEYS")

    def test_build_sh_checks_exactly_what_prod_requires(self):
        self.assertEqual(self.build_sh_required_keys(),
                         set(_prod_required_env))


class ManageErrorReportingTest(SimpleTestCase):
    """A misconfigured Prod has to say so through manage.py.

    Django's ManagementUtility swallows an ImproperlyConfigured raised while
    the settings are read, then skips django.setup() and runs the command
    anyway, so the failure resurfaces as an unrelated "AppRegistryNotReady:
    Models aren't loaded yet" out of the system checks. In a Kubernetes pod
    that is the entire log: a crash loop with nothing pointing at the Secret.
    manage.py reads the settings itself to keep the real message.
    """

    def run_manage(self, *args, **env):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "DJANGO_CONFIGURATION": "Prod",
            **env,
        }
        return subprocess.run(
            [sys.executable, "manage.py", *args],
            cwd=REPO_ROOT, env=environment, capture_output=True, text=True)

    def test_a_missing_variable_is_named_instead_of_the_app_registry(self):
        result = self.run_manage("check")
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("SECRET_KEY", output)
        self.assertIn("STRIPE_WEBHOOK_SECRET", output)
        self.assertNotIn("AppRegistryNotReady", output)


class DeployManifestTest(TestCase):
    """Invariants in deploy.yaml that a broken deploy depends on."""

    def setUp(self):
        with open(REPO_ROOT / "deploy.yaml") as fh:
            self.docs = [doc for doc in yaml.safe_load_all(fh) if doc]
        self.deployments = [
            doc for doc in self.docs if doc.get("kind") == "Deployment"]

    def pod_spec(self, deployment):
        return deployment["spec"]["template"]["spec"]

    def all_images(self):
        images = []
        for deployment in self.deployments:
            spec = self.pod_spec(deployment)
            for container in (spec.get("initContainers", [])
                              + spec.get("containers", [])):
                images.append(container["image"])
        return images

    def test_every_image_reference_uses_the_same_tag(self):
        # A split tag would run the migration gate and the app off different
        # builds, which is exactly the situation the gate exists to prevent.
        self.assertEqual(len(set(self.all_images())), 1, self.all_images())

    def test_the_image_tag_is_not_the_previously_released_one(self):
        # Pushing over an already-deployed tag makes `kubectl apply` a no-op:
        # the spec is unchanged, so no rollout happens and the pods keep the
        # old image. build.sh enforces this against the live cluster; this
        # keeps the known-stale tag from creeping back in.
        for image in self.all_images():
            self.assertNotIn(":v0.10.0", image)

    def test_probes_target_the_health_endpoint(self):
        # `/` is answered with a 301 by SecurityMiddleware before any database
        # access, and Kubernetes counts a 3xx as success -- so probes against
        # it pass on a completely broken app.
        for deployment in self.deployments:
            for container in self.pod_spec(deployment)["containers"]:
                for probe in ("livenessProbe", "readinessProbe",
                              "startupProbe"):
                    with self.subTest(deployment=deployment["metadata"]["name"],
                                      probe=probe):
                        self.assertEqual(
                            container[probe]["httpGet"]["path"], "/healthz")

    def test_probes_send_a_host_that_allowed_hosts_accepts(self):
        # Without the override the kubelet sends the pod IP, which is not a
        # name ALLOWED_HOSTS can know in advance.
        for deployment in self.deployments:
            for container in self.pod_spec(deployment)["containers"]:
                for probe in ("livenessProbe", "readinessProbe",
                              "startupProbe"):
                    with self.subTest(deployment=deployment["metadata"]["name"],
                                      probe=probe):
                        headers = container[probe]["httpGet"]["httpHeaders"]
                        host = next(h["value"] for h in headers
                                    if h["name"].lower() == "host")
                        self.assertIn(host, Prod.ALLOWED_HOSTS)

    def test_the_serving_deployment_waits_for_migrations(self):
        # web-primary migrates, but both Deployments roll at once, so without
        # this gate these pods serve new code against the old schema.
        web = next(d for d in self.deployments
                   if d["metadata"]["name"] == "web")
        init_containers = self.pod_spec(web).get("initContainers", [])
        self.assertTrue(init_containers)
        self.assertIn(
            "migrate --check",
            " ".join(init_containers[0].get("args", [])))

    def test_containers_declare_resources(self):
        for deployment in self.deployments:
            spec = self.pod_spec(deployment)
            for container in (spec.get("initContainers", [])
                              + spec.get("containers", [])):
                with self.subTest(container=container["name"]):
                    self.assertIn("requests", container.get("resources", {}))
