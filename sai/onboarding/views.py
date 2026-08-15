from django.core.exceptions import ImproperlyConfigured
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
from handbook.finalizing import finalize_entries
from handbook.models import CompanyScope, HandbookEntry
from policy.models import RiskKeyword
from sources.models import Connection

from . import questions, services
from .models import Question
from .serializers import (
    OnboardingCompleteSerializer,
    OnboardingQuestionSerializer,
    OnboardingQuestionUpdateSerializer,
    OnboardingSerializer,
    OnboardingStepSerializer,
)

SCOPE_PARAMETER = openapi.Parameter(
    'scopeId',
    openapi.IN_QUERY,
    description='프로젝트 지식공간 id. 주면 그 프로젝트 질문 8개를, 생략하면 회사 질문 19개를 돌려줍니다.',
    type=openapi.TYPE_INTEGER,
)


def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# 프로젝트 질문일 때만 쓰는 지식공간. 회사 질문에 보내면 거절한다.
def _get_project_scope(company, scope_id):
    if scope_id is None:
        return None

    scope = get_object_or_404(CompanyScope, id=scope_id, company=company)
    if scope.kind != CompanyScope.Kind.PROJECT:
        raise ValidationError({'scopeId': ['project scope required']})

    return scope


class OnboardingView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='온보딩 진행 조회',
        operation_description=(
            '고정 질문 목록과 지금까지의 답을 함께 돌려줍니다. '
            '질문 문구와 선택지는 서버가 정해 두었고, status 는 PENDING / ANSWERED / SKIPPED 입니다. '
            'options 가 비어 있으면 자유 입력만 받는 질문입니다.'
        ),
        manual_parameters=[SCOPE_PARAMETER],
        responses={
            200: OnboardingSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 지식공간을 찾을 수 없음',
        },
        tags=['Onboarding'],
    )
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        scope = _get_project_scope(company, request.query_params.get('scopeId'))
        serializer = OnboardingSerializer(
            {
                'onboardingStep': company.onboarding_step,
                'questions': services.list_questions(company, scope),
            }
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='온보딩 진행 단계 저장',
        request_body=OnboardingStepSerializer,
        responses={
            200: OnboardingSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Onboarding'],
    )
    def patch(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = OnboardingStepSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        company.onboarding_step = serializer.validated_data['onboardingStep']
        company.save(update_fields=['onboarding_step'])

        response_serializer = OnboardingSerializer(
            {
                'onboardingStep': company.onboarding_step,
                'questions': services.list_questions(company),
            }
        )

        return Response(response_serializer.data, status=status.HTTP_200_OK)


class OnboardingQuestionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='온보딩 질문 답변 저장',
        operation_description=(
            'templateKey 로 지정합니다. 선택지를 고르면 그 문구를, 직접 입력하면 쓴 내용을 answerKo 로 보냅니다. '
            '답은 그 자리에서 확정 규칙이 됩니다. 다시 답하면 같은 규칙을 덮어씁니다. '
            'status=SKIPPED 로 보내면 앞서 만든 규칙도 함께 지웁니다. '
            '프로젝트 질문은 scopeId 를 함께 보냅니다.'
        ),
        request_body=OnboardingQuestionUpdateSerializer,
        responses={
            200: OnboardingQuestionSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 질문을 찾을 수 없음',
        },
        tags=['Onboarding'],
    )
    def patch(self, request, company_id, template_key):
        company = get_owner_company(request.user, company_id)
        spec = questions.find(template_key)
        if spec is None:
            raise ValidationError({'templateKey': ['unknown question']})

        serializer = OnboardingQuestionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        scope = _get_project_scope(company, data.get('scopeId'))
        if spec.area_key is None and scope is None:
            raise ValidationError({'scopeId': ['project scope required for this question']})

        answer = data.get('answerKo')
        try:
            if answer:
                services.answer_question(company, template_key, answer, scope)
            else:
                services.skip_question(company, template_key, scope)
        except ImproperlyConfigured as exc:
            raise ValidationError({'scopeId': [str(exc)]})

        items = services.list_questions(company, scope)
        item = next(i for i in items if i['templateKey'] == template_key)

        return Response(
            OnboardingQuestionSerializer(item).data, status=status.HTTP_200_OK
        )


class OnboardingCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='온보딩 완료',
        operation_description=(
            '답변으로 만든 규칙을 검색 가능한 상태로 만듭니다. '
            '선택지를 고른 답은 영어가 이미 있어 임베딩만, 직접 입력한 답은 번역까지 함께 처리합니다. '
            '질문마다 부르지 않고 여기서 한 번에 묶습니다.'
        ),
        responses={
            200: OnboardingCompleteSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Onboarding'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        company.onboarding_step = 4
        company.save(update_fields=['onboarding_step'])

        # 실패해도 온보딩은 끝난 것으로 둔다. 임베딩이 비어 있으면 다음 실행이 이어서 채운다.
        finalize_entries(list(
            HandbookEntry.objects.filter(
                company=company,
                origin=HandbookEntry.Origin.ONBOARDING,
                status=HandbookEntry.Status.CONFIRMED,
                embedded_at__isnull=True,
            )
        ))

        response_serializer = OnboardingCompleteSerializer(
            {
                'onboardingStep': company.onboarding_step,
                'nextRoute': 'owner/dashboard',
                'summary': {
                    'sourceCount': Connection.objects.filter(
                        company=company,
                        status=Connection.Status.CONNECTED,
                    ).count(),
                    'handbookEntryCount': HandbookEntry.objects.filter(
                        company=company,
                        status=HandbookEntry.Status.CONFIRMED,
                    ).count(),
                    'riskKeywordCount': RiskKeyword.objects.filter(company=company).count(),
                },
            }
        )

        return Response(response_serializer.data, status=status.HTTP_200_OK)
