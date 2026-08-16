from django.db import models
from pgvector.django import VectorField


class InstructionCard(models.Model):
    # 사람이 옮기는 상태. 보드의 WAITING / ANSWERED 는 질문 상태에서 나오므로 여기 없다.
    class Status(models.TextChoices):
        READY = 'READY'
        IN_PROGRESS = 'IN_PROGRESS'
        DONE = 'DONE'

    class Column(models.TextChoices):
        READY = 'READY'
        IN_PROGRESS = 'IN_PROGRESS'
        WAITING = 'WAITING'
        ANSWERED = 'ANSWERED'
        DONE = 'DONE'

    # 완곡한 한국어 요청을 외국인 독자가 오판하는 지점이 급함의 정도다.
    # 문장으로만 두면 '급하지 않지만 빨리' 같은 모순이 섞여 나온다. 먼저 하나를 고르게 한다.
    class Urgency(models.TextChoices):
        URGENT = 'URGENT'      # 하던 일을 멈추고
        SOON = 'SOON'          # 기한이 있음
        WHENEVER = 'WHENEVER'  # 여유 있을 때. 요청자가 그렇게 말했다
        UNCLEAR = 'UNCLEAR'    # 근거가 없어 판단하지 않음

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
    urgency = models.CharField(
        max_length=8, choices=Urgency.choices, default=Urgency.UNCLEAR
    )
    tone_note = models.TextField(null=True, blank=True)
    tone_note_en = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.READY)
    read_at = models.DateTimeField(null=True, blank=True)
    # 같은 요청을 슬랙에 여러 번 올리면 카드도 여러 장이 된다.
    # 원문마다 카드를 남기되, 처음 것만 목록에 보여 주고 나머지는 여기로 묶는다.
    duplicate_of = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='duplicates'
    )
    # 중복 판정에 쓰는 원문 벡터. 카드마다 들고 있어야 청크 유무와 무관하게 비교할 수 있다.
    embedding = VectorField(dimensions=1536, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'cards_instructioncard'


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
    # 누가 이 빈칸을 채웠는가. 비어 있으면 아직 아무도 답하지 않았다는 뜻이고,
    # 그때만 사람을 기다린다. 핸드북에 답이 있는 것까지 대표에게 보내면 안 된다.
    class AnsweredBy(models.TextChoices):
        SAI = 'SAI'
        OWNER = 'OWNER'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='card_blanks')
    card = models.ForeignKey(InstructionCard, on_delete=models.CASCADE, related_name='blanks')
    question_en = models.TextField()
    sai_answer_en = models.TextField(null=True, blank=True)
    sai_answer_ko = models.TextField(null=True, blank=True)
    answered_by = models.CharField(max_length=5, choices=AnsweredBy.choices, null=True, blank=True)
    # SAI가 답했을 때 근거로 쓴 규칙과 과거 대화. 출처 없는 답은 화면에 띄우지 않는다.
    answer_citations = models.JSONField(null=True, blank=True)
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
