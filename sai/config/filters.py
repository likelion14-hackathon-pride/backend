from drf_yasg import openapi

from .errors import INVALID_PARAMETER, field_error


def enum_parameter(name, choices, description=None):
    return openapi.Parameter(
        name, openapi.IN_QUERY, type=openapi.TYPE_STRING,
        enum=list(choices.values), description=description,
    )


def int_parameter(name, description=None):
    return openapi.Parameter(
        name, openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description=description,
    )


# 한 화면이 여러 값을 함께 봐야 하는 필터. enum_parameter 와 달리 쉼표로 나열한다.
def enum_list_parameter(name, choices, description=None):
    usage = f'쉼표로 여러 개를 보낼 수 있습니다. 허용값: {", ".join(choices.values)}'

    return openapi.Parameter(
        name, openapi.IN_QUERY, type=openapi.TYPE_STRING,
        description=f'{description} {usage}' if description else usage,
    )


def enum_value(request, name, choices):
    value = request.query_params.get(name)
    if not value:
        return None
    if value not in choices.values:
        raise field_error(name, f'invalid {name}', INVALID_PARAMETER)

    return value


def enum_list_value(request, name, choices):
    raw = request.query_params.get(name)
    if not raw:
        return None

    values = [value.strip() for value in raw.split(',') if value.strip()]
    if not values:
        return None

    for value in values:
        if value not in choices.values:
            raise field_error(name, f'invalid {name}', INVALID_PARAMETER)

    return values


def int_value(request, name):
    value = request.query_params.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise field_error(name, f'invalid {name}', INVALID_PARAMETER)


def flag(request, name):
    return request.query_params.get(name, '').lower() in ('1', 'true')


def filter_enum(queryset, request, name, choices, field=None):
    value = enum_value(request, name, choices)

    return queryset if value is None else queryset.filter(**{field or name: value})


def filter_enum_list(queryset, request, name, choices, field=None):
    values = enum_list_value(request, name, choices)

    return queryset if values is None else queryset.filter(**{f'{field or name}__in': values})


def filter_int(queryset, request, name, field=None):
    value = int_value(request, name)

    return queryset if value is None else queryset.filter(**{field or name: value})
