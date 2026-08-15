from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import no_body, swagger_auto_schema
from openai import OpenAIError
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from cards.models import Blank
from companies.access import get_member_company, get_owner_company
from handbook.models import CompanyScope, HandbookEntry, HandbookEvidence
from sources.models import Item
from sources.slack import SlackError

from .answering import (
    PROMPT_VERSION,
    AnswerRateLimited,
    answer_question,
    find_risk_warnings,
)
from .escalation import draft_from_blank, fetch_reply, judge_reply, send_to_slack
from .models import Citation, Escalation, Message, Thread
from .serializers import (
    AskInputSerializer,
    AskResultSerializer,
    EscalationCreateSerializer,
    EscalationDraftUpdateSerializer,
    EscalationListSerializer,
    EscalationSendSerializer,
    EscalationSerializer,
    MessageListSerializer,
    MessageSerializer,
)

STATUS_PARAMETER = openapi.Parameter(
    'status',
    openapi.IN_QUERY,
    type=openapi.TYPE_STRING,
    enum=['DRAFT', 'SENT', 'ANSWERED', 'APPROVED', 'DISMISSED'],
)

# 근거가 없거나 판단이 필요한 경우는 대표 확인이 필요하다는 뜻이다.
NEEDS_OWNER = {'NO_SOURCE', 'NEEDS_DECISION'}


class AnswerUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = '답변 생성을 사용할 수 없습니다.'


