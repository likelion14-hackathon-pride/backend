import django.contrib.postgres.indexes
import django.contrib.postgres.operations
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('handbook', '0012_handbookentry_auto_promoted_at_and_more'),
    ]

    operations = [
        django.contrib.postgres.operations.TrigramExtension(),
        django.contrib.postgres.operations.AddIndexConcurrently(
            model_name='handbookentry',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['title'],
                name='hb_title_trgm_idx',
                opclasses=['gin_trgm_ops'],
            ),
        ),
        django.contrib.postgres.operations.AddIndexConcurrently(
            model_name='handbookentry',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['title_en'],
                name='hb_title_en_trgm_idx',
                opclasses=['gin_trgm_ops'],
            ),
        ),
        django.contrib.postgres.operations.AddIndexConcurrently(
            model_name='handbookentry',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['body_ko'],
                name='hb_body_ko_trgm_idx',
                opclasses=['gin_trgm_ops'],
            ),
        ),
        django.contrib.postgres.operations.AddIndexConcurrently(
            model_name='handbookentry',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['body_en'],
                name='hb_body_en_trgm_idx',
                opclasses=['gin_trgm_ops'],
            ),
        ),
    ]
