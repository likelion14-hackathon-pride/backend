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
  When is_answer is false: empty string.

title_ko - a short Korean name for this rule, the way it would sit in a handbook list.
  A noun phrase, not a sentence and not a question. Under 40 characters.
  Name what the rule settles, including the project or area when the reply is specific to one.
  Copy project, repository, channel and tool names exactly as written. Never translate them:
  "payment-api" stays "payment-api", not "결제-api".
  Good: "payment-api PR 리뷰어", "긴급 배포 예외 조건", "연차 사용 일수"
  Bad: "PR 리뷰어는 누구인가요?", "지훈님을 리뷰어로 지정합니다", "규칙"
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


ADDITION_PROMPT = """A foreign employee is sending a Korean question to their company owner.
SAI already wrote the main question in Korean. These are extra lines the employee added in their
own words, in their own language.

Each line you receive becomes one Korean sentence in the same message, in the same order.

- Match the register of a message to one's employer: polite 합니다체, direct, no honorific padding.
- Do not translate word for word. Write what a Korean employee would actually write.
- Do not add anything the employee did not say. Do not merge, split, or summarise lines.
- Leave untouched: channel names (#dev), tool and product names, file names, code, URLs,
  numbers, and times.
- Return one line per index you received, using the same index."""