def _citation_payload(source):
    return source.payload()


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
        question = serializer.validated_data['question']
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
            thread = Thread.objects.create(company=company, user=request.user, scope=scope)

        lang = request.user.ui_language if request.user.ui_language in ('ko', 'en') else 'en'
        body_field = 'body_en' if lang == 'en' else 'body_ko'

        Message.objects.create(
            company=company, thread=thread, role=Message.Role.USER, **{body_field: question}
        )

        try:
            result, cited, retrieval, usage = answer_question(company, question, lang, scope)
        except AnswerRateLimited as exc:
            response = Response(
                {'detail': 'AI 사용량 한도에 걸렸습니다. 잠시 후 다시 시도해 주세요.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response['Retry-After'] = str(exc.retry_after)
            return response
        except (ImproperlyConfigured, RuntimeError) as exc:
            raise AnswerUnavailable(str(exc))

        warnings = find_risk_warnings(company, question, result.answer)

        bodies = {body_field: result.answer or None}
        # 대표 확인이 필요한 답변은 한국어 초안이 본체다.
        # 여기서 저장해 두지 않으면 나중에 에스컬레이션을 만들 때 초안을 잃어버린다.
        if result.verdict in NEEDS_OWNER and result.draft_ko:
            bodies['body_ko'] = result.draft_ko

        with transaction.atomic():
            message = Message.objects.create(
                company=company,
                thread=thread,
                role=Message.Role.AI,
                verdict=result.verdict,
                model=usage['model'],
                prompt_version=PROMPT_VERSION,
                prompt_tokens=usage['promptTokens'],
                completion_tokens=usage['completionTokens'],
                # 청크 본문은 넣지 않는다. id와 점수만 남긴다.
                retrieval=retrieval,
                latency_ms=usage['latencyMs'],
                **bodies,
            )
            Citation.objects.bulk_create([
                Citation(
                    company=company, message=message,
                    entry=source.entry, chunk=source.chunk,
                )
                for source in cited
            ])

        payload = {
            'threadId': thread.id,
            'messageId': message.id,
            'resultType': 'NEEDS_OWNER' if result.verdict in NEEDS_OWNER else 'ANSWERED',
            'verdict': result.verdict,
            'answer': result.answer or None,
            'draftKo': result.draft_ko or None,
            'citations': [_citation_payload(source) for source in cited],
            'warnings': warnings,
            'latencyMs': usage['latencyMs'],
        }

        return Response(AskResultSerializer(payload).data, status=status.HTTP_200_OK)


def _visible_escalations(company, user):
    queryset = Escalation.objects.filter(company=company).select_related('asked_by')
    is_owner = Membership.objects.filter(
        user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True
    ).exists()
    # 대표는 전부 보고, 팀원은 자기가 올린 것만 본다.
    return queryset if is_owner else queryset.filter(asked_by=user)


class EscalationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 확인 질문 목록',
        operation_description='대표는 회사 전체를, 팀원은 본인이 올린 것만 봅니다.',
        manual_parameters=[STATUS_PARAMETER],
        responses={200: EscalationListSerializer(), 401: '인증되지 않음', 403: '회사 접근 권한 없음'},
        tags=['Question'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        escalations = _visible_escalations(company, request.user)

        escalation_status = request.query_params.get('status')
        if escalation_status:
            escalations = escalations.filter(status=escalation_status)

        serializer = EscalationSerializer(escalations.order_by('-created_at'), many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)

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

        origin = None
        blank = None
        scope = None
        question_en = data.get('questionEn')
        draft_ko = data.get('draftKo')

        if data.get('blankId'):
            blank = get_object_or_404(
                Blank.objects.select_related('card', 'card__scope', 'card__document'),
                id=data['blankId'], company=company,
            )
            if blank.escalation_id:
                raise ValidationError({'blankId': ['already escalated']})

            scope = blank.card.scope
            question_en = question_en or blank.question_en
            if not draft_ko:
                try:
                    draft_ko = draft_from_blank(blank)
                except ImproperlyConfigured as exc:
                    raise AnswerUnavailable(str(exc))
                except (OpenAIError, ValueError) as exc:
                    raise AnswerUnavailable(f'draft_failed: {type(exc).__name__}')

        if data.get('messageId'):
            origin = get_object_or_404(
                Message.objects.select_related('thread'),
                id=data['messageId'], company=company, thread__user=request.user,
            )
            if hasattr(origin, 'escalation'):
                raise ValidationError({'messageId': ['already escalated']})
            question = Message.objects.filter(
                thread=origin.thread, role=Message.Role.USER, id__lt=origin.id
            ).order_by('-id').first()
            question_en = question_en or (question.body_en or question.body_ko if question else None)
            draft_ko = draft_ko or _draft_from_message(origin)

        if not question_en:
            raise ValidationError({'questionEn': ['question text not found']})
        # 초안이 없으면 영어 원문이 그대로 대표에게 나간다. 그럴 바엔 막고 받는다.
        if not draft_ko:
            raise ValidationError({'draftKo': ['korean draft required']})

        with transaction.atomic():
            escalation = Escalation.objects.create(
                company=company,
                asked_by=request.user,
                scope=origin.thread.scope if origin else scope,
                origin_message=origin,
                question_en=question_en,
                draft_ko=draft_ko,
            )
            if blank is not None:
                blank.escalation = escalation
                blank.save(update_fields=['escalation'])

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_201_CREATED)


# AI 답변 메시지에 저장해 둔 한국어 초안. NEEDS_OWNER 판정일 때만 채워져 있다.
def _draft_from_message(message):
    if message.verdict not in NEEDS_OWNER:
        return None

    return (message.body_ko or '').strip() or None


class EscalationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 확인 질문 상세',
        responses={200: EscalationSerializer(), 401: '인증되지 않음', 403: '회사 접근 권한 없음', 404: '없음'},
        tags=['Question'],
    )
    def get(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(_visible_escalations(company, request.user), id=escalation_id)

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='발송 전 초안 수정',
        operation_description='아직 보내지 않은 질문만 고칠 수 있습니다.',
        request_body=EscalationDraftUpdateSerializer,
        responses={200: EscalationSerializer(), 400: '이미 발송됨', 401: '인증되지 않음', 404: '없음'},
        tags=['Question'],
    )
    def patch(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(_visible_escalations(company, request.user), id=escalation_id)

        if escalation.status != Escalation.Status.DRAFT:
            raise ValidationError({'draftKo': ['already sent']})

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
            raise ValidationError({'status': ['already approved']})

        escalation.status = Escalation.Status.DISMISSED
        escalation.save(update_fields=['status'])

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


class EscalationSendView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='슬랙으로 질문 발송',
        operation_description=(
            '지정한 채널에 한국어 질문을 올립니다. 대표가 그 스레드에 답장하면 check-answer로 회수합니다. '
            '한 번 보낸 질문은 다시 보낼 수 없습니다.'
        ),
        request_body=EscalationSendSerializer,
        responses={
            200: EscalationSerializer(), 400: '이미 발송됨 / 슬랙 오류',
            401: '인증되지 않음', 403: '회사 접근 권한 없음', 404: '질문 또는 채널 없음',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_member_company(request.user, company_id)
        escalation = get_object_or_404(_visible_escalations(company, request.user), id=escalation_id)
        serializer = EscalationSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if escalation.status != Escalation.Status.DRAFT:
            raise ValidationError({'status': ['already sent']})

        item = get_object_or_404(
            Item, id=serializer.validated_data['itemId'], company=company, removed_at__isnull=True
        )
        try:
            escalation = send_to_slack(escalation, item)
        except SlackError as exc:
            raise ValidationError({'slack': [exc.code]})

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
        escalation = get_object_or_404(_visible_escalations(company, request.user), id=escalation_id)

        if escalation.status == Escalation.Status.DRAFT:
            raise ValidationError({'status': ['not sent yet']})

        try:
            reply, text = fetch_reply(escalation)
        except SlackError as exc:
            raise ValidationError({'slack': [exc.code]})

        if reply is None:
            return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)

        try:
            judgement = judge_reply(escalation.question_en, escalation.draft_ko, text)
        except (ImproperlyConfigured, RuntimeError) as exc:
            raise AnswerUnavailable(str(exc))

        escalation.answer_is_answer = judgement.is_answer
        escalation.answer_reason = judgement.reason[:200]
        escalation.answer_needs_review = judgement.needs_review
        if judgement.is_answer:
            escalation.answer_ko = judgement.answer_ko
            escalation.answer_en = judgement.answer_en
            escalation.answered_at = timezone.now()
            escalation.status = Escalation.Status.ANSWERED
        escalation.save()

        # 카드에서 올라온 질문이면 카드에도 답을 채운다.
        # 여기서 안 채우면 답은 왔는데 카드는 그대로 비어 있다.
        if judgement.is_answer:
            escalation.card_blanks.update(
                sai_answer_ko=judgement.answer_ko, sai_answer_en=judgement.answer_en
            )

        return Response(EscalationSerializer(escalation).data, status=status.HTTP_200_OK)


# 답변을 핸드북 규칙으로 승격하는 view
class EscalationApproveView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='답변을 핸드북 규칙으로 승격',
        operation_description=(
            '대표 답변을 핸드북 초안으로 만듭니다. 다음 사람이 같은 질문을 하면 Ask SAI가 바로 답할 수 있게 됩니다. '
            '만들어진 항목은 DRAFT이며 확정은 별도로 해야 합니다.'
        ),
        request_body=no_body,
        responses={
            201: EscalationSerializer(), 400: '아직 답변이 없음',
            401: '인증되지 않음', 403: 'Owner 권한 없음', 404: '질문 없음',
        },
        tags=['Question'],
    )
    def post(self, request, company_id, escalation_id):
        company = get_owner_company(request.user, company_id)
        escalation = get_object_or_404(Escalation, id=escalation_id, company=company)

        if escalation.status != Escalation.Status.ANSWERED or not escalation.answer_ko:
            raise ValidationError({'status': ['no answer to promote']})

        scope = escalation.scope or CompanyScope.objects.filter(
            company=company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY,
        ).first()
        if scope is None:
            raise ValidationError({'scope': ['no scope available']})

        with transaction.atomic():
            entry = HandbookEntry.objects.create(
                company=company,
                scope=scope,
                title=escalation.question_en[:200],
                body_ko=escalation.answer_ko,
                body_en=escalation.answer_en or None,
                original_lang='ko',
                status=HandbookEntry.Status.DRAFT,
                origin=HandbookEntry.Origin.ESCALATION,
                confidence=HandbookEntry.Confidence.MEDIUM,
            )
            HandbookEvidence.objects.create(
                company=company,
                entry=entry,
                quote=escalation.answer_ko,
                tag=HandbookEvidence.Tag.OWNER,
                source_label='대표 확인 답변',
                speaker_name=None,
                occurred_at=escalation.answered_at,
            )
            escalation.proposed_entry = entry
            escalation.status = Escalation.Status.APPROVED
            escalation.save(update_fields=['proposed_entry', 'status'])

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
        messages = (
            Message.objects.filter(thread=thread)
            .prefetch_related('citations__entry__scope')
            .order_by('id')
        )
        serializer = MessageSerializer(messages, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)
