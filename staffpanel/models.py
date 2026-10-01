import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone


class StaffProfile(models.Model):
    ROLE_STAFF = "staff"
    ROLE_ADMIN = "admin"
    ROLE_SUPER_ADMIN = "super_admin"
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_BLOCKED = "blocked"
    STATUS_REMOVED = "removed"

    ROLE_CHOICES = [
        (ROLE_STAFF, "Staff"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_SUPER_ADMIN, "Super Admin"),
    ]
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending invite"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_BLOCKED, "Blocked"),
        (STATUS_REMOVED, "Removed"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profile")
    role = models.CharField(max_length=24, choices=ROLE_CHOICES, default=ROLE_STAFF)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    invite_code_hash = models.CharField(max_length=256, blank=True)
    invite_code_display = models.CharField(max_length=32, blank=True)
    invite_code_created_at = models.DateTimeField(null=True, blank=True)
    last_code_login_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_staff_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__first_name", "user__email"]

    def __str__(self):
        return f"{self.user.email or self.user.username} - {self.get_role_display()}"

    @staticmethod
    def generate_invite_code():
        part_one = secrets.token_hex(2).upper()
        part_two = secrets.token_hex(2).upper()
        part_three = secrets.token_hex(2).upper()
        return f"ELG-{part_one}-{part_two}-{part_three}"

    def set_invite_code(self, raw_code):
        self.invite_code_hash = make_password(raw_code)
        self.invite_code_display = (raw_code or "").strip().upper()
        self.invite_code_created_at = timezone.now()

    def check_invite_code(self, raw_code):
        if not self.invite_code_hash:
            return False
        return check_password((raw_code or "").strip().upper(), self.invite_code_hash)

    @property
    def is_admin_level(self):
        return self.role in {self.ROLE_ADMIN, self.ROLE_SUPER_ADMIN}

    @property
    def is_super_admin_level(self):
        return self.role == self.ROLE_SUPER_ADMIN

    @property
    def can_access_panel(self):
        return self.status == self.STATUS_ACTIVE
