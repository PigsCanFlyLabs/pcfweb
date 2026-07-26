"""run_local.sh must refuse to run against a production environment.

manage.py resolves the Django configuration with
``os.environ.setdefault('DJANGO_CONFIGURATION', os.getenv("ENVIRONMENT", 'Dev'))``.
``setdefault`` does not override an exported value, so an exported
``DJANGO_CONFIGURATION=Prod`` selects Prod outright, and an exported
``ENVIRONMENT=Prod`` selects it through the fallback even with
``DJANGO_CONFIGURATION`` unset. ``Prod.DATABASES`` then reads its connection
details from the environment.

That made a script named ``run_local.sh`` capable of applying migrations and
fixture upserts to the live database from a developer's production shell.
``seed_products`` overwrites fixture-owned fields on existing rows, so it is a
data-loss path.

These tests run the real script as a subprocess. They assert both that it
exits non-zero and that it did so *before* running any manage.py command --
an exit code alone would not distinguish refusing from failing halfway
through a migration.
"""

import os
import subprocess

from django.test import SimpleTestCase

from main.tests.base import REPO_ROOT

SCRIPT = REPO_ROOT / "run_local.sh"

# Commands the script must not have reached. `set -x` traces every command to
# stderr, so their absence from the trace is the evidence.
DATABASE_COMMANDS = ("manage.py migrate", "manage.py seed_products")


class RunLocalProductionGuardTest(SimpleTestCase):
    def run_script(self, **env_overrides):
        """Run run_local.sh with a scrubbed environment plus overrides."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "DJANGO_CONFIGURATION", "ENVIRONMENT",
                "DBHOST", "DBNAME", "DBUSER", "DBPASSWORD",
            }
        }
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def assertRefused(self, result, expected_variable):
        self.assertNotEqual(
            result.returncode, 0,
            f"script exited 0; stdout={result.stdout!r}")
        self.assertIn(expected_variable, result.stderr)
        self.assertIn("Refusing to touch", result.stderr)
        # The message has to be actionable, not just a complaint.
        self.assertIn("env -u", result.stderr)

        combined = result.stdout + result.stderr
        for command in DATABASE_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(
                    command, combined,
                    f"{command} ran before the guard refused")

    def test_it_refuses_when_dbhost_is_set(self):
        result = self.run_script(DBHOST="pcfweb-pg-rw.pcfweb.svc")

        self.assertRefused(result, "DBHOST")

    def test_it_refuses_when_environment_is_prod(self):
        """The door the first review missed: the ENVIRONMENT fallback."""
        result = self.run_script(ENVIRONMENT="Prod")

        self.assertRefused(result, "ENVIRONMENT=Prod")

    def test_it_refuses_when_django_configuration_is_prod(self):
        result = self.run_script(DJANGO_CONFIGURATION="Prod")

        self.assertRefused(result, "DJANGO_CONFIGURATION=Prod")

    def test_it_refuses_before_generating_a_certificate(self):
        """The guard is first, so nothing at all happens in a prod shell."""
        result = self.run_script(DBHOST="db.internal")

        combined = result.stdout + result.stderr
        self.assertNotIn("mkcert", combined)
        self.assertNotIn("apt-get", combined)

    def test_the_script_pins_the_configuration_rather_than_inheriting_it(self):
        """Braces to the refusal's belt.

        The refusal covers the doors that exist today. Pinning means a value
        inherited through some future door still cannot select Prod.
        """
        source = SCRIPT.read_text()

        self.assertIn("export DJANGO_CONFIGURATION=Dev", source)
        self.assertIn("unset ENVIRONMENT", source)
        # Both must come before the first database command.
        pin = source.index("export DJANGO_CONFIGURATION=Dev")
        for command in DATABASE_COMMANDS:
            with self.subTest(command=command):
                self.assertLess(pin, source.index(command))

    def test_the_guard_precedes_every_database_command_in_the_source(self):
        source = SCRIPT.read_text()

        guard = source.index("refuse_production")
        for command in DATABASE_COMMANDS:
            with self.subTest(command=command):
                self.assertLess(guard, source.index(command))
