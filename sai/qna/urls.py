from django.urls import path

from .views import AskView, ThreadMessageListView


urlpatterns = [
    path('<int:company_id>/ask', AskView.as_view()),
    path('<int:company_id>/qna/threads/<int:thread_id>/messages', ThreadMessageListView.as_view()),
]
