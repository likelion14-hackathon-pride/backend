from django.db import migrations, models
import pgvector.django.vector


class Migration(migrations.Migration):

    dependencies = [
        ('handbook', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='companyscope',
            name='state',
            field=models.CharField(blank=True, max_length=8, null=True),
        ),
        migrations.AlterField(
            model_name='handbookentry',
            name='origin',
            field=models.CharField(max_length=16),
        ),
        migrations.AlterField(
            model_name='handbookentry',
            name='embedding_ko',
            field=pgvector.django.vector.VectorField(
                blank=True,
                dimensions=1536,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name='handbookentry',
            name='embedding_en',
            field=pgvector.django.vector.VectorField(
                blank=True,
                dimensions=1536,
                null=True,
            ),
        ),
    ]
