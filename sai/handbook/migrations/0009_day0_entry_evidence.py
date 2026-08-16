from django.db import migrations

SOURCE_LABEL = 'Day 0 기본 규칙'


# Day 0 답변으로 만든 항목에 근거를 남기기 전에 만들어진 것들은 출처가 비어 있다.
# 팀원이 출처를 누르면 빈 화면이 뜨므로, 지금 코드가 만들었을 것과 같은 근거를 채운다.
def add_owner_evidence(apps, schema_editor):
    HandbookEntry = apps.get_model('handbook', 'HandbookEntry')
    HandbookEvidence = apps.get_model('handbook', 'HandbookEvidence')

    pending = HandbookEntry.objects.filter(
        origin='ONBOARDING', evidences__isnull=True
    ).exclude(body_ko=None)

    HandbookEvidence.objects.bulk_create([
        HandbookEvidence(
            company_id=entry.company_id,
            entry=entry,
            quote=entry.body_ko,
            tag='OWNER',
            source_label=SOURCE_LABEL,
            occurred_at=entry.confirmed_at or entry.created_at,
        )
        for entry in pending
    ])


def remove_owner_evidence(apps, schema_editor):
    HandbookEvidence = apps.get_model('handbook', 'HandbookEvidence')
    HandbookEvidence.objects.filter(
        tag='OWNER', source_label=SOURCE_LABEL, entry__origin='ONBOARDING'
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('handbook', '0008_handbookentry_uniq_entry_dedupe_key'),
    ]

    operations = [
        migrations.RunPython(add_owner_evidence, remove_owner_evidence),
    ]
