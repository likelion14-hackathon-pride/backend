from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_member_company
from config.filters import enum_parameter, filter_enum, filter_int, flag, int_parameter
from config.pagination import CURSOR_PARAMETER, LIMIT_PARAMETER, paged_response
from qna.answering import find_risk_warnings
from qna.serializers import AskResultSerializer
from qna.services import ask, open_thread

from .home import GROWTH_WEEKS, RESOLUTION_DAYS, home_for
from .models import InstructionCard, Task
from .queries import cards_for
from .serializers import (
    CardAskSerializer,
    CardDetailSerializer,
    CardListSerializer,
    CardSerializer,
    CardUpdateSerializer,
    HomeSerializer,
    TaskCreateSerializer,
    TaskListSerializer,
    TaskSerializer,
    TaskUpdateSerializer,
    TimingSerializer,
)
from .services import (
    card_context,
    check_move,
    mark_read,
    original_text,
    related_rules,
)
from .timing import BUCKET_LIMIT, timing_for
from .todos import TODO_SLOTS, todos_for

COLUMN_PARAMETER = enum_parameter(
    'column', InstructionCard.Column,
    'WAITING / ANSWERED 는 질문 상태에서 나오므로 status 와 다릅니다.',
)
SCOPE_PARAMETER = int_parameter(
    'scopeId', '프로젝트 지식공간 id. 그 프로젝트 카드만 봅니다.'
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


class HomeView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='팀원 홈',
        operation_description=(
            '홈 화면을 한 번에 그리는 데 필요한 값입니다. '
            'readToday 는 오늘(회사 시각 기준) 들어온 원문 수와 그중 카드가 된 수, '
            '그리고 내가 답을 기다리는 질문 수입니다. 앞의 둘은 같은 날짜를 보므로 '
            'cards 는 언제나 messages 이하입니다. '
            'unread 는 아직 아무도 열어 보지 않은 지시이며 read_at 이 카드마다 하나라 회사 기준입니다. '
            f'resolution 은 최근 {RESOLUTION_DAYS}일 동안 SAI가 답해 끝난 질문의 비율입니다. '
            '분모는 SAI가 답한 것과 답하지 못해 슬랙으로 보낸 것을 더한 값이며, '
            '근거가 없다고 답했어도 보내지 않았으면 세지 않습니다. '
            f'handbook.weekly 는 최근 {GROWTH_WEEKS}주에 주별로 새로 확정된 규칙 수입니다(오래된 주부터). '
            '확정 시각이 없는 옛 항목은 어느 주에도 들어가지 않아 주별 합이 confirmed 보다 작을 수 있습니다. '
            '시차 칩은 /timing 을 따로 부릅니다.'
        ),
        responses={
            200: HomeSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Task'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)

        return Response(
            HomeSerializer(home_for(company, request.user)).data,
            status=status.HTTP_200_OK,
        )


class TaskListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='할 일 목록',
        operation_description=(
            f'홈 화면의 할 일입니다. 미완료가 {TODO_SLOTS}칸보다 적으면 '
            '나에게 배정된 Ready 카드에서 최신순으로 채워 넣습니다. '
            '완료한 항목은 목록 아래로 내려가고 하루 뒤에 사라집니다. '
            'origin 이 CARD 면 카드에서 담긴 것, SELF 면 직접 적은 것입니다. '
            '카드에서 담긴 할 일을 완료해도 카드의 상태는 바뀌지 않습니다.'
        ),
        responses={
            200: TaskListSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Task'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        tasks = todos_for(company, request.user)

        return Response(
            {'items': TaskSerializer(tasks, many=True).data}, status=status.HTTP_200_OK
        )

    @swagger_auto_schema(
        operation_summary='할 일 직접 추가',
        operation_description='카드 없이 내가 적는 할 일입니다. origin 은 SELF 가 됩니다.',
        request_body=TaskCreateSerializer,
        responses={
            201: TaskSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Task'],
    )
    def post(self, request, company_id):
        company = get_member_company(request.user, company_id)
        serializer = TaskCreateSerializer(
            data=request.data, context={'company': company, 'user': request.user}
        )
        serializer.is_valid(raise_exception=True)
        task = serializer.save()

        return Response(TaskSerializer(task).data, status=status.HTTP_201_CREATED)


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _task(self, request, company_id, task_id):
        company = get_member_company(request.user, company_id)

        return get_object_or_404(
            Task.objects.select_related('scope', 'card__document__author_identity'),
            id=task_id, company=company, user=request.user,
        )

    @swagger_auto_schema(
        operation_summary='할 일 수정 · 완료',
        operation_description=(
            'status 를 DONE 으로 보내면 완료입니다. 되돌리려면 TODO 로 보냅니다. '
            '카드에서 담긴 할 일이어도 카드의 상태는 건드리지 않습니다.'
        ),
        request_body=TaskUpdateSerializer,
        responses={
            200: TaskSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 할 일을 찾을 수 없음',
        },
        tags=['Task'],
    )
    def patch(self, request, company_id, task_id):
        task = self._task(request, company_id, task_id)
        serializer = TaskUpdateSerializer(task, data=request.data)
        serializer.is_valid(raise_exception=True)

        return Response(TaskSerializer(serializer.save()).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='할 일 삭제',
        operation_description='카드에서 담긴 할 일을 지워도 카드는 남습니다.',
        responses={
            204: '삭제됨',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 할 일을 찾을 수 없음',
        },
        tags=['Task'],
    )
    def delete(self, request, company_id, task_id):
        self._task(request, company_id, task_id).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class TimingView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='시차 · 응답 대기 현황',
        operation_description=(
            '내 시각과 대표 시각, 지금이 근무시간인지, 지금 보내면 언제쯤 답이 오는지를 돌려줍니다. '
            'state 는 근무시간 기준이며 접속 여부가 아닙니다. 근무시간을 쓰지 않는 회사는 UNKNOWN 입니다. '
            'replyExpected.basis 가 HISTORY 면 실제 답변 이력의 중앙값, '
            'WORKING_HOURS 면 표본이 모자라 다음 근무 시작 시각을 쓴 것입니다. '
            'canDo 는 대표의 답을 기다리지 않는 카드의 단계로, 핸드북 근거가 있는 것부터 나옵니다. '
            'entryId 가 있으면 그 규칙이 근거이고 없으면 근거로 삼을 규칙이 없다는 뜻입니다. '
            'needsPerson 은 대표의 답이 있어야 풀리는 미정 항목이며, '
            '미정 항목이 남은 카드의 단계는 canDo 에 나오지 않습니다. '
            f'두 목록은 각각 {BUCKET_LIMIT}건까지 나오고 전체 개수는 canDoTotal / needsPersonTotal 입니다.'
        ),
        manual_parameters=[SCOPE_PARAMETER, MINE_PARAMETER],
        responses={
            200: TimingSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Card'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        cards = filter_int(cards_for(company), request, 'scopeId', 'scope_id')
        if flag(request, 'mine'):
            cards = cards.filter(assignee=request.user)

        return Response(
            TimingSerializer(timing_for(company, request.user, cards)).data,
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
