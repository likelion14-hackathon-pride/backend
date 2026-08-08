from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password as run_password_validators
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from companies.models import Company
from companies.utils import generate_company_code, normalize_code

from .models import Membership

User = get_user_model()


# 회원가입용 시리얼라이저
# role(owner/member)
class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    role = serializers.ChoiceField(choices=Membership.Role.choices)

    # owner 전용
    companyName = serializers.CharField(required=False, allow_blank=True)

    # member 전용
    companyCode = serializers.CharField(required=False, allow_blank=True)
    readingLanguage = serializers.CharField(required=False, allow_blank=True)

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

    def validate(self, attrs):
        
        if attrs['role'] == Membership.Role.OWNER:
            if not attrs.get('companyName', '').strip():
                raise serializers.ValidationError({'companyName': 'this field is required'})
            attrs.pop('companyCode', None)
            attrs.pop('readingLanguage', None)
        else:
            code = attrs.get('companyCode', '').strip()
            if not code:
                raise serializers.ValidationError({'companyCode': 'this field is required'})
            if not attrs.get('readingLanguage', '').strip():
                raise serializers.ValidationError({'readingLanguage': 'this field is required'})

            try:
                attrs['company'] = Company.objects.get(code_normalized=normalize_code(code))
            except Company.DoesNotExist:
                raise serializers.ValidationError(
                    'no company matches this code', code='company_code_not_found'
                )
            attrs.pop('companyName', None)

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        # 회사 생성 + 유저 생성 + 멤버십 생성 -> 트랜잭션 하나에서
        # 중간 실패 시 주인 없는 회사 남게되는거 방지 
        if validated_data['role'] == Membership.Role.OWNER:
            name = validated_data['companyName'].strip()
            display_code, normalized_code = generate_company_code(name)
            company = Company.objects.create(
                name=name, code=display_code, code_normalized=normalized_code
            )
            reading_language = None
        else:
            company = validated_data['company']
            reading_language = validated_data['readingLanguage'].strip()

        user = User.objects.create_user(
            email=validated_data['email'], password=validated_data['password']
        )
        return Membership.objects.create(
            user=user,
            company=company,
            role=validated_data['role'],
            reading_language=reading_language,
        )

# 로그인용 시리얼라이저
class AuthSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)
    companyCode = serializers.CharField(required=True)

    def validate(self, attrs):
        try:
            company = Company.objects.get(
                code_normalized=normalize_code(attrs['companyCode'].strip())
            )
        except Company.DoesNotExist:
            raise serializers.ValidationError(
                'no company matches this code', code='company_code_not_found'
            )

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
            .filter(user=user, company=company)
            .first()
        )
        # 소속이 아닌 회사로 로그인한 경우.
        if membership is None:
            raise serializers.ValidationError(
                'email or password is incorrect', code='invalid_credentials'
            )

        attrs['membership'] = membership
        return attrs