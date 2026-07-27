#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pigscanfly.settings')
    try:
        from configurations.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    # Load the settings here, where an error in them is still reportable.
    # ManagementUtility.execute() reads settings.INSTALLED_APPS inside a
    # try/except, keeps any ImproperlyConfigured to itself, and -- because the
    # settings are then still unconfigured -- skips django.setup() while
    # running the command anyway. The command's system checks walk into an
    # empty app registry, so a pod missing one environment variable dies with
    # "AppRegistryNotReady: Models aren't loaded yet" and never names the
    # variable. See Prod.pre_setup in pigscanfly/settings.py.
    #
    # runserver is exempt: Django skips setup for it deliberately, so that the
    # autoreloader starts even on broken code and picks up the fix.
    if sys.argv[1:2] != ['runserver']:
        from django.conf import settings
        settings.INSTALLED_APPS

    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pigscanfly.settings')
    os.environ.setdefault(
        'DJANGO_CONFIGURATION',
        os.getenv("ENVIRONMENT", 'Dev'))

    main()
