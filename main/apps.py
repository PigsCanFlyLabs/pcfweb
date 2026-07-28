from django.apps import AppConfig


class MainConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'main'

    def ready(self):
        # main.mailing registers a signal on django-newsletter's Subscription
        # (see rotate_activation_code_on_unsubscribe). Importing it here rather
        # than relying on the views being imported first means the signal is
        # connected in a management command or a shell too, where nothing would
        # otherwise pull the module in.
        from main import mailing  # noqa: F401
