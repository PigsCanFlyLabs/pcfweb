# See https://github.com/typeddjango/django-stubs/pull/180#issuecomment-820062352
import os
from configurations.importer import install
from mypy.version import __version__

from mypy_django_plugin import main


def plugin(version):
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pigscanfly.settings')
    os.environ.setdefault('DJANGO_CONFIGURATION', 'Dev')
    install()
    return main.plugin(version)
