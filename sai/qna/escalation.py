from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from sources.models import Connection
from sources.slack import SlackClient, SlackError
from sources.text import normalize_slack_text

JUDGE_PROMPT = """A foreign employee asked a question that the company handbook could not answer.
SAI relayed it to the company owner in Korean. Here is the owner's reply from Slack.

Fill the fields in the order they are given. Write `reason` FIRST, before you decide anything.

reason - always required, never empty. One short Korean sentence stating what the reply actually
  said and why that does or does not settle the question. This is shown to a person, so it must
  be specific. "답변으로 볼 수 없습니다" alone is not acceptable; say what was missing.
  Good: "휴가 일수를 한 달 4일로 명확히 답했습니다."
  Good: "확인해보겠다고만 하고 일수는 말하지 않았습니다."

is_answer - true only when the reply gives the employee something they can act on.
  false for deflections and holding replies: "확인해볼게요", "나중에 얘기해요", "음...",
  a question back, or an unrelated message.

needs_review - true when the reply is ambiguous, only partly answers, or you had to guess.

answer_ko - when is_answer is true: the answer restated as a clear rule in Korean. Keep the
  owner's meaning exactly. Copy numbers, dates and names as written. Never add a condition
  the owner did not state. When is_answer is false: empty string.

answer_en - the same in plain workplace English, for the employee to read.
  When is_answer is false: empty string."""


BLANK_PROMPT = """A foreign employee received a work request in Korean on Slack. SAI turned it into
a card, and one thing is missing before they can start. Write the question they should send to the
person who made the request.

Write it in Korean, as that employee would write it. Polite workplace Korean, one or two sentences.

- Name the work you are asking about, using the words from the original Slack message, so the
  reader knows which request this is about without scrolling back.
- Ask only what is missing. Do not restate the whole request and do not add pleasantries.
- Do not invent details. If the English question is vague, keep the Korean question equally narrow.

Return only the question text."""


# 카드의 미정 항목을 대표에게 보낼 한국어 질문으로 바꾼다.
# 영어 질문을 그대로 보내면 대표가 무슨 건인지 모른다. 원문과 목적을 함께 넣는다.
def draft_from_blank(blank):
    card = blank.card
    original = (card.document.raw_text if card.document else '') or ''

    completion = _get_client().chat.completions.create(
        model=settings.OPENAI_TRANSLATOR_MODEL,
        messages=[
            {'role': 'system', 'content': BLANK_PROMPT},
            {
                'role': 'user',
                'content': (
                    f'Original Slack request:\n{original[:500]}\n\n'
                    f'What the card says the work is:\n{card.purpose}\n\n'
                    f'What the employee needs to know:\n{blank.question_en}'
                ),
            },
        ],
        temperature=0,
    )

    return (completion.choices[0].message.content or '').strip() or None


# 필드 순서가 곧 생성 순서다. 근거를 먼저 쓰게 두면 판정 품질도 같이 올라간다.
class AnswerJudgement(BaseModel):
    reason: str = Field(description='판정 근거 한 문장. 비워 두면 안 된다.')
    is_answer: bool = Field(description='답변이 질문을 실제로 해결하는가')
    needs_review: bool = Field(description='사람이 한 번 봐야 하는가')
    answer_ko: str = Field(description='정리한 규칙(한국어). is_answer=false면 빈 문자열')
    answer_en: str = Field(description='정리한 규칙(영어). is_answer=false면 빈 문자열')


def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY)


def get_slack_connection(company):
    connection = Connection.objects.filter(
        company=company, kind=Connection.Kind.SLACK, disconnected_at__isnull=True
    ).first()
    if connection is None:
        raise SlackError('slack_not_connected')

    return connection


# 대표에게 보낼 문구. 질문자가 누구인지와 왜 묻는지가 보여야 답이 잘 온다.
def build_message(escalation):
    asker = escalation.asked_by.display_name

    return (
        f'*{asker}* 님이 물었는데 핸드북에 근거가 없어 확인 요청드립니다.\n\n'
        f'> {escalation.draft_ko}\n\n'
        f'_답장해 주시면 SAI가 정리해서 전달합니다. 스레드로 달아도 되고 채널에 그냥 쓰셔도 됩니다._'
    )


