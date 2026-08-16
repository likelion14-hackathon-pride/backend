from django.urls import path

from .views import (
    IngestionJobDetailView,
    IngestionJobListCreateView,
    SourceAvailableChannelListView,
    SourceAvailableRepositoryListView,
    SourceChannelDetailView,
    SourceChannelListView,
    SourceConnectionDetailView,
    SourceConnectionListCreateView,
    SourceFileListCreateView,
    SourceRepositoryDetailView,
    SourceRepositoryListView,
)


urlpatterns = [
    path('<int:company_id>/source-connections', SourceConnectionListCreateView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>', SourceConnectionDetailView.as_view()),
    path('<int:company_id>/source-files', SourceFileListCreateView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels', SourceChannelListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels/available', SourceAvailableChannelListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/channels/<int:item_id>', SourceChannelDetailView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/repositories', SourceRepositoryListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/repositories/available', SourceAvailableRepositoryListView.as_view()),
    path('<int:company_id>/source-connections/<int:connection_id>/repositories/<int:item_id>', SourceRepositoryDetailView.as_view()),
    path('<int:company_id>/ingestion-jobs', IngestionJobListCreateView.as_view()),
    path('<int:company_id>/ingestion-jobs/<int:job_id>', IngestionJobDetailView.as_view()),
]
