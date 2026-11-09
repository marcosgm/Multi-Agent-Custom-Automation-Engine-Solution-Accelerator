import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "seed_content_job", Path(__file__).with_name("seed_content_job.py")
)
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)


class DatasetFilesTests(unittest.TestCase):
    def test_selects_matching_files_and_preserves_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            pack = Path(directory)
            source = pack / "datasets"
            source.mkdir()
            (source / "nested").mkdir()
            (source / "nested" / "data.csv").write_text("id\n1\n")
            (source / "notes.txt").write_text("notes")
            files = seed.dataset_files(pack, {"source": "datasets", "pattern": "*.csv"})
            self.assertEqual([name for _, name in files], ["nested/data.csv"])

    def test_rejects_source_outside_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Invalid dataset source"):
                seed.dataset_files(Path(directory), {"source": ".."})

    def test_rejects_empty_match(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No dataset files"):
                seed.dataset_files(Path(directory), {"source": "."})

    def test_rejects_symlink_outside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            pack = Path(directory)
            (pack / "data").mkdir()
            (pack / "outside.txt").write_text("outside")
            (pack / "data" / "link.txt").symlink_to(pack / "outside.txt")
            with self.assertRaisesRegex(ValueError, "escapes"):
                seed.dataset_files(pack, {"source": "data"})


class FullSeedingTests(unittest.TestCase):
    def test_default_is_full_option_seven_without_prompts(self):
        args = seed.parse_args([])
        self.assertEqual(len(args.packs), 6)
        self.assertEqual(set(args.packs), set(seed.TEAM_PACKS))
        self.assertFalse(args.datasets_only)
        self.assertEqual(sum(map(len, seed.PACK_KNOWLEDGE_BASES.values())), 9)

    def test_dataset_only_mode_remains_available(self):
        args = seed.parse_args(["--datasets-only", "--packs", "retail_customer"])
        self.assertTrue(args.datasets_only)
        self.assertEqual(args.packs, ["retail_customer"])

    def test_team_upload_verifies_persisted_team_ids(self):
        response = Mock()
        response.json.return_value = [{"team_id": seed.TEAM_PACKS["hr_onboarding"]}]
        requests = SimpleNamespace(get=Mock(return_value=response))
        with patch.dict("sys.modules", {"requests": requests}):
            with patch.dict("os.environ", {"BACKEND_URL": "https://backend.example"}):
                with patch.object(seed, "run_script") as run:
                    self.assertEqual(seed.seed_teams(["hr_onboarding"]), [seed.TEAM_PACKS["hr_onboarding"]])
                    self.assertEqual(run.call_args.args[0], "upload_team_config.py")
                    response.json.return_value = []
                    with self.assertRaisesRegex(RuntimeError, "missing"):
                        seed.seed_teams(["hr_onboarding"])

    def test_selected_pack_only_configures_its_foundry_resources(self):
        kb = "macae-content-gen-products-kb"
        search = "https://search.example"
        kb_response = Mock()
        kb_response.json.return_value = {"knowledgeSources": [{"name": "products"}]}
        connection_response = Mock()
        connection_response.json.return_value = {"properties": {
            "target": f"{search}/knowledgebases/{kb}/mcp?api-version=2025-11-01-preview",
            "authType": "ProjectManagedIdentity",
        }}
        httpx = SimpleNamespace(get=Mock(side_effect=[kb_response, connection_response]))
        project_client = Mock()
        modules = {
            "httpx": httpx,
            "azure": SimpleNamespace(),
            "azure.ai": SimpleNamespace(),
            "azure.ai.projects": SimpleNamespace(AIProjectClient=project_client),
        }
        environment = {
            "AZURE_AI_SEARCH_ENDPOINT": search,
            "AZURE_AI_PROJECT_RESOURCE_ID": "/subscriptions/test/projects/example",
        }
        with patch.dict("sys.modules", modules), patch.dict("os.environ", environment):
            with patch.object(seed, "run_script") as run:
                result = seed.seed_foundry(["content_gen"], Mock())
        self.assertEqual(result, {"vector_stores": {}, "knowledge_bases": [kb], "connections": 1})
        self.assertEqual(run.call_args_list, [
            unittest.mock.call("seed_knowledge_bases.py", "--only", kb),
            unittest.mock.call("seed_kb_connections.py", "--only", kb),
        ])
        project_client.assert_not_called()
        self.assertEqual(httpx.get.call_count, 2)
        kb_response.raise_for_status.assert_called_once()
        connection_response.raise_for_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
