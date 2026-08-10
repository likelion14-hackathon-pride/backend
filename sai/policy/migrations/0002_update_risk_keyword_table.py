from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('policy', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='riskkeyword',
            name='uniq_company_risk_keyword',
        ),
        migrations.RenameField(
            model_name='riskkeyword',
            old_name='keyword',
            new_name='word',
        ),
        migrations.RenameField(
            model_name='riskkeyword',
            old_name='message',
            new_name='note',
        ),
        migrations.RenameField(
            model_name='riskkeyword',
            old_name='severity',
            new_name='level',
        ),
        migrations.AddField(
            model_name='riskkeyword',
            name='aliases',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='riskkeyword',
            name='word',
            field=models.CharField(max_length=50),
        ),
        migrations.AlterField(
            model_name='riskkeyword',
            name='note',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='riskkeyword',
            name='level',
            field=models.CharField(
                choices=[('CAUTION', 'Caution'), ('DANGER', 'Danger')],
                default='CAUTION',
                max_length=20,
            ),
        ),
        migrations.AlterModelTable(
            name='riskkeyword',
            table='policy_riskykeyword',
        ),
        migrations.AddConstraint(
            model_name='riskkeyword',
            constraint=models.UniqueConstraint(
                fields=('company', 'word'),
                name='uniq_company_risky_keyword',
            ),
        ),
    ]
