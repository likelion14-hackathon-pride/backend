from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from accounts.serializers import CompanySerializer

from .models import Company


# 회사 정보 조회 view
class CompanyDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, company_id):
        company = get_object_or_404(Company, id=company_id)
        is_member = Membership.objects.filter(user=request.user, company=company, left_at__isnull=True).exists()

        if not is_member:
            raise PermissionDenied('company permission required')

        serializer = CompanySerializer(company)
        return Response(serializer.data, status=status.HTTP_200_OK)
