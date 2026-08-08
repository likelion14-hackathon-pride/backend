from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.views import exception_handler

# 에러 코드 
# field가 None이면 폼 상단에 표시
ERROR_FIELDS = {
    'invalid_credentials': None,
    'company_code_not_found': 'companyCode',
    'email_taken': 'email',
    'weak_password': 'password',
    'rate_limited': None,
}


def _find_code(detail):
    if isinstance(detail, dict):
        details = detail.values()
    elif isinstance(detail, list):
        details = detail
    else:
        code = getattr(detail, 'code', None)
        return code if code in ERROR_FIELDS else None

    for item in details:
        code = _find_code(item)
        if code:
            return code
    return None

#  {"error": {"code", "field"}} 형태로 통일
# 스팩에 없는건 DRF 기본 응답 사용
# 한/영 둘다 필요하므로 프론트에서 code로 골라 쓰는걸로
def api_exception_handler(exc, context):

    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, Throttled):
        code = 'rate_limited'
    elif isinstance(exc, ValidationError):
        code = _find_code(exc.detail)
    else:
        code = None

    if code:
        response.data = {'error': {'code': code, 'field': ERROR_FIELDS[code]}}

    return response
