from dataclasses import dataclass, field

from handbook.models import CompanyScope


@dataclass(frozen=True)
class Choice:
    label: str
    body_ko: str = ''
    body_en: str = ''
    # '예외 없음' 처럼 회사 규칙을 그대로 쓴다는 답. 규칙을 또 만들면 같은 내용이 두 벌 생긴다.
    creates_rule: bool = True


@dataclass(frozen=True)
class Spec:
    key: str
    category: str
    title: str
    question: str
    # 회사 질문은 답이 들어갈 지식공간이 정해져 있다. 프로젝트 질문은 그 프로젝트로 간다.
    area_key: str | None = None
    choices: tuple[Choice, ...] = ()
    placeholder: str = ''


CULTURE = 'CULTURE'
COMMUNICATION = 'COMMUNICATION'
DEV_FLOW = 'DEV_FLOW'
CODE_ACCESS = 'CODE_ACCESS'
PROJECT = 'PROJECT'

COMPANY = CompanyScope.AreaKey.COMPANY
PEOPLE = CompanyScope.AreaKey.PEOPLE
PRODUCT_ENG = CompanyScope.AreaKey.PRODUCT_ENG
SECURITY = CompanyScope.AreaKey.SECURITY


COMPANY_QUESTIONS = (
    Spec(
        key='fq1', category=CULTURE, area_key=COMPANY,
        title='우리가 푸는 문제',
        question='우리 회사가 궁극적으로 해결하려는 고객의 핵심 문제는 무엇인가요?',
        placeholder='예: 원격 개발팀이 사수 없이도 같은 기준으로 판단하게 만든다',
    ),
    Spec(
        key='fq2', category=CULTURE, area_key=COMPANY,
        title='지금의 최우선 목표',
        question='현재 우리 회사가 당면한 가장 중요한 비즈니스 목표는?',
        choices=(
            Choice(
                '초기 지표 달성 (신속한 기능 배포 및 매출 확보)',
                '지금은 초기 지표 달성이 우선입니다. 기능을 빠르게 배포해 매출을 확보하는 쪽을 선택합니다.',
                'Early traction comes first right now. Ship features quickly and secure revenue.',
            ),
            Choice(
                '시스템 안정성 확보 (장기적 관점의 아키텍처 구축)',
                '지금은 시스템 안정성이 우선입니다. 오래 유지되는 구조를 만드는 쪽을 선택합니다.',
                'System stability comes first right now. Build an architecture that lasts.',
            ),
        ),
    ),
    Spec(
        key='fq3', category=CULTURE, area_key=PRODUCT_ENG,
        title='일정과 품질이 부딪힐 때',
        question='일정(마감)과 코드 퀄리티가 충돌할 때 무엇을 우선하나요?',
        choices=(
            Choice(
                '기술 부채를 감수하더라도 기한 내 배포 우선',
                '일정과 코드 품질이 부딪히면 기한을 지킵니다. 기술 부채는 감수하고 배포합니다.',
                'When a deadline and code quality conflict, meet the deadline. Ship and accept the technical debt.',
            ),
            Choice(
                '기한이 지연되더라도 코드 품질 및 원칙 준수 우선',
                '일정과 코드 품질이 부딪히면 품질을 지킵니다. 기한이 늦어지더라도 원칙을 따릅니다.',
                'When a deadline and code quality conflict, keep the quality. Follow the principles even if it slips.',
            ),
        ),
    ),
    Spec(
        key='fq4', category=CULTURE, area_key=PRODUCT_ENG,
        title='장애를 냈을 때 알리는 법',
        question='실수로 서버를 다운시키거나 DB를 날렸을 때 대처 방식은?',
        choices=(
            Choice(
                '즉시 공개 소통 채널에 서면으로 전체 상황 공유',
                '서버를 내리거나 데이터를 잃는 사고가 나면 즉시 공개 채널에 상황을 글로 공유합니다.',
                'If you take down a server or lose data, post the full situation in the open channel immediately.',
            ),
            Choice(
                '책임자(대표/테크리드)에게 개별 메시지로 선보고',
                '서버를 내리거나 데이터를 잃는 사고가 나면 책임자에게 개별 메시지로 먼저 알립니다.',
                'If you take down a server or lose data, message the lead directly first.',
            ),
        ),
    ),
    Spec(
        key='fq6', category=COMMUNICATION, area_key=COMPANY,
        title='데일리 진행 공유',
        question='팀의 데일리 진행 상황 공유 방식은?',
        choices=(
            Choice(
                '자동화 봇을 활용하여 매일 지정된 양식으로 서면 제출',
                '매일 진행 상황을 정해진 양식으로 봇에 글로 남깁니다.',
                'Post your daily progress in the set format through the bot every day.',
            ),
            Choice(
                '별도 보고 없이 이슈 관리 도구(Jira/GitHub) 업데이트로 대체',
                '별도 보고는 하지 않습니다. 이슈 관리 도구를 최신 상태로 유지하는 것으로 대신합니다.',
                'There is no separate stand-up report. Keeping the issue tracker up to date is the report.',
            ),
        ),
    ),
    Spec(
        key='fq7', category=COMMUNICATION, area_key=COMPANY,
        title='슬랙 응답 시간',
        question='시차를 고려했을 때, 슬랙 응답을 보장해야 하는 시간은?',
        choices=(
            Choice(
                '지정된 코어 타임(예: 14시~18시)에는 실시간 응답 대기 필수',
                '코어 타임에는 슬랙에 실시간으로 응답합니다.',
                'During core hours you are expected to answer on Slack in real time.',
            ),
            Choice(
                '100% 비동기 업무 (코어 타임 없이 24시간 이내 응답 보장)',
                '코어 타임은 없습니다. 슬랙 메시지에는 24시간 안에 답하면 됩니다.',
                'There are no core hours. Answer Slack messages within 24 hours.',
            ),
        ),
    ),
    Spec(
        key='fq8', category=COMMUNICATION, area_key=PRODUCT_ENG,
        title='P0 장애 연락 방법',
        question='치명적인 장애(P0) 발생 시 원격 근무자에게 연락하는 방식은?',
        choices=(
            Choice(
                '슬랙 등 업무 채널 전체 멘션 및 에러 로그 즉각 공유',
                'P0 장애가 나면 업무 채널에 전체 멘션하고 에러 로그를 바로 공유합니다.',
                'For a P0 incident, mention everyone in the work channel and share the error log right away.',
            ),
            Choice(
                '자동화된 긴급 알림 시스템(PagerDuty 등) 작동',
                'P0 장애는 긴급 알림 시스템으로 전달합니다.',
                'P0 incidents go out through the automated paging system.',
            ),
        ),
    ),
    Spec(
        key='fq9', category=COMMUNICATION, area_key=COMPANY,
        title='막혔을 때 질문하기 전에',
        question='문제(Blocker)를 설명하기 전, 기대하는 행동은?',
        choices=(
            Choice(
                '최소 1시간 원인 분석 및 자체 시도 내역 정리 후 질문',
                '막히면 최소 1시간은 스스로 원인을 찾아보고, 시도한 내용을 정리해서 질문합니다.',
                'When you are blocked, spend at least an hour investigating first, then ask with what you already tried.',
            ),
            Choice(
                '문제 발생 즉시 에러 로그를 첨부하여 바로 질문',
                '막히면 바로 물어봅니다. 에러 로그를 함께 올립니다.',
                'When you are blocked, ask right away and attach the error log.',
            ),
        ),
    ),
    Spec(
        key='fq10', category=COMMUNICATION, area_key=PEOPLE,
        title='연차와 병가',
        question='연차나 병가 등으로 자리를 비워야 할 때 룰은?',
        choices=(
            Choice(
                '최소 1주 전 전사 채널 공지 및 서면 인수인계서 작성 필수',
                '연차나 병가로 자리를 비울 때는 최소 1주 전에 전사 채널에 공지하고 인수인계를 글로 남깁니다.',
                'For leave, post in the company channel at least a week ahead and write a handover note.',
            ),
            Choice(
                '당일이라도 책임자의 사전 서면 승인 획득 시 사용 가능',
                '연차나 병가는 당일이라도 책임자의 서면 승인을 받으면 쓸 수 있습니다.',
                'You can take leave even on the day itself, as long as you get written approval from your lead first.',
            ),
        ),
    ),
    Spec(
        key='fq11', category=DEV_FLOW, area_key=PRODUCT_ENG,
        title='티켓을 받고 착수하기까지',
        question='텍스트로 된 지라/이슈 티켓을 받으면 바로 코딩을 시작하나요?',
        choices=(
            Choice(
                '소통 채널에 간략한 구현 방향성을 서면 공유 후 즉각 착수',
                '티켓을 받으면 구현 방향을 짧게 채널에 공유하고 바로 시작합니다.',
                'When you pick up a ticket, post a short plan in the channel and start right away.',
            ),
            Choice(
                '상세 구현 계획을 문서로 작성하고 책임자 리뷰 승인 후 착수',
                '티켓을 받으면 구현 계획을 문서로 쓰고 책임자 승인을 받은 뒤 시작합니다.',
                'When you pick up a ticket, write a detailed plan and wait for your lead to approve it before starting.',
            ),
        ),
    ),
    Spec(
        key='fq12', category=DEV_FLOW, area_key=PRODUCT_ENG,
        title='완료(Done)의 기준',
        question='우리 팀에서 "그 일 다 끝났습니다(Done)"의 기준은?',
        choices=(
            Choice(
                '로컬 환경 테스트 완료 및 PR(Pull Request) 생성 시점',
                '로컬에서 테스트를 마치고 PR을 올리면 완료로 봅니다.',
                'Work is done when it passes local testing and the pull request is open.',
            ),
            Choice(
                '코드 리뷰 통과 및 메인(Main) 브랜치 병합 완료 시점',
                '코드 리뷰를 통과하고 메인 브랜치에 병합되면 완료로 봅니다.',
                'Work is done when the review passes and it is merged into main.',
            ),
            Choice(
                '운영(Production) 서버 배포 및 정상 작동 확인 시점',
                '운영 서버에 배포되고 정상 작동을 확인하면 완료로 봅니다.',
                'Work is done when it is deployed to production and confirmed working.',
            ),
        ),
    ),
    Spec(
        key='fq13', category=DEV_FLOW, area_key=PRODUCT_ENG,
        title='일정 지연 보고',
        question='약속된 마감일보다 작업이 밀릴 것 같을 때 언제 알리나요?',
        choices=(
            Choice(
                '지연 예상 즉시 소통 채널에 사유와 함께 서면 보고',
                '일정이 밀릴 것 같으면 그것을 안 즉시 사유와 함께 채널에 알립니다.',
                'If you expect to miss a deadline, say so in the channel as soon as you know, with the reason.',
            ),
            Choice(
                '지정된 정기 업무 보고(데일리 스탠드업 등) 시간에 공유',
                '일정이 밀릴 것 같으면 정기 보고 시간에 공유합니다.',
                'If you expect to miss a deadline, raise it at the regular stand-up.',
            ),
        ),
    ),
    Spec(
        key='fq14', category=DEV_FLOW, area_key=SECURITY,
        title='AI 코딩 도구 사용',
        question='AI 코딩 어시스턴트(ChatGPT · Copilot 등) 사용 규정은?',
        choices=(
            Choice(
                '전사적으로 적극 활용 권장 (코드 입력 제한 없음)',
                'AI 코딩 도구를 적극적으로 씁니다. 코드 입력에 제한은 없습니다.',
                'Use AI coding assistants freely. There is no restriction on pasting code into them.',
            ),
            Choice(
                '보안 유출 방지를 위해 사내 소스 코드 입력 전면 금지',
                'AI 코딩 도구에 사내 소스 코드를 넣지 않습니다. 보안 유출을 막기 위함입니다.',
                'Never paste company source code into AI coding assistants. This is to prevent leaks.',
            ),
        ),
    ),
    Spec(
        key='fq15', category=DEV_FLOW, area_key=PRODUCT_ENG,
        title='하루 끝 Git 상태',
        question='그날 하루 일과를 마칠 때 개발자의 깃(Git) 상태는?',
        choices=(
            Choice(
                '작업이 미완성이어도 매일 임시(WIP)로 원격 저장소 푸시 필수',
                '하루를 마칠 때는 작업이 끝나지 않았어도 WIP로 원격 저장소에 푸시합니다.',
                'At the end of each day, push to the remote even if the work is unfinished, marked WIP.',
            ),
            Choice(
                '독립적인 기능 단위로 작업이 완결되었을 때만 원격 저장소 푸시',
                '기능 단위로 작업이 끝났을 때만 원격 저장소에 푸시합니다.',
                'Only push to the remote when a self-contained piece of work is finished.',
            ),
        ),
    ),
    Spec(
        key='fq16', category=CODE_ACCESS, area_key=PRODUCT_ENG,
        title='머지 승인 조건',
        question='작업한 코드를 메인 브랜치에 합치기(Merge) 위한 조건은?',
        choices=(
            Choice(
                '책임자(테크리드/시니어) 1인 이상의 승인(Approve) 필수',
                '메인 브랜치에 병합하려면 책임자 1인 이상의 승인이 필요합니다.',
                'Merging into main requires approval from at least one lead.',
            ),
            Choice(
                '동료 개발자 1인 이상의 코드 리뷰 통과 시 병합 가능',
                '메인 브랜치에 병합하려면 동료 개발자 1인 이상의 리뷰를 통과해야 합니다.',
                'Merging into main requires a passing review from at least one peer.',
            ),
            Choice(
                '리뷰어를 둘 여력이 없어 리뷰 없이 본인이 바로 병합',
                '리뷰 없이 본인이 바로 메인 브랜치에 병합합니다.',
                'You merge into main yourself without a review.',
            ),
        ),
    ),
    Spec(
        key='fq17', category=CODE_ACCESS, area_key=PRODUCT_ENG,
        title='코드 리뷰 말투',
        question='서면으로만 진행되는 코드 리뷰 시 선호하는 피드백 방식은?',
        choices=(
            Choice(
                '감정적 표현을 배제하고 논리와 효율성 중심의 직관적 피드백',
                '코드 리뷰는 논리와 효율 중심으로 직설적으로 씁니다. 완곡한 표현을 덧붙이지 않습니다.',
                'Code review comments are direct and focused on logic and efficiency, without softening.',
            ),
            Choice(
                '오해 방지를 위해 권고(Suggestion) 등 부드러운 표현 지향',
                '코드 리뷰는 권고 형태의 부드러운 표현을 씁니다. 오해를 줄이기 위함입니다.',
                'Code review comments are phrased gently, as suggestions, to avoid misunderstanding.',
            ),
        ),
    ),
    Spec(
        key='fq18', category=CODE_ACCESS, area_key=PRODUCT_ENG,
        title='프로덕션 배포 시간대',
        question='프로덕션(운영) 서버 배포 주기 및 시간대 룰은?',
        choices=(
            Choice(
                '주말 및 휴일 전(금요일 오후 등) 배포 원칙적 금지',
                '주말과 휴일 직전에는 운영 서버에 배포하지 않습니다. 금요일 오후도 포함됩니다.',
                'Do not deploy to production right before a weekend or holiday, including Friday afternoon.',
            ),
            Choice(
                'CI/CD 파이프라인을 통한 상시 배포 (시간대 제한 없음)',
                '운영 배포는 CI/CD로 상시 진행합니다. 시간대 제한은 없습니다.',
                'Production deploys run continuously through CI/CD. There is no time-of-day restriction.',
            ),
        ),
    ),
    Spec(
        key='fq19', category=CODE_ACCESS, area_key=PRODUCT_ENG,
        title='테스트 코드 작성',
        question='유닛(Unit) 및 E2E 테스트 코드 작성 기준은?',
        choices=(
            Choice(
                '지정된 커버리지 이상 테스트 코드 필수 작성 (미작성 시 PR 반려)',
                '정해진 커버리지 이상으로 테스트 코드를 씁니다. 없으면 PR이 반려됩니다.',
                'Write tests to meet the required coverage. Pull requests without them are rejected.',
            ),
            Choice(
                '초기 개발 속도 확보를 위해 테스트 코드 작성 전면 생략',
                '지금은 개발 속도를 위해 테스트 코드를 쓰지 않습니다.',
                'We skip tests for now to keep development speed up.',
            ),
        ),
    ),
    Spec(
        key='fq20', category=CODE_ACCESS, area_key=SECURITY,
        title='새 패키지 도입',
        question='새로운 오픈소스나 패키지(npm, pip 등)를 추가할 때 룰은?',
        choices=(
            Choice(
                '의존성 및 보안 검토를 위해 책임자의 사전 서면 승인 필수',
                '새 오픈소스나 패키지를 추가하려면 책임자의 사전 승인을 받습니다. 의존성과 보안을 검토하기 위함입니다.',
                'Adding a new open-source package requires written approval from a lead first, '
                'so dependencies and security can be checked.',
            ),
            Choice(
                '소통 채널에 도입 목적과 패키지 정보 공유 즉시 자율 도입',
                '새 오픈소스나 패키지는 도입 목적과 정보를 채널에 공유하고 바로 씁니다.',
                'You can add a new open-source package right away as long as you post what it is and why '
                'in the channel.',
            ),
        ),
    ),
)


