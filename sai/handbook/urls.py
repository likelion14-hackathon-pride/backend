from django.urls import path

from .views import (
    CompanyScopeListView,
    HandbookEntryBulkReviewView,
    HandbookEntryDetailView,
    HandbookEntryEvidenceView,
    HandbookEntryListCreateView,
    HandbookEntryReviewView,
)


urlpatterns = [
    path('<int:company_id>/handbook/scopes', CompanyScopeListView.as_view()),
    path('<int:company_id>/handbook/entries', HandbookEntryListCreateView.as_view()),
    path('<int:company_id>/handbook/entries/review-all', HandbookEntryBulkReviewView.as_view()),
    path('<int:company_id>/handbook/entries/<int:entry_id>', HandbookEntryDetailView.as_view()),
    path('<int:company_id>/handbook/entries/<int:entry_id>/evidence', HandbookEntryEvidenceView.as_view()),
    path('<int:company_id>/handbook/entries/<int:entry_id>/review', HandbookEntryReviewView.as_view()),
]
