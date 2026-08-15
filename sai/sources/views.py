import json
import logging

from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseForbidden
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_owner_company
from config.filters import enum_parameter, filter_enum
from config.pagination import (
    CURSOR_PARAMETER,
    LIMIT_PARAMETER,
    page_response,
    paged_response,
)

from .models import Connection, IngestionJob, Item
from .serializers import (
    AvailableChannelListSerializer,
    AvailableChannelSerializer,
    AvailableRepositoryListSerializer,
    AvailableRepositorySerializer,
    ChannelAddSerializer,
    ChannelListSerializer,
    ChannelScopeUpdateSerializer,
    ChannelSerializer,
    ConnectionListSerializer,
    ConnectionSerializer,
    IngestionJobCreateSerializer,
    IngestionJobListSerializer,
    IngestionJobSerializer,
    GitHubConnectionCreateSerializer,
    RepositoryAddSerializer,
    RepositoryListSerializer,
    RepositoryScopeUpdateSerializer,
    RepositorySerializer,
    SlackConnectionCreateSerializer,
)
from .services import (
    add_channel,
    add_repository,
    clear_connection_error,
    connect_github,
    connect_slack,
    disconnect,
    list_available_channels,
    list_available_repositories,
    remove_channel,
    remove_repository,
)
from .github import GitHubError
from .slack import SlackError
from .worker import drain
from .webhook import (
    find_connection,
    handle_event,
    verify_signature,
    verify_url_verification,
)

logger = logging.getLogger(__name__)


# 슬랙 이벤트 수신 엔드포인트.
# 슬랙은 3초 안에 200을 받지 못하면 최대 3회 재시도하므로, 여기서는 저장까지만 하고
# 분류·초안 생성 같은 AI 작업은 하지 않는다.
# 재시도로 같은 이벤트가 다시 와도 (item, external_ref) 유니크 제약이 중복을 막는다.
@csrf_exempt
@require_POST
def slack_events(request):
    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    signature = request.headers.get('X-Slack-Signature', '')

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseForbidden()

    if data.get('type') == 'url_verification':
        if not verify_url_verification(timestamp, signature, request.body):
            return HttpResponseForbidden()
        return JsonResponse({'challenge': data.get('challenge', '')})

    # 본문은 아직 신뢰할 수 없다. team_id로 연결을 찾아 그 시크릿으로 서명을 확인한 뒤에야 쓴다.
    connection = find_connection(data.get('team_id'))
    if connection is None:
        return HttpResponseForbidden()
    if not verify_signature(connection.signing_secret, timestamp, signature, request.body):
        return HttpResponseForbidden()

    try:
        handle_event(connection, data.get('event') or {})
    except Exception:
        # 여기서 500을 내면 슬랙이 같은 이벤트를 세 번 더 보낸다.
        # 실패는 로그로 남기고 200을 준다. 놓친 메시지는 다음 수집 작업이 주워 온다.
        logger.exception('슬랙 이벤트 처리 실패 team=%s', data.get('team_id'))

    return JsonResponse({'ok': True})


class SourceConnectionListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='소스 연결 목록 조회',
        responses={
            200: ConnectionListSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        connections = (
            Connection.objects.filter(company=company, disconnected_at__isnull=True)
            .prefetch_related('items')
            .order_by('kind')
        )
        return page_response(ConnectionSerializer, connections)

    @swagger_auto_schema(
        operation_summary='소스 연결',
        operation_description=(
            'provider가 SLACK이면 봇 토큰과 시그닝 시크릿으로 워크스페이스를 연결합니다. '
            'provider가 GITHUB이면 서버에 설정된 GitHub App 설치 정보를 검증해 연결합니다. '
            'GitHub App 개인키는 요청으로 받거나 응답에 포함하지 않습니다.'
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['provider'],
            properties={
                'provider': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    enum=[Connection.Kind.SLACK, Connection.Kind.GITHUB],
                    description='연동할 소스 종류',
                ),
                'botToken': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='SLACK 연결일 때 사용하는 Bot User OAuth Token',
                ),
                'signingSecret': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='SLACK 연결일 때 사용하는 Signing Secret',
                ),
            },
            example={'provider': 'GITHUB'},
        ),
        responses={
            200: ConnectionSerializer(),
            201: ConnectionSerializer(),
            400: '잘못된 요청 (소스 인증 실패 / 이미 다른 회사에 연결된 소스)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Source'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        provider = request.data.get('provider')

        if provider == Connection.Kind.GITHUB:
            serializer = GitHubConnectionCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            connection, is_created = connect_github(company)

            return Response(
                ConnectionSerializer(connection).data,
                status=status.HTTP_201_CREATED if is_created else status.HTTP_200_OK,
            )

        serializer = SlackConnectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        connection, is_created = connect_slack(
            company,
            serializer.validated_data['botToken'],
            serializer.validated_data['signingSecret'],
        )

        return Response(
            ConnectionSerializer(connection).data,
            status=status.HTTP_201_CREATED if is_created else status.HTTP_200_OK,
        )

def get_connection(company, connection_id, kind=None):
    filters = {
        'id': connection_id,
        'company': company,
        'disconnected_at__isnull': True,
    }
    if kind:
        filters['kind'] = kind

    return get_object_or_404(Connection, **filters)


class SourceConnectionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='소스 연결 해제',
        operation_description=(
            '연결을 끊습니다. 모아 둔 원문과 규칙, 카드는 그대로 남고 새 수집만 멈춥니다. '
            '웹훅으로 들어오는 메시지도 더 이상 저장하지 않습니다. '
            '토큰을 바꾸려면 해제하지 말고 슬랙 연동을 다시 호출하세요.'
        ),
        responses={
            204: '해제됨',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def delete(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        disconnect(get_connection(company, connection_id))

        return Response(status=status.HTTP_204_NO_CONTENT)


class SourceChannelListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='수집 대상 채널 목록 조회',
        operation_description='봇이 참여 중인 슬랙 채널 목록입니다. 봇이 나간 채널은 목록에서 빠집니다.',
        responses={
            200: ChannelListSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.SLACK)
        return page_response(ChannelSerializer, _registered_channels(connection))

    @swagger_auto_schema(
        operation_summary='채널 추가',
        operation_description=(
            '채널을 수집 대상으로 추가합니다. 봇이 아직 참여하지 않은 공개 채널이면 봇이 스스로 참여합니다. '
            '이때 해당 채널에 봇 참여 알림이 표시됩니다. '
            '비공개 채널은 봇이 스스로 들어갈 수 없으므로 슬랙에서 먼저 초대해야 합니다.'
        ),
        request_body=ChannelAddSerializer,
        responses={
            201: ChannelSerializer(),
            400: '잘못된 요청 (없는 채널 / 비공개 채널 참여 불가 / 슬랙 오류)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def post(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.SLACK)
        serializer = ChannelAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            item = add_channel(connection, serializer.validated_data['externalId'])
        except SlackError as exc:
            raise ValidationError({'externalId': [exc.code]})

        clear_connection_error(connection)
        response_serializer = ChannelSerializer(item)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class SourceAvailableChannelListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='추가 가능한 채널 목록',
        operation_description=(
            '아직 수집 대상으로 등록되지 않은 워크스페이스 채널입니다. '
            'isMember가 false인 공개 채널은 추가 시 봇이 자동으로 참여합니다. '
            '봇이 참여하지 않은 비공개 채널은 슬랙 특성상 목록에 나타나지 않습니다.'
        ),
        responses={
            200: AvailableChannelListSerializer(),
            400: '슬랙 조회 실패',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.SLACK)

        try:
            channels = list_available_channels(connection)
        except SlackError as exc:
            raise ValidationError({'slack': [exc.code]})

        return page_response(AvailableChannelSerializer, channels)


class SourceChannelDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='채널 지식공간 연결',
        operation_description=(
            '채널을 회사 전반 규칙 범위나 프로젝트 범위에 연결합니다. '
            '이 채널에서 뽑아낸 규칙 초안은 여기서 지정한 범위를 기본값으로 갖습니다. '
            'scopeId를 null로 보내면 연결이 해제됩니다.'
        ),
        request_body=ChannelScopeUpdateSerializer,
        responses={
            200: ChannelSerializer(),
            400: '잘못된 요청 (없는 범위 / 다른 회사의 범위)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사, 연결 또는 채널을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def patch(self, request, company_id, connection_id, item_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.SLACK)
        item = get_object_or_404(
            Item, id=item_id, connection=connection, removed_at__isnull=True
        )
        serializer = ChannelScopeUpdateSerializer(
            item, data=request.data, context={'company': company}
        )
        serializer.is_valid(raise_exception=True)
        item = serializer.save()
        response_serializer = ChannelSerializer(item)

        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='수집 대상에서 제외',
        operation_description=(
            '채널을 수집 대상에서 제외합니다. 봇은 채널에 그대로 남으며 슬랙에는 아무 알림도 가지 않습니다. '
            '이미 수집한 문서는 삭제하지 않습니다. 같은 채널을 다시 추가하면 되살아납니다.'
        ),
        responses={
            204: '제외 완료',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사, 연결 또는 채널을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def delete(self, request, company_id, connection_id, item_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.SLACK)
        item = get_object_or_404(
            Item, id=item_id, connection=connection, removed_at__isnull=True
        )
        remove_channel(item)

        return Response(status=status.HTTP_204_NO_CONTENT)


def _registered_channels(connection):
    return (
        Item.objects.filter(connection=connection, removed_at__isnull=True)
        .select_related('scope')
        .order_by('label')
    )


# GitHub 수집 대상 레포 목록 조회 / 추가 view
class SourceRepositoryListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='수집 대상 레포 목록 조회',
        operation_description='GitHub에서 수집할 레포 목록입니다. 제외한 레포는 목록에서 빠집니다.',
        responses={
            200: RepositoryListSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 GitHub 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.GITHUB)
        repositories = (
            Item.objects.filter(connection=connection, removed_at__isnull=True)
            .select_related('scope')
            .order_by('label')
        )
        return page_response(RepositorySerializer, repositories)

    @swagger_auto_schema(
        operation_summary='레포 추가',
        operation_description=(
            'GitHub App이 접근 가능한 레포를 수집 대상으로 추가합니다. '
            'App 설치 범위에 없는 레포는 추가할 수 없습니다.'
        ),
        request_body=RepositoryAddSerializer,
        responses={
            201: RepositorySerializer(),
            400: '잘못된 요청 (없는 레포 / GitHub API 오류)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 GitHub 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def post(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.GITHUB)
        serializer = RepositoryAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            repository = add_repository(connection, serializer.validated_data['externalId'])
        except GitHubError as exc:
            raise ValidationError({'externalId': [exc.code]})

        response_serializer = RepositorySerializer(repository)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


# 아직 등록되지 않은 GitHub App 접근 가능 레포
class SourceAvailableRepositoryListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='추가 가능한 레포 목록',
        operation_description='GitHub App이 접근 가능하지만 아직 수집 대상으로 등록하지 않은 레포입니다.',
        responses={
            200: AvailableRepositoryListSerializer(),
            400: 'GitHub API 오류',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 GitHub 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.GITHUB)

        try:
            repositories = list_available_repositories(connection)
        except GitHubError as exc:
            raise ValidationError({'github': [exc.code]})

        return page_response(AvailableRepositorySerializer, repositories)


# 수집 대상 레포 범위 연결 / 제외 view
class SourceRepositoryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='레포 지식공간 연결',
        operation_description=(
            '레포를 회사 전반 규칙 범위나 프로젝트 범위에 연결합니다. '
            '이 레포에서 뽑아낸 규칙 초안은 여기서 지정한 범위를 기본값으로 갖습니다. '
            'scopeId를 null로 보내면 연결이 해제됩니다.'
        ),
        request_body=RepositoryScopeUpdateSerializer,
        responses={
            200: RepositorySerializer(),
            400: '잘못된 요청 (없는 범위 / 다른 회사의 범위)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사, GitHub 연결 또는 레포를 찾을 수 없음',
        },
        tags=['Source'],
    )
    def patch(self, request, company_id, connection_id, item_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.GITHUB)
        item = get_object_or_404(
            Item, id=item_id, connection=connection, removed_at__isnull=True
        )
        serializer = RepositoryScopeUpdateSerializer(
            item, data=request.data, context={'company': company}
        )
        serializer.is_valid(raise_exception=True)
        item = serializer.save()
        response_serializer = RepositorySerializer(item)

        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='수집 대상 레포에서 제외',
        operation_description=(
            '레포를 수집 대상에서 제외합니다. 이미 수집한 문서는 삭제하지 않습니다. '
            '같은 레포를 다시 추가하면 되살아납니다.'
        ),
        responses={
            204: '제외 완료',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사, GitHub 연결 또는 레포를 찾을 수 없음',
        },
        tags=['Source'],
    )
    def delete(self, request, company_id, connection_id, item_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id, Connection.Kind.GITHUB)
        item = get_object_or_404(
            Item, id=item_id, connection=connection, removed_at__isnull=True
        )
        remove_repository(item)

        return Response(status=status.HTTP_204_NO_CONTENT)


JOB_STATUS_PARAMETER = enum_parameter('status', IngestionJob.Status)

# Swagger는 정수 배열 필드에 [0] 을 예시로 채워 넣는다.
# 그대로 보내면 없는 채널이라 400이 나므로, 기본 예시를 빈 객체로 지정한다.
INGESTION_JOB_REQUEST_BODY = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'itemIds': openapi.Schema(
            type=openapi.TYPE_ARRAY,
            items=openapi.Items(type=openapi.TYPE_INTEGER),
            description=(
                '수집할 채널 ID 목록. 생략하면 등록된 채널 전체가 대상입니다. '
                'ID는 GET /source-connections/{connectionId}/channels 로 확인하세요.'
            ),
        ),
    },
    example={},
)


class IngestionJobListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='수집 작업 목록',
        operation_description=(
            '최근 작업부터 돌려줍니다. 사람이 시작한 것과 워커가 주기적으로 만든 것이 함께 나오며, '
            'kind 로 구분합니다. COLLECT 는 슬랙에서 새로 가져온 작업, '
            'PROCESS 는 이미 받아 둔 원문만 처리한 작업입니다.'
        ),
        manual_parameters=[JOB_STATUS_PARAMETER, CURSOR_PARAMETER, LIMIT_PARAMETER],
        responses={
            200: IngestionJobListSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        jobs = IngestionJob.objects.filter(company=company)
        jobs = filter_enum(jobs, request, 'status', IngestionJob.Status)

        return paged_response(IngestionJobSerializer, jobs, request)

    @swagger_auto_schema(
        operation_summary='슬랙 메시지 수집 시작',
        operation_description=(
            '수집 대상 채널의 메시지를 원문으로 가져옵니다. 스레드 답글도 함께 수집합니다. '
            'itemIds를 생략하면 등록된 채널 전체가 대상입니다. '
            '이미 가져온 메시지는 다시 저장하지 않습니다(내용이 바뀌면 갱신). '
            '작업은 큐에 쌓이고 워커가 처리하므로 즉시 202로 응답합니다. '
            'GET /ingestion-jobs/{jobId} 로 progress 와 status 를 폴링하세요. '
            '채널당 최대 1000건까지 가져옵니다.'
        ),
        request_body=INGESTION_JOB_REQUEST_BODY,
        responses={
            202: IngestionJobSerializer(),
            400: '잘못된 요청 (수집 대상 채널 없음)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        connection = get_object_or_404(
            Connection,
            company=company,
            kind=Connection.Kind.SLACK,
            disconnected_at__isnull=True,
        )
        serializer = IngestionJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        items = Item.objects.filter(connection=connection, removed_at__isnull=True)
        requested_ids = serializer.validated_data.get('itemIds')
        if requested_ids is not None:
            items = items.filter(id__in=requested_ids)

        item_ids = list(items.values_list('id', flat=True))
        if not item_ids:
            # 어느 쪽이 문제인지 구분해 준다. Swagger 기본값 [0] 을 그대로 보내는 일이 흔하다.
            code = 'no matching channel' if requested_ids else 'no channel registered'
            raise ValidationError({'itemIds': [code]})

        job = IngestionJob.objects.create(company=company, item_ids=item_ids)

        # 수집 한 번에 LLM 호출이 수십 번 나간다. 요청 안에서 처리하면 타임아웃이다.
        # 워커(manage.py run_jobs)가 큐에서 꺼내 처리하고, 클라이언트는 진행률을 폴링한다.
        # 워커를 띄우기 번거로운 로컬에서는 INGESTION_RUN_INLINE 로 그 자리에서 돌린다.
        if settings.INGESTION_RUN_INLINE:
            drain(limit=1)
            job.refresh_from_db()

        response_serializer = IngestionJobSerializer(job)

        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)


class IngestionJobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='수집 작업 상태 조회',
        responses={
            200: IngestionJobSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 작업을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def get(self, request, company_id, job_id):
        company = get_owner_company(request.user, company_id)
        job = get_object_or_404(IngestionJob, id=job_id, company=company)
        serializer = IngestionJobSerializer(job)

        return Response(serializer.data, status=status.HTTP_200_OK)
