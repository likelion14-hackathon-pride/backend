from django.urls import path

from .views import RiskKeywordDeleteView, RiskKeywordListCreateView


urlpatterns = [
    path('<int:company_id>/risk-keywords', RiskKeywordListCreateView.as_view()),
    path('<int:company_id>/risk-keywords/<int:keyword_id>', RiskKeywordDeleteView.as_view(),),
]
