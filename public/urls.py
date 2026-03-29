from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='public_home'),
    path('search/', views.search, name='public_search'),
    path('favorites/', views.favorites, name='public_favorites'),
    path('faq/', views.faq, name='public_faq'),
    path('account/', views.account, name='public_account'),
    path('settings/', views.settings, name='public_settings'),
]
