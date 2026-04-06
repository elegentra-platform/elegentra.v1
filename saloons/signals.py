from core.file_cleanup import register_file_cleanup
from .models import GalleryMedia, Saloon, SaloonVerification


register_file_cleanup(Saloon, ["banner_image"])
register_file_cleanup(
    SaloonVerification,
    [
        "owner_id_proof",
        "inside_image_1",
        "inside_image_2",
        "inside_image_3",
        "outside_image_1",
        "outside_image_2",
        "outside_image_3",
    ],
)
register_file_cleanup(GalleryMedia, ["file"])
