import json
import logging

from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseForbidden
from drf_yasg import openapi
from drf_yasg.utils import no_body, swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .ingestion import run_ingestion
from .models import Connection, IngestionJob, Item
from .serializers import (
    AvailableChannelListSerializer,
    AvailableChannelSerializer,
    ChannelAddSerializer,
    ChannelListSerializer,
    ChannelScopeUpdateSerializer,
    ChannelSerializer,
    ConnectionListSerializer,
    ConnectionSerializer,
    IngestionJobCreateSerializer,
    IngestionJobSerializer,
    SlackConnectionCreateSerializer,
)
from .services import (
    add_channel,
    clear_connection_error,
    list_available_channels,
    register_channels_recording_error,
    remove_channel,
)
from .slack import SlackClient, SlackError
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


# 요청한 사용자가 해당 회사의 대표인지 확인
def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# 소스 연결 목록 조회 및 슬랙 연동 view
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
        serializer = ConnectionSerializer(connections, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='슬랙 연동',
        operation_description=(
            '봇 토큰과 시그닝 시크릿을 받아 슬랙 워크스페이스를 연결합니다. '
            '저장 전에 슬랙 auth.test로 토큰을 검증하며, 실패하면 아무것도 저장하지 않습니다. '
            '이미 연결된 회사가 다시 호출하면 자격증명을 교체하고 200을 반환합니다. '
            '자격증명은 암호화해 저장하며 응답에 포함하지 않습니다.'
        ),
        request_body=SlackConnectionCreateSerializer,
        responses={
            200: ConnectionSerializer(),
            201: ConnectionSerializer(),
            400: '잘못된 요청 (토큰 형식 오류 / 슬랙 인증 실패 / 이미 다른 회사에 연결된 워크스페이스)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Source'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = SlackConnectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bot_token = serializer.validated_data['botToken']
        signing_secret = serializer.validated_data['signingSecret']

        # 저장 전에 슬랙에 직접 물어본다. 잘못된 키를 DB에 남기지 않기 위함.
        try:
            auth = SlackClient(bot_token).auth_test()
        except SlackError as exc:
            # 시리얼라이저 검증 오류와 같은 {"field": ["code"]} 형태로 맞춘다.
            raise ValidationError({'botToken': [exc.code]})

        workspace_id = auth.get('team_id')

        # 한 워크스페이스가 두 회사에 붙으면 웹훅의 team_id로 회사를 특정할 수 없다.
        is_taken = (
            Connection.objects.filter(
                external_workspace_id=workspace_id, disconnected_at__isnull=True
            )
            .exclude(company=company)
            .exists()
        )
        if is_taken:
            raise ValidationError({'botToken': ['workspace already connected to another company']})

        connection = Connection.objects.filter(
            company=company, kind=Connection.Kind.SLACK, disconnected_at__isnull=True
        ).first()
        is_created = connection is None

        if is_created:
            connection = Connection(company=company, kind=Connection.Kind.SLACK)

        connection.status = Connection.Status.CONNECTED
        connection.external_workspace_id = workspace_id
        connection.display_name = auth.get('team')
        # 웹훅에서 permalink를 조립할 때 쓴다. 여기서 받아 두면 나중에 부를 일이 없다.
        connection.workspace_url = auth.get('url')
        connection.bot_token = bot_token
        connection.signing_secret = signing_secret
        connection.error_message = None
        connection.save()

        # 봇이 이미 들어가 있는 채널을 바로 등록한다. 실패해도 연결은 유지하고 status에 남긴다.
        if is_created:
            register_channels_recording_error(connection)

        response_serializer = ConnectionSerializer(connection)
        response_status = status.HTTP_201_CREATED if is_created else status.HTTP_200_OK

        return Response(response_serializer.data, status=response_status)


def get_connection(company, connection_id):
    return get_object_or_404(
        Connection, id=connection_id, company=company, disconnected_at__isnull=True
    )


# 수집 대상 채널 목록 조회 view
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
        connection = get_connection(company, connection_id)
        serializer = ChannelSerializer(_registered_channels(connection), many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)

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
        connection = get_connection(company, connection_id)
        serializer = ChannelAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            item = add_channel(connection, serializer.validated_data['externalId'])
        except SlackError as exc:
            raise ValidationError({'externalId': [exc.code]})

        clear_connection_error(connection)
        response_serializer = ChannelSerializer(item)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


# 추가 가능한 채널 목록 view
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
        connection = get_connection(company, connection_id)

        try:
            channels = list_available_channels(connection)
        except SlackError as exc:
            raise ValidationError({'slack': [exc.code]})

        serializer = AvailableChannelSerializer(channels, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)


# 수집 대상 채널 지식공간 연결 / 제외 view
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
        connection = get_connection(company, connection_id)
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
        connection = get_connection(company, connection_id)
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


# 슬랙 메시지 수집 작업 view
class IngestionJobListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='슬랙 메시지 수집 시작',
        operation_description=(
            '수집 대상 채널의 메시지를 원문으로 가져옵니다. 스레드 답글도 함께 수집합니다. '
            'itemIds를 생략하면 등록된 채널 전체가 대상입니다. '
            '이미 가져온 메시지는 다시 저장하지 않습니다(내용이 바뀌면 갱신). '
            '현재는 요청 안에서 동기로 처리하며 채널당 최대 1000건까지만 가져옵니다.'
        ),
        request_body=INGESTION_JOB_REQUEST_BODY,
        responses={
            201: IngestionJobSerializer(),
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
        # 지금은 동기 실행. 데이터가 커지면 이 한 줄만 큐 적재로 바꾸면 된다.
        run_ingestion(job, connection)

        response_serializer = IngestionJobSerializer(job)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


# 수집 작업 상태 조회 view
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