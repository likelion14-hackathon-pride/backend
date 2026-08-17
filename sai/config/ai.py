import logging
import time
from contextlib import contextmanager

from django.conf import settings

logger = logging.getLogger(__name__)

# 이보다 오래 걸린 호출은 경고로 남긴다. 타임아웃 한 번에 재시도까지 붙은 경우다.
SLOW_CALL_SECONDS = 30


# OpenAI 클라이언트 공통 인자.
#
# SDK 기본값(timeout=600초, max_retries=2)을 그대로 쓰면 호출 한 건이 최대 30분 매달린다.
# 워커는 단일 스레드라 그동안 큐에 쌓인 다른 작업이 하나도 돌지 못한다.
#
# 클라이언트를 여기서 만들지 않고 인자만 넘기는 이유는, 테스트가 모듈마다
# OpenAI 심볼을 patch 하고 있어서다. 생성 위치를 옮기면 그 patch 가 전부 빗나간다.
def client_options(**overrides):
    return {
        'timeout': settings.OPENAI_TIMEOUT,
        'max_retries': settings.OPENAI_MAX_RETRIES,
        **overrides,
    }


# 추론 모델은 샘플링을 스스로 고정한다. temperature 를 함께 보내면
# 'only the default (1) value is supported' 로 400 이 떨어져 호출 자체가 실패한다.
# 모델 이름으로 가르는 이유는, 어느 모델을 쓸지가 secrets.json 에서 정해져
# 코드가 미리 알 수 없기 때문이다.
FIXED_SAMPLING_MODELS = ('gpt-5', 'o1', 'o3', 'o4')


# chat.completions 호출에 붙일 샘플링 인자.
# 모델이 받지 않으면 아무것도 붙이지 않는다.
def sampling_options(model, temperature=0):
    if (model or '').startswith(FIXED_SAMPLING_MODELS):
        return {}

    return {'temperature': temperature}


def reasoning_options(model, effort=None, verbosity=None):
    if not (model or '').startswith(FIXED_SAMPLING_MODELS):
        return {}

    options = {}
    if effort:
        options['reasoning_effort'] = effort
    if verbosity and (model or '').startswith('gpt-5.6'):
        options['verbosity'] = verbosity

    return options


def generation_options(model, temperature=0, reasoning_effort=None, verbosity=None):
    return {
        **sampling_options(model, temperature),
        **reasoning_options(model, reasoning_effort, verbosity),
    }


# 호출 한 건이 얼마나 걸렸는지 남긴다.
# 실패해도 남겨야 한다. 타임아웃으로 죽은 호출이야말로 알고 싶은 것이기 때문이다.
@contextmanager
def timed_call(model, inputs=1):
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        record = logger.warning if elapsed >= SLOW_CALL_SECONDS else logger.info
        record('OpenAI 호출 model=%s inputs=%d 소요=%.1fs', model, inputs, elapsed)
