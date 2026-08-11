from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .models import Question
from .serializers import OnboardingSerializer, OnboardingStepSerializer


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
