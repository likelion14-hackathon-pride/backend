from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_member_company
from config.filters import enum_parameter, filter_enum, filter_int, flag
from config.pagination import CURSOR_PARAMETER, LIMIT_PARAMETER, paged_response
from qna.answering import find_risk_warnings
from qna.serializers import AskResultSerializer
from qna.services import ask, open_thread

from .models import InstructionCard
from .queries import cards_for
from .serializers import (
    CardAskSerializer,
    CardDetailSerializer,
    CardListSerializer,
    CardSerializer,
    CardUpdateSerializer,
)
from .services import (
    card_context,
    check_move,
    mark_read,
    original_text,
    related_rules,
)

COLUMN_PARAMETER = enum_parameter(
    'column', InstructionCard.Column,
    'WAITING / ANSWERED 는 질문 상태에서 나오므로 status 와 다릅니다.',
)
SCOPE_PARAMETER = openapi.Parameter(
    'scopeId', openapi.IN_QUERY, type=openapi.TYPE_INTEGER,
    description='프로젝트 지식공간 id. 그 프로젝트 카드만 봅니다.',
)
MINE_PARAMETER = openapi.Parameter(
    'mine', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN,
    description='true 면 나에게 배정된 카드만 봅니다.',
)


def _detailed(company, card_id):
    card = get_object_or_404(
        cards_for(company).prefetch_related(
            'steps__entry', 'blanks__escalation', 'tone_evidences'
        ),
        id=card_id,
    )
    card.relatedRules = related_rules(card)
    card.riskWarnings = find_risk_warnings(company, original_text(card))
    card.questions = [blank for blank in card.blanks.all() if blank.escalation_id]

    return card


class CardListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='지시 카드 목록',
        operation_description=(
            '슬랙 지시를 해석한 카드입니다. mine=true 로 내게 배정된 것만 볼 수 있습니다. '
            '담당자는 슬랙 멘션과 이메일이 일치하는 계정으로 자동 지정되며, 없으면 비어 있습니다. '
            '같은 요청이 여러 번 올라온 경우 처음 것만 나오고 duplicateCount 로 반복 횟수를 알립니다.'
        ),
        manual_parameters=[
            COLUMN_PARAMETER, SCOPE_PARAMETER, MINE_PARAMETER,
            CURSOR_PARAMETER, LIMIT_PARAMETER,
        ],
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
        cards = cards_for(company).filter(duplicate_of__isnull=True).prefetch_related('blanks')
        cards = filter_enum(cards, request, 'column', InstructionCard.Column)
        cards = filter_int(cards, request, 'scopeId', 'scope_id')
        if flag(request, 'mine'):
            cards = cards.filter(assignee=request.user)

        return paged_response(CardSerializer, cards, request)


class CardDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='지시 카드 상세',
        operation_description=(
            '수행 단계, 미정 항목, 말투 해석 근거를 함께 돌려줍니다. '
            'relatedRules 는 이 지시에 걸리는 확정 규칙, riskWarnings 는 대표가 등록한 위험 작업, '
            'questions 는 이 카드에서 대표에게 보낸 질문입니다. '
            '여는 순간 읽음으로 표시됩니다.'
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
        card = _detailed(company, card_id)
        mark_read(card)

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
        card = get_object_or_404(cards_for(company), id=card_id)
        serializer = CardUpdateSerializer(
            card, data=request.data, context={'company': company}
        )
        serializer.is_valid(raise_exception=True)

        target = serializer.validated_data.get('status')
        if target:
            check_move(card.column, target)

        serializer.save()

        # 상태가 바뀌면 열도 바뀐다. 계산된 값을 다시 받으려면 새로 읽어야 한다.
        return Response(
            CardDetailSerializer(_detailed(company, card_id)).data,
            status=status.HTTP_200_OK,
        )


class CardAskView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='카드에 대해 SAI에게 묻기',
        operation_description=(
            '카드의 원문과 목적을 함께 넘겨 이 지시에 대한 질문으로 답합니다. '
            '카드의 지식공간을 기본 검색 범위로 씁니다. '
            '근거가 없으면 미정 항목으로 올려 대표에게 물어보세요.'
        ),
        request_body=CardAskSerializer,
        responses={
            200: AskResultSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 카드를 찾을 수 없음',
            429: 'AI 사용량 한도 초과',
            503: '답변 생성 불가',
        },
        tags=['Card'],
    )
    def post(self, request, company_id, card_id):
        company = get_member_company(request.user, company_id)
        card = get_object_or_404(cards_for(company), id=card_id)
        serializer = CardAskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        thread = open_thread(company, request.user, card.scope)
        payload = ask(
            company, request.user, thread,
            serializer.validated_data['question'],
            card.scope,
            context=card_context(card),
        )

        return Response(AskResultSerializer(payload).data, status=status.HTTP_200_OK)
