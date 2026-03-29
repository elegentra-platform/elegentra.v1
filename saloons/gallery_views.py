from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.urls import reverse
from django.db.models import F

from .models import Saloon, TrustedDevice, GalleryPost, GalleryMedia


MAX_IMAGES_ONLY = 10


def _dashboard_guard(request, username):
    if request.user.username != username:
        return None, redirect("home")

    saloon = Saloon.objects.filter(owner=request.user).first()
    if not saloon:
        return None, redirect("partner_home")

    if not saloon.is_whatsapp_verified:
        return None, redirect("verify_whatsapp")

    device_token = request.COOKIES.get("salon_device")
    if not device_token:
        return None, redirect("verify_whatsapp")

    trusted = TrustedDevice.objects.filter(
        user=request.user,
        device_token=device_token
    ).first()

    if not trusted:
        return None, redirect("verify_whatsapp")

    trusted.last_used_at = timezone.now()
    trusted.save(update_fields=["last_used_at"])

    return saloon, None


def _get_media_type(upload):
    content_type = (upload.content_type or "").lower()
    if content_type.startswith("image/"):
        return GalleryMedia.TYPE_IMAGE
    return None


def _validate_media_limits(image_count):
    if image_count > MAX_IMAGES_ONLY:
        return "You can upload at most 10 images in a post."
    return None


