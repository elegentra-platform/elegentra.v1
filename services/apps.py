from django.apps import AppConfig


class ServicesConfig(AppConfig):
    name = 'services'

    def ready(self):
        import services.signals  # noqa: F401
