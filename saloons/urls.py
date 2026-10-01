from django.urls import path
from . import views
from .import dashboard_views
from . import gallery_views

urlpatterns = [
    path("partner/", views.partner_home, name="partner_home"),
    path("partner/pricing/", views.partner_pricing, name="partner_pricing"),

    path("dashboard/<str:username>/", dashboard_views.dashboard_overview, name="saloon_dashboard"),
    path("dashboard/<str:username>/connect/myfestivo/", dashboard_views.dashboard_connect_myfestivo, name="saloon_dashboard_connect_myfestivo"),
    path("dashboard/<str:username>/analytics/", dashboard_views.dashboard_analytics, name="saloon_dashboard_analytics"),
    path("dashboard/<str:username>/search/", dashboard_views.dashboard_search, name="saloon_dashboard_search"),
    path("dashboard/<str:username>/notifications/", dashboard_views.dashboard_notifications, name="saloon_dashboard_notifications"),
    path("dashboard/<str:username>/notifications/clear/", dashboard_views.dashboard_notifications_clear, name="saloon_dashboard_notifications_clear"),
    path("dashboard/<str:username>/notifications/<int:notification_id>/delete/", dashboard_views.dashboard_notification_delete, name="saloon_dashboard_notification_delete"),
    path("dashboard/<str:username>/services/", dashboard_views.dashboard_services, name="saloon_dashboard_services"),
    path("dashboard/<str:username>/mysaloon/", dashboard_views.dashboard_mysaloon, name="saloon_dashboard_mysaloon"),
    path("dashboard/<str:username>/mysaloon/slug/check/", dashboard_views.dashboard_slug_check, name="saloon_dashboard_slug_check"),
    path("dashboard/<str:username>/mysaloon/slug/update/", dashboard_views.dashboard_slug_update, name="saloon_dashboard_slug_update"),
    path("dashboard/<str:username>/subscription/", dashboard_views.dashboard_subscription, name="saloon_dashboard_subscription"),
    path("dashboard/<str:username>/subscription/create/", dashboard_views.dashboard_subscription_create, name="saloon_dashboard_subscription_create"),
    path("dashboard/<str:username>/subscription/verify/", dashboard_views.dashboard_subscription_verify, name="saloon_dashboard_subscription_verify"),
    path("dashboard/<str:username>/subscription/recurring/create/", dashboard_views.dashboard_subscription_recurring_create, name="saloon_dashboard_subscription_recurring_create"),
    path("dashboard/<str:username>/subscription/recurring/verify/", dashboard_views.dashboard_subscription_recurring_verify, name="saloon_dashboard_subscription_recurring_verify"),
    path("dashboard/<str:username>/edit-basic/", dashboard_views.dashboard_edit_basic_detail, name="saloon_dashboard_edit_basic_detail"),
    path("dashboard/<str:username>/change-whatsapp/", dashboard_views.dashboard_change_whatsapp, name="saloon_dashboard_change_whatsapp"),
    path("dashboard/<str:username>/settings/", dashboard_views.dashboard_settings, name="saloon_dashboard_settings"),
    path("dashboard/<str:username>/gallery/add/", gallery_views.gallery_create, name="saloon_gallery_create"),
    path("dashboard/<str:username>/gallery/draft/save/", gallery_views.gallery_draft_save, name="saloon_gallery_draft_save"),
    path("dashboard/<str:username>/gallery/<int:post_id>/", gallery_views.gallery_detail, name="saloon_gallery_detail"),
    path("dashboard/<str:username>/gallery/<int:post_id>/edit/", gallery_views.gallery_edit, name="saloon_gallery_edit"),
    path("dashboard/<str:username>/gallery/<int:post_id>/delete/", gallery_views.gallery_delete, name="saloon_gallery_delete"),


    path("verify-whatsapp/", views.verify_whatsapp, name="verify_whatsapp"),
    path("resolve-map-location/", views.resolve_map_location, name="resolve_map_location"),
    path("billing/razorpay/webhook/", dashboard_views.razorpay_subscription_webhook, name="razorpay_subscription_webhook"),

    path("onboarding/step-1/", views.saloon_onboarding_step_one, name="saloon_onboarding_step_one"),
    path("onboarding/step-2/", views.saloon_onboarding_step_two, name="saloon_onboarding_step_two"),
    path("onboarding/step-3/", views.saloon_onboarding_step_three, name="saloon_onboarding_step_three"),
    path("onboarding/step-4/",views.saloon_onboarding_step_four,name="saloon_onboarding_step_four"),
    path("<slug:slug>/gallery/<int:post_id>/", views.public_gallery_detail, name="public_gallery_detail"),
    path("<slug:slug>/favorite-toggle/", views.public_saloon_favorite_toggle, name="public_saloon_favorite_toggle"),
    path("<slug:slug>/map-click/", views.public_saloon_map_click, name="public_saloon_map_click"),
    path("<slug:slug>/service-interest/", views.public_service_interest, name="public_service_interest"),
    path("<slug:slug>/review/", views.public_saloon_add_review, name="public_saloon_add_review"),
    path("<slug:slug>/review/<int:review_id>/edit/", views.public_saloon_edit_review, name="public_saloon_edit_review"),
    path("<slug:slug>/review/<int:review_id>/delete/", views.public_saloon_delete_review, name="public_saloon_delete_review"),
    path("<slug:slug>/", views.public_saloon, name="public_saloon"),
]



