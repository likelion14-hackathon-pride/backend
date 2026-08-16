from django.urls import path

from .views import CardAskView, CardDetailView, CardListView, TimingView


urlpatterns = [
    path('<int:company_id>/cards', CardListView.as_view()),
    path('<int:company_id>/cards/<int:card_id>', CardDetailView.as_view()),
    path('<int:company_id>/cards/<int:card_id>/ask', CardAskView.as_view()),
    path('<int:company_id>/timing', TimingView.as_view()),
]
