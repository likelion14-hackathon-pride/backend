from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models

from .profile import JobRole, WorkLocation

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError('email is required')
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra)

class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    display_name = models.CharField(max_length=60)
    ui_language = models.CharField(max_length=2, default='ko')
    # 고른 근무 위치가 타임존을 정한다. 시각 계산은 timezone 만 읽는다.
    # 비어 있으면 아직 초기 설정을 하지 않은 것이다. 별도 완료 플래그를 두지 않는다.
    work_location = models.CharField(
        max_length=10, choices=WorkLocation.choices, null=True, blank=True
    )
    job_role = models.CharField(
        max_length=10, choices=JobRole.choices, null=True, blank=True
    )
    timezone = models.CharField(max_length=40, default='Asia/Seoul')

    objects = UserManager()
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

#  소속 + 역할
class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = 'OWNER'
        MEMBER = 'MEMBER'

    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='memberships')
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user'], name='uniq_membership_user'),
            models.UniqueConstraint(
                fields=['company'],
                condition=models.Q(role='OWNER', left_at__isnull=True),
                name='uniq_company_active_owner',
            ),
        ]