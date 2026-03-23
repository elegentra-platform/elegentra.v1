from django.urls import path

from . import views


app_name = "staffpanel"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("saloons/", views.saloon_queue, name="saloon_queue"),
    path("saloons/<int:saloon_id>/", views.saloon_review, name="saloon_review"),
    path("saloons/<int:saloon_id>/approve/", views.saloon_approve, name="saloon_approve"),
    path("saloons/<int:saloon_id>/reject/", views.saloon_reject, name="saloon_reject"),
    path("categories/", views.category_queue, name="category_queue"),
    path("categories/<int:category_id>/approve/", views.category_approve, name="category_approve"),
    path("categories/<int:category_id>/reject/", views.category_reject, name="category_reject"),
    path("categories/bulk-map/", views.category_bulk_map_main, name="category_bulk_map_main"),
    path("categories/<int:category_id>/map/", views.category_map_main, name="category_map_main"),
]
