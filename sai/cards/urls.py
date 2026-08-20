from django.urls import path

from .views import (
    CardAskView,
    CardDetailView,
    CardListView,
    HomeView,
    TaskDetailView,
    TaskListCreateView,
    TimingView,
)


urlpatterns = [
    path('<int:company_id>/home', HomeView.as_view()),
    path('<int:company_id>/cards', CardListView.as_view()),
    path('<int:company_id>/cards/<int:card_id>', CardDetailView.as_view()),
    path('<int:company_id>/cards/<int:card_id>/ask', CardAskView.as_view()),
    path('<int:company_id>/tasks', TaskListCreateView.as_view()),
    path('<int:company_id>/tasks/<int:task_id>', TaskDetailView.as_view()),
    path('<int:company_id>/timing', TimingView.as_view()),
]
