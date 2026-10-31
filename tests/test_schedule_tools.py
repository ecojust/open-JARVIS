import copy
import json
from pathlib import Path
import tempfile
import unittest

from jarvis.schedule_tools import audit_schedule, create_schedule, plan_schedule


class ScheduleTests(unittest.TestCase):
    def test_thirty_people_plan_rechecks_coverage_rest_and_workload(self):
        plan = plan_schedule(start_date="2026-11-02")
        audit = audit_schedule(plan)
        self.assertTrue(audit["passed"], audit["violations"])
        self.assertEqual(audit["total_assignments"], 126)
        self.assertEqual(len(plan["assignments"]), 21)
        self.assertEqual(len(plan["employees"]), 30)
        self.assertTrue(plan["anonymous_example"])
        self.assertEqual({row["shifts"] for row in audit["employees"]}, {4, 5})
        self.assertTrue(
            all(row["shortest_rest_hours"] >= 12 for row in audit["employees"])
        )
        self.assertTrue(
            all(row["max_consecutive_days"] <= 5 for row in audit["employees"])
        )

    def test_infeasible_staffing_is_rejected_instead_of_inventing_coverage(self):
        with self.assertRaisesRegex(ValueError, "总班次数需求"):
            plan_schedule(start_date="2026-11-02", max_shifts_per_employee=4)
        with self.assertRaisesRegex(ValueError, "每日需求"):
            plan_schedule(start_date="2026-11-02", employee_count=12)
        with self.assertRaisesRegex(ValueError, "有界排班搜索"):
            plan_schedule(
                start_date="2026-11-02",
                employee_count=1,
                days=2,
                shifts=[
                    {"name": "白班", "start": "08:00", "end": "16:00", "required": 1}
                ],
                max_consecutive_days=1,
            )

    def test_audit_detects_double_shifts_and_false_coverage(self):
        plan = plan_schedule(start_date="2026-11-02")
        corrupted = copy.deepcopy(plan)
        corrupted["assignments"][1]["employees"][0] = corrupted["assignments"][0][
            "employees"
        ][0]
        audit = audit_schedule(corrupted)
        self.assertFalse(audit["passed"])
        self.assertTrue(any("多班" in issue for issue in audit["violations"]))
        corrupted["assignments"][2]["employees"] = []
        self.assertTrue(
            any(
                "人数不足" in issue for issue in audit_schedule(corrupted)["violations"]
            )
        )

    def test_invalid_names_and_limits_are_not_silently_normalized(self):
        for extra in (
            {"days": 32},
            {"days": True},
            {"min_rest_hours": float("nan")},
            {"employees": ["重复"] * 30},
            {"shifts": []},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                plan_schedule(start_date="2026-11-02", **extra)

    def test_workbook_contains_real_people_rows_rules_and_json_audit(self):
        from openpyxl import load_workbook

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = create_schedule(root, "reports/排班.xlsx", start_date="2026-11-02")
            self.assertTrue(result["audit"]["passed"])
            self.assertTrue((root / result["path"]).is_file())
            workbook = load_workbook(
                root / result["path"], read_only=True, data_only=True
            )
            cells = list(workbook.active.values)
            workbook.close()
            text = "\n".join(
                " ".join(str(value) for value in row if value is not None)
                for row in cells
            )
            self.assertIn("员工30", text)
            self.assertIn("班间休息≥12小时", text)
            self.assertIn("无请假", text)
            audit_path = root / "reports/排班.xlsx.schedule.json"
            plan = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertTrue(audit_schedule(plan)["passed"])
            self.assertEqual(len(result["artifacts"]), 3)
            with self.assertRaises(ValueError):
                create_schedule(root, "reports/排班.xlsx", start_date="2026-11-02")


if __name__ == "__main__":
    unittest.main()
