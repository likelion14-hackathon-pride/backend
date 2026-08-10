from django.db import models


class RiskKeyword(models.Model):
    class Severity(models.TextChoices):
        CAUTION = 'CAUTION'
        DANGER = 'DANGER'

    company = models.ForeignKey(
        'companies.Company',
        on_delete=models.CASCADE,
        related_name='risk_keywords',
    )
    keyword = models.CharField(max_length=100)
    message = models.CharField(max_length=255, null=True, blank=True)
    severity = models.CharField(
        max_length=10,
        choices=Severity.choices,
        default=Severity.CAUTION,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'keyword'],
                name='uniq_company_risk_keyword',
            ),
        ]
