"""发布脚本安全检查与资源路径校验。"""

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))

from checks import (  # noqa: E402
    DeployCheckError,
    check_dist_index,
    list_release_records,
    parse_redis_persistence,
    rsync_is_safe,
    sibling_index_paths,
    snapshot_marker,
)


class DeployChecksTest(unittest.TestCase):
    def test_rejects_unprotected_delete_of_agent_parent(self) -> None:
        with self.assertRaises(DeployCheckError):
            rsync_is_safe("/var/www/projects/agent/", delete=True)
        with self.assertRaises(DeployCheckError):
            rsync_is_safe("/var/www/projects/", delete=True)
        with self.assertRaises(DeployCheckError):
            rsync_is_safe("/opt/hello-agent/", delete=True)

    def test_allows_main_site_delete_with_sibling_excludes(self) -> None:
        path = rsync_is_safe(
            "/var/www/projects/agent/",
            delete=True,
            excludes=["admin/", "todo/"],
        )
        self.assertEqual(path, "/var/www/projects/agent")

    def test_allows_leaf_frontend_and_backend_package_delete(self) -> None:
        self.assertEqual(
            rsync_is_safe("/var/www/projects/agent/todo/", delete=True),
            "/var/www/projects/agent/todo",
        )
        self.assertEqual(
            rsync_is_safe("/opt/hello-agent/src/hello_agent/", delete=True),
            "/opt/hello-agent/src/hello_agent",
        )

    def test_rejects_path_escape_and_empty_dest(self) -> None:
        with self.assertRaises(DeployCheckError):
            rsync_is_safe("/var/www/projects/agent/../projects/", delete=False)
        with self.assertRaises(DeployCheckError):
            rsync_is_safe("", delete=False)

    def test_dist_index_requires_app_prefix_and_rejects_bare_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.html"
            bad = Path(directory) / "bad.html"
            good.write_text(
                '<script src="/agent/todo/assets/index-abc.js"></script>'
                '<link href="/agent/todo/assets/index-abc.css" rel="stylesheet">',
                encoding="utf-8",
            )
            bad.write_text(
                '<script src="/assets/index-abc.js"></script>',
                encoding="utf-8",
            )
            urls = check_dist_index(good, "todo")
            self.assertTrue(any(item.endswith(".js") for item in urls))
            with self.assertRaises(DeployCheckError):
                check_dist_index(bad, "todo")
            with self.assertRaises(DeployCheckError):
                check_dist_index(good, "admin")

    def test_redis_persistence_requires_rdb_or_aof(self) -> None:
        parsed = parse_redis_persistence(
            "rdb_last_bgsave_status:ok\nrdb_last_save_time:1\n",
            "3600 1 300 100",
            "no",
        )
        self.assertTrue(parsed["ok"])
        with self.assertRaises(DeployCheckError):
            parse_redis_persistence("rdb_last_bgsave_status:ok\n", "", "no")
        with self.assertRaises(DeployCheckError):
            parse_redis_persistence("rdb_last_bgsave_status:err\n", "3600 1", "yes")

    def test_sibling_index_paths_stay_under_agent_root(self) -> None:
        paths = sibling_index_paths("/var/www/projects/agent/")
        self.assertEqual(paths["frontend"], "/var/www/projects/agent/index.html")
        self.assertEqual(paths["todo"], "/var/www/projects/agent/todo/index.html")
        self.assertEqual(paths["admin"], "/var/www/projects/agent/admin/index.html")

    def test_lists_release_records_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log.jsonl"
            log.write_text(
                '{"id":"a","status":"success"}\n{"id":"b","status":"rolled_back"}\n',
                encoding="utf-8",
            )
            rows = list_release_records(Path(directory), limit=10)
            self.assertEqual([item["id"] for item in rows], ["b", "a"])
            self.assertEqual(list_release_records(Path(directory) / "missing"), [])

    def test_snapshot_markers_cover_rollback_targets(self) -> None:
        self.assertEqual(snapshot_marker("backend"), "api.py")
        self.assertEqual(snapshot_marker("frontend"), "index.html")
        with self.assertRaises(DeployCheckError):
            snapshot_marker("unknown")


if __name__ == "__main__":
    unittest.main()
