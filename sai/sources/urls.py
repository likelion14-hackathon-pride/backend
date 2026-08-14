from django.urls import path

from .views import (
    SourceChannelListView,
    SourceChannelSyncView,
    SourceConnectionListCreateView,
)


urlpatterns = [
    path('<int:company_id>/source-connections', SourceConnectionListCreateView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels', SourceChannelListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels/sync', SourceChannelSyncView.as_view()),
]
