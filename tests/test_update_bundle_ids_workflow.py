import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (
    ROOT / ".github/workflows/update-bundle-ids.yml"
).read_text(encoding="utf-8")


class UpdateBundleIdsWorkflowTests(unittest.TestCase):
    def test_feature_branch_workflow_run_cannot_run_writer(self):
        job = WORKFLOW.split("  update-bundle-ids:", 1)[1]
        condition = job.split("    steps:", 1)[0]
        self.assertIn("github.event.workflow_run.conclusion == 'success'", condition)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", condition)
        self.assertIn(
            "github.event_name != 'workflow_run'",
            condition,
        )

    def test_main_workflow_run_and_manual_schedule_remain_allowed(self):
        condition = WORKFLOW.split("  update-bundle-ids:", 1)[1].split(
            "    steps:", 1
        )[0]
        self.assertIn("&&", condition)
        self.assertIn("||", condition)
        self.assertIn("schedule:", WORKFLOW)
        self.assertIn("workflow_dispatch:", WORKFLOW)

    def test_checkout_and_push_are_explicitly_main(self):
        self.assertIn("ref: main", WORKFLOW)
        self.assertIn("git pull --rebase origin main", WORKFLOW)
        self.assertIn("git push origin HEAD:main", WORKFLOW)
        self.assertNotIn("git push --force", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
