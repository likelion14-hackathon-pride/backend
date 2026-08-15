from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .answering import (
    PROMPT_VERSION,
    AnswerRateLimited,
    answer_question,
    find_risk_warnings,
)
from .models import Citation, Message, Thread
from .serializers import (
    AskInputSerializer,
    AskResultSerializer,
    MessageListSerializer,
    MessageSerializer,
)

# 근거가 없거나 판단이 필요한 경우는 대표 확인이 필요하다는 뜻이다.
NEEDS_OWNER = {'NO_SOURCE', 'NEEDS_DECISION'}


class AnswerUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = '답변 생성을 사용할 수 없습니다.'


# 요청한 사용자가 해당 회사의 구성원인지 확인
def get_member_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_member = Membership.objects.filter(user=user, company=company, left_at__isnull=True).exists()

    if not is_member:
        raise PermissionDenied('company permission required')

    return company


def _citation_payload(entry):
    return {
        'entryId': entry.id,
        'title': entry.title,
        'scopeName': entry.scope.name,
        'chunkId': None,
    }


# SAI에게 묻기 view
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
            thread = get_object_or_404(Thread, id=thread_id, company=company, user=request.user)
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
                **{body_field: result.answer or None},
            )
            Citation.objects.bulk_create([
                Citation(company=company, message=message, entry=entry) for entry in cited
            ])

        payload = {
            'threadId': thread.id,
            'messageId': message.id,
            'resultType': 'NEEDS_OWNER' if result.verdict in NEEDS_OWNER else 'ANSWERED',
            'verdict': result.verdict,
            'answer': result.answer or None,
            'draftKo': result.draft_ko or None,
            'citations': [_citation_payload(entry) for entry in cited],
            'warnings': warnings,
            'latencyMs': usage['latencyMs'],
        }

        return Response(AskResultSerializer(payload).data, status=status.HTTP_200_OK)


# 질문 스레드 대화 이력 view
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
