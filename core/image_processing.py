import os
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


PUBLIC_IMAGE_MAX_SIZE = (2000, 2000)
PUBLIC_IMAGE_QUALITY = 85


def optimize_public_image_upload(upload, *, max_size=PUBLIC_IMAGE_MAX_SIZE, quality=PUBLIC_IMAGE_QUALITY):
    """Return a resized/compressed JPEG upload for public salon images.

    Verification documents are intentionally handled elsewhere and should not
    use this helper, because staff may need the original file for review.
    """
    if not upload:
        return upload

    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type and not content_type.startswith("image/"):
        return upload

    original_name = getattr(upload, "name", "upload.jpg") or "upload.jpg"
    try:
        if hasattr(upload, "seek"):
            upload.seek(0)
        image = Image.open(upload)
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError, ValueError):
        if hasattr(upload, "seek"):
            upload.seek(0)
        return upload

    if getattr(image, "is_animated", False):
        if hasattr(upload, "seek"):
            upload.seek(0)
        return upload

    image.thumbnail(max_size, Image.Resampling.LANCZOS)

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    output = BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    optimized_bytes = output.getvalue()

    original_size = getattr(upload, "size", None)
    if original_size and len(optimized_bytes) >= original_size:
        if hasattr(upload, "seek"):
            upload.seek(0)
        return upload

    stem = os.path.splitext(os.path.basename(original_name))[0] or "image"
    return ContentFile(optimized_bytes, name=f"{stem}.jpg")
