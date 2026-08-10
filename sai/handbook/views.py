from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .serializers import HandbookEntryCreateSerializer, HandbookEntrySerializer


# 요청한 사용자가 해당 회사의 대표인지 확인
def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# 핸드북 항목 직접 등록 담당 view
class HandbookEntryListCreateView(APIView):
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = HandbookEntryCreateSerializer(data=request.data, context={'company': company})
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
