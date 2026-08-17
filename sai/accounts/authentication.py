from rest_framework_simplejwt.authentication import JWTAuthentication

from .presence import touch


# 토큰이 확인될 때마다 마지막 접속 시각을 남긴다.
#
# 별도의 하트비트 엔드포인트를 두지 않는 이유는, 화면이 그것을 부르는 것을 잊으면
# 멀쩡히 켜 둔 사람이 오프라인으로 보이기 때문이다. 인증은 어차피 모든 요청이 거친다.
class LastSeenJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            touch(result[0])

        return result
