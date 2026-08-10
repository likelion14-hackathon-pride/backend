from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Membership
from companies.models import Company

from .models import HandbookEntry
from .serializers import HandbookEntryCreateSerializer, HandbookEntrySerializer


# 요청한 사용자가 해당 회사의 대표인지 확인
def get_owner_company(user, company_id):
    company = get_object_or_404(Company, id=company_id)
    is_owner = Membership.objects.filter(user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True).exists()

    if not is_owner:
        raise PermissionDenied('owner permission required')

    return company


# 핸드북 항목 직접 등록 view
class HandbookEntryListCreateView(APIView):
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        entries = HandbookEntry.objects.filter(company=company).select_related('scope')

        scope = request.query_params.get('scope')
        project_id = request.query_params.get('projectId')
        entry_status = request.query_params.get('status')
        cursor = request.query_params.get('cursor')

        if scope:
            entries = entries.filter(scope__kind=scope)
        if project_id:
            entries = entries.filter(scope__area_key=project_id)
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

    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        serializer = HandbookEntryCreateSerializer(data=request.data, context={'company': company})
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        response_serializer = HandbookEntrySerializer(entry)

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


# 핸드북 항목 상세 조회 view
class HandbookEntryDetailView(APIView):
    def get(self, request, company_id, entry_id):
        company = get_owner_company(request.user, company_id)
        entry = get_object_or_404(
            HandbookEntry.objects.select_related('scope'),
            id=entry_id,
            company=company,
        )
        serializer = HandbookEntrySerializer(entry)

        return Response(serializer.data, status=status.HTTP_200_OK)
