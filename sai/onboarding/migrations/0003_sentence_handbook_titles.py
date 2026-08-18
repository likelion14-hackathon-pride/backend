from django.db import migrations


def rule_title(text):
    return ' '.join(text.split())[:200]


# 이전 Day 0 답변도 질문 주제 대신 실제 규칙 문장을 제목으로 사용한다.
def update_titles(apps, schema_editor):
    HandbookEntry = apps.get_model('handbook', 'HandbookEntry')
    entries = HandbookEntry.objects.filter(origin='ONBOARDING').exclude(body_ko=None)

    changed = []
    for entry in entries:
        entry.title = rule_title(entry.body_ko)
        entry.title_en = rule_title(entry.body_en) if entry.body_en else None
        changed.append(entry)

    HandbookEntry.objects.bulk_update(changed, ['title', 'title_en'])


class Migration(migrations.Migration):
    dependencies = [
        ('onboarding', '0002_slim_question'),
        ('handbook', '0011_handbookentry_title_en'),
    ]

    operations = [
        migrations.RunPython(update_titles, migrations.RunPython.noop),
    ]
