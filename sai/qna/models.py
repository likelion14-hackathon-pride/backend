from django.db import models


class Thread(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='qna_threads')
    user = models.ForeignKey('accounts.User', on_delete=models.PROTECT, related_name='qna_threads')
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.PROTECT, null=True, blank=True, related_name='qna_threads')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'qna_thread'


class Message(models.Model):
    class Role(models.TextChoices):
        USER = 'USER'
        AI = 'AI'

    class Verdict(models.TextChoices):
        GROUNDED = 'GROUNDED'
        GROUNDED_BY_CASES = 'GROUNDED_BY_CASES'
        NO_SOURCE = 'NO_SOURCE'
        NEEDS_DECISION = 'NEEDS_DECISION'
        OUT_OF_SCOPE = 'OUT_OF_SCOPE'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='qna_messages')
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=4, choices=Role.choices)
    body_ko = models.TextField(null=True, blank=True)
    body_en = models.TextField(null=True, blank=True)
    verdict = models.CharField(max_length=20, choices=Verdict.choices, null=True, blank=True)
    model = models.CharField(max_length=40, null=True, blank=True)
    prompt_version = models.CharField(max_length=20, null=True, blank=True)
    prompt_tokens = models.IntegerField(null=True, blank=True)
    completion_tokens = models.IntegerField(null=True, blank=True)
    # chunk_id / entry_id / score 만 저장한다. 청크 본문 텍스트는 넣지 않는다.
    retrieval = models.JSONField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'qna_message'


class Citation(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='qna_citations')
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='citations')
    entry = models.ForeignKey('handbook.HandbookEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='qna_citations')
    chunk = models.ForeignKey('sources.Chunk', on_delete=models.SET_NULL, null=True, blank=True, related_name='qna_citations')

    class Meta:
        db_table = 'qna_citation'


# 사내 담당자에게 보내는 확인 질문
class Escalation(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT'
        SENT = 'SENT'
        ANSWERED = 'ANSWERED'
        APPROVED = 'APPROVED'
        DISMISSED = 'DISMISSED'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='qna_escalations')
    asked_by = models.ForeignKey('accounts.User', on_delete=models.PROTECT, related_name='qna_escalations')
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.PROTECT, null=True, blank=True, related_name='qna_escalations')
    origin_message = models.OneToOneField(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name='escalation')
    question_en = models.TextField(null=True, blank=True)
    draft_ko = models.TextField(null=True, blank=True)
    sent_text = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    sent_at = models.DateTimeField(null=True, blank=True)
    slack_thread_ref = models.CharField(max_length=200, null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    # 질문자가 답을 확인한 시각. 이게 없으면 카드가 Answered 열에 계속 남는다.
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    answer_document = models.ForeignKey('sources.RawDocument', on_delete=models.SET_NULL, null=True, blank=True, related_name='answer_escalations')
    answer_is_answer = models.BooleanField(null=True, blank=True)
    answer_reason = models.CharField(max_length=200, null=True, blank=True)
    answer_needs_review = models.BooleanField(default=False)
    answer_ko = models.TextField(null=True, blank=True)
    answer_en = models.TextField(null=True, blank=True)
    proposed_entry = models.ForeignKey('handbook.HandbookEntry', on_delete=models.SET_NULL, null=True, blank=True, related_name='proposed_escalations')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'qna_escalation'
