from django.db import migrations, models

RENAMED = {'NEW': 'READY', 'OPEN': 'IN_PROGRESS'}


def to_new_names(apps, schema_editor):
    InstructionCard = apps.get_model('cards', 'InstructionCard')
    for old, new in RENAMED.items():
        InstructionCard.objects.filter(status=old).update(status=new)


def to_old_names(apps, schema_editor):
    InstructionCard = apps.get_model('cards', 'InstructionCard')
    for old, new in RENAMED.items():
        InstructionCard.objects.filter(status=new).update(status=old)


class Migration(migrations.Migration):

    dependencies = [
        ('cards', '0005_instructioncard_duplicate_of_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='instructioncard',
            name='read_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='instructioncard',
            name='status',
            field=models.CharField(
                choices=[('READY', 'Ready'), ('IN_PROGRESS', 'In Progress'), ('DONE', 'Done')],
                default='READY', max_length=12,
            ),
        ),
        migrations.RunPython(to_new_names, to_old_names),
    ]
