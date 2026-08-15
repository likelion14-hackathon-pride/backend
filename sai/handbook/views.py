from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .finalizing import finalize_entries
from .models import CompanyScope, HandbookEntry, HandbookEvidence
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
    'scopeId',
    openapi.IN_QUERY,
    type=openapi.TYPE_INTEGER,
)
SCOPE_KIND_PARAMETER = openapi.Parameter(
    'scopeKind',
    openapi.IN_QUERY,
    type=openapi.TYPE_STRING,
    enum=['COMPANY', 'PROJECT'],
)
KIND_PARAMETER = openapi.Parameter(
    'kind',
    openapi.IN_QUERY,
    type=openapi.TYPE_STRING,
    enum=['COMPANY', 'PROJECT'],
)
STATUS_PARAMETER = openapi.Parameter(
    'status',
    openapi.IN_QUERY,
    type=openapi.TYPE_STRING,
    enum=['DRAFT', 'CONFIRMED', 'BLANK', 'ARCHIVED'],
)
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


# 요청한 사용자가 해당 회사의 대표인지 확인
def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# 요청한 사용자가 해당 회사의 구성원인지 확인
def get_member_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_member = Membership.objects.filter(user=user, company=company, left_at__isnull=True).exists()

    if not is_member:
        raise PermissionDenied('company permission required')

    return company


# 핸드북 항목 직접 등록 view
class HandbookEntryListCreateView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 항목 목록 조회',
        manual_parameters=[
            SCOPE_ID_PARAMETER,
            SCOPE_KIND_PARAMETER,
            STATUS_PARAMETER,
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
        entries = HandbookEntry.objects.filter(company=company).select_related('scope')

        scope_id = request.query_params.get('scopeId')
        scope_kind = request.query_params.get('scopeKind')
        entry_status = request.query_params.get('status')
        cursor = request.query_params.get('cursor')

        if scope_kind:
            entries = entries.filter(scope__kind=scope_kind)
        if scope_id:
            try:
                entries = entries.filter(scope_id=int(scope_id))
            except ValueError:
                raise ValidationError({'scopeId': 'invalid scopeId'})
        if entry_status:
            entries = entries.filter(status=entry_status)
        if cursor:
            try:
                entries = entries.filter(id__lt=int(cursor))
            except ValueError:
                raise ValidationError({'cursor': 'invalid cursor'})

        try:
            limit = int(request.query_params.get('limit', 20))
        except ValueError:
            raise ValidationError({'limit': 'limit must be an integer'})

        if limit < 1 or limit > 100:
            raise ValidationError({'limit': 'limit must be between 1 and 100'})

        entries = entries.order_by('-id')[:limit + 1]
        has_next = len(entries) > limit
        items = entries[:limit]
        serializer = HandbookEntrySerializer(items, many=True)
        next_cursor = str(items[-1].id) if has_next else None

        return Response({'items': serializer.data, 'nextCursor': next_cursor}, status=status.HTTP_200_OK)

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


# 핸드북 항목 상세 조회 view
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


# 핸드북 항목 근거 조회 view
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
        serializer = HandbookEvidenceSerializer(evidences, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)


def _apply_decision(entry, decision):
    if decision == 'APPROVE':
        entry.status = HandbookEntry.Status.CONFIRMED
        entry.confirmed_at = timezone.now()
        entry.save(update_fields=['status', 'confirmed_at'])
    elif decision == 'REJECT':
        entry.status = HandbookEntry.Status.ARCHIVED
        entry.save(update_fields=['status'])
    # HOLD는 DRAFT를 그대로 둔다. 보류 상태를 따로 담을 컬럼이 없다.

    return entry


# 핸드북 초안 검토 view
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


# 핸드북 초안 일괄 승인 view
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


# 회사 규칙과 프로젝트 범위 목록 조회 view
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
        scopes = CompanyScope.objects.filter(company=company)
        scope_kind = request.query_params.get('kind')

        if scope_kind:
            if scope_kind not in CompanyScope.Kind.values:
                raise ValidationError({'kind': 'invalid kind'})
            scopes = scopes.filter(kind=scope_kind)

        scopes = scopes.order_by('kind', 'name')
        serializer = CompanyScopeSerializer(scopes, many=True)

        return Response({'items': serializer.data}, status=status.HTTP_200_OK)

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
