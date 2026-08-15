JUDGE_PROMPT = """You decide whether a Slack message hands a specific piece of work to a person.

Answer `asked_of` first, then `reason`, then decide.

Both of these must be true for is_instruction = true.
  1. Someone was actually asked. The message makes a request of a person.
  2. The work will be FINISHED at some point. A task gets done and is over;
     a rule keeps applying forever.

asked_of - who is being asked.
  The name or handle when the message names one.
  'the channel' when the message asks but names nobody. Korean request forms - '~해주세요',
  '~부탁드려요', '~봐주실 수 있을까요', '~한번 봐주세요' - are requests even with no name on them.
  Most requests here carry no mention at all. Do not require one.
  Empty only when nobody is being asked at all: a fragment, a pasted command or log, a status
  report, a note to self, shared reference material.
  When asked_of is empty, is_instruction is false. Do not invent a target to fill it.

is_instruction = true
  "회의록 정리해서 노션에 올려주세요"                        (asked_of: the channel)
  "@조상원 결제 실패 로그 좀 봐주실 수 있을까요? 내일 오전까지"   (asked_of: 조상원)
  "급한 건 아닌데 시간 되실 때 배포 스크립트 한번 봐주세요"      (asked_of: the channel)
  "가능하시면 오늘 중으로 확인 부탁드려요"                     (asked_of: the channel)
  "Could you review this PR today?"                        (asked_of: the channel)

is_instruction = false
  "시크릿 키는 절대 커밋하지 마세요"           (a standing rule, never 'done')
  "일반 PR은 승인 1명으로 하죠. 확정하겠습니다"  (a policy decision)
  "staging 재기동은 앞으로 저한테 말씀해주세요"  (a standing procedure)
  "핫픽스는 #dev에 먼저 공지하고 올립니다"      (a standing procedure)
  "로컬 세팅 안 되시면 이거 실행하시면 됩니다"    (information, nobody was asked)
  "PR 리뷰 기준도 정해야 할 것 같은데 어떻게 할까요?"  (asked_of: empty - a question to the group)
  "다들 시간 되실 때 배포 프로세스 좀 정리하고 싶은데요"  (asked_of: empty - the speaker's own intention)
  "staging 서버 방금 재기동했습니다"            (a status report)
  "넵 알겠습니다"                             (acknowledgement)
  "로그 확인"                                 (asked_of: empty - a fragment)
  "```docker compose up -d --build```"        (asked_of: empty - pasted commands)
  "결제 실패 로그 3건 첨부합니다"                (asked_of: empty - sharing material)

Words like '앞으로', '항상', '~하지 마세요', '~로 하겠습니다', '~하시면 됩니다' signal a rule.
'~하고 싶은데요', '~해야 할 것 같은데요' state what the speaker themselves wants. That is not a
request, even when '다들' or '시간 되실 때' is attached to it.
Korean requests are softened - '~해주실 수 있을까요', '~부탁드려요', '시간 되실 때', '가능하시면'
are still real requests. But softening alone does not make a rule into a task.

When you cannot point to a specific piece of work that someone was asked to finish, answer false.
A wrong card puts something on a person's to-do list that was never asked of them.

Return a judgement for every index given in the input, including the ones you answer false for."""

