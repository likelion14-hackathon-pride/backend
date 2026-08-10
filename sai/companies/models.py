from datetime import time

from django.db import models

class Company(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=16, unique=True)
    timezone = models.CharField(max_length=40, default='Asia/Seoul')
    working_hours_enabled = models.BooleanField(default=True)
    working_hours_start = models.TimeField(default=time(9, 0))
    working_hours_end = models.TimeField(default=time(18, 0))
    onboarding_step = models.SmallIntegerField(default=0)   # 0=시작 전 ~ 4=위험 작업
    created_at = models.DateTimeField(auto_now_add=True)
