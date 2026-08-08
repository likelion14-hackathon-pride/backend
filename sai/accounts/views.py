from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth import logout

from .serializers import AuthSerializer, SignupSerializer

NEXT_ROUTES = {
    'owner': 'onboarding/day0',
    'member': 'app/home',
}


class SignupView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = 'signup'

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

    def post(self, request):
        logout(request)
        return Response({"message": "logout success!"}, status=status.HTTP_200_OK)