@login_required
def gallery_create(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        uploads = request.FILES.getlist("media")

        if not uploads:
            messages.error(request, "Please add at least one image.")
            return render(
                request,
                "saloons/gallery/gallery_create.html",
                {
                    "saloon": saloon,
                    "active_tab": "mysaloon",
                    "show_back_button": True,
                    "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
                    "back_parent_label": "My Saloon",
                    "back_label": "Gallery",
                }
            )

        media_items = []
        image_count = 0
        for upload in uploads:
            media_type = _get_media_type(upload)
            if not media_type:
                messages.error(request, "Only images are allowed.")
                return render(
                    request,
                    "saloons/gallery/gallery_create.html",
                    {
                        "saloon": saloon,
                        "active_tab": "mysaloon",
                        "show_back_button": True,
                        "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
                        "back_parent_label": "My Saloon",
                        "back_label": "Gallery",
                    }
                )
            if media_type == GalleryMedia.TYPE_IMAGE:
                image_count += 1
            media_items.append((upload, media_type))

        error = _validate_media_limits(image_count)
        if error:
            messages.error(request, error)
            return render(
                request,
                "saloons/gallery/gallery_create.html",
                {
                    "saloon": saloon,
                    "active_tab": "mysaloon",
                    "show_back_button": True,
                    "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
                    "back_parent_label": "My Saloon",
                    "back_label": "Gallery",
                }
            )

        post = GalleryPost.objects.create(
            saloon=saloon,
            title=title,
            description=description,
        )

        for idx, (upload, media_type) in enumerate(media_items, start=1):
            GalleryMedia.objects.create(
                post=post,
                file=upload,
                media_type=media_type,
                order=idx,
            )

        messages.success(request, "Gallery post created.")
        return redirect("saloon_gallery_detail", username=saloon.owner.username, post_id=post.id)

    return render(
        request,
        "saloons/gallery/gallery_create.html",
        {
            "saloon": saloon,
            "active_tab": "mysaloon",
            "show_back_button": True,
            "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
            "back_parent_label": "My Saloon",
            "back_label": "Gallery",
        }
    )


@login_required
def gallery_detail(request, username, post_id):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    post = get_object_or_404(GalleryPost, id=post_id, saloon=saloon)
    if not post.image_media:
        messages.info(request, "This gallery post no longer appears publicly because video posts are disabled.")
        return redirect("saloon_dashboard_mysaloon", username=saloon.owner.username)
    viewed_key = f"gallery_viewed_{post.id}"
    if not request.session.get(viewed_key):
        GalleryPost.objects.filter(id=post.id).update(views=F("views") + 1)
        request.session[viewed_key] = True
        request.session.modified = True
        post.refresh_from_db(fields=["views"])
    posts = [item for item in saloon.gallery_posts.prefetch_related("media").all() if item.image_media]
    post_ids = [p.id for p in posts]
    try:
        index = post_ids.index(post.id)
    except ValueError:
        index = 0

    prev_post = posts[index - 1] if len(posts) > 1 else None
    next_post = posts[index + 1] if index + 1 < len(posts) else None

    return render(
        request,
        "saloons/gallery/gallery_detail.html",
        {
            "saloon": saloon,
            "post": post,
            "ordered_posts": posts,
            "selected_post_id": post.id,
            "prev_post": prev_post,
            "next_post": next_post,
            "active_tab": "mysaloon",
            "show_back_button": True,
            "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}) + "?tab=gallery",
            "back_parent_label": "My Saloon",
            "back_label": "Gallery",
            "back_sticky": True,
            "content_bg_white": True,
        }
    )


@login_required
def gallery_edit(request, username, post_id):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    post = get_object_or_404(GalleryPost, id=post_id, saloon=saloon)

    if request.method == "POST":
        post.title = request.POST.get("title", "").strip()
        post.description = request.POST.get("description", "").strip()

        remove_ids = request.POST.getlist("remove_media")
        uploads = request.FILES.getlist("media")

        existing_media = list(post.media.exclude(id__in=remove_ids))
        image_count = sum(1 for m in existing_media if m.media_type == GalleryMedia.TYPE_IMAGE)

        new_items = []
        for upload in uploads:
            media_type = _get_media_type(upload)
            if not media_type:
                messages.error(request, "Only images are allowed.")
                return redirect("saloon_gallery_edit", username=saloon.owner.username, post_id=post.id)
            if media_type == GalleryMedia.TYPE_IMAGE:
                image_count += 1
            new_items.append((upload, media_type))

        error = _validate_media_limits(image_count)
        if error:
            messages.error(request, error)
            return redirect("saloon_gallery_edit", username=saloon.owner.username, post_id=post.id)

        if image_count == 0 and not new_items:
            messages.error(request, "A post needs at least one image.")
            return redirect("saloon_gallery_edit", username=saloon.owner.username, post_id=post.id)

        if remove_ids:
            GalleryMedia.objects.filter(
                post=post,
                id__in=remove_ids
            ).delete()

        post.save()

        if new_items:
            last_order = post.media.order_by("-order").first()
            start = last_order.order + 1 if last_order else 1
            for idx, (upload, media_type) in enumerate(new_items, start=start):
                GalleryMedia.objects.create(
                    post=post,
                    file=upload,
                    media_type=media_type,
                    order=idx,
                )

        messages.success(request, "Gallery post updated.")
        return redirect("saloon_gallery_detail", username=saloon.owner.username, post_id=post.id)

    return render(
        request,
        "saloons/gallery/gallery_edit.html",
        {
            "saloon": saloon,
            "post": post,
            "active_tab": "mysaloon",
            "show_back_button": True,
            "back_url": reverse("saloon_gallery_detail", kwargs={"username": saloon.owner.username, "post_id": post.id}),
            "back_parent_label": "Gallery",
            "back_label": "Edit",
        }
    )


@login_required
def gallery_delete(request, username, post_id):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response

    post = get_object_or_404(GalleryPost, id=post_id, saloon=saloon)

    if request.method == "POST":
        post.delete()
        messages.success(request, "Gallery post deleted.")
        return redirect("saloon_dashboard_mysaloon", username=saloon.owner.username)

    return render(
        request,
        "saloons/gallery/gallery_delete_confirm.html",
        {
            "saloon": saloon,
            "post": post,
            "active_tab": "mysaloon",
            "show_back_button": True,
            "back_url": reverse("saloon_gallery_detail", kwargs={"username": saloon.owner.username, "post_id": post.id}),
            "back_parent_label": "Gallery",
            "back_label": "Delete",
        }
    )
