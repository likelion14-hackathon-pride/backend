import re
import secrets
import string

PREFIX_MAX = 6
FALLBACK_LEN = 4
_UPPER = re.compile(r'[A-Z]')

# 회사 코드 생성 규칙
# 1. 회사 이름에서 영문 대문자만 뽑아 접두어로 사용. 접두어는 최대 6자.
def normalize_code(raw):

    return raw.upper().replace('-', '').replace(' ', '')


def _random_prefix():
    return ''.join(secrets.choice(string.ascii_uppercase) for _ in range(FALLBACK_LEN))


def _prefix_from_name(name):
    # 영문 없으면 랜덤 4자
    letters = ''.join(_UPPER.findall(name.upper()))[:PREFIX_MAX]
    return letters or _random_prefix()


def generate_company_code(name, max_attempts=10):
    from .models import Company

    prefix = _prefix_from_name(name)
    for attempt in range(max_attempts):
        # 마지막 시도는 접두어까지 랜덤으로 바꿔 충돌 가능성을 낮춘다.
        if attempt == max_attempts - 1:
            prefix = _random_prefix()

        display = f'{prefix}-{secrets.randbelow(10000):04d}'
        normalized = normalize_code(display)
        if not Company.objects.filter(code_normalized=normalized).exists():
            return display, normalized

    raise RuntimeError('failed to generate a unique company code')
