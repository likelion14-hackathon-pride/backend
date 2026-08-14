from .models import CompanyScope


# 회사 전반 규칙의 기본 범위. 오너 대시보드 핸드북 계층을 그대로 따른다.
DEFAULT_COMPANY_SCOPES = [
    (CompanyScope.AreaKey.COMPANY, 'Company', '가치 · 미션 · 커뮤니케이션 · 핸드북 운영'),
    (CompanyScope.AreaKey.PEOPLE, 'People Group', '인사 · 채용 · 다양성 · 보상 · 학습'),
    (CompanyScope.AreaKey.PRODUCT_ENG, 'Product / Engineering', '제품 원칙 · 개발 운영 · 고객지원 · 오픈소스'),
    (CompanyScope.AreaKey.SECURITY, 'Security', '보안 표준 · 제품 보안 · 보안 운영 · 위협 관리'),
]


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
