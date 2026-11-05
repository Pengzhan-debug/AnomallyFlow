import json
import unittest
from pathlib import Path

from qualityflow.domain import (
    AccountService,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStage,
    ApprovalStatus,
    PolicyError,
    ReinspectionDecision,
    ReinspectionResult,
    RiskLevel,
    Role,
    WorkOrder,
    WorkOrderStatus,
)


def assert_code(test: unittest.TestCase, code: str, fn):
    with test.assertRaises(PolicyError) as caught:
        fn()
    test.assertEqual(caught.exception.code, code)


class ApprovalTests(unittest.TestCase):
    def test_high_risk_requires_two_distinct_approvers(self):
        approval = ApprovalRequest("APR-1", "RPT-1", 1, RiskLevel.HIGH)
        assert_code(self, "FORBIDDEN", lambda: approval.decide(
            actor_id="manager-1", actor_role=Role.MANAGER, decision=ApprovalDecision.APPROVE,
            expected_version=0, expected_report_version=1, reason="已复核"))
        engineer = approval.decide(
            actor_id="engineer-1", actor_role=Role.ENGINEER, decision=ApprovalDecision.APPROVE,
            expected_version=0, expected_report_version=1, reason="证据已核验")
        self.assertEqual(approval.stage, ApprovalStage.MANAGER)
        self.assertEqual(approval.status, ApprovalStatus.PENDING)
        self.assertFalse(approval.can_create_order)
        self.assertIs(engineer, approval.decide(
            actor_id="engineer-1", actor_role=Role.ENGINEER, decision=ApprovalDecision.APPROVE,
            expected_version=0, expected_report_version=1, reason="证据已核验"))
        self.assertEqual(approval.version, 1)
        assert_code(self, "SAME_APPROVER", lambda: approval.decide(
            actor_id="engineer-1", actor_role=Role.MANAGER, decision=ApprovalDecision.APPROVE,
            expected_version=1, expected_report_version=1, reason="同人复核"))
        approval.decide(
            actor_id="manager-1", actor_role=Role.MANAGER, decision=ApprovalDecision.APPROVE,
            expected_version=1, expected_report_version=1, reason="主管确认")
        self.assertEqual(approval.status, ApprovalStatus.APPROVED)
        self.assertTrue(approval.can_create_order)
        self.assertEqual(len(approval.decisions), 2)

    def test_high_risk_reasons_and_report_version_are_checked(self):
        approval = ApprovalRequest("APR-2", "RPT-2", 3, RiskLevel.HIGH)
        assert_code(self, "REPORT_VERSION_CONFLICT", lambda: approval.decide(
            actor_id="eng", actor_role=Role.ENGINEER, decision=ApprovalDecision.APPROVE,
            expected_version=0, expected_report_version=2, reason="已核对"))
        assert_code(self, "REASON_REQUIRED", lambda: approval.decide(
            actor_id="eng", actor_role=Role.ENGINEER, decision=ApprovalDecision.APPROVE,
            expected_version=0, expected_report_version=3))
        self.assertEqual(approval.version, 0)

    def test_reject_and_return_never_create_order(self):
        for decision, expected_status in [
            (ApprovalDecision.REJECT, ApprovalStatus.REJECTED),
            (ApprovalDecision.RETURN_FOR_MORE_EVIDENCE, ApprovalStatus.RETURNED),
        ]:
            with self.subTest(decision=decision):
                approval = ApprovalRequest("APR-3", "RPT-3", 1, RiskLevel.HIGH)
                approval.decide(actor_id="eng", actor_role=Role.ENGINEER,
                                decision=decision, expected_version=0,
                                expected_report_version=1, reason="需要补查")
                self.assertEqual(approval.status, expected_status)
                self.assertFalse(approval.can_create_order)
                assert_code(self, "APPROVAL_FINALIZED", lambda: approval.decide(
                    actor_id="manager", actor_role=Role.MANAGER,
                    decision=ApprovalDecision.APPROVE, expected_version=1,
                    expected_report_version=1, reason="不同意"))

    def test_low_risk_needs_one_engineer(self):
        approval = ApprovalRequest("APR-4", "RPT-4", 1, RiskLevel.LOW)
        approval.decide(actor_id="eng", actor_role=Role.ENGINEER,
                        decision=ApprovalDecision.APPROVE, expected_version=0,
                        expected_report_version=1)
        self.assertTrue(approval.can_create_order)


