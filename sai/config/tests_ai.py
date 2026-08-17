from django.test import SimpleTestCase

from .ai import generation_options, reasoning_options, sampling_options


class SamplingOptionsTests(SimpleTestCase):
    def test_older_models_take_temperature(self):
        self.assertEqual(sampling_options('gpt-4o'), {'temperature': 0})
        self.assertEqual(sampling_options('gpt-4o-mini'), {'temperature': 0})

    # 추론 모델에 temperature 를 보내면 400 이 떨어져 호출 자체가 실패한다.
    def test_reasoning_models_take_nothing(self):
        for model in ('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5', 'o3-mini'):
            self.assertEqual(sampling_options(model), {}, model)

    def test_a_missing_model_does_not_raise(self):
        self.assertEqual(sampling_options(''), {'temperature': 0})
        self.assertEqual(sampling_options(None), {'temperature': 0})

    def test_gpt56_reasoning_options_include_verbosity(self):
        self.assertEqual(
            reasoning_options('gpt-5.6-sol', 'high', 'medium'),
            {'reasoning_effort': 'high', 'verbosity': 'medium'},
        )

    def test_older_models_do_not_take_reasoning_options(self):
        self.assertEqual(reasoning_options('gpt-4o', 'high', 'medium'), {})

    def test_generation_options_combines_supported_options(self):
        self.assertEqual(generation_options('gpt-4o'), {'temperature': 0})
        self.assertEqual(
            generation_options('gpt-5.6-sol', reasoning_effort='high', verbosity='medium'),
            {'reasoning_effort': 'high', 'verbosity': 'medium'},
        )
