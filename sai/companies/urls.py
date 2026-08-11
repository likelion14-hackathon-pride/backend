from django.urls import path

from .views import CompanyDetailView


urlpatterns = [
    path('<int:company_id>', CompanyDetailView.as_view()),
]
