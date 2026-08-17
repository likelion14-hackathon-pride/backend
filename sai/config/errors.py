from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail, ValidationError

# 에러 코드 카탈로그.
#
# 응답 봉투(config/exceptions.py)의 code 칸에 들어가는 값이다.
# 프론트는 이 값 하나로 분기하므로 한 번 내보낸 코드는 계약으로 본다. 함부로 바꾸지 않는다.
# 반대로 message 문장은 언제든 고쳐도 된다. 분기에 쓰이지 않기 때문이다.
#
# 여기 없는 코드는 DRF 가 스스로 붙인 것이다(required, invalid, not_found, permission_denied ...).

# 공통
SERVER_ERROR = 'server_error'
RATE_LIMITED = 'rate_limited'
AI_UNAVAILABLE = 'ai_unavailable'
STORAGE_UNAVAILABLE = 'storage_unavailable'
INVALID_PARAMETER = 'invalid_parameter'
INVALID_LIMIT = 'invalid_limit'
INVALID_CURSOR = 'invalid_cursor'
UNKNOWN_TIMEZONE = 'unknown_timezone'

# 계정
INVALID_CREDENTIALS = 'invalid_credentials'
COMPANY_CODE_NOT_FOUND = 'company_code_not_found'
EMAIL_TAKEN = 'email_taken'
WEAK_PASSWORD = 'weak_password'
PROFILE_FIELD_REQUIRED = 'profile_field_required'

# 회사
WORKING_HOURS_IDENTICAL = 'working_hours_identical'
KEYWORD_TAKEN = 'keyword_taken'

# 지식공간
SCOPE_NOT_FOUND = 'scope_not_found'
SCOPE_KIND_NOT_ALLOWED = 'scope_kind_not_allowed'
SCOPE_NAME_BLANK = 'scope_name_blank'
SCOPE_NAME_TAKEN = 'scope_name_taken'
PROJECT_SCOPE_REQUIRED = 'project_scope_required'
SCOPE_UNAVAILABLE = 'scope_unavailable'

# 핸드북
ENTRY_NOT_CONFIRMED = 'entry_not_confirmed'
CANNOT_APPROVE_BLANK = 'cannot_approve_blank'

# 지시 카드
INVALID_STATUS_MOVE = 'invalid_status_move'
CARD_FIELD_REQUIRED = 'card_field_required'
TASK_FIELD_REQUIRED = 'task_field_required'
NOT_A_MEMBER = 'not_a_member'

# 대표 확인 질문
ESCALATION_SOURCE_REQUIRED = 'escalation_source_required'
CONFLICTING_SOURCE = 'conflicting_source'
QUESTION_TEXT_MISSING = 'question_text_missing'
DRAFT_REQUIRED = 'draft_required'
ALREADY_ESCALATED = 'already_escalated'
ALREADY_SENT = 'already_sent'
NOT_SENT_YET = 'not_sent_yet'
ALREADY_APPROVED = 'already_approved'
NO_ANSWER_YET = 'no_answer_yet'
NO_ANSWER_TO_PROMOTE = 'no_answer_to_promote'
NO_SCOPE_AVAILABLE = 'no_scope_available'

# 온보딩
UNKNOWN_QUESTION = 'unknown_question'
ANSWER_REQUIRED = 'answer_required'

# 소스 연결
GITHUB_INSTALLATION_TAKEN = 'github_installation_taken'
SLACK_WORKSPACE_TAKEN = 'slack_workspace_taken'
NO_INGESTION_TARGET = 'no_ingestion_target'
INVALID_BOT_TOKEN = 'invalid_bot_token'
INVALID_APP_ID = 'invalid_app_id'
INVALID_INSTALLATION_ID = 'invalid_installation_id'
INVALID_PRIVATE_KEY = 'invalid_private_key'
PRIVATE_KEY_TOO_LARGE = 'private_key_too_large'
INVALID_FILE_NAME = 'invalid_file_name'
UNSUPPORTED_FILE_TYPE = 'unsupported_file_type'
MIME_TYPE_MISMATCH = 'mime_type_mismatch'

# 슬랙과 깃허브가 돌려주는 코드(invalid_auth, github_auth_failed ...)는 여기 적지 않는다.
# 외부가 정하는 값이라 목록을 따라갈 수 없다. 그대로 code 로 내보내고 field 는 비운다.