class ReinspectionTests(unittest.TestCase):
    def test_uncertain_requires_recapture_and_cannot_close(self):
        order = WorkOrder("WO-1", WorkOrderStatus.PENDING_REINSPECTION)
        first = order.submit_reinspection(inspector_id="inspector", image_key="img-1",
                                          result=ReinspectionResult.UNCERTAIN,
                                          note="光照反射影响判断", expected_version=0)
        assert_code(self, "UNCERTAIN_CANNOT_CLOSE", lambda: order.review_reinspection(
            reviewer_id="eng", reviewer_role=Role.ENGINEER,
            decision=ReinspectionDecision.CONFIRM_PASS, expected_version=1,
            reason="无法确认"))
        self.assertIsNone(first.decision)
        order.review_reinspection(reviewer_id="eng", reviewer_role=Role.ENGINEER,
                                  decision=ReinspectionDecision.REQUEST_RECAPTURE,
                                  expected_version=1, reason="请更换光照补拍")
        self.assertEqual(order.status, WorkOrderStatus.PENDING_REINSPECTION)
        second = order.submit_reinspection(inspector_id="inspector", image_key="img-2",
                                           result=ReinspectionResult.PASS,
                                           note="补拍后未见原异常", expected_version=2)
        order.review_reinspection(reviewer_id="eng", reviewer_role=Role.ENGINEER,
                                  decision=ReinspectionDecision.CONFIRM_PASS,
                                  expected_version=3, reason="图像与实物复核通过")
        self.assertEqual(order.status, WorkOrderStatus.CLOSED)
        self.assertEqual([first.id, second.id], ["WO-1-RI-01", "WO-1-RI-02"])

    def test_failed_reinspection_preserves_round_and_returns_to_handling(self):
        order = WorkOrder("WO-2", WorkOrderStatus.IN_PROGRESS)
        order.complete_handling(actor_id="eng", actor_role=Role.ENGINEER,
                                action="清洁夹具", expected_version=0)
        first = order.submit_reinspection(inspector_id="ins", image_key="img-1",
                                          result=ReinspectionResult.FAIL,
                                          note="仍见异常", expected_version=1)
        order.review_reinspection(reviewer_id="eng", reviewer_role=Role.ENGINEER,
                                  decision=ReinspectionDecision.CONFIRM_FAIL,
                                  expected_version=2, reason="复检不合格")
        self.assertEqual(order.status, WorkOrderStatus.IN_PROGRESS)
        order.complete_handling(actor_id="eng", actor_role=Role.ENGINEER,
                                action="更换周转箱内衬", expected_version=3)
        order.submit_reinspection(inspector_id="ins", image_key="img-2",
                                  result=ReinspectionResult.PASS,
                                  note="未见原异常", expected_version=4)
        self.assertEqual(first.decision, ReinspectionDecision.CONFIRM_FAIL)
        self.assertEqual(len(order.handling_history), 2)
        self.assertEqual(len(order.reinspections), 2)

    def test_reinspection_checks_role_and_optimistic_version(self):
        order = WorkOrder("WO-3", WorkOrderStatus.PENDING_REINSPECTION)
        order.submit_reinspection(inspector_id="ins", image_key="img",
                                  result=ReinspectionResult.PASS,
                                  note="未见异常", expected_version=0)
        assert_code(self, "FORBIDDEN", lambda: order.review_reinspection(
            reviewer_id="ins", reviewer_role=Role.INSPECTOR,
            decision=ReinspectionDecision.CONFIRM_PASS,
            expected_version=1, reason="通过"))
        assert_code(self, "WORK_ORDER_VERSION_CONFLICT", lambda: order.review_reinspection(
            reviewer_id="eng", reviewer_role=Role.ENGINEER,
            decision=ReinspectionDecision.CONFIRM_PASS,
            expected_version=0, reason="通过"))


class AccountTests(unittest.TestCase):
    def test_admin_created_account_has_one_time_password_and_forced_change(self):
        service = AccountService()
        assert_code(self, "FORBIDDEN", lambda: service.create_account(
            actor_id="ins", actor_role=Role.INSPECTOR, actor_tenant_id="A",
            tenant_id="A", username="person", display_name="人员",
            role=Role.ENGINEER, reason="演示"))
        account, temporary = service.create_account(
            actor_id="admin", actor_role=Role.ADMIN, actor_tenant_id="A",
            tenant_id="A", username="Person", display_name="人员",
            role=Role.ENGINEER, reason="新增工程师")
        self.assertEqual(account.username, "person")
        self.assertTrue(account.must_change_password)
        self.assertNotIn(temporary, account.password_hash)
        self.assertTrue(service.verify_password(temporary, account.password_hash))
        assert_code(self, "ACCOUNT_EXISTS", lambda: service.create_account(
            actor_id="admin", actor_role=Role.ADMIN, actor_tenant_id="A",
            tenant_id="A", username="person", display_name="另一人",
            role=Role.ENGINEER, reason="重复"))
        service.change_password(tenant_id="A", username="person", old=temporary,
                                new="a-new-password-long-enough")
        self.assertFalse(account.must_change_password)
        self.assertFalse(service.verify_password(temporary, account.password_hash))

    def test_account_scope_role_and_audit(self):
        service = AccountService()
        account, _ = service.create_account(actor_id="admin", actor_role=Role.ADMIN,
                                            actor_tenant_id="A", tenant_id="A",
                                            username="auditor", display_name="审计员",
                                            role=Role.AUDITOR, reason="审计需求")
        assert_code(self, "FORBIDDEN", lambda: service.update_account(
            actor_id="admin-b", actor_role=Role.ADMIN, actor_tenant_id="B",
            tenant_id="A", username="auditor", role=Role.ENGINEER,
            active=True, reason="跨租户"))
        service.update_account(actor_id="admin", actor_role=Role.ADMIN,
                               actor_tenant_id="A", tenant_id="A",
                               username="auditor", role=Role.ENGINEER,
                               active=False, reason="岗位调整")
        self.assertEqual(account.role, Role.ENGINEER)
        self.assertFalse(account.active)
        self.assertEqual(service.audit[-1].before, "AUDITOR:active")
        self.assertEqual(service.audit[-1].after, "ENGINEER:disabled")


class ScenarioTests(unittest.TestCase):
    def test_fixed_scenario_catalog_has_ten_distinct_cases(self):
        root = Path(__file__).resolve().parents[2]
        cases = json.loads((root / "data" / "scenarios.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertTrue(all(case["expected"] for case in cases))


if __name__ == "__main__":
    unittest.main()
