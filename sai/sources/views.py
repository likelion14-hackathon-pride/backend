from django.shortcuts import get_object_or_404, render
import hmac, hashlib, time, json
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseForbidden
from django.conf import settings
from drf_yasg.utils import no_body, swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .models import Connection, Item
from .serializers import (
    ChannelListSerializer,
    ChannelSerializer,
    ConnectionListSerializer,
    ConnectionSerializer,
    SlackConnectionCreateSerializer,
)
from .services import sync_channels_recording_error, sync_slack_channels
from .slack import SlackClient, SlackError


def _verify(request):
    secret = settings.SLACK_SIGNING_SECRET
    if not secret:
        return False
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{request.body.decode()}"
    mine = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, request.headers.get("X-Slack-Signature", ""))


@csrf_exempt
def slack_events(request):
    if request.method != "POST":
        return JsonResponse({"ok": True})        # 헬스체크용

    if not _verify(request):
        return HttpResponseForbidden()

    data = json.loads(request.body)

    if data.get("type") == "url_verification":
        return JsonResponse({"challenge": data["challenge"]})

    event = data.get("event", {})
    if event.get("bot_id") or event.get("subtype"):
        return JsonResponse({"ok": True})

    print("받음:", event.get("text"), "|", event.get("user"))
    return JsonResponse({"ok": True})


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
            raise ValidationError({'botToken': exc.code})

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
            raise ValidationError({'botToken': 'workspace already connected to another company'})

        connection = Connection.objects.filter(
            company=company, kind=Connection.Kind.SLACK, disconnected_at__isnull=True
        ).first()
        is_created = connection is None

        if is_created:
            connection = Connection(company=company, kind=Connection.Kind.SLACK)

        connection.status = Connection.Status.CONNECTED
        connection.external_workspace_id = workspace_id
        connection.display_name = auth.get('team')
        connection.bot_token = bot_token
        connection.signing_secret = signing_secret
        connection.error_message = None
        connection.save()

        # 연동 직후 채널 목록을 바로 가져온다. 실패해도 연결은 유지하고 status에 남긴다.
        sync_channels_recording_error(connection)

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
        channels = (
            Item.objects.filter(connection=connection, removed_at__isnull=True)
            .select_related('scope')
            .order_by('label')
        )
        serializer = ChannelSerializer(channels, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)


# 채널 목록 재동기화 view
class SourceChannelSyncView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='채널 목록 재동기화',
        operation_description=(
            '슬랙에서 채널 목록을 다시 가져옵니다. 봇을 새 채널에 초대한 뒤 호출하세요. '
            '대표가 지정해 둔 지식공간 매핑은 유지됩니다.'
        ),
        request_body=no_body,
        responses={
            200: ChannelListSerializer(),
            400: '슬랙 조회 실패',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 연결을 찾을 수 없음',
        },
        tags=['Source'],
    )
    def post(self, request, company_id, connection_id):
        company = get_owner_company(request.user, company_id)
        connection = get_connection(company, connection_id)

        # 수동 재동기화는 실패를 그대로 알려 준다. 대표가 직접 누른 동작이기 때문.
        try:
            sync_slack_channels(connection)
        except SlackError as exc:
            connection.status = Connection.Status.ERROR
            connection.error_message = exc.code
            connection.save(update_fields=['status', 'error_message'])
            raise ValidationError({'slack': exc.code})

        if connection.status != Connection.Status.CONNECTED or connection.error_message:
            connection.status = Connection.Status.CONNECTED
            connection.error_message = None
            connection.save(update_fields=['status', 'error_message'])

        channels = (
            Item.objects.filter(connection=connection, removed_at__isnull=True)
            .select_related('scope')
            .order_by('label')
        )
        serializer = ChannelSerializer(channels, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)