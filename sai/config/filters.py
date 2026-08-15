from drf_yasg import openapi
from rest_framework.exceptions import ValidationError


def enum_parameter(name, choices, description=None):
    return openapi.Parameter(
        name, openapi.IN_QUERY, type=openapi.TYPE_STRING,
        enum=list(choices.values), description=description,
    )


def enum_value(request, name, choices):
    value = request.query_params.get(name)
    if not value:
        return None
    if value not in choices.values:
        raise ValidationError({name: [f'invalid {name}']})

    return value


def int_value(request, name):
    value = request.query_params.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValidationError({name: [f'invalid {name}']})


def flag(request, name):
    return request.query_params.get(name, '').lower() in ('1', 'true')


def filter_enum(queryset, request, name, choices, field=None):
    value = enum_value(request, name, choices)

    return queryset if value is None else queryset.filter(**{field or name: value})


def filter_int(queryset, request, name, field=None):
    value = int_value(request, name)

    return queryset if value is None else queryset.filter(**{field or name: value})
