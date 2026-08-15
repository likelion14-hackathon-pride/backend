from django.db import models


class InstructionCard(models.Model):
    class Status(models.TextChoices):
        NEW = 'NEW'
        OPEN = 'OPEN'
        DONE = 'DONE'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='instruction_cards')
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.PROTECT, null=True, blank=True, related_name='instruction_cards')
    document = models.ForeignKey('sources.RawDocument', on_delete=models.SET_NULL, null=True, blank=True, related_name='instruction_cards')
    assignee = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_cards')
    # 카드를 읽는 사람은 외국인 직원이다. 영어가 본문이고 한국어는 대조용이다.
    # 번역을 따로 돌리지 않고 카드를 만들 때 두 언어를 함께 생성한다.
    purpose = models.TextField()
    purpose_en = models.TextField(null=True, blank=True)
    deliverable = models.TextField(null=True, blank=True)
    deliverable_en = models.TextField(null=True, blank=True)
    deadline_text = models.CharField(max_length=60, null=True, blank=True)
    deadline_text_en = models.CharField(max_length=60, null=True, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    is_deadline_inferred = models.BooleanField(default=False)
    tone_note = models.TextField(null=True, blank=True)
    tone_note_en = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'cards_instructioncard'


# 말투 해석의 근거가 된 과거 사례
class ToneEvidence(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='tone_evidences')
    card = models.ForeignKey(InstructionCard, on_delete=models.CASCADE, related_name='tone_evidences')
    document = models.ForeignKey('sources.RawDocument', on_delete=models.SET_NULL, null=True, blank=True, related_name='tone_evidences')
    quote = models.TextField(null=True, blank=True)
    outcome_note = models.CharField(max_length=120, null=True, blank=True)
    source_label = models.CharField(max_length=200, null=True, blank=True)
    permalink = models.TextField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'cards_toneevidence'


class Step(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='card_steps')
    card = models.ForeignKey(InstructionCard, on_delete=models.CASCADE, related_name='steps')
    ord = models.SmallIntegerField(default=0)
    text = models.TextField()
    text_en = models.TextField(null=True, blank=True)
    entry = models.ForeignKey('handbook.HandbookEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_steps')

    class Meta:
        db_table = 'cards_step'


# 카드에서 확인이 필요한 미정 항목
class Blank(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='card_blanks')
    card = models.ForeignKey(InstructionCard, on_delete=models.CASCADE, related_name='blanks')
    question_en = models.TextField()
    sai_answer_en = models.TextField(null=True, blank=True)
    sai_answer_ko = models.TextField(null=True, blank=True)
    escalation = models.ForeignKey('qna.Escalation', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_blanks')

    class Meta:
        db_table = 'cards_blank'


class Task(models.Model):
    class Status(models.TextChoices):
        TODO = 'TODO'
        IN_PROGRESS = 'IN_PROGRESS'
        DONE = 'DONE'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='tasks')
    user = models.ForeignKey('accounts.User', on_delete=models.PROTECT, related_name='tasks')
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.PROTECT, null=True, blank=True, related_name='tasks')
    title = models.CharField(max_length=200)
    due_at = models.DateTimeField(null=True, blank=True)
    card = models.ForeignKey(InstructionCard, on_delete=models.SET_NULL, null=True, blank=True, related_name='tasks')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TODO)
    done_at = models.DateTimeField(null=True, blank=True)
    ord = models.DecimalField(max_digits=20, decimal_places=10, default=0)

    class Meta:
        db_table = 'cards_task'
