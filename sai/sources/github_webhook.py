import hashlib
import hmac


# GitHub가 보낸 원문 body를 같은 시크릿으로 서명해 요청 헤더와 비교한다.
def verify_github_signature(secret, signature, body):
    if not secret or not signature:
        return False
    if not signature.startswith('sha256='):
        return False

    expected = 'sha256=' + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
