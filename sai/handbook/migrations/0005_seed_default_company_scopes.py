from django.db import migrations


# handbook.services.DEFAULT_COMPANY_SCOPES 와 같은 내용.
# 데이터 마이그레이션은 과거 시점의 모델을 쓰므로 services를 import하지 않고 값을 복제한다.
DEFAULT_COMPANY_SCOPES = [
    ('COMPANY', 'Company', '가치 · 미션 · 커뮤니케이션 · 핸드북 운영'),
    ('PEOPLE', 'People Group', '인사 · 채용 · 다양성 · 보상 · 학습'),
    ('PRODUCT_ENG', 'Product / Engineering', '제품 원칙 · 개발 운영 · 고객지원 · 오픈소스'),
    ('SECURITY', 'Security', '보안 표준 · 제품 보안 · 보안 운영 · 위협 관리'),
]


# 이 마이그레이션 이전에 만들어진 회사에는 범위가 없다.
# HandbookEntry.scope가 NOT NULL이라 범위가 없으면 규칙 초안을 만들 수 없으므로 채워준다.
def seed_default_scopes(apps, schema_editor):
    Company = apps.get_model('companies', 'Company')
    CompanyScope = apps.get_model('handbook', 'CompanyScope')

    scopes = []
    for company in Company.objects.all():
        existing = set(
            CompanyScope.objects.filter(company=company, kind='COMPANY')
            .values_list('area_key', flat=True)
        )
        scopes += [
            CompanyScope(
                company=company,
                kind='COMPANY',
                area_key=area_key,
                name=name,
                description=description,
            )
            for area_key, name, description in DEFAULT_COMPANY_SCOPES
            if area_key not in existing
        ]

    CompanyScope.objects.bulk_create(scopes)


class Migration(migrations.Migration):

    dependencies = [
        ('handbook', '0004_alter_companyscope_area_key_and_more'),
    ]

    operations = [
        # 되돌리기는 일부러 no-op으로 둔다.
        # HandbookEntry.scope가 CASCADE라 범위를 지우면 핸드북 항목까지 함께 삭제된다.
        migrations.RunPython(seed_default_scopes, migrations.RunPython.noop),
    ]
