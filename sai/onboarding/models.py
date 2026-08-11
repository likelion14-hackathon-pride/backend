from django.db import models


class Question(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING'
        ANSWERED = 'ANSWERED'
        SKIPPED = 'SKIPPED'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='onboarding_questions')
    template_key = models.CharField(max_length=40, null=True, blank=True)
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.PROTECT, null=True, blank=True, related_name='onboarding_questions')
    question_ko = models.TextField()
    reason_text = models.CharField(max_length=120, null=True, blank=True)
    hint = models.TextField(null=True, blank=True)
    placeholder = models.CharField(max_length=200, null=True, blank=True)
    priority = models.SmallIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    answer_ko = models.TextField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    created_entry = models.ForeignKey('handbook.HandbookEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='onboarding_questions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'onboarding_question'
        constraints = [
            models.UniqueConstraint(fields=['company', 'template_key'], name='uniq_question_template_key'),
        ]
