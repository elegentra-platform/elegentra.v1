from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),

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
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
