from django.db import models


# 질문 문구와 선택지는 questions.py 에 있다. 여기에는 대표가 무엇을 골랐는지만 남긴다.
# 답하지 않은 질문은 행이 없다. 행이 없으면 아직 안 한 것이다.
class Question(models.Model):
    class Status(models.TextChoices):
        ANSWERED = 'ANSWERED'
        SKIPPED = 'SKIPPED'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='onboarding_questions')
    template_key = models.CharField(max_length=40)
    # 프로젝트 질문이면 그 프로젝트. 회사 질문은 비어 있고, 답이 들어갈 자리는 questions.py 가 정한다.
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.CASCADE, null=True, blank=True, related_name='onboarding_questions')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ANSWERED)
    answer_ko = models.TextField(null=True, blank=True)
    created_entry = models.ForeignKey('handbook.HandbookEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='onboarding_questions')
    answered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'onboarding_question'
        constraints = [
            # 프로젝트 질문은 프로젝트마다 한 번씩 나온다. 회사 질문은 scope 가 비어 회사당 한 번이다.
            models.UniqueConstraint(
                fields=['company', 'scope', 'template_key'],
                name='uniq_question_scoped_key',
            ),
            models.UniqueConstraint(
                fields=['company', 'template_key'],
                condition=models.Q(scope__isnull=True),
                name='uniq_question_company_key',
            ),
        ]
