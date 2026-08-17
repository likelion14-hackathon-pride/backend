from django.test import SimpleTestCase

from .ai import sampling_options


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