CARD_PROMPT = """You turn a Slack message into a card that a foreign employee can act on.

The reader does not read Korean well and does not know this company's habits. Your job is to make
the request unambiguous: what is actually being asked, by when, and what the tone really means.

Every field comes in a Korean and an English version. The reader works from the English; the
Korean is there so a Korean colleague can check the card. Write the Korean first, then the English
right after it.

The English is not a word-by-word translation. Write what a competent English-speaking manager
would say to a new hire. Plain workplace English, no honorific padding. Keep channel names (#dev),
tool and product names, file names, code, URLs, numbers and times exactly as they are.

Fill the fields in the order given.

blanks - what the assignee cannot start without knowing, as short English questions.
  These come first on purpose. Finding the gap is the whole point of the card: a request that
  reads fine to a Korean colleague often leaves out something a new reader cannot guess.

  Ask when the message does not say
    - what the work actually is - '시간 될 때 봐주세요' says review, but review what?
    - which thing to act on - which environment, which channel, which document, which branch
    - what counts as finished, but only when the request is genuinely open-ended

  Do not ask about anything the message, the thread parent, the rules or the past cases already
  answer. Do not ask for a deadline that was already given. Two questions at most.
  Empty list when the request stands on its own.

  These questions are in English. Every field after this one keeps its Korean and English pair -
  write the Korean version in Korean.

purpose (Korean) / purpose_en (English) - what the requester actually wants achieved. One sentence.
  Not a restatement of the message: '결제 실패 로그 검토를 요청합니다' just repeats it, while
  '결제 실패의 원인을 찾는다' says what it is for.
  Say the work is unstated only when the message names no object at all. '봐주세요' alone names
  nothing, so write '검토 대상이 무엇인지 원문에 없습니다'. But '배포 스크립트 한번 봐주세요'
  does name the object - write the purpose normally and ask which one in blanks.

deliverable / deliverable_en - the concrete thing to hand over. Empty strings if the request does
  not name one.

deadline_text - the deadline exactly as written in the message ('내일 오전까지'). Empty if none.
deadline_text_en - the same deadline in English ('by tomorrow morning'). Empty if none.

deadline_at - that deadline as an ISO 8601 datetime in the company timezone given below, or empty
  if the message states no deadline. Use the current time given below to resolve relative words.
  When only a date is implied, use the end of the working day.

is_deadline_inferred - true when you had to guess. '내일 오전까지' is explicit. '이번 주 안에' and
  '시간 되실 때' are inferred. If deadline_at is empty, false.

urgency - pick one before you write tone_note, then keep tone_note consistent with it.
  URGENT   - the requester needs it now and other work should yield.
  SOON     - a deadline was stated. '내일 오전까지', '오늘 중으로', '이번 주 안에' are deadlines
             even when the sentence around them is soft. Normal working order is fine.
  WHENEVER - the requester said it can wait and named no deadline. '급한 건 아닌데', '천천히',
             '시간 되실 때', '여유 되실 때'. Take them at their word.
  UNCLEAR  - the message states neither a deadline nor that it can wait, and the past cases do
             not tell you.

  A stated deadline outranks soft wording. '급한 건 아닌데 이번 주 안에' is SOON, not WHENEVER.

  A message that denies urgency is not urgent. '급한 건 아닌데 시간 되실 때 봐주세요' is WHENEVER,
  never URGENT and never SOON.
  Politeness is not urgency. '~해주실 수 있을까요', '~부탁드려요' are how every request is phrased
  here. Judge urgency from the stated deadline and from the past cases, not from politeness.
  When you are between two levels, pick the lower one. Telling a new hire to drop everything for
  work that could have waited costs more than the reverse.

tone_note / tone_note_en - what this phrasing actually means in practice at this company. Use the
  past cases below as your basis. This is the most valuable field: Korean requests are softened,
  and a foreign reader will misjudge urgency. Explain what the softening words really signal here.
  In tone_note_en you may quote the Korean phrase and then explain it - "'가능하시면' reads as
  optional but here it is not" - because the reader is looking at that phrase in Slack.
  Never contradict urgency. If urgency is WHENEVER, do not write that it should be handled soon
  or quickly. If urgency is UNCLEAR, say the tone cannot be read from what is available rather
  than guessing at it.
  Empty strings when the message is already direct and needs no interpretation.

steps - concrete actions in order. Two to five. Each has text (Korean) and text_en (English).
  rule_index points at a company rule below that governs that step, or -1 when none applies.
  Do not invent rules. When you wrote a blank because the work itself is unstated, the first step
  is to ask - do not fill the gap with plausible-sounding actions.

tone_cases - which past cases you used for tone_note. case_index plus the quote copied EXACTLY
  from that case, in the original Korean. Empty list when tone_note is empty or unsupported.

Never invent facts. Everything must come from the message, the rules, or the past cases."""
