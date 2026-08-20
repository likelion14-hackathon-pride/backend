import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .errors import CODE_ALIASES, ERROR_FIELDS, SERVER_ERROR

logger = logging.getLogger(__name__)

# 모든 에러 응답은 이 한 가지 모양으로 나간다.
#
#   {"error": {"code": "scope_not_found", "field": "scopeId", "message": "scope not found"}}
#
# code    프론트가 분기할 때 쓴다. 업무 코드(config/errors.py)이거나 DRF 기본 코드(invalid, not_found).
# field   폼의 어느 칸에 붙일지. 칸과 무관한 에러면 null.
# message 사람이 읽는 문장. 한 건만 보낸다.
#
# 여러 칸이 한 번에 틀렸어도 첫 번째만 보낸다. 화면은 한 번에 한 가지를 말해 주는 편이 낫고,
# 어차피 고치고 다시 보내면 다음 것이 온다.
#
# 상태 코드는 봉투에 넣지 않는다. 응답 상태줄에 이미 있고, 두 군데 두면 어긋날 수 있다.


def _envelope(code, field, message):
    return {
        'error': {'code': CODE_ALIASES.get(code, code), 'field': field, 'message': message}
    }


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


# DRF 가 모르는 예외. 여기서 잡지 않으면 장고 기본 500 HTML 이 나가고,
# JSON 을 기대하던 클라이언트는 파싱에서 죽는다. 원인은 로그에만 남긴다.
def _unhandled(exc, context):
    logger.error('처리되지 않은 예외 view=%s', context.get('view'), exc_info=exc)
    if settings.DEBUG:
        # 개발 중에는 장고 디버그 페이지를 그대로 본다.
        return None

    return Response(
        _envelope(SERVER_ERROR, None, 'unexpected server error'),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)

    # DRF 는 장고 예외를 자기 예외로 바꿔 응답을 만들지만 exc 자체는 그대로 넘겨준다.
    # 코드를 뽑으려면 여기서도 같은 변환을 해야 한다.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    if response is None:
        return _unhandled(exc, context)

    if isinstance(exc, ValidationError):
        field, message, code = _first_error(exc.detail) or (None, 'invalid request', 'invalid')
        # 업무 코드는 붙을 칸이 정해져 있다. 그 밖에는 detail 에서 찾은 칸을 쓴다.
        field = ERROR_FIELDS[code] if code in ERROR_FIELDS else _clean_field(field)
    else:
        # 우리가 만든 예외(DomainError)는 code 를 직접 들고 있다. 나머지는 DRF 기본 코드를 쓴다.
        code = getattr(exc, 'code', None) or getattr(exc, 'default_code', None) or 'error'
        message = str(getattr(exc, 'detail', exc))
        # 예외가 칸을 직접 들고 있으면 그것을 쓴다. 같은 코드라도 붙을 칸이 달라지는 경우다.
        field = getattr(exc, 'field', None) or ERROR_FIELDS.get(code)

    # 5xx 는 사용자가 고칠 수 없는 일이다. 화면에는 고정 문장만 나가므로 원인은 여기서 남긴다.
    if response.status_code >= 500:
        logger.error(
            '%s code=%s reason=%s', type(exc).__name__, code, getattr(exc, 'reason', None),
            exc_info=exc,
        )

    response.data = _envelope(code, field, message)
    retry_after = getattr(exc, 'retry_after', None)
    if retry_after is not None:
        response['Retry-After'] = str(retry_after)

    return response
