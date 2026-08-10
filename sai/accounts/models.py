from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models

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