from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from accounts.models import Membership

from .models import Company


def _company_for(user, company_id, role=None):
    company = get_object_or_404(Company, id=company_id)
    memberships = Membership.objects.filter(
        user=user, company=company, left_at__isnull=True
    )
    if role is not None:
        memberships = memberships.filter(role=role)

    return company, memberships.exists()


# 회사에 소속된 사람만 통과한다.
def get_member_company(user, company_id):
    company, allowed = _company_for(user, company_id)
    if not allowed:
        raise PermissionDenied('company permission required')

    return company


# 대표만 통과한다. 규칙 확정, 슬랙 연결처럼 되돌리기 어려운 일에 쓴다.
def get_owner_company(user, company_id):
    company, allowed = _company_for(user, company_id, Membership.Role.OWNER)
    if not allowed:
        raise PermissionDenied('owner permission required')

    return company
