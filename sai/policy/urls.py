from django.urls import path

from .views import RiskKeywordListCreateView


urlpatterns = [
    path('<int:company_id>/risk-keywords', RiskKeywordListCreateView.as_view()),
]
