import unittest
from types import SimpleNamespace

from api.register_routes import ProjectRegistrationItem, _repository_specs_from_project_registration
from core.onboarding_service import (
    _aggregate_repo_stats,
    _normalize_repository_specs,
    _project_item_for_ensure,
    _repo_specs_for_task,
    _repository_connector_item_from_spec,
)
from core.models import OnboardingTaskModel


class OnboardingRepositorySpecTest(unittest.TestCase):
    def test_normalize_repository_specs_keeps_all_repositories_and_dedupes_primary(self) -> None:
        specs = _normalize_repository_specs(
            project_id="demo",
            repo_url="https://gitlab.example.com/backend/order-api.git",
            branch="main",
            repositories=[
                {
                    "repo_url": "https://gitlab.example.com/backend/order-api.git",
                    "display_name": "Order API",
                },
                {
                    "repo_url": "https://gitlab.example.com/backend/order-worker.git",
                    "branch": "release",
                    "build_venv": False,
                },
                "https://gitlab.example.com/backend/order-admin.git",
            ],
        )

        self.assertEqual(
            [item["repo_url"] for item in specs],
            [
                "https://gitlab.example.com/backend/order-api.git",
                "https://gitlab.example.com/backend/order-worker.git",
                "https://gitlab.example.com/backend/order-admin.git",
            ],
        )
        self.assertEqual([item["id"] for item in specs], ["default", "order-worker", "order-admin"])
        self.assertEqual(specs[0]["branch"], "main")
        self.assertEqual(specs[1]["branch"], "release")
        self.assertIs(specs[1]["build_venv"], False)

    def test_normalize_repository_specs_accepts_textarea_style_repo_urls(self) -> None:
        specs = _normalize_repository_specs(
            project_id="demo",
            repo_url="",
            branch="master",
            repo_urls=[
                "https://gitlab.example.com/a/one.git\nhttps://gitlab.example.com/a/two.git",
                "https://gitlab.example.com/a/three.git,https://gitlab.example.com/a/four.git",
            ],
        )

        self.assertEqual([item["id"] for item in specs], ["default", "two", "three", "four"])
        self.assertEqual(
            [item["repo_url"] for item in specs],
            [
                "https://gitlab.example.com/a/one.git",
                "https://gitlab.example.com/a/two.git",
                "https://gitlab.example.com/a/three.git",
                "https://gitlab.example.com/a/four.git",
            ],
        )

    def test_repository_connector_item_from_spec_uses_onboarding_metadata(self) -> None:
        spec = _normalize_repository_specs(
            project_id="demo",
            repo_url="",
            branch="master",
            repositories=[
                {
                    "id": "worker",
                    "repo_url": "git@gitlab.example.com:backend/order-worker.git",
                    "display_name": "Order Worker",
                    "branch": "release",
                    "sort_order": 30,
                    "build_venv": "false",
                }
            ],
        )[0]

        item = _repository_connector_item_from_spec("demo", spec)

        self.assertEqual(item.id, "worker")
        self.assertEqual(item.project_id, "demo")
        self.assertEqual(item.display_name, "Order Worker")
        self.assertEqual(item.git_url, "git@gitlab.example.com:backend/order-worker.git")
        self.assertEqual(item.default_branch, "release")
        self.assertEqual(item.sort_order, 30)
        self.assertIs(item.build_venv, False)

    def test_repo_specs_for_task_prefers_profile_repository_metadata(self) -> None:
        task = OnboardingTaskModel(
            task_id="onboard_test",
            project_id="demo",
            repo_url="https://gitlab.example.com/backend/order-api.git",
            branch="master",
            profile={
                "repositories": [
                    {
                        "id": "order-api",
                        "repo_url": "https://gitlab.example.com/backend/order-api.git",
                        "display_name": "Order API",
                    },
                    {
                        "id": "order-worker",
                        "repo_url": "https://gitlab.example.com/backend/order-worker.git",
                    },
                ],
            },
        )

        specs = _repo_specs_for_task(task)

        self.assertEqual([item["id"] for item in specs], ["order-api", "order-worker"])
        self.assertEqual(specs[0]["display_name"], "Order API")

    def test_project_registration_item_accepts_multiple_repositories(self) -> None:
        item = ProjectRegistrationItem(
            id="demo",
            name="Demo",
            repositories=[
                {
                    "id": "api",
                    "repo_url": "https://gitlab.example.com/backend/demo-api.git",
                    "display_name": "Demo API",
                },
                "https://gitlab.example.com/backend/demo-worker.git",
            ],
        )

        specs = _repository_specs_from_project_registration(item)

        self.assertEqual([spec["id"] for spec in specs], ["api", "demo-worker"])
        self.assertEqual(specs[0]["display_name"], "Demo API")

    def test_aggregate_repo_stats_counts_repository_connector_artifacts(self) -> None:
        stats = _aggregate_repo_stats(
            [
                {"repo_url": "repo-a", "files_seen": 3, "artifacts": 4},
                {"repo_url": "repo-b", "files_seen": 5, "artifacts": 6},
            ],
            [{"repo_url": "repo-c", "error": "boom"}],
            repo_count=3,
            repository_artifacts=3,
        )

        self.assertEqual(stats["repositories_total"], 3)
        self.assertEqual(stats["repositories_analyzed"], 2)
        self.assertEqual(stats["repositories_failed"], 1)
        self.assertEqual(stats["files_seen"], 8)
        self.assertEqual(stats["artifacts"], 13)

    def test_project_item_for_ensure_preserves_existing_project_metadata(self) -> None:
        item = _project_item_for_ensure(
            project_id="demo",
            name="demo",
            description="",
            repo_url="https://gitlab.example.com/backend/new-worker.git",
            branch="release",
            existing=SimpleNamespace(
                name="Demo Project",
                description="Existing description",
                git_url="https://gitlab.example.com/backend/demo-api.git",
                default_branch="master",
            ),
        )

        self.assertEqual(item.name, "Demo Project")
        self.assertEqual(item.description, "Existing description")
        self.assertEqual(item.git_url, "https://gitlab.example.com/backend/demo-api.git")
        self.assertEqual(item.default_branch, "master")


if __name__ == "__main__":
    unittest.main()