# 슬랙 스레드는 (채널, ts) 쌍으로만 식별된다. ts만으로는 어느 채널인지 알 수 없어
# slack_thread_ref 에 두 값을 함께 담는다.
def make_thread_ref(channel_id, ts):
    return f'{channel_id}:{ts}'


def parse_thread_ref(thread_ref):
    channel_id, _, ts = (thread_ref or '').partition(':')

    return (channel_id, ts) if channel_id and ts else (None, None)


# 슬랙 채널에 질문을 올린다. 응답 ts를 스레드 참조로 저장해 두었다가 답변을 되받는다.
def send_to_slack(escalation, item):
    connection = get_slack_connection(escalation.company)
    text = build_message(escalation)
    response = SlackClient(connection.bot_token).post_message(item.external_id, text)

    escalation.sent_text = text
    escalation.slack_thread_ref = make_thread_ref(item.external_id, response['ts'])
    escalation.status = escalation.Status.SENT
    escalation.sent_at = timezone.now()
    escalation.save(update_fields=['sent_text', 'slack_thread_ref', 'status', 'sent_at'])

    return escalation


# 우리 질문 뒤에 채널에서 주워 담을 최대 메시지 수.
MAX_FOLLOW_UPS = 5


def _human_text(message):
    if message.get('bot_id') or message.get('subtype'):
        return None

    return (message.get('text') or '').strip() or None


# 슬랙에서 스레드 답장은 한 번 더 눌러야 해서 대부분 그냥 채널에 답한다.
# 다음 봇 메시지(= 다른 질문)가 나오면 거기서 끊는다.
def _channel_follow_ups(client, channel_id, ts):
    history = client.channel_history(channel_id, max_messages=50)
    after = [m for m in history if float(m['ts']) > float(ts)]
    after.sort(key=lambda m: float(m['ts']))

    collected = []
    for message in after:
        if message.get('bot_id'):
            break
        text = _human_text(message)
        if text:
            collected.append((message, text))
        if len(collected) >= MAX_FOLLOW_UPS:
            break

    return collected


# 스레드 답글을 먼저 보고, 없으면 채널에 이어 붙은 메시지를 본다.
# 여러 건이면 합쳐서 넘긴다. 슬랙에서는 한 답을 여러 줄로 나눠 쓰는 일이 흔하다.
# 엉뚱한 메시지가 섞여도 답변 판정 단계에서 걸러진다.
def fetch_reply(escalation):
    channel_id, ts = parse_thread_ref(escalation.slack_thread_ref)
    if not channel_id:
        raise SlackError('thread_ref_missing')

    connection = get_slack_connection(escalation.company)
    client = SlackClient(connection.bot_token)

    collected = [
        (reply, text)
        for reply in client.thread_replies(channel_id, ts)
        if reply['ts'] != ts and (text := _human_text(reply))
    ]
    if not collected:
        collected = _channel_follow_ups(client, channel_id, ts)

    if not collected:
        return None, None

    last_message = collected[-1][0]
    joined = '\n'.join(normalize_slack_text(text) for _, text in collected)

    return last_message, joined


# 대표 답장이 실제로 답이 되는지 판정하고, 되면 양쪽 언어로 정리한다.
def judge_reply(question_en, draft_ko, reply_text):
    client = _get_client()
    try:
        completion = client.chat.completions.parse(
            model=settings.OPENAI_ANSWER_MODEL,
            messages=[
                {'role': 'system', 'content': JUDGE_PROMPT},
                {
                    'role': 'user',
                    'content': (
                        f"Employee's question (English):\n{question_en}\n\n"
                        f'What SAI asked the owner (Korean):\n{draft_ko}\n\n'
                        f"Owner's reply (Korean):\n{reply_text}"
                    ),
                },
            ],
            response_format=AnswerJudgement,
            temperature=0,
        )
    except (OpenAIError, ValueError) as exc:
        raise RuntimeError(f'judge_failed: {type(exc).__name__}') from exc

    return completion.choices[0].message.parsed
