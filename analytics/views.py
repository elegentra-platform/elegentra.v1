from django.http import HttpResponse

def analytics_home(request):
    return HttpResponse("Analytics Page")
