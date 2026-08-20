from .models import CompanyScope


# 회사 전반 규칙의 기본 범위. 오너 대시보드 핸드북 계층을 그대로 따른다.
DEFAULT_COMPANY_SCOPES = [
    (CompanyScope.AreaKey.COMPANY, 'Company', '가치 · 미션 · 커뮤니케이션 · 핸드북 운영'),
    (CompanyScope.AreaKey.PEOPLE, 'People Group', '인사 · 채용 · 다양성 · 보상 · 학습'),
    (CompanyScope.AreaKey.PRODUCT_ENG, 'Product / Engineering', '제품 원칙 · 개발 운영 · 고객지원 · 오픈소스'),
    (CompanyScope.AreaKey.SECURITY, 'Security', '보안 표준 · 제품 보안 · 보안 운영 · 위협 관리'),
]

DEFAULT_COMPANY_SCOPE_DESCRIPTIONS_EN = {
    CompanyScope.AreaKey.COMPANY: 'Values · mission · communication · handbook',
    CompanyScope.AreaKey.PEOPLE: 'HR · hiring · compensation · learning',
    CompanyScope.AreaKey.PRODUCT_ENG: 'Product principles · dev ops · support',
    CompanyScope.AreaKey.SECURITY: 'Security standards · operations',
}


# 회사 전반 규칙 범위를 만든다.
# HandbookEntry.scope는 NOT NULL이라 범위가 하나도 없으면 규칙 초안을 만들 수 없다.
# 회사 생성 시점에 반드시 호출한다. 이미 있는 범위는 건너뛰므로 재실행해도 안전하다.
def seed_default_scopes(company):
    existing = set(
        CompanyScope.objects.filter(
            company=company, kind=CompanyScope.Kind.COMPANY
        ).values_list('area_key', flat=True)
    )
    scopes = [
        CompanyScope(
            company=company,
            kind=CompanyScope.Kind.COMPANY,
            area_key=area_key,
            name=name,
            description=description,
        )
        for area_key, name, description in DEFAULT_COMPANY_SCOPES
        if area_key not in existing
    ]

    return CompanyScope.objects.bulk_create(scopes)


# 검색할 범위. 화면의 '회사 전반 / 프로젝트1 / 프로젝트2' 중 무엇을 골랐는지에 대응한다.
#
# 프로젝트를 골라도 회사 규칙은 함께 본다. 프로젝트 규칙은 회사 규칙 위에 얹히는 것이지
# 대체하는 것이 아니다. 프로젝트만 뒤지면 'PR 승인 몇 명 필요해요?' 같은 회사 규칙을 놓친다.
#
# '회사 전반'은 범위 하나가 아니라 Company / People / Product / Security 네 개다.
# 그래서 아무것도 고르지 않은 것이 곧 회사 전반이다.
def scopes_in_view(company, scope=None):
    ids = list(
        CompanyScope.objects.filter(
            company=company, kind=CompanyScope.Kind.COMPANY
        ).values_list('id', flat=True)
    )
    if scope is not None and scope.id not in ids:
        ids.append(scope.id)

    return ids
