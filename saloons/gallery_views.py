import uuid

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.urls import reverse
from django.db.models import F
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.core.files import File
from django.core.files.storage import default_storage

from services.models import Service
from plans.billing import get_or_create_saloon_subscription, sync_local_subscription_state, subscription_has_platform_access, subscription_lock_reason
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



def _subscription_access_context(saloon):
    subscription, plan = get_or_create_saloon_subscription(saloon)
    subscription = sync_local_subscription_state(subscription)
    locked = not subscription_has_platform_access(subscription)
    reason = subscription_lock_reason(subscription) if locked else {"title": "", "message": ""}
    return subscription, plan, {
        "subscription_locked": locked,
        "subscription_lock_title": reason["title"],
        "subscription_lock_message": reason["message"],
    }


def _locked_dashboard_response(request, saloon):
    subscription, plan, access = _subscription_access_context(saloon)
    return render(
        request,
        "saloons/dashboard/locked.html",
        {
            "saloon": saloon,
            "subscription": subscription,
            "subscription_plan": plan,
            "active_tab": "mysaloon",
            **access,
        },
        status=403,
    )


def _is_subscription_locked(saloon):
    _, _, access = _subscription_access_context(saloon)
    return access["subscription_locked"]

def _get_media_type(upload):
    content_type = (upload.content_type or "").lower()
    if content_type.startswith("image/"):
        return GalleryMedia.TYPE_IMAGE
    return None


def _validate_media_limits(image_count):
    if image_count > MAX_IMAGES_ONLY:
        return "You can upload at most 10 images in a post."
    return None


def _gallery_services(saloon):
    return Service.objects.filter(
        saloon=saloon,
        is_active=True,
        is_visible=True,
        is_deleted=False,
    ).select_related("category")


def _selected_service(saloon, service_id):
    if not service_id:
        return None
    return Service.objects.filter(
        id=service_id,
        saloon=saloon,
        is_active=True,
        is_deleted=False,
    ).first()


def _draft_key(saloon):
    return f"gallery_create_draft_{saloon.id}"


def _get_gallery_draft(request, saloon):
    return request.session.get(_draft_key(saloon)) or {}


def _clear_gallery_draft(request, saloon):
    draft = _get_gallery_draft(request, saloon)
    for item in draft.get("media", []):
        path = item.get("path")
        if path and default_storage.exists(path):
            default_storage.delete(path)
    request.session.pop(_draft_key(saloon), None)
    request.session.modified = True


def _draft_media_context(draft):
    items = []
    for item in draft.get("media", []):
        path = item.get("path")
        if not path or not default_storage.exists(path):
            continue
        try:
            url = default_storage.url(path)
        except Exception:
            url = ""
        items.append({
            "url": url,
            "name": item.get("name") or "Image",
        })
    return items


def _create_media_from_draft(post, draft):
    created = 0
    for idx, item in enumerate(draft.get("media", []), start=1):
        path = item.get("path")
        if not path or not default_storage.exists(path):
            continue
        filename = item.get("name") or path.rsplit("/", 1)[-1]
        with default_storage.open(path, "rb") as draft_file:
            media = GalleryMedia(
                post=post,
                media_type=GalleryMedia.TYPE_IMAGE,
                order=idx,
            )
            media.file.save(filename, File(draft_file), save=True)
        default_storage.delete(path)
        created += 1
    return created


def _gallery_form_context(request, saloon, **extra):
    draft = _get_gallery_draft(request, saloon)
    selected_service_id = (
        request.GET.get("selected_service")
        or request.POST.get("service")
        or draft.get("service")
        or ""
    )
    context = {
        "saloon": saloon,
        "services": _gallery_services(saloon),
        "selected_service_id": str(selected_service_id),
        "form_title": request.POST.get("title", "") or draft.get("title", ""),
        "form_description": request.POST.get("description", "") or draft.get("description", ""),
        "draft_media": _draft_media_context(draft),
        "has_gallery_draft": bool(draft.get("media")),
        "gallery_draft_save_url": reverse("saloon_gallery_draft_save", kwargs={"username": saloon.owner.username}),
        "active_tab": "mysaloon",
        "show_back_button": True,
        "back_url": reverse("saloon_dashboard_mysaloon", kwargs={"username": saloon.owner.username}),
        "back_parent_label": "My Saloon",
        "back_label": "Gallery",
    }
    context.update(extra)
    return context


