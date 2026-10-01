from django.urls import path

from . import views


app_name = "staffpanel"

urlpatterns = [
    path("login/", views.staff_login, name="staff_login"),
    path("admin-login/", views.admin_login, name="admin_login"),
    path("logout/", views.staff_logout, name="staff_logout"),
    path("", views.dashboard, name="dashboard"),
    path("saloons/", views.saloon_queue, name="saloon_queue"),
    path("vendors/", views.saloon_list, name="saloon_list"),
    path("saloons/<int:saloon_id>/", views.saloon_review, name="saloon_review"),
    path("saloons/<int:saloon_id>/approve/", views.saloon_approve, name="saloon_approve"),
    path("saloons/<int:saloon_id>/reject/", views.saloon_reject, name="saloon_reject"),
    path("saloons/<int:saloon_id>/gallery/<int:post_id>/", views.saloon_gallery_detail, name="saloon_gallery_detail"),
    path("saloons/<int:saloon_id>/gallery/<int:post_id>/<slug:action>/", views.gallery_post_moderate, name="gallery_post_moderate"),
    path("categories/", views.category_queue, name="category_queue"),
    path("categories/<int:category_id>/approve/", views.category_approve, name="category_approve"),
    path("categories/<int:category_id>/reject/", views.category_reject, name="category_reject"),
    path("categories/bulk-map/", views.category_bulk_map_main, name="category_bulk_map_main"),
    path("categories/<int:category_id>/map/", views.category_map_main, name="category_map_main"),
    path("staff-management/", views.staff_management, name="staff_management"),
    path("staff-management/<int:profile_id>/", views.staff_detail, name="staff_detail"),
    path("staff-management/<int:profile_id>/<slug:action>/", views.staff_action, name="staff_action"),
    path("subscriptions/", views.subscription_management, name="subscription_management"),
    path("reports/", views.reports, name="reports"),
    path("notifications/", views.notifications, name="notifications"),
    path("admin-management/", views.admin_management, name="admin_management"),
    path("founder-program/", views.founder_program, name="founder_program"),
    path("plans/", views.plans, name="plans"),
    path("analytics/", views.analytics, name="analytics"),
    path("audit-logs/", views.audit_logs, name="audit_logs"),
    path("system-settings/", views.system_settings, name="system_settings"),
    path("razorpay/", views.razorpay, name="razorpay"),
    path("feature-flags/", views.feature_flags, name="feature_flags"),
]

