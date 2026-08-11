from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from accounts.serializers import CompanySerializer, MembershipSerializer

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


# 회사 구성원 목록 조회 view
class CompanyMemberListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, company_id):
        company = get_object_or_404(Company, id=company_id)
        is_member = Membership.objects.filter(user=request.user, company=company, left_at__isnull=True).exists()

        if not is_member:
            raise PermissionDenied('company permission required')

        cursor = request.query_params.get('cursor')
        try:
            limit = int(request.query_params.get('limit', 20))
        except ValueError:
            raise ValidationError({'limit': 'limit must be an integer'})

        if limit < 1 or limit > 100:
            raise ValidationError({'limit': 'limit must be between 1 and 100'})

        members = Membership.objects.select_related('user').filter(company=company, left_at__isnull=True)
        if cursor:
            try:
                members = members.filter(id__lt=int(cursor))
            except ValueError:
                raise ValidationError({'cursor': 'invalid cursor'})

        members = members.order_by('-id')[:limit + 1]
        has_next = len(members) > limit
        items = members[:limit]
        serializer = MembershipSerializer(items, many=True)
        next_cursor = str(items[-1].id) if has_next else None

        return Response(
            {'items': serializer.data, 'nextCursor': next_cursor},
            status=status.HTTP_200_OK,
        )
