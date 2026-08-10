from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .serializers import RiskKeywordSerializer


# 요청한 사용자가 해당 회사의 대표인지 확인한다.
def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(
        user=user,
        company=company,
        role=Membership.Role.OWNER,
        left_at__isnull=True,
    ).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# 위험 작업 키워드 등록 담당 view
class RiskKeywordCreateView(APIView):
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)

        # URL에서 확인한 회사 정보와 요청 데이터를 함께 시리얼라이저에 전달한다.
        serializer = RiskKeywordSerializer(
            data=request.data,
            context={'company': company},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(company=company)

        return Response(serializer.data, status=status.HTTP_201_CREATED)
