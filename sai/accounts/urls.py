from django.urls import path

from .views import *
from rest_framework_simplejwt.views import (
    TokenRefreshView,
    TokenVerifyView,
)


urlpatterns = [
    path("signup/owner", OwnerSignupView.as_view()),
    path("signup/member", MemberSignupView.as_view()),
    path("login", AuthView.as_view()),
    path("logout", LogoutView.as_view()),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("token/verify/", TokenVerifyView.as_view(), name="token_verify"),
]
