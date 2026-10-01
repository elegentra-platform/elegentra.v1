from django.contrib import admin
from django.conf import settings
from django.urls import re_path
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path, include
from public import views as public_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path("favicon.ico", public_views.favicon_ico, name="favicon_ico"),
    path("favicon.png", public_views.favicon_png, name="favicon_png"),
    path("site.webmanifest", public_views.site_webmanifest, name="site_webmanifest"),
    path("robots.txt", public_views.robots_txt, name="robots_txt"),
    path("sitemap.xml", public_views.sitemap_xml, name="sitemap_xml"),
    re_path(r"^media/(?P<path>.*)$", public_views.media_range_serve, name="media_serve"),

    path('', include('public.urls')),

    path('accounts/', include('allauth.urls')),
    path('accounts/', include('accounts.urls')),

    path('saloons/', include('saloons.urls')),
    path('services/', include('services.urls')),
    path('plans/', include('plans.urls')),
    path('analytics/', include('analytics.urls')),
    path('staff/', include(("staffpanel.urls", "staffpanel"), namespace="staffpanel")),
]


if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()

