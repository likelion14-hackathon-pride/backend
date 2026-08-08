from django.urls import path

from .views import AuthView, SignupView, AuthView

urlpatterns = [
    path("signup", SignupView.as_view()),
    path("login", AuthView.as_view()),
]
