from django.urls import path

from .views import CardDetailView, CardListView


urlpatterns = [
    path('<int:company_id>/cards', CardListView.as_view()),
    path('<int:company_id>/cards/<int:card_id>', CardDetailView.as_view()),
]
