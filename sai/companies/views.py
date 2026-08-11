from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from accounts.serializers import CompanySerializer, MembershipListSerializer, MembershipSerializer

from .models import Company


CURSOR_PARAMETER = openapi.Parameter(
    'cursor',
    openapi.IN_QUERY,
    type=openapi.TYPE_STRING,
)
LIMIT_PARAMETER = openapi.Parameter(
    'limit',
    openapi.IN_QUERY,
    type=openapi.TYPE_INTEGER,
    default=20,
)


# 회사 정보 조회 view
class CompanyDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='회사 정보 조회',
        responses={
            200: CompanySerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Company'],
    )
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

    @swagger_auto_schema(
        operation_summary='회사 구성원 목록 조회',
        manual_parameters=[CURSOR_PARAMETER, LIMIT_PARAMETER],
        responses={
            200: MembershipListSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Company'],
    )
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
