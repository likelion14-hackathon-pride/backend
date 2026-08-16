from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_member_company, get_owner_company
from config.filters import enum_parameter, enum_value, filter_enum, filter_int
from config.pagination import (
    CURSOR_PARAMETER,
    LIMIT_PARAMETER,
    page_response,
    paged_response,
)

from .finalizing import finalize_entries
from .models import CompanyScope, HandbookEntry, HandbookEvidence
from .queries import entries_for, filter_by_review_status, scopes_with_counts
from .serializers import (
    CompanyScopeCreateSerializer,
    CompanyScopeListSerializer,
    CompanyScopeSerializer,
    HandbookBulkReviewResultSerializer,
    HandbookBulkReviewSerializer,
    HandbookEntryCreateSerializer,
    HandbookEntryListSerializer,
    HandbookEntrySerializer,
    HandbookEntryUpdateSerializer,
    HandbookEvidenceListSerializer,
    HandbookEvidenceSerializer,
    HandbookReviewSerializer,
)


SCOPE_ID_PARAMETER = openapi.Parameter(
    'scopeId', openapi.IN_QUERY, type=openapi.TYPE_INTEGER,
)
SCOPE_KIND_PARAMETER = enum_parameter('scopeKind', CompanyScope.Kind)
KIND_PARAMETER = enum_parameter('kind', CompanyScope.Kind)
STATUS_PARAMETER = enum_parameter('status', HandbookEntry.Status)
REVIEW_STATUS_PARAMETER = enum_parameter(
    'reviewStatus', HandbookEntry.ReviewStatus,
    'PENDING은 아직 보지 않은 초안, HELD는 보고 미뤄 둔 초안입니다.',
)