# 코드가 붙을 칸.
#
# 폼의 입력 칸 이름만 적는다. 화면에 그 칸이 없으면 None 을 적어 두어야 프론트가
# 칸 밑이 아니라 토스트로 띄운다. 'slack' 이나 'github' 처럼 칸이 아닌 이름은 넣지 않는다.
#
# 여기 없는 코드는 detail 에 담겨 온 칸 이름을 그대로 쓴다. 칸이 요청마다 달라지는
# 쿼리 파라미터 에러(INVALID_PARAMETER)가 그래서 빠져 있다.
ERROR_FIELDS = {
    INVALID_CREDENTIALS: None,
    COMPANY_CODE_NOT_FOUND: 'companyCode',
    EMAIL_TAKEN: 'email',
    WEAK_PASSWORD: 'password',
    PROFILE_FIELD_REQUIRED: None,

    WORKING_HOURS_IDENTICAL: 'workingHoursEnd',
    KEYWORD_TAKEN: 'keyword',

    SCOPE_NOT_FOUND: 'scopeId',
    SCOPE_KIND_NOT_ALLOWED: 'kind',
    SCOPE_NAME_BLANK: 'name',
    SCOPE_NAME_TAKEN: 'name',
    PROJECT_SCOPE_REQUIRED: 'scopeId',
    SCOPE_UNAVAILABLE: None,

    ENTRY_NOT_CONFIRMED: None,
    CANNOT_APPROVE_BLANK: 'decision',

    INVALID_STATUS_MOVE: 'status',
    CARD_FIELD_REQUIRED: None,
    TASK_FIELD_REQUIRED: None,
    NOT_A_MEMBER: 'assigneeId',

    ESCALATION_SOURCE_REQUIRED: None,
    CONFLICTING_SOURCE: None,
    QUESTION_TEXT_MISSING: None,
    DRAFT_REQUIRED: 'draftKo',
    ALREADY_SENT: None,
    NOT_SENT_YET: None,
    ALREADY_APPROVED: None,
    NO_ANSWER_YET: None,
    NO_ANSWER_TO_PROMOTE: None,
    NO_SCOPE_AVAILABLE: None,

    UNKNOWN_QUESTION: None,
    ANSWER_REQUIRED: None,

    GITHUB_INSTALLATION_TAKEN: None,
    SLACK_WORKSPACE_TAKEN: None,
    NO_INGESTION_TARGET: 'itemIds',
    INVALID_BOT_TOKEN: 'botToken',
    INVALID_APP_ID: 'appId',
    INVALID_INSTALLATION_ID: 'installationId',
    INVALID_PRIVATE_KEY: 'privateKey',
    PRIVATE_KEY_TOO_LARGE: 'privateKey',
    INVALID_FILE_NAME: 'fileName',
    UNSUPPORTED_FILE_TYPE: 'fileName',
    MIME_TYPE_MISMATCH: 'mimeType',

    UNKNOWN_TIMEZONE: 'timezone',
    INVALID_LIMIT: 'limit',
    INVALID_CURSOR: 'cursor',

    SERVER_ERROR: None,
    RATE_LIMITED: None,
    AI_UNAVAILABLE: None,
    STORAGE_UNAVAILABLE: None,
}

# DRF 기본 코드 중 이름을 바꿔 내보내는 것.
CODE_ALIASES = {'throttled': RATE_LIMITED}


# 업무 규칙에 걸렸다는 뜻. 코드가 곧 사유이고, 붙을 칸은 ERROR_FIELDS 가 정한다.
#
# 슬랙·깃허브처럼 밖에서 온 예외도 이 클래스를 상속한다. 그래야 뷰마다
# try/except 로 옮겨 담지 않아도 봉투까지 그대로 올라간다.
class DomainError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, code, message=None, field=None):
        self.code = code
        # 같은 코드가 요청마다 다른 칸에 붙을 때만 준다. 고정이면 ERROR_FIELDS 에 적는다.
        self.field = field
        # 문장을 따로 주지 않으면 코드를 그대로 보여 준다.
        # 밖에서 온 코드(invalid_auth)는 우리가 쓸 문장을 미리 갖고 있을 수 없다.
        super().__init__(message or code)


# 우리 잘못도 사용자 잘못도 아니고, 밖(OpenAI, S3, 슬랙)이 지금 응답하지 않는 경우.
# 같은 요청을 잠시 뒤에 다시 보내면 될 수 있다는 뜻이라 503 으로 낸다.
class UpstreamError(DomainError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


# 아이디나 비밀번호가 틀렸다는 뜻.
# 폼이 덜 찬 것(400)과 상태 코드로 갈라야 화면이 '칸을 채우세요'와
# '다시 입력하세요'를 가려 말할 수 있다.
#
# 없는 이메일과 틀린 비밀번호가 같은 응답이어야 한다. 다르면 이 주소가
# 가입돼 있는지를 로그인 화면에서 확인할 수 있게 된다.
class InvalidCredentials(DomainError):
    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(self):
        super().__init__(INVALID_CREDENTIALS, 'email or password is incorrect')


class RateLimited(DomainError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__(RATE_LIMITED, 'AI usage limit reached, try again shortly')


# 칸 이름이 요청마다 달라지는 에러용. ERROR_FIELDS 로는 칸을 정할 수 없어 여기서 직접 담는다.
def field_error(field, message, code):
    return ValidationError({field: [ErrorDetail(str(message), code=code)]})
