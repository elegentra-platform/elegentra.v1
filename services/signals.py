from core.file_cleanup import register_file_cleanup
from .models import MainCategory, Service


register_file_cleanup(MainCategory, ["icon_image"])
register_file_cleanup(Service, ["image"])
