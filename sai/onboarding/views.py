from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company
from handbook.models import HandbookEntry
from policy.models import RiskKeyword
from sources.models import Connection

from .models import Question
from .serializers import (
    OnboardingQuestionSerializer,
    OnboardingQuestionUpdateSerializer,
    OnboardingCompleteSerializer,
    OnboardingSerializer,
    OnboardingStepSerializer,
)


# 요청한 사용자가 해당 회사의 대표인지 확인
def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# Day 0 온보딩 조회 및 단계 저장 view
class OnboardingView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='온보딩 진행 조회',
        responses={
            200: OnboardingSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Onboarding'],
    )
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        questions = Question.objects.filter(company=company).order_by('priority', 'id')
        serializer = OnboardingSerializer(
            {
                'onboardingStep': company.onboarding_step,
                'questions': questions,
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

        questions = Question.objects.filter(company=company).order_by('priority', 'id')
        response_serializer = OnboardingSerializer(
            {
                'onboardingStep': company.onboarding_step,
                'questions': questions,
            }
        )

        return Response(response_serializer.data, status=status.HTTP_200_OK)


# 온보딩 질문 답변 저장 view
class OnboardingQuestionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='온보딩 질문 답변 저장',
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
    def patch(self, request, company_id, question_id):
        company = get_owner_company(request.user, company_id)
        question = get_object_or_404(Question, id=question_id, company=company)
        serializer = OnboardingQuestionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        answer = serializer.validated_data.get('answerKo')
        question_status = serializer.validated_data.get('status')

        if answer:
            question.answer_ko = answer
            question.status = Question.Status.ANSWERED
            question.answered_at = timezone.now()
        elif question_status == Question.Status.SKIPPED:
            question.answer_ko = None
            question.status = Question.Status.SKIPPED
            question.answered_at = None

        question.save(update_fields=['answer_ko', 'status', 'answered_at'])
        response_serializer = OnboardingQuestionSerializer(question)

        return Response(response_serializer.data, status=status.HTTP_200_OK)


# Day 0 온보딩 완료 view
class OnboardingCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='온보딩 완료',
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
