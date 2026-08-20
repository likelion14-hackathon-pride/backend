import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('onboarding', '0001_initial'),
        ('handbook', '0007_handbookentry_reviewed_at'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='question',
            name='uniq_question_template_key',
        ),
        migrations.RemoveField(model_name='question', name='question_ko'),
        migrations.RemoveField(model_name='question', name='reason_text'),
        migrations.RemoveField(model_name='question', name='hint'),
        migrations.RemoveField(model_name='question', name='placeholder'),
        migrations.RemoveField(model_name='question', name='priority'),
        migrations.AlterField(
            model_name='question',
            name='template_key',
            field=models.CharField(default='', max_length=40),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='question',
            name='status',
            field=models.CharField(
                choices=[('ANSWERED', 'Answered'), ('SKIPPED', 'Skipped')],
                default='ANSWERED',
                max_length=10,
            ),
        ),
        # 프로젝트를 지우면 그 프로젝트 답도 함께 사라져야 한다.
        migrations.AlterField(
            model_name='question',
            name='scope',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='onboarding_questions',
                to='handbook.companyscope',
            ),
        ),
        migrations.AddConstraint(
            model_name='question',
            constraint=models.UniqueConstraint(
                fields=('company', 'scope', 'template_key'),
                name='uniq_question_scoped_key',
            ),
        ),
        migrations.AddConstraint(
            model_name='question',
            constraint=models.UniqueConstraint(
                condition=models.Q(('scope__isnull', True)),
                fields=('company', 'template_key'),
                name='uniq_question_company_key',
            ),
        ),
    ]
