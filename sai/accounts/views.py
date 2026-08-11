from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth import logout
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from .serializers import AuthSerializer, MemberSignupSerializer, OwnerSignupSerializer

NEXT_ROUTES = {
    'OWNER': 'onboarding/day0',
    'MEMBER': 'app/home',
}

FORM_AND_JSON = ['application/x-www-form-urlencoded', 'application/json']

def _string(example):
    return openapi.Schema(type=openapi.TYPE_STRING, example=example)


OWNER_SIGNUP_RESPONSE = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'userId': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
        'role': _string('OWNER'),
        'company': openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'id': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
                'name': _string('에코랩'),
                'code': _string('A7K2M9QX4RTB'),
            },
        ),
        'next': _string('onboarding/day0'),
        'accessToken': _string('eyJhbGciOi...'),
        'refreshToken': _string('eyJhbGciOi...'),
    },
)

# 팀원 가입과 로그인 응답. company에는 code를 담지 않는다.
AUTH_RESPONSE = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'userId': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
        'role': _string('MEMBER'),
        'company': openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'id': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
                'name': _string('에코랩'),
            },
        ),
        'next': _string('app/home'),
        'accessToken': _string('eyJhbGciOi...'),
        'refreshToken': _string('eyJhbGciOi...'),
    },
)

LOGOUT_RESPONSE = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={'message': _string('logout success!')},
)


class OwnerSignupView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'signup'

    @swagger_auto_schema(
        operation_summary='대표 회원가입',
        operation_description=(
            '발급된 회사 코드는 이 응답에서만 확인할 수 있습니다. '
            '화면 언어는 ko로 고정됩니다.'
        ),
        request_body=OwnerSignupSerializer,
        consumes=FORM_AND_JSON,
        responses={
            201: OWNER_SIGNUP_RESPONSE,
            400: '잘못된 요청 (email_taken / weak_password)',
            429: '요청 한도 초과 (rate_limited)',
        },
        tags=['Auth'],
        security=[],
    )
    def post(self, request):
        serializer = OwnerSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()

        token = RefreshToken.for_user(membership.user)

        return Response(
            {
                'userId': membership.user_id,
                'role': membership.role,
                'company': {
                    'id': membership.company_id,
                    'name': membership.company.name,
                    'code': membership.company.code,
                },
                'next': NEXT_ROUTES[membership.role],
                'accessToken': str(token.access_token),
                'refreshToken': str(token),
            },
            status=status.HTTP_201_CREATED,
        )


class MemberSignupView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'signup'

    @swagger_auto_schema(
        operation_summary='팀원 회원가입',
        operation_description='회사 코드로 기존 회사에 합류합니다. 화면 언어는 en으로 고정됩니다.',
        request_body=MemberSignupSerializer,
        consumes=FORM_AND_JSON,
        responses={
            201: AUTH_RESPONSE,
            400: '잘못된 요청 (email_taken / weak_password / company_code_not_found)',
            429: '요청 한도 초과 (rate_limited)',
        },
        tags=['Auth'],
        security=[],
    )
    def post(self, request):
        serializer = MemberSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()

        token = RefreshToken.for_user(membership.user)

        return Response(
            {
                'userId': membership.user_id,
                'role': membership.role,
                'company': {
                    'id': membership.company_id,
                    'name': membership.company.name,
                },
                'next': NEXT_ROUTES[membership.role],
                'accessToken': str(token.access_token),
                'refreshToken': str(token),
            },
            status=status.HTTP_201_CREATED,
        )

# 로그인 담당 view
class AuthView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'login'

    @swagger_auto_schema(
        operation_summary='로그인',
        operation_description='비밀번호가 틀렸거나 소속이 없으면 모두 invalid_credentials로 응답합니다.',
        request_body=AuthSerializer,
        consumes=FORM_AND_JSON,
        responses={
            200: AUTH_RESPONSE,
            400: '잘못된 요청 (invalid_credentials)',
            429: '요청 한도 초과 (rate_limited)',
        },
        tags=['Auth'],
        security=[],
    )
    def post(self, request):
        serializer = AuthSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        membership = serializer.validated_data['membership']

        token = RefreshToken.for_user(membership.user)

        return Response(
            {
                'userId': membership.user_id,
                'role': membership.role,
                'company': {
                    'id': membership.company_id,
                    'name': membership.company.name,
                },
                'next': NEXT_ROUTES[membership.role],
                'accessToken': str(token.access_token),
                'refreshToken': str(token),
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary='로그아웃',
        operation_description='현재 서버는 발급된 토큰을 무효화하지 않습니다.',
        responses={
            200: LOGOUT_RESPONSE,
            401: '인증되지 않음',
        },
        tags=['Auth'],
    )
    def post(self, request):
        logout(request)
        return Response({"message": "logout success!"}, status=status.HTTP_200_OK)