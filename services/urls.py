from django.urls import path
from . import views

urlpatterns = [
    path("", views.service_list, name="service_list"),
    path("add/", views.service_create, name="service_create"),
    path("<int:service_id>/edit/", views.service_edit, name="service_edit"),
    path("<int:service_id>/delete/", views.service_delete, name="service_delete"),
    path("bulk-delete/", views.service_bulk_delete, name="service_bulk_delete"),
    path("undo-delete/", views.service_undo_delete, name="service_undo_delete"),
]
