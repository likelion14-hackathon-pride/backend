from django.shortcuts import get_object_or_404
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.access import get_owner_company

from .models import RiskKeyword
from .serializers import RiskKeywordSerializer


class RiskKeywordListCreateView(APIView):
    @swagger_auto_schema(
        operation_summary='위험 키워드 목록 조회',
        responses={
            200: RiskKeywordSerializer(many=True),
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Setting'],
    )
    def get(self, request, company_id):
        company = get_owner_company(request.user, company_id)
        keywords = RiskKeyword.objects.filter(company=company).order_by('-created_at')
        serializer = RiskKeywordSerializer(keywords, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary='위험 키워드 등록',
        request_body=RiskKeywordSerializer,
        responses={
            201: RiskKeywordSerializer(),
            400: '잘못된 요청',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사를 찾을 수 없음',
        },
        tags=['Setting'],
    )
    def post(self, request, company_id):
        company = get_owner_company(request.user, company_id)

        # URL에서 확인한 회사 정보와 요청 데이터를 함께 시리얼라이저에 전달한다.
        serializer = RiskKeywordSerializer(
            data=request.data,
            context={'company': company},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(company=company)

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RiskKeywordDeleteView(APIView):
    @swagger_auto_schema(
        operation_summary='위험 키워드 삭제',
        responses={
            204: '삭제 성공',
            401: '인증되지 않음',
            403: 'Owner 권한 없음',
            404: '회사 또는 키워드를 찾을 수 없음',
        },
        tags=['Setting'],
    )
    def delete(self, request, company_id, keyword_id):
        company = get_owner_company(request.user, company_id)
        keyword = get_object_or_404(
            RiskKeyword,
            id=keyword_id,
            company=company,
        )
        keyword.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
