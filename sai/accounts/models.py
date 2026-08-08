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

    objects = UserManager()
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

#  소속 + 역할
class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = 'owner'
        MEMBER = 'member'

    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='memberships')
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=10, choices=Role.choices)
    reading_language = models.CharField(max_length=5, null=True, blank=True)   # member만
    last_route = models.CharField(max_length=255, null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'company'], name='uniq_user_company'),
            models.UniqueConstraint(
                fields=['company'],
                condition=models.Q(role='owner'),
                name='uniq_owner_per_company',
            ),
        ]