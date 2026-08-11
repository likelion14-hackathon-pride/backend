from django.urls import path

from .views import CompanyScopeListView, HandbookEntryDetailView, HandbookEntryListCreateView


urlpatterns = [
    path('<int:company_id>/handbook/scopes', CompanyScopeListView.as_view()),
    path('<int:company_id>/handbook/entries', HandbookEntryListCreateView.as_view()),
    path('<int:company_id>/handbook/entries/<int:entry_id>', HandbookEntryDetailView.as_view()),
]
