from django.urls import path

from .views import HandbookEntryDetailView, HandbookEntryListCreateView


urlpatterns = [
    path('<int:company_id>/handbook/entries', HandbookEntryListCreateView.as_view()),
    path('<int:company_id>/handbook/entries/<int:entry_id>', HandbookEntryDetailView.as_view()),
]
