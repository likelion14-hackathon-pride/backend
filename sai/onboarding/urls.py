from django.urls import path

from .views import OnboardingQuestionDetailView, OnboardingView


urlpatterns = [
    path('<int:company_id>/onboarding/questions/<int:question_id>', OnboardingQuestionDetailView.as_view()),
    path('<int:company_id>/onboarding', OnboardingView.as_view()),
]
