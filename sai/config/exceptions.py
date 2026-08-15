from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions
from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler

# 모든 에러 응답은 이 한 가지 모양으로 나간다.
#
#   {"error": {"code": "invalid", "field": "status", "message": "invalid status"}}
#
# code    프론트가 분기할 때 쓴다. 업무 코드(email_taken)이거나 DRF 기본 코드(invalid, not_found).
# field   폼의 어느 칸에 붙일지. 칸과 무관한 에러면 null.
# message 사람이 읽는 문장. 한 건만 보낸다.
#
# 여러 칸이 한 번에 틀렸어도 첫 번째만 보낸다. 화면은 한 번에 한 가지를 말해 주는 편이 낫고,
# 어차피 고치고 다시 보내면 다음 것이 온다.

# 업무 코드가 붙을 칸. 코드만 보고는 어느 칸인지 알 수 없어 여기서 정한다.
ERROR_FIELDS = {
    'invalid_credentials': None,
    'company_code_not_found': 'companyCode',
    'email_taken': 'email',
    'weak_password': 'password',
}

# DRF 기본 코드 중 이름을 바꿔 내보내는 것.
CODE_ALIASES = {'throttled': 'rate_limited'}


# ValidationError 의 detail 은 dict / list / 문자열이 섞여 들어온다.
# 가장 먼저 나오는 (칸, 문장, 코드) 하나를 찾는다.
def _first_error(detail, field=None):
    if isinstance(detail, dict):
        for key, value in detail.items():
            found = _first_error(value, key)
            if found:
                return found
        return None

    if isinstance(detail, (list, tuple)):
        for item in detail:
            found = _first_error(item, field)
            if found:
                return found
        return None

    return field, str(detail), getattr(detail, 'code', None)


# DRF 는 칸 이름이 없는 에러를 non_field_errors 에 담는다. 칸 이름이 아니므로 지운다.
def _clean_field(field):
    return None if field in (None, 'non_field_errors', 'detail') else field


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    # DRF 는 장고 예외를 자기 예외로 바꿔 응답을 만들지만 exc 자체는 그대로 넘겨준다.
    # 코드를 뽑으려면 여기서도 같은 변환을 해야 한다.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    if isinstance(exc, ValidationError):
        field, message, code = _first_error(exc.detail) or (None, 'invalid request', 'invalid')
        # 업무 코드는 붙을 칸이 정해져 있다. 그 밖에는 detail 에서 찾은 칸을 쓴다.
        field = ERROR_FIELDS[code] if code in ERROR_FIELDS else _clean_field(field)
    else:
        code = getattr(exc, 'default_code', None) or 'error'
        message = str(getattr(exc, 'detail', exc))
        field = None

    response.data = {
        'error': {'code': CODE_ALIASES.get(code, code), 'field': field, 'message': message}
    }

    return response