# 회사 규칙과 다른 예외만 묻는 것이 목적이라, '예외 없음' 답은 규칙을 만들지 않는다.
PROJECT_QUESTIONS = (
    Spec(
        key='pq1', category=PROJECT,
        title='성공 기준',
        question='이 프로젝트의 성공 여부를 판단하는 핵심 지표(KPI)나 마일스톤은?',
        placeholder='예: 11월 1일까지 MVP 배포 및 활성 유저 1,000명 달성',
    ),
    Spec(
        key='pq2', category=PROJECT,
        title='타깃 사용자',
        question='이 프로젝트가 서비스될 주요 타겟 국가 및 사용자층은?',
        placeholder='예: 다국어 지원이 필요한 베트남 현지 20대 대학생',
    ),
    Spec(
        key='pq3', category=PROJECT,
        title='최종 결정권자',
        question='기획 변경 · 디자인 수정 · 최종 병합(Merge) 승인 권한을 가진 최종 결정권자는?',
        placeholder='담당자 이름을 적어 주세요',
    ),
    Spec(
        key='pq4', category=PROJECT,
        title='기술 스택 예외',
        question='전사 표준과 다르게 이 프로젝트에만 예외로 적용되는 스택이 있나요?',
        choices=(
            Choice('전사 표준 기술 스택과 100% 동일하게 진행', creates_rule=False),
        ),
        placeholder='예: 본 프로젝트에 한하여 프론트엔드는 React 대신 Vue.js 사용',
    ),
    Spec(
        key='pq5', category=PROJECT,
        title='배포 규정 예외',
        question='전사 배포 규정과 별개로, 이 프로젝트에만 적용되는 예외 룰이 있나요?',
        choices=(
            Choice('예외 없음 (전사 배포 규정 엄격 적용)', creates_rule=False),
            Choice(
                '프로토타입 단계이므로 사전 승인 절차 생략 후 상시 자율 배포 허용',
                '이 프로젝트는 프로토타입 단계라 사전 승인 없이 상시 배포합니다.',
                'This project is a prototype, so you can deploy at any time without prior approval.',
            ),
        ),
    ),
    Spec(
        key='pq6', category=PROJECT,
        title='테스트 규정 예외',
        question='이 프로젝트에만 적용되는 테스트 코드 작성 예외 룰이 있나요?',
        choices=(
            Choice('예외 없음 (전사 테스트 규정 엄격 적용)', creates_rule=False),
            Choice(
                '신속한 MVP 검증을 위해 본 프로젝트에 한하여 테스트 코드 작성 생략',
                '이 프로젝트에서는 MVP 검증을 위해 테스트 코드를 쓰지 않습니다.',
                'In this project we skip tests so we can validate the MVP quickly.',
            ),
        ),
    ),
    Spec(
        key='pq7', category=PROJECT,
        title='일정 산정 방식',
        question='개별 기능(티켓)의 일정 산정(스토리 포인트)은 누가 주도하나요?',
        choices=(
            Choice(
                '기획자 및 테크리드가 마감일을 지정하여 하향식(Top-down)으로 할당',
                '이 프로젝트의 일정은 기획자와 테크리드가 마감일을 정해 배분합니다.',
                'In this project, the planner and tech lead set the deadlines and assign the work.',
            ),
            Choice(
                '담당 개발자가 직접 소요 시간을 산정하여 상향식(Bottom-up)으로 제안',
                '이 프로젝트의 일정은 담당 개발자가 직접 산정해서 제안합니다.',
                'In this project, the developer doing the work estimates it and proposes the schedule.',
            ),
        ),
    ),
    Spec(
        key='pq8', category=PROJECT,
        title='기획 변경 소통',
        question='개발 도중 기획·디자인 수정이 필요할 때의 소통 방식은?',
        choices=(
            Choice(
                '해당 이슈 티켓에 서면 코멘트를 남기고 기획자/디자이너의 최종 승인 대기',
                '개발 중 기획이나 디자인을 바꿔야 하면 이슈 티켓에 코멘트를 남기고 승인을 기다립니다.',
                'If a spec or design change is needed mid-development, comment on the ticket and wait '
                'for approval.',
            ),
            Choice(
                '개발자가 자체적으로 판단 및 수정하여 구현 후 사후 서면 공유',
                '개발 중 기획이나 디자인은 개발자가 판단해서 고치고, 끝난 뒤에 글로 공유합니다.',
                'The developer decides and makes spec or design changes, then shares what changed afterwards.',
            ),
        ),
    ),
)


BY_KEY = {spec.key: spec for spec in COMPANY_QUESTIONS + PROJECT_QUESTIONS}


def find(key):
    return BY_KEY.get(key)
