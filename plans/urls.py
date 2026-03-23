from django.urls import path
from . import views

urlpatterns = [
    path('', views.plan_list, name='plan_list'),
]
