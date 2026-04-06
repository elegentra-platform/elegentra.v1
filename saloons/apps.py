from django.apps import AppConfig


class SalonsConfig(AppConfig):
    name = 'saloons'

    def ready(self):
        import saloons.signals  # noqa: F401
