"""Deterministic business rules. These functions do not call an LLM or external service."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import scrypt
from hmac import compare_digest
from secrets import token_bytes, token_urlsafe
from typing import Iterable


class PolicyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Role(StrEnum):
    INSPECTOR = "INSPECTOR"
    ENGINEER = "ENGINEER"
    MANAGER = "MANAGER"
    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    RETURN_FOR_MORE_EVIDENCE = "RETURN_FOR_MORE_EVIDENCE"


class ApprovalStage(StrEnum):
    ENGINEER = "ENGINEER"
    MANAGER = "MANAGER"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"


@dataclass(frozen=True)
class ApprovalRecord:
    actor_id: str
    actor_role: Role
    stage: ApprovalStage
    decision: ApprovalDecision
    reason: str
    report_version: int


@dataclass
class ApprovalRequest:
    id: str
    report_id: str
    report_version: int
    risk_level: RiskLevel
    stage: ApprovalStage = ApprovalStage.ENGINEER
    status: ApprovalStatus = ApprovalStatus.PENDING
    version: int = 0
    decisions: list[ApprovalRecord] = field(default_factory=list)

    @property
    def can_create_order(self) -> bool:
        return self.status == ApprovalStatus.APPROVED

    def decide(
        self,
        *,
        actor_id: str,
        actor_role: Role,
        decision: ApprovalDecision,
        expected_version: int,
        expected_report_version: int,
        reason: str = "",
    ) -> ApprovalRecord:
        if expected_report_version != self.report_version:
            raise PolicyError("REPORT_VERSION_CONFLICT", "审批的报告版本已变化")
        if not actor_id:
            raise PolicyError("INVALID_ACTOR", "审批人不能为空")
        if self.stage == ApprovalStage.MANAGER and actor_role == Role.MANAGER:
            engineer_ids = {r.actor_id for r in self.decisions if r.stage == ApprovalStage.ENGINEER}
            if actor_id in engineer_ids:
                raise PolicyError("SAME_APPROVER", "工程师与主管共同审批必须由不同账户完成")
        for record in self.decisions:
            if record.actor_id == actor_id and record.actor_role == actor_role:
                if record.decision == decision:
                    return record  # identical retry returns the first durable decision
                raise PolicyError("DECISION_CONFLICT", "该审批人已作出不同决定")
        if self.status != ApprovalStatus.PENDING:
            raise PolicyError("APPROVAL_FINALIZED", "该报告版本已有最终决定")
        if expected_version != self.version:
            raise PolicyError("APPROVAL_VERSION_CONFLICT", "审批状态已变化，请重新读取")
        required_role = Role.ENGINEER if self.stage == ApprovalStage.ENGINEER else Role.MANAGER
        if actor_role != required_role:
            raise PolicyError("FORBIDDEN", f"当前阶段需要 {required_role.value} 角色")
        reason = reason.strip()
        if decision != ApprovalDecision.APPROVE and not reason:
            raise PolicyError("REASON_REQUIRED", "驳回和退回补证必须填写理由")
        if self.risk_level == RiskLevel.HIGH and not reason:
            raise PolicyError("REASON_REQUIRED", "高风险审批每一步都必须填写判断依据")
        record = ApprovalRecord(actor_id, actor_role, self.stage, decision, reason, self.report_version)
        self.decisions.append(record)
        self.version += 1
        if decision == ApprovalDecision.REJECT:
            self.status = ApprovalStatus.REJECTED
        elif decision == ApprovalDecision.RETURN_FOR_MORE_EVIDENCE:
            self.status = ApprovalStatus.RETURNED
        elif self.risk_level == RiskLevel.HIGH and self.stage == ApprovalStage.ENGINEER:
            self.stage = ApprovalStage.MANAGER
        else:
            self.status = ApprovalStatus.APPROVED
        return record


class WorkOrderStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_REINSPECTION = "PENDING_REINSPECTION"
    CLOSED = "CLOSED"


class ReinspectionResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


class ReinspectionDecision(StrEnum):
    CONFIRM_PASS = "CONFIRM_PASS"
    CONFIRM_FAIL = "CONFIRM_FAIL"
    REQUEST_RECAPTURE = "REQUEST_RECAPTURE"


@dataclass
class Reinspection:
    id: str
    inspector_id: str
    image_key: str
    result: ReinspectionResult
    note: str
    decision: ReinspectionDecision | None = None
    reviewer_id: str | None = None
    review_reason: str = ""


@dataclass(frozen=True)
class HandlingRecord:
    actor_id: str
    action: str


@dataclass
class WorkOrder:
    id: str
    status: WorkOrderStatus
    version: int = 0
    reinspections: list[Reinspection] = field(default_factory=list)
    handling_history: list[HandlingRecord] = field(default_factory=list)

    def complete_handling(
        self, *, actor_id: str, actor_role: Role, action: str, expected_version: int
    ) -> HandlingRecord:
        if actor_role != Role.ENGINEER:
            raise PolicyError("FORBIDDEN", "只有质量工程师可提交处置")
        if self.status != WorkOrderStatus.IN_PROGRESS:
            raise PolicyError("INVALID_WORK_ORDER_STATE", "工单当前不在处理中")
        if expected_version != self.version:
            raise PolicyError("WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        if not actor_id or not action.strip():
            raise PolicyError("VALIDATION_ERROR", "处置人和具体措施不能为空")
        record = HandlingRecord(actor_id, action.strip())
        self.handling_history.append(record)
        self.status = WorkOrderStatus.PENDING_REINSPECTION
        self.version += 1
        return record

    def submit_reinspection(
        self,
        *,
        inspector_id: str,
        image_key: str,
        result: ReinspectionResult,
        note: str,
        expected_version: int,
    ) -> Reinspection:
        if self.status != WorkOrderStatus.PENDING_REINSPECTION:
            raise PolicyError("INVALID_WORK_ORDER_STATE", "工单当前不能提交复检")
        if expected_version != self.version:
            raise PolicyError("WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        if self.reinspections and self.reinspections[-1].decision is None:
            raise PolicyError("REINSPECTION_PENDING_REVIEW", "上一轮复检仍在等待工程师确认")
        if not inspector_id or not image_key or not note.strip():
            raise PolicyError("VALIDATION_ERROR", "复检需包含人员、图像和观察说明")
        record = Reinspection(
            id=f"{self.id}-RI-{len(self.reinspections) + 1:02d}",
            inspector_id=inspector_id,
            image_key=image_key,
            result=result,
            note=note.strip(),
        )
        self.reinspections.append(record)
        self.version += 1
        return record

    def review_reinspection(
        self,
        *,
        reviewer_id: str,
        reviewer_role: Role,
        decision: ReinspectionDecision,
        expected_version: int,
        reason: str = "",
    ) -> Reinspection:
        if reviewer_role != Role.ENGINEER:
            raise PolicyError("FORBIDDEN", "只有质量工程师可确认复检")
        if self.status != WorkOrderStatus.PENDING_REINSPECTION or not self.reinspections:
            raise PolicyError("INVALID_WORK_ORDER_STATE", "没有待确认的复检")
        if expected_version != self.version:
            raise PolicyError("WORK_ORDER_VERSION_CONFLICT", "工单状态已变化")
        record = self.reinspections[-1]
        if record.decision is not None:
            raise PolicyError("REINSPECTION_ALREADY_REVIEWED", "该轮复检已确认")
        if not reviewer_id or not reason.strip():
            raise PolicyError("REASON_REQUIRED", "复检确认必须填写理由")
        if record.result == ReinspectionResult.UNCERTAIN and decision == ReinspectionDecision.CONFIRM_PASS:
            raise PolicyError("UNCERTAIN_CANNOT_CLOSE", "无法判断的复检不能关闭工单")
        if decision == ReinspectionDecision.CONFIRM_PASS and record.result != ReinspectionResult.PASS:
            raise PolicyError("RESULT_CONFLICT", "只有明确通过的复检才能关闭工单")
        record.decision = decision
        record.reviewer_id = reviewer_id
        record.review_reason = reason.strip()
        self.version += 1
        if decision == ReinspectionDecision.CONFIRM_PASS:
            self.status = WorkOrderStatus.CLOSED
        elif decision == ReinspectionDecision.CONFIRM_FAIL:
            self.status = WorkOrderStatus.IN_PROGRESS
        return record


@dataclass
class Account:
    tenant_id: str
    username: str
    display_name: str
    role: Role
    password_hash: str
    active: bool = True
    must_change_password: bool = True


@dataclass(frozen=True)
class AccountAuditEvent:
    actor_id: str
    tenant_id: str
    username: str
    action: str
    reason: str
    before: str = ""
    after: str = ""


class AccountService:
    """In-memory policy layer shared by tests and the persisted API rules."""

    def __init__(self, accounts: Iterable[Account] = ()) -> None:
        self.accounts: dict[tuple[str, str], Account] = {
            (a.tenant_id, a.username): a for a in accounts
        }
        self.audit: list[AccountAuditEvent] = []

    @staticmethod
    def _hash_password(password: str, salt: bytes | None = None) -> str:
        salt = salt or token_bytes(16)
        digest = scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
            if algorithm != "scrypt" or (n, r, p) != ("16384", "8", "1"):
                return False
            digest = scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n), r=int(r), p=int(p),
            )
            return compare_digest(digest, bytes.fromhex(digest_hex))
        except (ValueError, TypeError):
            return False

    def create_account(
        self,
        *,
        actor_id: str,
        actor_role: Role,
        actor_tenant_id: str,
        tenant_id: str,
        username: str,
        display_name: str,
        role: Role,
        reason: str,
        active: bool = True,
    ) -> tuple[Account, str]:
        if actor_role != Role.ADMIN or actor_tenant_id != tenant_id or not actor_id:
            raise PolicyError("FORBIDDEN", "只有本租户管理员可创建账户")
        username = username.strip().lower()
        if not username or not display_name.strip() or not reason.strip():
            raise PolicyError("VALIDATION_ERROR", "账号、姓名和创建原因不能为空")
        key = (tenant_id, username)
        if key in self.accounts:
            raise PolicyError("ACCOUNT_EXISTS", "该租户中的账号已存在")
        temporary_password = token_urlsafe(24)
        account = Account(
            tenant_id=tenant_id,
            username=username,
            display_name=display_name.strip(),
            role=role,
            password_hash=self._hash_password(temporary_password),
            active=active,
        )
        self.accounts[key] = account
        self.audit.append(AccountAuditEvent(actor_id, tenant_id, username, "CREATE", reason.strip(),
                                            after=f"{role.value}:{'active' if active else 'disabled'}"))
        return account, temporary_password

    def update_account(
        self,
        *,
        actor_id: str,
        actor_role: Role,
        actor_tenant_id: str,
        tenant_id: str,
        username: str,
        role: Role,
        active: bool,
        reason: str,
    ) -> Account:
        if actor_role != Role.ADMIN or actor_tenant_id != tenant_id or not actor_id:
            raise PolicyError("FORBIDDEN", "只有本租户管理员可修改账户")
        account = self.accounts.get((tenant_id, username.strip().lower()))
        if account is None:
            raise PolicyError("ACCOUNT_NOT_FOUND", "账户不存在")
        if not reason.strip():
            raise PolicyError("REASON_REQUIRED", "权限变更必须填写原因")
        if account.role == Role.ADMIN and account.active and (role != Role.ADMIN or not active):
            other_admins = [candidate for candidate in self.accounts.values()
                            if candidate.tenant_id == tenant_id and candidate.username != account.username
                            and candidate.role == Role.ADMIN and candidate.active]
            if not other_admins:
                raise PolicyError("LAST_ADMIN", "不能停用或移除本租户最后一位管理员")
        before = f"{account.role.value}:{'active' if account.active else 'disabled'}"
        after = f"{role.value}:{'active' if active else 'disabled'}"
        if before == after:
            return account
        account.role = role
        account.active = active
        self.audit.append(AccountAuditEvent(actor_id, tenant_id, account.username, "UPDATE", reason.strip(), before, after))
        return account

    def change_password(self, *, tenant_id: str, username: str, old: str, new: str) -> None:
        account = self.accounts.get((tenant_id, username.lower()))
        if account is None or not account.active or not self.verify_password(old, account.password_hash):
            raise PolicyError("INVALID_CREDENTIALS", "账号或密码不正确")
        if len(new) < 12 or new == old:
            raise PolicyError("WEAK_PASSWORD", "新密码至少 12 位且不能与旧密码相同")
        account.password_hash = self._hash_password(new)
        account.must_change_password = False
        self.audit.append(AccountAuditEvent(username, tenant_id, username, "CHANGE_PASSWORD", "首次登录修改密码"))
