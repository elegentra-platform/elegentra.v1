from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='public_home'),
    path('search/', views.search, name='public_search'),
    path('places/<slug:district_slug>/category/<slug:category_slug>/', views.district_place_listing, name='public_place_district_category'),
    path('places/<slug:district_slug>/', views.district_place_listing, name='public_place_district'),
    path('places/<slug:district_slug>/<slug:city_slug>/', views.place_listing, name='public_place_city'),
    path('places/<slug:district_slug>/<slug:city_slug>/category/<slug:category_slug>/', views.place_listing, name='public_place_city_category'),
    path('places/<slug:district_slug>/<slug:city_slug>/<slug:locality_slug>/', views.place_listing, name='public_place_locality'),
    path('places/<slug:district_slug>/<slug:city_slug>/<slug:locality_slug>/category/<slug:category_slug>/', views.place_listing, name='public_place_locality_category'),
    path('places/<slug:city_slug>/category/<slug:category_slug>/', views.legacy_place_redirect, name='public_place_city_legacy_category'),
    path('places/<slug:city_slug>/<slug:locality_slug>/category/<slug:category_slug>/', views.legacy_place_redirect, name='public_place_locality_legacy_category'),
    path('places/<slug:city_slug>/<slug:locality_slug>/', views.legacy_place_redirect, name='public_place_locality_legacy'),
    path('places/<slug:city_slug>/', views.legacy_place_redirect, name='public_place_city_legacy'),
    path('favorites/', views.favorites, name='public_favorites'),
    path('notifications/', views.notifications, name='public_notifications'),
    path('notifications/clear/', views.notifications_clear, name='public_notifications_clear'),
    path('notifications/<int:notification_id>/delete/', views.notification_delete, name='public_notification_delete'),
    path('faq/', views.faq, name='public_faq'),
    path('privacy-policy/', views.legal_page, {'slug': 'privacy-policy'}, name='public_privacy_policy'),
    path('terms-and-conditions/', views.legal_page, {'slug': 'terms-and-conditions'}, name='public_terms_and_conditions'),
    path('cancellation-refund-policy/', views.legal_page, {'slug': 'cancellation-refund-policy'}, name='public_cancellation_refund_policy'),
    path('contact-support/', views.legal_page, {'slug': 'contact-support'}, name='public_contact_support'),
    path('account/', views.account, name='public_account'),
    path('settings/', views.settings, name='public_settings'),
]

