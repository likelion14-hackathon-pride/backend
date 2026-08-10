from django.urls import path

from .views import RiskKeywordCreateView


urlpatterns = [
    path('<int:company_id>/risk-keywords', RiskKeywordCreateView.as_view()),
]

