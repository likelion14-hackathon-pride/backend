from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth import logout
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from .serializers import AuthSerializer, SignupSerializer

NEXT_ROUTES = {
    'owner': 'onboarding/day0',
    'member': 'app/home',
}

def _string(example):
    return openapi.Schema(type=openapi.TYPE_STRING, example=example)


# 에러는 전부 {"error": {"code", "field"}} 형태 
ERROR_SCHEMA = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'error': openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'code': _string('invalid_credentials'),
                'field': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    nullable=True,
                    description='문구를 표시할 필드. null이면 폼 상단에 표시한다.',
                ),
            },
        ),
    },
)

SIGNUP_RESPONSE = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'userId': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
        'role': _string('owner'),
        'company': openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'id': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
                'name': _string('에코랩'),
                'code': _string('ECHO-4821'),
            },
        ),
        'next': _string('onboarding/day0'),
        'accessToken': _string('eyJhbGciOi...'),
        'refreshToken': _string('eyJhbGciOi...'),
    },
)

LOGIN_RESPONSE = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'userId': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
        'role': _string('member'),
        # 로그인 응답의 company에는 code를 담지 않는다.
        'company': openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'id': openapi.Schema(type=openapi.TYPE_INTEGER, example=1),
                'name': _string('에코랩'),
            },
        ),
        'next': _string('app/home'),
        'lastRoute': openapi.Schema(
            type=openapi.TYPE_STRING, nullable=True, example='/instructions/482'
        ),
        'accessToken': _string('eyJhbGciOi...'),
        'refreshToken': _string('eyJhbGciOi...'),
    },
)


class SignupView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'signup'

    @swagger_auto_schema(
        operation_summary="회원가입",
        operation_description=(
            "계정을 생성합니다. role에 따라 필요한 필드가 다릅니다.\n\n"
            "- owner: 회사를 새로 만들고 회사 코드를 발급받습니다.\n"
            "- member: 기존 회사에 합류합니다.\n\n"
        ),
        request_body=SignupSerializer,
        responses={
            201: openapi.Response("가입 성공", SIGNUP_RESPONSE),
            400: openapi.Response(
                "email_taken / weak_password / company_code_not_found", ERROR_SCHEMA
            ),
            429: openapi.Response("rate_limited", ERROR_SCHEMA),
        },
        security=[],  # 인증 없이 호출하는 API
    )
    def post(self, request):
        serializer = SignupSerializer(data=request.data)
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

# 로그인 담당 view
class AuthView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'login'

    @swagger_auto_schema(
        operation_summary="로그인",
        operation_description=(
            "이메일, 비밀번호, 회사 코드로 로그인합니다.\n\n"
            "회사 코드는 대소문자와 공백/하이픈을 무시하고 비교합니다 "
        ),
        request_body=AuthSerializer,
        responses={
            200: openapi.Response("로그인 성공", LOGIN_RESPONSE),
            400: openapi.Response(
                "invalid_credentials / company_code_not_found", ERROR_SCHEMA
            ),
            429: openapi.Response("rate_limited", ERROR_SCHEMA),
        },
        security=[],  # 인증 없이 호출하는 API
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
                'lastRoute': membership.last_route,
                'accessToken': str(token.access_token),
                'refreshToken': str(token),
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="로그아웃",
        operation_description=(
            "로그아웃합니다. Authorization 헤더에 access token이 필요합니다.\n\n"
            "현재 서버는 발급된 토큰을 무효화하지 않습니다. "
        ),
        responses={
            200: openapi.Response(
                "로그아웃 성공",
                openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={'message': _string('logout success!')},
                ),
            ),
            401: "인증되지 않음",
        },
    )
    def post(self, request):
        logout(request)
        return Response({"message": "logout success!"}, status=status.HTTP_200_OK)