@login_required
@require_POST
def gallery_draft_save(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return JsonResponse({"ok": False, "message": "Please sign in again."}, status=403)
    if _is_subscription_locked(saloon):
        return JsonResponse({"ok": False, "message": "Subscription required."}, status=403)

    uploads = request.FILES.getlist("media")
    if not uploads:
        request.session[_draft_key(saloon)] = {
            "title": request.POST.get("title", "").strip(),
            "description": request.POST.get("description", "").strip(),
            "service": request.POST.get("service", ""),
            "media": _get_gallery_draft(request, saloon).get("media", []),
        }
        request.session.modified = True
        return JsonResponse({"ok": True})

    if len(uploads) > MAX_IMAGES_ONLY:
        return JsonResponse({"ok": False, "message": "You can upload at most 10 images in a post."}, status=400)

    old_draft = _get_gallery_draft(request, saloon)
    for item in old_draft.get("media", []):
        path = item.get("path")
        if path and default_storage.exists(path):
            default_storage.delete(path)

    media = []
    draft_id = uuid.uuid4().hex
    for index, upload in enumerate(uploads, start=1):
        media_type = _get_media_type(upload)
        if not media_type:
            return JsonResponse({"ok": False, "message": "Only images are allowed."}, status=400)
        extension = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else "jpg"
        saved_path = default_storage.save(
            f"saloons/gallery_drafts/{saloon.id}/{draft_id}/{index}.{extension}",
            upload,
        )
        media.append({
            "path": saved_path,
            "name": upload.name,
            "content_type": upload.content_type or "image/*",
        })

    request.session[_draft_key(saloon)] = {
        "title": request.POST.get("title", "").strip(),
        "description": request.POST.get("description", "").strip(),
        "service": request.POST.get("service", ""),
        "media": media,
    }
    request.session.modified = True
    return JsonResponse({"ok": True})


@login_required
def gallery_create(request, username):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    if _is_subscription_locked(saloon):
        return _locked_dashboard_response(request, saloon)

    if request.method == "POST":
        draft = _get_gallery_draft(request, saloon)
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        uploads = request.FILES.getlist("media")
        draft_media = _draft_media_context(draft) if not uploads else []

        if not uploads and not draft_media:
            messages.error(request, "Please add at least one image.")
            return render(
                request,
                "saloons/gallery/gallery_create.html",
                _gallery_form_context(request, saloon),
            )

        media_items = []
        image_count = len(draft_media)
        for upload in uploads:
            media_type = _get_media_type(upload)
            if not media_type:
                messages.error(request, "Only images are allowed.")
                return render(
                    request,
                    "saloons/gallery/gallery_create.html",
                    _gallery_form_context(request, saloon),
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
                _gallery_form_context(request, saloon),
            )

        post = GalleryPost.objects.create(
            saloon=saloon,
            service=_selected_service(saloon, request.POST.get("service")),
            title=title,
            description=description,
        )

        if draft_media and not uploads:
            _create_media_from_draft(post, draft)
            request.session.pop(_draft_key(saloon), None)
            request.session.modified = True
        else:
            _clear_gallery_draft(request, saloon)
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
        _gallery_form_context(request, saloon),
    )


@login_required
def gallery_detail(request, username, post_id):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    if _is_subscription_locked(saloon):
        return _locked_dashboard_response(request, saloon)

    post = get_object_or_404(
        GalleryPost.objects.select_related("service", "service__category"),
        id=post_id,
        saloon=saloon,
    )
    if not post.image_media:
        messages.info(request, "This gallery post no longer appears publicly because video posts are disabled.")
        return redirect("saloon_dashboard_mysaloon", username=saloon.owner.username)
    viewed_key = f"gallery_viewed_{post.id}"
    if not request.session.get(viewed_key):
        GalleryPost.objects.filter(id=post.id).update(views=F("views") + 1)
        request.session[viewed_key] = True
        request.session.modified = True
        post.refresh_from_db(fields=["views"])
    posts = [
        item
        for item in saloon.gallery_posts.filter(is_hidden=False).select_related("service", "service__category").prefetch_related("media").all()
        if item.image_media
    ]
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
    if _is_subscription_locked(saloon):
        return _locked_dashboard_response(request, saloon)

    post = get_object_or_404(
        GalleryPost.objects.select_related("service", "service__category"),
        id=post_id,
        saloon=saloon,
    )

    if request.method == "POST":
        post.title = request.POST.get("title", "").strip()
        post.description = request.POST.get("description", "").strip()
        post.service = _selected_service(saloon, request.POST.get("service"))

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
        _gallery_form_context(
            request,
            saloon,
            post=post,
            selected_service_id=str(post.service_id or ""),
            back_url=reverse("saloon_gallery_detail", kwargs={"username": saloon.owner.username, "post_id": post.id}),
            back_parent_label="Gallery",
            back_label="Edit",
        ),
    )


@login_required
def gallery_delete(request, username, post_id):
    saloon, redirect_response = _dashboard_guard(request, username)
    if redirect_response:
        return redirect_response
    if _is_subscription_locked(saloon):
        return _locked_dashboard_response(request, saloon)

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
