from django.db.models import Count
from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_member_company
from config.pagination import CURSOR_PARAMETER, LIMIT_PARAMETER, paginate

from .models import InstructionCard
from .serializers import (
    CardDetailSerializer,
    CardListItemSerializer,
    CardListSerializer,
    CardUpdateSerializer,
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


def _cards(company):
    return (
        InstructionCard.objects.filter(company=company)
        .select_related(
            'assignee', 'scope', 'document', 'document__item', 'document__author_identity'
        )
        # 반복 횟수를 카드마다 세면 목록 한 번에 COUNT 이 카드 수만큼 나간다.
        .annotate(duplicate_count=Count('duplicates'))
    )


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

        items, next_cursor = paginate(cards, request)

        return Response(
            {'items': CardListItemSerializer(items, many=True).data, 'nextCursor': next_cursor},
            status=status.HTTP_200_OK,
        )


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
        operation_summary='지시 카드 상태 · 담당자 변경',
        operation_description=(
            'status 와 assigneeId 를 각각 또는 함께 보낼 수 있습니다. '
            '담당자는 슬랙 멘션으로 자동 지정되므로 멘션이 없었거나 잘못 잡힌 경우 여기서 고칩니다. '
            'assigneeId 를 null 로 보내면 담당자를 비웁니다.'
        ),
        request_body=CardUpdateSerializer,
        responses={
            200: CardDetailSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 카드를 찾을 수 없음',
        },
        tags=['Card'],
    )
    def patch(self, request, company_id, card_id):
        company = get_member_company(request.user, company_id)
        card = get_object_or_404(_cards(company), id=card_id)
        serializer = CardUpdateSerializer(
            card, data=request.data, context={'company': company}
        )
        serializer.is_valid(raise_exception=True)
        card = serializer.save()

        return Response(CardDetailSerializer(card).data, status=status.HTTP_200_OK)
