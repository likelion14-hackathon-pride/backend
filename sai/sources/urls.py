from django.urls import path

from .views import SourceConnectionListCreateView


urlpatterns = [
    path('<int:company_id>/source-connections', SourceConnectionListCreateView.as_view()),
]
