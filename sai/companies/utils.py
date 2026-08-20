import secrets

CODE_LENGTH = 12
# 헷갈리는 문제 제외함 
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def generate_company_code(max_attempts=10):
    from .models import Company

    for _ in range(max_attempts):
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not Company.objects.filter(code=code).exists():
            return code

    raise RuntimeError('failed to generate a unique company code')
