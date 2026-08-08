from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import SignupSerializer

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
