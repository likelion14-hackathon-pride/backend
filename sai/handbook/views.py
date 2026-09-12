from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_member_company, get_owner_company
from config.errors import CANNOT_APPROVE_BLANK, ENTRY_NOT_CONFIRMED
from config.filters import (
    enum_list_parameter,
    enum_parameter,
    enum_value,
    filter_enum,
    filter_enum_list,
    filter_int,
)
from config.pagination import (
    CURSOR_PARAMETER,
    LIMIT_PARAMETER,
    page_response,
    paged_response,
)

from .finalizing import finalize_entries, mark_confirmed
from .models import CompanyScope, HandbookEntry, HandbookEvidence, HandbookRevision
from .queries import (
    entries_for,
    filter_by_review_status,
    live_entries,
    scopes_with_counts,
)
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
PROMOTION_TYPE_PARAMETER = enum_parameter('promotionType', HandbookEntry.PromotionType)
REVIEW_STATUS_PARAMETER = enum_parameter(
    'reviewStatus', HandbookEntry.ReviewStatus,
    'PENDING은 아직 보지 않은 초안, HELD는 보고 미뤄 둔 초안입니다. '
    '내용이 없는 BLANK 항목은 승인할 수 없으므로 어느 쪽에도 들지 않습니다. '
    'status=BLANK 로 따로 조회하세요.',
)
# 확인보관함은 소스에서 뽑은 것만 본다. 대표 답변(ESCALATION)이나 직접 등록(DIRECT_ENTRY)이
# 같은 목록에 섞이면 대표가 이미 한 결정을 다시 하게 된다.
ORIGIN_PARAMETER = enum_list_parameter(
    'origin', HandbookEntry.Origin,
    '항목이 만들어진 경로입니다. 확인보관함은 SLACK,GITHUB,FILE 입니다.',
)


