from django.urls import path

from .views import HandbookEntryListCreateView


urlpatterns = [
    path('<int:company_id>/handbook/entries', HandbookEntryListCreateView.as_view()),
]
