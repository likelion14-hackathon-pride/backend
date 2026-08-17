from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg.utils import no_body, swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from cards.models import Blank
from companies.access import get_member_company, get_owner_company
from config.errors import (
    ALREADY_APPROVED,
    ALREADY_ESCALATED,
    ALREADY_SENT,
    NO_ANSWER_YET,
    NOT_SENT_YET,
    field_error,
)
from config.filters import enum_list_parameter, filter_enum_list
from config.pagination import (
    CURSOR_PARAMETER,
    LIMIT_PARAMETER,
    page_response,
    paged_response,
)
from sources.models import Item

from .escalation import send_to_slack
from .models import Escalation, Message, Thread
from .queries import escalations_for, messages_in
from .serializers import (
    MAX_ADDITIONS,
    AskInputSerializer,
    AskResultSerializer,
    EscalationApproveSerializer,
    EscalationCreateSerializer,
    EscalationDetailSerializer,
    EscalationDraftUpdateSerializer,
    EscalationListSerializer,
    EscalationSendSerializer,
    EscalationSerializer,
    MessageListSerializer,
    MessageSerializer,
)
from .services import (
    ask,
    collect_answer,
    create_escalation,
    draft_for_blank,
    draft_from_message,
    korean_additions,
    open_thread,
    promote_to_entry,
    proposal_for,
)

# 답변대기는 DRAFT(아직 못 보냄)와 SENT(보내고 답 기다리는 중)를 함께 본다.
STATUS_PARAMETER = enum_list_parameter(
    'status', Escalation.Status, '답변대기는 DRAFT,SENT 입니다.',
)


class AskView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='SAI에게 묻기',
        operation_description=(
            '확정된 핸드북 규칙에서만 답합니다. 근거가 없으면 지어내지 않고 NO_SOURCE로 응답하며, '
            '이때 대표에게 보낼 한국어 질문 초안(draftKo)을 함께 돌려줍니다. '
            '답변 언어는 요청한 사용자의 화면 언어를 따릅니다.'
        ),
        request_body=AskInputSerializer,
        responses={
            200: AskResultSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 스레드를 찾을 수 없음',
            429: 'AI 사용량 한도 초과 (Retry-After 헤더 참고)',
            503: '답변 생성 불가 (OpenAI 미설정 또는 장애)',
        },
        tags=['Ask SAI'],
    )
    def post(self, request, company_id):
        company = get_member_company(request.user, company_id)
        serializer = AskInputSerializer(data=request.data, context={'company': company})
        serializer.is_valid(raise_exception=True)
        scope = serializer.validated_data.get('scope')

        thread_id = serializer.validated_data.get('threadId')
        if thread_id:
            thread = get_object_or_404(
                Thread.objects.select_related('scope'),
                id=thread_id, company=company, user=request.user,
            )
            # 후속 질문에 scopeId 를 다시 안 보내면 화면에는 프로젝트가 선택돼 있는데
            # 검색만 회사 전반으로 풀린다. 스레드에 저장해 둔 것을 기본값으로 쓴다.
            scope = scope or thread.scope
        else:
            thread = open_thread(company, request.user, scope)

        payload = ask(
            company, request.user, thread, serializer.validated_data['question'], scope
        )

        return Response(AskResultSerializer(payload).data, status=status.HTTP_200_OK)


class EscalationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 확인 질문 목록',
        operation_description='대표는 회사 전체를, 팀원은 본인이 올린 것만 봅니다.',
        manual_parameters=[STATUS_PARAMETER, CURSOR_PARAMETER, LIMIT_PARAMETER],
        responses={200: EscalationListSerializer(), 401: '인증되지 않음', 403: '회사 접근 권한 없음'},
        tags=['Question'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        escalations = escalations_for(company, request.user)
        escalations = filter_enum_list(escalations, request, 'status', Escalation.Status)

        return paged_response(EscalationSerializer, escalations, request)

    @swagger_auto_schema(
        operation_summary='대표 확인 질문 초안 생성',
        operation_description=(
            '답을 얻지 못한 질문을 대표 확인 대기로 올립니다. '
            'messageId 는 Ask SAI가 NO_SOURCE / NEEDS_DECISION으로 답한 메시지이며, '
            '그 질문과 SAI가 만든 한국어 초안을 그대로 가져옵니다. '
            'blankId 는 지시 카드의 미정 항목이며, 카드의 원문과 목적을 함께 넣어 '
            '한국어 질문을 새로 만듭니다. 답이 오면 카드의 해당 항목에도 함께 채워집니다. '
            '아직 발송되지는 않으며, 초안을 확인·수정한 뒤 send를 호출해야 슬랙으로 나갑니다.'
        ),
        request_body=EscalationCreateSerializer,
        responses={
            201: EscalationSerializer(), 400: '잘못된 요청', 401: '인증되지 않음',
            403: '회사 접근 권한 없음', 404: '메시지 또는 미정 항목을 찾을 수 없음',
            503: 'AI 사용 불가',
        },
        tags=['Question'],
    )
    def post(self, request, company_id):
        company = get_member_company(request.user, company_id)
        serializer = EscalationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        origin = blank = scope = None
        question_en = data.get('questionEn')
        draft_ko = data.get('draftKo')

        if data.get('blankId'):
            blank = get_object_or_404(
                Blank.objects.select_related('card', 'card__scope', 'card__document'),
                id=data['blankId'], company=company,
            )
            if blank.escalation_id:
                raise field_error('blankId', 'already escalated', ALREADY_ESCALATED)

            scope = blank.card.scope
            question_en = question_en or blank.question_en
            draft_ko = draft_ko or draft_for_blank(blank)

        if data.get('messageId'):
            origin = get_object_or_404(
                Message.objects.select_related('thread', 'thread__card'),
                id=data['messageId'], company=company, thread__user=request.user,
            )
            if hasattr(origin, 'escalation'):
                raise field_error('messageId', 'already escalated', ALREADY_ESCALATED)
            question = Message.objects.filter(
                thread=origin.thread, role=Message.Role.USER, id__lt=origin.id
            ).order_by('-id').first()
            question_en = question_en or (question.body_en or question.body_ko if question else None)
            draft_ko = draft_ko or draft_from_message(origin)
            card = origin.thread.card
            if card is not None and question_en and draft_ko:
                blank = Blank.objects.create(
                    company=company, card=card, question_en=question_en
                )
                scope = card.scope

        escalation = create_escalation(
            company, request.user, question_en, draft_ko, scope, origin, blank
        )

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_201_CREATED)


class EscalationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 확인 질문 상세',
        operation_description=(
            '답이 온 질문에는 proposal 이 함께 나옵니다. 승인하면 어떤 규칙이 어느 계층에 '
            '저장될지 미리 보여 주는 값이며, 고쳐서 승인하려면 approve 에 그대로 담아 보냅니다.'
        ),
        responses={
            200: EscalationDetailSerializer(), 401: '인증되지 않음',
            403: '회사 접근 권한 없음', 404: '없음',
        },
        tags=['Question'],
    )
    def get(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(escalations_for(company, request.user), id=escalation_id)
        escalation.proposal = proposal_for(escalation)

        return Response(
            EscalationDetailSerializer(escalation).data, status=status.HTTP_200_OK
        )

    @swagger_auto_schema(
        operation_summary='발송 전 초안 수정',
        operation_description='아직 보내지 않은 질문만 고칠 수 있습니다.',
        request_body=EscalationDraftUpdateSerializer,
        responses={200: EscalationSerializer(), 400: '이미 발송됨', 401: '인증되지 않음', 404: '없음'},
        tags=['Question'],
    )
    def patch(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(escalations_for(company, request.user), id=escalation_id)

        if escalation.status != Escalation.Status.DRAFT:
            raise ValidationError('already sent', code=ALREADY_SENT)

        serializer = EscalationDraftUpdateSerializer(escalation, data=request.data)
        serializer.is_valid(raise_exception=True)
        escalation = serializer.save()

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


class EscalationDismissView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 확인 질문 물리기',
        operation_description=(
            '답할 필요가 없다고 판단한 질문을 목록에서 내립니다. '
            '이미 규칙으로 승격된 질문은 물릴 수 없습니다. 대표만 할 수 있습니다.'
        ),
        request_body=no_body,
        responses={
            200: EscalationSerializer(), 400: '이미 승격됨',
            401: '인증되지 않음', 403: 'Owner 권한 없음', 404: '질문을 찾을 수 없음',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_owner_company(request.user, company_id)
        escalation = get_object_or_404(
            Escalation.objects.filter(company=company), id=escalation_id
        )

        if escalation.status == Escalation.Status.APPROVED:
            raise ValidationError('already approved', code=ALREADY_APPROVED)

        escalation.status = Escalation.Status.DISMISSED
        escalation.save(update_fields=['status'])

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


class EscalationAcknowledgeView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='답 확인',
        operation_description=(
            '받은 답을 읽었다고 표시합니다. 카드가 Answered 열에서 빠집니다. '
            '질문을 올린 사람이 누릅니다.'
        ),
        request_body=no_body,
        responses={
            200: EscalationSerializer(), 400: '아직 답이 오지 않음',
            401: '인증되지 않음', 403: '회사 접근 권한 없음', 404: '질문을 찾을 수 없음',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(
            escalations_for(company, request.user), id=escalation_id
        )

        if escalation.answered_at is None:
            raise ValidationError('no answer yet', code=NO_ANSWER_YET)

        if escalation.acknowledged_at is None:
            escalation.acknowledged_at = timezone.now()
            escalation.save(update_fields=['acknowledged_at'])

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


class EscalationSendView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='슬랙으로 질문 발송',
        operation_description=(
            '지정한 채널에 한국어 질문을 올립니다. 대표가 그 스레드에 답장하면 check-answer로 회수합니다. '
            '한 번 보낸 질문은 다시 보낼 수 없습니다. '
            'extraEn 으로 팀원이 덧붙인 줄을 함께 보내면 보내는 시점에 한국어 문장으로 바뀌어 '
            f'초안 뒤에 붙습니다. 최대 {MAX_ADDITIONS}줄입니다.'
        ),
        request_body=EscalationSendSerializer,
        responses={
            200: EscalationSerializer(), 400: '이미 발송됨 / 슬랙 오류',
            401: '인증되지 않음', 403: '회사 접근 권한 없음', 404: '질문 또는 채널 없음',
            503: '한국어 변환 불가',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(escalations_for(company, request.user), id=escalation_id)
        serializer = EscalationSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if escalation.status != Escalation.Status.DRAFT:
            raise ValidationError('already sent', code=ALREADY_SENT)

        item = get_object_or_404(
            Item, id=serializer.validated_data['itemId'], company=company, removed_at__isnull=True
        )
        # 덧붙인 줄을 먼저 한국어로 바꾼다. 여기서 실패하면 아무것도 보내지 않는다.
        additions = korean_additions(serializer.validated_data.get('extraEn'))
        # SlackError 는 DomainError 라서 슬랙이 돌려준 코드가 그대로 봉투의 code 가 된다.
        escalation = send_to_slack(escalation, item, additions)

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


class EscalationCheckAnswerView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 답장 확인',
        operation_description=(
            '보낸 슬랙 스레드에 달린 답장을 가져와, 그것이 실제로 질문에 답하는지 AI가 판정합니다. '
            '"확인해볼게요" 같은 회피성 답변은 answerIsAnswer=false로 남고 상태는 그대로입니다. '
            '답이 맞으면 한국어·영어로 정리해 저장하고 ANSWERED로 바뀝니다.'
        ),
        request_body=no_body,
        responses={
            200: EscalationSerializer(), 400: '아직 발송되지 않음 / 슬랙 오류',
            401: '인증되지 않음', 404: '질문 없음', 503: '판정 불가',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(escalations_for(company, request.user), id=escalation_id)

        if escalation.status == Escalation.Status.DRAFT:
            raise ValidationError('not sent yet', code=NOT_SENT_YET)

        escalation = collect_answer(escalation)

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


class EscalationApproveView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='답변을 핸드북 규칙으로 승격',
        operation_description=(
            '대표 답변을 핸드북 규칙으로 만듭니다. 다음 사람이 같은 질문을 하면 Ask SAI가 바로 답할 수 있게 됩니다. '
            '상세의 proposal 을 그대로 저장하며, 미리보기에서 고친 title / ruleEn / scopeId 를 보내면 '
            '그 값으로 저장합니다. 저장 즉시 확정 상태가 되며 확인보관함에는 올라가지 않습니다.'
        ),
        request_body=EscalationApproveSerializer,
        responses={
            201: EscalationSerializer(), 400: '아직 답변이 없음',
            401: '인증되지 않음', 403: 'Owner 권한 없음', 404: '질문 없음',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_owner_company(request.user, company_id)
        serializer = EscalationApproveSerializer(
            data=request.data, context={'company': company}
        )
        serializer.is_valid(raise_exception=True)
        edits = serializer.validated_data
        escalation = promote_to_entry(
            get_object_or_404(Escalation, id=escalation_id, company=company),
            title=edits.get('title'),
            body_en=edits.get('ruleEn'),
            scope=edits.get('scope'),
        )

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_201_CREATED)


class ThreadMessageListView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='질문 스레드 대화 이력',
        operation_description='본인이 만든 스레드만 조회할 수 있습니다.',
        responses={
            200: MessageListSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 스레드를 찾을 수 없음',
        },
        tags=['Ask SAI'],
    )
    def get(self, request, company_id, thread_id):
        company = get_member_company(request.user, company_id)
        thread = get_object_or_404(Thread, id=thread_id, company=company, user=request.user)
        return page_response(MessageSerializer, messages_in(thread))
