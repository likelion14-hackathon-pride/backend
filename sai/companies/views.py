from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from accounts.profile import LOCATION_ZONES, JobRole, WorkLocation
from accounts.serializers import CompanySerializer, MembershipListSerializer, MembershipSerializer
from config.pagination import CURSOR_PARAMETER, LIMIT_PARAMETER, paged_response

from .access import get_member_company, get_owner_company
from .dashboard import dashboard_data
from .serializers import (
    CompanySettingsSerializer,
    OwnerDashboardSerializer,
    ProfileOptionsSerializer,
)
from .timing import WorkingHours, local_window, overlap_hours


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


class OwnerDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='대표 대시보드 조회',
        operation_description=(
            '실제 Ask SAI 질문, 핸드북 인용, 대표 확인 질문과 확정 핸드북을 집계합니다. '
            '대표 시간 절약은 SAI 해결 1건당 5분으로 추정하며, '
            '대표 대기 질문은 전체 개수와 최신 4개를 반환합니다.'
        ),
        responses={
            200: OwnerDashboardSerializer(),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Dashboard'],
    )
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)

        return Response(
            OwnerDashboardSerializer(dashboard_data(company)).data,
            status=status.HTTP_200_OK,
        )


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


class ProfileOptionsView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='근무 위치 · 담당 역할 선택지',
        operation_description=(
            '가입 직후 초기 설정 화면과 설정 모달이 쓰는 고정 목록입니다. '
            '위치를 고르면 타임존이 함께 정해지므로 타임존을 따로 보내지 않습니다. '
            'ownerHoursStart / ownerHoursEnd 는 대표 근무시간을 그 위치의 시계로 읽은 값이고, '
            'overlapHours 는 양쪽이 같은 근무시간을 쓸 때 하루에 겹치는 시간입니다. '
            '서머타임을 쓰는 위치는 조회 시점에 따라 값이 달라집니다.'
        ),
        responses={
            200: ProfileOptionsSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['User'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        now = timezone.now()
        hours = WorkingHours(
            company.timezone,
            company.working_hours_start,
            company.working_hours_end,
            company.working_hours_enabled,
        )

        locations = []
        for location in WorkLocation:
            zone = LOCATION_ZONES[location]
            start, end = local_window(hours, zone, now)
            locations.append({
                'value': location.value,
                'label': location.label,
                'timezone': zone,
                'ownerHoursStart': start,
                'ownerHoursEnd': end,
                'overlapHours': overlap_hours(hours, zone, now),
            })

        return Response(
            ProfileOptionsSerializer({
                'locations': locations,
                'roles': [
                    {'value': role.value, 'label': role.label} for role in JobRole
                ],
            }).data,
            status=status.HTTP_200_OK,
        )


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
