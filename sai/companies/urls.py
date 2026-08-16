from django.urls import path

from .views import (
    CompanyDetailView,
    CompanyMemberListView,
    CompanySettingsView,
    ProfileOptionsView,
)


urlpatterns = [
    path('<int:company_id>/members', CompanyMemberListView.as_view()),
    path('<int:company_id>/settings', CompanySettingsView.as_view()),
    path('<int:company_id>/profile-options', ProfileOptionsView.as_view()),
    path('<int:company_id>', CompanyDetailView.as_view()),
]
