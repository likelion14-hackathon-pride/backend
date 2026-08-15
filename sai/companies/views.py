from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from accounts.serializers import CompanySerializer, MembershipListSerializer, MembershipSerializer
from config.pagination import CURSOR_PARAMETER, LIMIT_PARAMETER, paged_response

from .access import get_member_company, get_owner_company
from .serializers import CompanySettingsSerializer


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
        company = get_member_company(request.user, company_id)

        return Response(CompanySerializer(company).data, status=status.HTTP_200_OK)


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
        company = get_member_company(request.user, company_id)
        # slackHandle 을 채우느라 구성원마다 조회하지 않도록 미리 가져온다.
        members = (
            Membership.objects.select_related('user')
            .prefetch_related('user__source_identities')
            .filter(company=company, left_at__isnull=True)
        )
        return paged_response(MembershipSerializer, members, request)


class CompanySettingsView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='회사 근무시간 설정 조회',
        responses={
            200: CompanySettingsSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Company'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)

        return Response(CompanySettingsSerializer(company).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='회사 근무시간 설정 수정',
        request_body=CompanySettingsSerializer,
        responses={
            200: CompanySettingsSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Company'],
    )
    def patch(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = CompanySettingsSerializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data, status=status.HTTP_200_OK)
