from django.db import migrations, models
from django.db.models import Count


# 제약을 걸기 전에 이미 겹쳐 있는 것을 푼다.
# 항목 자체는 지우지 않는다. 가장 최근 것만 열쇠를 지키고 나머지는 열쇠를 비운다.
# 열쇠가 없으면 다음 초안 생성에서 새로 만들어질 뿐이라 잃는 것이 없다.
def clear_duplicate_keys(apps, schema_editor):
    HandbookEntry = apps.get_model('handbook', 'HandbookEntry')
    groups = (
        HandbookEntry.objects.filter(dedupe_key__isnull=False)
        .values('company_id', 'dedupe_key')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    for group in groups:
        stale = (
            HandbookEntry.objects.filter(
                company_id=group['company_id'], dedupe_key=group['dedupe_key']
            )
            .order_by('-id')
            .values_list('id', flat=True)[1:]
        )
        HandbookEntry.objects.filter(id__in=list(stale)).update(dedupe_key=None)


class Migration(migrations.Migration):

    dependencies = [
        ('companies', '0002_remove_company_onboarding_completed_at'),
        ('handbook', '0007_handbookentry_reviewed_at'),
    ]

    operations = [
        migrations.RunPython(clear_duplicate_keys, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='handbookentry',
            constraint=models.UniqueConstraint(
                condition=models.Q(('dedupe_key__isnull', False)),
                fields=('company', 'dedupe_key'),
                name='uniq_entry_dedupe_key',
            ),
        ),
    ]
