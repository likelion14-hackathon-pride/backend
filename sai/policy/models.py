from django.db import models


class RiskKeyword(models.Model):
    class Level(models.TextChoices):
        CAUTION = 'CAUTION'
        DANGER = 'DANGER'

    company = models.ForeignKey(
        'companies.Company',
        on_delete=models.CASCADE,
        related_name='risk_keywords',
    )
    word = models.CharField(max_length=50)
    aliases = models.JSONField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    level = models.CharField(
        max_length=20,
        choices=Level.choices,
        default=Level.CAUTION,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'policy_riskykeyword'
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'word'],
                name='uniq_company_risky_keyword',
            ),
        ]
