from django.urls import path

from .views import OnboardingCompleteView, OnboardingQuestionDetailView, OnboardingView


urlpatterns = [
    path('<int:company_id>/onboarding/complete', OnboardingCompleteView.as_view()),
    path('<int:company_id>/onboarding/questions/<str:template_key>', OnboardingQuestionDetailView.as_view()),
    path('<int:company_id>/onboarding', OnboardingView.as_view()),
]
