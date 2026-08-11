from django.urls import path

from .views import OnboardingView


urlpatterns = [
    path('<int:company_id>/onboarding', OnboardingView.as_view()),
]
