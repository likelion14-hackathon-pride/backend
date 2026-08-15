from django.db.models import Count
from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .models import InstructionCard
from .serializers import (
    CardDetailSerializer,
    CardListItemSerializer,
    CardListSerializer,
    CardStatusUpdateSerializer,
)

STATUS_PARAMETER = openapi.Parameter(
    'status', openapi.IN_QUERY, type=openapi.TYPE_STRING, enum=['NEW', 'OPEN', 'DONE']
)
MINE_PARAMETER = openapi.Parameter(
    'mine',
    openapi.IN_QUERY,
    description='true 면 나에게 배정된 카드만 봅니다.',
    type=openapi.TYPE_BOOLEAN,
)
CURSOR_PARAMETER = openapi.Parameter('cursor', openapi.IN_QUERY, type=openapi.TYPE_STRING)
LIMIT_PARAMETER = openapi.Parameter(
    'limit', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, default=20
)


def get_member_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_member = Membership.objects.filter(
        user=user, company=company, left_at__isnull=True
    ).exists()

    if not is_member:
        raise PermissionDenied('company permission required')

    return company


def _cards(company):
    return (
        InstructionCard.objects.filter(company=company)
        .select_related(
            'assignee', 'scope', 'document', 'document__item', 'document__author_identity'
        )
        # 반복 횟수를 카드마다 세면 목록 한 번에 COUNT 이 카드 수만큼 나간다.
        .annotate(duplicate_count=Count('duplicates'))
    )


# 지시 카드 목록 view
class CardListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='지시 카드 목록',
        operation_description=(
            '슬랙 지시를 해석한 카드입니다. mine=true 로 내게 배정된 것만 볼 수 있습니다. '
            '담당자는 슬랙 멘션과 이메일이 일치하는 계정으로 자동 지정되며, 없으면 비어 있습니다. '
            '같은 요청이 여러 번 올라온 경우 처음 것만 나오고 duplicateCount 로 반복 횟수를 알립니다.'
        ),
        manual_parameters=[STATUS_PARAMETER, MINE_PARAMETER, CURSOR_PARAMETER, LIMIT_PARAMETER],
        responses={
            200: CardListSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Card'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        # 같은 요청을 반복해 올린 것은 목록에 한 번만 보인다. 나머지는 원본에 묶여 있다.
        cards = _cards(company).filter(duplicate_of__isnull=True).prefetch_related('blanks')

        card_status = request.query_params.get('status')
        if card_status:
            if card_status not in InstructionCard.Status.values:
                raise ValidationError({'status': ['invalid status']})
            cards = cards.filter(status=card_status)

        if request.query_params.get('mine', '').lower() in ('1', 'true'):
            cards = cards.filter(assignee=request.user)

        cursor = request.query_params.get('cursor')
        if cursor:
            try:
                cards = cards.filter(id__lt=int(cursor))
            except ValueError:
                raise ValidationError({'cursor': ['invalid cursor']})

        try:
            limit = int(request.query_params.get('limit', 20))
        except ValueError:
            raise ValidationError({'limit': ['limit must be an integer']})

        if limit < 1 or limit > 100:
            raise ValidationError({'limit': ['limit must be between 1 and 100']})

        rows = list(cards.order_by('-id')[:limit + 1])
        has_next = len(rows) > limit
        items = rows[:limit]
        serializer = CardListItemSerializer(items, many=True)
        next_cursor = str(items[-1].id) if has_next else None

        return Response(
            {'items': serializer.data, 'nextCursor': next_cursor}, status=status.HTTP_200_OK
        )


# 지시 카드 상세 / 상태 변경 view
class CardDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='지시 카드 상세',
        operation_description=(
            '수행 단계, 미정 항목, 말투 해석 근거를 함께 돌려줍니다. '
            'toneEvidences 의 quote 는 과거 대화에서 그대로 발췌한 문장이며, '
            '원문과 대조에 실패한 인용은 저장되지 않습니다.'
        ),
        responses={
            200: CardDetailSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 카드를 찾을 수 없음',
        },
        tags=['Card'],
    )
    def get(self, request, company_id, card_id):
        company = get_member_company(request.user, company_id)
        card = get_object_or_404(
            _cards(company).prefetch_related('steps__entry', 'blanks', 'tone_evidences'),
            id=card_id,
        )

        return Response(CardDetailSerializer(card).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='지시 카드 상태 변경',
        request_body=CardStatusUpdateSerializer,
        responses={
            200: CardDetailSerializer(),
            400: '잘못된 상태',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 카드를 찾을 수 없음',
        },
        tags=['Card'],
    )
    def patch(self, request, company_id, card_id):
        company = get_member_company(request.user, company_id)
        card = get_object_or_404(_cards(company), id=card_id)
        serializer = CardStatusUpdateSerializer(card, data=request.data)
        serializer.is_valid(raise_exception=True)
        card = serializer.save()

        return Response(CardDetailSerializer(card).data, status=status.HTTP_200_OK)
