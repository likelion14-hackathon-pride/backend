import django.contrib.postgres.indexes
import django.contrib.postgres.operations
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('sources', '0012_chunk_embedding_en_chunk_text_en_chunk_translated_at_and_more'),
    ]

    operations = [
        django.contrib.postgres.operations.TrigramExtension(),
        django.contrib.postgres.operations.AddIndexConcurrently(
            model_name='chunk',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['text'],
                name='chunk_text_trgm_idx',
                opclasses=['gin_trgm_ops'],
            ),
        ),
        django.contrib.postgres.operations.AddIndexConcurrently(
            model_name='chunk',
            index=django.contrib.postgres.indexes.GinIndex(
                fields=['text_en'],
                name='chunk_text_en_trgm_idx',
                opclasses=['gin_trgm_ops'],
            ),
        ),
    ]
