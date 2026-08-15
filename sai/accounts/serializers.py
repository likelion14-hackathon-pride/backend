from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password as run_password_validators
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from companies.models import Company
from companies.utils import generate_company_code
from handbook.services import seed_default_scopes

from .models import Membership

User = get_user_model()


# 회원가입 공통 필드
class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    displayName = serializers.CharField(max_length=60)

    def validate_email(self, value):
        # 대소문자만 다른 이메일로 중복 가입되지 않도록 정규화 후 비교한다.
        value = value.lower().strip()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError('email already registered', code='email_taken')
        return value

    def validate_password(self, value):
        # settings의 AUTH_PASSWORD_VALIDATORS는 직접 호출해야 동작한다.
        try:
            run_password_validators(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages, code='weak_password')
        return value

    def create_user(self, validated_data, ui_language):
        return User.objects.create_user(
            email=validated_data['email'],
            password=validated_data['password'],
            display_name=validated_data['displayName'].strip(),
            ui_language=ui_language,
        )


class OwnerSignupSerializer(SignupSerializer):
    companyName = serializers.CharField(max_length=100)

    @transaction.atomic
    def create(self, validated_data):
        # 회사 생성 + 유저 생성 + 멤버십 생성 -> 트랜잭션 하나에서
        # 중간 실패 시 주인 없는 회사 남게되는거 방지
        company = Company.objects.create(
            name=validated_data['companyName'].strip(), code=generate_company_code()
        )
        # 핸드북 항목은 범위 없이 만들 수 없으므로 회사 전반 규칙 범위를 함께 만든다.
        seed_default_scopes(company)
        user = self.create_user(validated_data, 'ko')
        return Membership.objects.create(
            user=user, company=company, role=Membership.Role.OWNER
        )


class MemberSignupSerializer(SignupSerializer):
    companyCode = serializers.CharField(max_length=16)

    def validate_companyCode(self, value):
        try:
            return Company.objects.get(code=value.strip())
        except Company.DoesNotExist:
            raise serializers.ValidationError(
                'no company matches this code', code='company_code_not_found'
            )

    @transaction.atomic
    def create(self, validated_data):
        user = self.create_user(validated_data, 'en')
        return Membership.objects.create(
            user=user, company=validated_data['companyCode'], role=Membership.Role.MEMBER
        )


class AuthSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        # 비밀번호 검증
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['email'].lower().strip(),
            password=attrs['password'],
        )
        if user is None:
            raise serializers.ValidationError(
                'email or password is incorrect', code='invalid_credentials'
            )

        membership = (
            Membership.objects.select_related('company')
            .filter(user=user, left_at__isnull=True)
            .first()
        )
        # 소속이 없거나 퇴사한 경우.
        if membership is None:
            raise serializers.ValidationError(
                'email or password is incorrect', code='invalid_credentials'
            )

        attrs['membership'] = membership
        return attrs


class UserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='display_name', read_only=True)
    locale = serializers.CharField(source='ui_language', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'email', 'name', 'locale', 'timezone']


class CompanySerializer(serializers.ModelSerializer):
    onboardingStatus = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = ['id', 'name', 'code', 'timezone', 'onboardingStatus']

    def get_onboardingStatus(self, obj):
        if obj.onboarding_step == 0:
            return 'NOT_STARTED'
        if obj.onboarding_step >= 4:
            return 'COMPLETED'
        return 'IN_PROGRESS'


class MembershipSerializer(serializers.ModelSerializer):
    userId = serializers.IntegerField(source='user_id', read_only=True)
    companyId = serializers.IntegerField(source='company_id', read_only=True)
    status = serializers.SerializerMethodField()
    user = UserSerializer(read_only=True)
    slackHandle = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ['id', 'userId', 'companyId', 'role', 'status', 'user', 'slackHandle']

    def get_status(self, obj):
        if obj.left_at:
            return 'LEFT'
        return 'ACTIVE'

    # 연결된 슬랙 계정 표시명. null이면 아직 매칭되지 않은 것이다.
    # 슬랙 이메일과 가입 이메일이 같아야 수집 작업이 이어 준다.
    def get_slackHandle(self, obj):
        identity = next(
            (i for i in obj.user.source_identities.all() if i.company_id == obj.company_id),
            None,
        )

        return identity.external_handle if identity else None


class MeSerializer(serializers.Serializer):
    user = UserSerializer()
    membership = MembershipSerializer()
    company = CompanySerializer()


class MembershipListSerializer(serializers.Serializer):
    items = MembershipSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)
