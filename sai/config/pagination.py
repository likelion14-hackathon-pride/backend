from drf_yasg import openapi
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

CURSOR_PARAMETER = openapi.Parameter(
    'cursor', openapi.IN_QUERY,
    description='이전 응답의 nextCursor. 첫 페이지에서는 생략합니다.',
    type=openapi.TYPE_STRING,
)
LIMIT_PARAMETER = openapi.Parameter(
    'limit', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, default=DEFAULT_LIMIT,
)


def read_limit(request):
    try:
        limit = int(request.query_params.get('limit', DEFAULT_LIMIT))
    except ValueError:
        raise ValidationError({'limit': ['limit must be an integer']})

    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationError({'limit': [f'limit must be between 1 and {MAX_LIMIT}']})

    return limit


# id 내림차순 커서 페이지네이션. (한 페이지, nextCursor) 반환.
#
# 커서는 마지막 항목의 id 다. 오프셋과 달리 앞쪽에 행이 추가돼도 페이지가 밀리지 않는다.
# 한 건 더 받아 보고 남았는지 판단한다. 전체 개수를 세지 않기 위함이다.
def paginate(queryset, request):
    limit = read_limit(request)

    cursor = request.query_params.get('cursor')
    if cursor:
        try:
            queryset = queryset.filter(id__lt=int(cursor))
        except ValueError:
            raise ValidationError({'cursor': ['invalid cursor']})

    rows = list(queryset.order_by('-id')[:limit + 1])
    items = rows[:limit]
    next_cursor = str(items[-1].id) if len(rows) > limit else None

    return items, next_cursor


def page_response(serializer_class, items, next_cursor=None):
    return Response(
        {'items': serializer_class(items, many=True).data, 'nextCursor': next_cursor},
        status=status.HTTP_200_OK,
    )


def paged_response(serializer_class, queryset, request):
    return page_response(serializer_class, *paginate(queryset, request))