class HandbookEntryListCreateView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 항목 목록 조회',
        manual_parameters=[
            SCOPE_ID_PARAMETER,
            SCOPE_KIND_PARAMETER,
            STATUS_PARAMETER,
            PROMOTION_TYPE_PARAMETER,
            REVIEW_STATUS_PARAMETER,
            ORIGIN_PARAMETER,
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
        entries = filter_enum(
            entries, request, 'promotionType', HandbookEntry.PromotionType, 'promotion_type'
        )
        entries = filter_enum_list(entries, request, 'origin', HandbookEntry.Origin)

        review_status = enum_value(request, 'reviewStatus', HandbookEntry.ReviewStatus)
        if review_status:
            entries = filter_by_review_status(entries, review_status)

        return paged_response(HandbookEntrySerializer, entries, request)

    @swagger_auto_schema(
        operation_summary='핸드북 항목 직접 추가',
        operation_description=(
            '질문을 기다리지 않고 규칙을 바로 등록합니다. 저장 즉시 확정 상태가 됩니다. '
            'ruleEn 을 생략하면 한국어 원문에서 영어 표시문을 만들어 채웁니다. '
            '저장과 함께 임베딩까지 해야 팀원 질문의 답변 근거로 쓰입니다.'
        ),
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
        # 번역과 임베딩이 없으면 확정 상태여도 검색에 걸리지 않아 답변에 쓰이지 않는다.
        finalize_entries([entry])

        return Response(HandbookEntrySerializer(entry).data, status=status.HTTP_201_CREATED)


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
        entry = get_object_or_404(entries_for(company), id=entry_id)
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
        entry = get_object_or_404(entries_for(company), id=entry_id)
        serializer = HandbookEntryUpdateSerializer(entry, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='핸드북 항목 삭제',
        operation_description=(
            '핸드북에 올라간 확정 항목을 지웁니다. 검토 전 초안은 거절(REJECT)로 내리므로 '
            '여기서는 지울 수 없습니다. '
            '지운 항목은 목록과 답변 검색에서 빠지며, 같은 규칙이 원문에서 다시 만들어지지 않습니다.'
        ),
        responses={
            204: '삭제됨',
            400: '잘못된 요청 (확정되지 않은 항목)',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 핸드북 항목을 찾을 수 없음',
        },
        tags=['Handbook'],
    )
    def delete(self, request, company_id, entry_id):
        company = get_owner_company(request.user, company_id)
        entry = get_object_or_404(live_entries(company), id=entry_id)

        if entry.status != HandbookEntry.Status.CONFIRMED:
            raise ValidationError(
                'only a confirmed entry can be deleted', code=ENTRY_NOT_CONFIRMED
            )

        with transaction.atomic():
            entry.deleted_at = timezone.now()
            HandbookRevision.objects.create(
                company=company,
                entry=entry,
                before={
                    'status': entry.status,
                    'deleted_at': None,
                    'promotion_type': entry.promotion_type,
                    'auto_promotion_method': entry.auto_promotion_method,
                    'promotion_reason': entry.promotion_reason,
                },
                reason='owner_deactivated',
            )
            entry.save(update_fields=['deleted_at'])

        return Response(status=status.HTTP_204_NO_CONTENT)


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
        entry = get_object_or_404(live_entries(company), id=entry_id)
        evidences = HandbookEvidence.objects.filter(entry=entry).order_by('occurred_at', 'id')

        return page_response(HandbookEvidenceSerializer, evidences)


@transaction.atomic
def _apply_decision(entry, decision):
    if decision == 'APPROVE' and entry.status == HandbookEntry.Status.CONFIRMED:
        return entry
    if decision == 'REJECT' and entry.status == HandbookEntry.Status.ARCHIVED:
        return entry

    HandbookRevision.objects.create(
        company=entry.company,
        entry=entry,
        before={
            'status': entry.status,
            'reviewed_at': entry.reviewed_at.isoformat() if entry.reviewed_at else None,
            'confirmed_at': entry.confirmed_at.isoformat() if entry.confirmed_at else None,
            'promotion_type': entry.promotion_type,
            'auto_promotion_method': entry.auto_promotion_method,
            'promotion_reason': entry.promotion_reason,
        },
        reason=f'owner_review_{decision.lower()}',
    )
    # 어떤 결정이든 '대표가 봤다'는 사실은 남는다. HOLD가 PENDING과 구분되는 근거다.
    entry.reviewed_at = timezone.now()
    fields = ['reviewed_at']

    if decision == 'APPROVE':
        return mark_confirmed(entry, at=entry.reviewed_at)
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
        entry = get_object_or_404(entries_for(company), id=entry_id)
        serializer = HandbookReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data['decision']

        if decision == 'APPROVE' and entry.status == HandbookEntry.Status.BLANK:
            raise ValidationError('cannot approve a blank entry', code=CANNOT_APPROVE_BLANK)

        entry = _apply_decision(entry, decision)
        # 확정된 규칙만 번역·임베딩한다. 실패해도 확정은 되돌리지 않는다.
        if decision == 'APPROVE':
            finalize_entries([entry])

        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_200_OK)


class HandbookEntryBulkReviewView(APIView):
    @swagger_auto_schema(
        operation_summary='핸드북 초안 일괄 승인/거절',
        operation_description=(
            'PENDING_REVIEW 초안을 한 번에 승인하거나 거절합니다. MANUAL_REQUIRED는 개별 검토만 '
            '허용하며, 다른 회사 ID가 하나라도 섞이면 전체 요청을 403으로 차단합니다.'
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
        decision = serializer.validated_data['decision']
        unique_ids = set(requested_ids)
        foreign_ids = list(
            HandbookEntry.objects.filter(id__in=unique_ids)
            .exclude(company=company)
            .values_list('id', flat=True)
        )
        if foreign_ids:
            raise PermissionDenied('entries from another company are not allowed')

        approved = []
        rejected = []
        results = []
        skipped = []
        seen = set()
        with transaction.atomic():
            entries = {
                entry.id: entry
                for entry in HandbookEntry.objects.select_for_update().filter(
                    company=company, id__in=unique_ids, deleted_at__isnull=True
                )
            }
            for entry_id in requested_ids:
                if entry_id in seen:
                    item = {'entryId': entry_id, 'reason': 'duplicate_request'}
                    skipped.append(item)
                    results.append({**item, 'result': 'SKIPPED'})
                    continue
                seen.add(entry_id)

                entry = entries.get(entry_id)
                if entry is None:
                    item = {'entryId': entry_id, 'reason': 'not_found'}
                    skipped.append(item)
                    results.append({**item, 'result': 'SKIPPED'})
                    continue
                if entry.promotion_type == HandbookEntry.PromotionType.MANUAL_REQUIRED:
                    item = {'entryId': entry_id, 'reason': 'individual_review_required'}
                    skipped.append(item)
                    results.append({**item, 'result': 'SKIPPED'})
                    continue
                if decision == 'APPROVE' and entry.status == HandbookEntry.Status.BLANK:
                    item = {'entryId': entry_id, 'reason': 'blank_entry'}
                    skipped.append(item)
                    results.append({**item, 'result': 'SKIPPED'})
                    continue
                if decision == 'APPROVE' and entry.status == HandbookEntry.Status.CONFIRMED:
                    item = {'entryId': entry_id, 'reason': 'already_approved'}
                    skipped.append(item)
                    results.append({**item, 'result': 'SKIPPED'})
                    continue
                if decision == 'REJECT' and entry.status == HandbookEntry.Status.ARCHIVED:
                    item = {'entryId': entry_id, 'reason': 'already_rejected'}
                    skipped.append(item)
                    results.append({**item, 'result': 'SKIPPED'})
                    continue

                changed = _apply_decision(entry, decision)
                if decision == 'APPROVE':
                    approved.append(changed)
                    result_name = 'APPROVED'
                else:
                    rejected.append(changed)
                    result_name = 'REJECTED'
                results.append({'entryId': entry_id, 'result': result_name})

        # 항목마다 호출하지 않고 한 번에 묶어서 번역·임베딩한다.
        finalize_entries(approved)

        return Response(
            {
                'decision': decision,
                'processedCount': len(approved) + len(rejected),
                'approvedCount': len(approved),
                'rejectedCount': len(rejected),
                'results': results,
                'skipped': skipped,
            },
            status=status.HTTP_200_OK,
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