class HandbookEntryListCreateView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 항목 목록 조회',
        manual_parameters=[
            SCOPE_ID_PARAMETER,
            SCOPE_KIND_PARAMETER,
            STATUS_PARAMETER,
            REVIEW_STATUS_PARAMETER,
            CURSOR_PARAMETER,
            LIMIT_PARAMETER,
        ],
        responses={
            200: HandbookEntryListSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        entries = entries_for(company)
        entries = filter_enum(entries, request, 'scopeKind', CompanyScope.Kind, 'scope__kind')
        entries = filter_int(entries, request, 'scopeId', 'scope_id')
        entries = filter_enum(entries, request, 'status', HandbookEntry.Status)

        review_status = enum_value(request, 'reviewStatus', HandbookEntry.ReviewStatus)
        if review_status:
            entries = filter_by_review_status(entries, review_status)

        return paged_response(HandbookEntrySerializer, entries, request)

    @swagger_auto_schema(
        operation_summary='핸드북 항목 직접 추가',
        request_body=HandbookEntryCreateSerializer,
        responses={
            201: HandbookEntrySerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = HandbookEntryCreateSerializer(data=request.data, context={'company': company})
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class HandbookEntryDetailView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 항목 별 상세 조회',
        responses={
            200: HandbookEntrySerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 핸드북 항목을 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def get(self, request, company_id, entry_id):
        company = get_member_company(request.user, company_id)
        entry = get_object_or_404(
            HandbookEntry.objects.select_related('scope'),
            id=entry_id,
            company=company,
        )
        serializer = HandbookEntrySerializer(entry)

        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='핸드북 항목 수정',
        request_body=HandbookEntryUpdateSerializer,
        responses={
            200: HandbookEntrySerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사, 범위 또는 핸드북 항목을 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def patch(self, request, company_id, entry_id):
        company = get_owner_company(request.user, company_id)
        entry = get_object_or_404(
            HandbookEntry.objects.select_related('scope'),
            id=entry_id,
            company=company,
        )
        serializer = HandbookEntryUpdateSerializer(entry, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_200_OK)


class HandbookEntryEvidenceView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 항목 근거 조회',
        operation_description=(
            'AI가 이 항목을 만들 때 인용한 원문입니다. quote는 슬랙 원문에서 그대로 발췌한 문장이며, '
            '원문과 대조에 실패한 인용은 저장 단계에서 버려집니다.'
        ),
        responses={
            200: HandbookEvidenceListSerializer(),
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사 또는 핸드북 항목을 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def get(self, request, company_id, entry_id):
        company = get_member_company(request.user, company_id)
        entry = get_object_or_404(HandbookEntry, id=entry_id, company=company)
        evidences = HandbookEvidence.objects.filter(entry=entry).order_by('occurred_at', 'id')

        return page_response(HandbookEvidenceSerializer, evidences)


def _apply_decision(entry, decision):
    # 어떤 결정이든 '대표가 봤다'는 사실은 남는다. HOLD가 PENDING과 구분되는 근거다.
    entry.reviewed_at = timezone.now()
    fields = ['reviewed_at']

    if decision == 'APPROVE':
        entry.status = HandbookEntry.Status.CONFIRMED
        entry.confirmed_at = entry.reviewed_at
        fields += ['status', 'confirmed_at']
    elif decision == 'REJECT':
        entry.status = HandbookEntry.Status.ARCHIVED
        fields += ['status']
    # HOLD는 상태를 바꾸지 않고 검토 시각만 남긴다.

    entry.save(update_fields=fields)

    return entry


class HandbookEntryReviewView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 초안 승인/거절/보류',
        operation_description=(
            'APPROVE는 확정(CONFIRMED), REJECT는 보관(ARCHIVED)으로 바꿉니다. '
            'HOLD는 초안 상태를 그대로 두며 아무것도 기록하지 않습니다. '
            '내용이 없는 BLANK 항목은 승인할 수 없습니다.'
        ),
        request_body=HandbookReviewSerializer,
        responses={
            200: HandbookEntrySerializer(),
            400: '잘못된 요청 (내용 없는 항목 승인)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 핸드북 항목을 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def post(self, request, company_id, entry_id):
        company = get_owner_company(request.user, company_id)
        entry = get_object_or_404(
            HandbookEntry.objects.select_related('scope'), id=entry_id, company=company
        )
        serializer = HandbookReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data['decision']

        if decision == 'APPROVE' and entry.status == HandbookEntry.Status.BLANK:
            raise ValidationError({'decision': ['cannot approve a blank entry']})

        entry = _apply_decision(entry, decision)
        # 확정된 규칙만 번역·임베딩한다. 실패해도 확정은 되돌리지 않는다.
        if decision == 'APPROVE':
            finalize_entries([entry])

        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_200_OK)


class HandbookEntryBulkReviewView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 초안 일괄 승인',
        operation_description=(
            '여러 초안을 한 번에 확정합니다. 승인할 수 없는 항목(내용 없는 BLANK, 다른 회사 항목)은 '
            '건너뛰고 skipped에 사유와 함께 담아 돌려줍니다.'
        ),
        request_body=HandbookBulkReviewSerializer,
        responses={
            200: HandbookBulkReviewResultSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = HandbookBulkReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_ids = serializer.validated_data['entryIds']

        entries = {
            entry.id: entry
            for entry in HandbookEntry.objects.filter(id__in=requested_ids, company=company)
        }

        approved = []
        skipped = []
        for entry_id in requested_ids:
            entry = entries.get(entry_id)
            if entry is None:
                skipped.append({'entryId': entry_id, 'reason': 'not_found'})
                continue
            if entry.status == HandbookEntry.Status.BLANK:
                skipped.append({'entryId': entry_id, 'reason': 'blank_entry'})
                continue
            approved.append(_apply_decision(entry, 'APPROVE'))

        # 항목마다 호출하지 않고 한 번에 묶어서 번역·임베딩한다.
        finalize_entries(approved)

        return Response(
            {'approvedCount': len(approved), 'skipped': skipped}, status=status.HTTP_200_OK
        )


class CompanyScopeListView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 범위 목록 조회',
        manual_parameters=[KIND_PARAMETER],
        responses={
            200: CompanyScopeListSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: '회사 접근 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def get(self, request, company_id):
        company = get_member_company(request.user, company_id)
        scopes = filter_enum(
            scopes_with_counts(company), request, 'kind', CompanyScope.Kind
        )

        return page_response(CompanyScopeSerializer, scopes.order_by('kind', 'name'))

    @swagger_auto_schema(
        operation_summary='프로젝트 범위 생성',
        operation_description=(
            '프로젝트별 규칙을 담을 범위를 만듭니다. '
            '회사 전반 규칙 범위는 회사 생성 시 고정 생성되므로 kind는 PROJECT만 허용합니다.'
        ),
        request_body=CompanyScopeCreateSerializer,
        responses={
            201: CompanyScopeSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = CompanyScopeCreateSerializer(data=request.data, context={'company': company})
        serializer.is_valid(raise_exception=True)
        scope = serializer.save()
        response_serializer = CompanyScopeSerializer(scope)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
