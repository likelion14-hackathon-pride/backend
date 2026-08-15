from django.urls import path

from .views import (
    AskView,
    EscalationApproveView,
    EscalationCheckAnswerView,
    EscalationDetailView,
    EscalationListCreateView,
    EscalationSendView,
    ThreadMessageListView,
)


urlpatterns = [
    path('<int:company_id>/ask', AskView.as_view()),
    path('<int:company_id>/qna/threads/<int:thread_id>/messages', ThreadMessageListView.as_view()),
    path('<int:company_id>/questions', EscalationListCreateView.as_view()),
    path('<int:company_id>/questions/<int:escalation_id>', EscalationDetailView.as_view()),
    path('<int:company_id>/questions/<int:escalation_id>/send', EscalationSendView.as_view()),
    path('<int:company_id>/questions/<int:escalation_id>/check-answer', EscalationCheckAnswerView.as_view()),
    path('<int:company_id>/questions/<int:escalation_id>/approve', EscalationApproveView.as_view()),
]
