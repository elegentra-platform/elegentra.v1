from django.urls import path
from . import views


urlpatterns = [
    # allauth has no sign-up page when Google is the only login; keep the old
    # name so existing "Create account" links land on the login page.
    path('signup/', views.signup_redirect, name='account_signup'),
    path('google/one-tap/', views.google_one_tap, name='google_one_tap'),
]
