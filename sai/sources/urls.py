from django.urls import path

from .views import (
    SourceAvailableChannelListView,
    SourceChannelDetailView,
    SourceChannelListView,
    SourceConnectionListCreateView,
)


urlpatterns = [
    path('<int:company_id>/source-connections', SourceConnectionListCreateView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels', SourceChannelListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels/available', SourceAvailableChannelListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels/<int:item_id>', SourceChannelDetailView.as_view()),
]
