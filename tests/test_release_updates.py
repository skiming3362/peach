"""发行通道、故障状态和独立托盘检查的隔离回归。"""
import json
import unittest
from unittest.mock import Mock, patch

import httpx

from peach import release_updates
from peach.http import HttpResponse
from peach.versioning import VersionManager


def release(version, **extra):
    return dict(tag_name=f"v{version}", prerelease=True, draft=False,
                assets=[{"name": f"Peach-{version}-windows-x64.zip", "state": "uploaded"}], **extra)


class ReleaseUpdateTests(unittest.TestCase):
    def check(self, rows, status=200, version="0.9.0", headers=None):
        transport = Mock(return_value=HttpResponse(status, headers or {}, json.dumps(rows).encode()))
        with patch.object(release_updates, "__version__", version):
            return release_updates.check(transport=transport)

    def test_the_channel_is_a_single_repository_without_a_mirror(self):
        """发布通道只有一处：多一个镜像源等于多一条要跟着改的地址。"""
        self.assertTrue(release_updates.API_URL.startswith("https://api.github.com/repos/"))
        self.assertIn(release_updates.REPOSITORY, release_updates.API_URL)
        self.assertEqual(release_updates.RELEASES_URL,
                         f"https://github.com/{release_updates.REPOSITORY}/releases")
        calls = []
        transport = Mock(side_effect=lambda request, *_: calls.append(request.url) or
                         HttpResponse(200, {}, json.dumps([release("1.0.0")]).encode()))
        with patch.object(release_updates, "__version__", "0.9.0"):
            release_updates.check(transport=transport)
        self.assertEqual(calls, [release_updates.API_URL])

    def test_a_download_url_is_built_from_the_tag_not_taken_from_the_row(self):
        """发行条目里的下载地址是发布者可改的字段，只认 tag 与摘要。"""
        row = release("1.0.0")
        row["assets"][0].update(size=123, digest="sha256:" + "a" * 64,
                                browser_download_url="https://untrusted.invalid/package.zip")
        result = self.check([row])
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["asset_url"], release_updates.RELEASES_URL + "/download/v1.0.0/Peach-1.0.0-windows-x64.zip")
        self.assertEqual(result["asset_digest"], "sha256:" + "a" * 64)
        self.assertEqual(result["release_url"], release_updates.RELEASES_URL + "/tag/v1.0.0")
        self.assertEqual(release_updates.download_sources(result["asset_url"]), (result["asset_url"],))

    def test_channel_includes_prereleases_and_orders_versions_numerically(self):
        result = self.check([release("0.9.0"), release("0.10.0")])
        self.assertEqual((result["state"], result["latest_version"]), ("available", "0.10.0"))
        self.assertEqual(result["release_url"], release_updates.RELEASES_URL + "/tag/v0.10.0")

    def test_unpublished_or_incomplete_packages_are_not_offered(self):
        draft = release("8.0.0"); draft["draft"] = True
        incomplete = release("9.0.0"); incomplete["assets"] = []
        result = self.check([draft, incomplete, {"tag_name": "invalid"}, release("0.9.0")])
        self.assertEqual(result["state"], "current")
        self.assertEqual(self.check([release("0.8.0")])["state"], "ahead")
        self.assertEqual(self.check([])["state"], "empty")

    def test_prefers_app_update_when_all_tool_assets_are_uploaded(self):
        row = release("1.0.0")
        row["assets"] += [
            {"name": "Peach-1.0.0-windows-x64-update.zip", "state": "uploaded", "size": 123, "digest": "sha256:" + "a" * 64},
            {"name": "Peach-1.0.0-windows-x64-setup.exe", "state": "uploaded"},
        ]
        self.assertEqual(self.check([row])["package_kind"], "full")
        row["assets"] += [{"name": f"Peach-1.0.0-windows-x64-tools-{tool}.zip", "state": "uploaded"}
                          for tool in ("ffmpeg", "untrunc", "cloudflared")]
        self.assertEqual(self.check([row])["package_kind"], "full", "缺人脸模型工具包时不能只下应用更新包")
        row["assets"].append({"name": "Peach-1.0.0-windows-x64-tools-face-models.zip", "state": "uploaded"})
        result = self.check([row])
        self.assertEqual(result["package_kind"], "update")
        self.assertTrue(result["asset_url"].endswith("-update.zip"))
        self.assertEqual(result["asset_size"], 123)
        self.assertEqual(result["full_url"], release_updates.RELEASES_URL + "/download/v1.0.0/Peach-1.0.0-windows-x64.zip",
                         "应用更新包准备不下去时改下的同版本完整包")
        row["assets"][1]["state"] = "new"
        self.assertEqual(self.check([row])["package_kind"], "full")
        row["assets"].pop(0)
        self.assertEqual(self.check([row])["state"], "empty")

    def test_failures_do_not_claim_current_or_keep_a_latest_version(self):
        self.assertEqual(self.check({}, status=429)["state"], "error")
        self.assertEqual(self.check({})["state"], "error")
        self.assertEqual(self.check([])["state"], "empty")
        for cause in (httpx.ConnectError("offline"), httpx.ReadTimeout("timeout")):
            result = release_updates.check(transport=Mock(side_effect=cause))
            self.assertEqual(result["state"], "error")
            self.assertIsNone(result["latest_version"])

    def test_a_refusal_says_what_went_wrong_instead_of_just_please_retry(self):
        """配额用尽和网络故障都要落到同一句「请重试」时，人看不出该等还是该改代理。"""
        exhausted = self.check({}, status=403, headers={"x-ratelimit-remaining": "0"})
        self.assertEqual(exhausted["state"], "error")
        self.assertIn("配额", exhausted["message"])
        missing = self.check({}, status=404)
        self.assertIn(release_updates.REPOSITORY, missing["message"])
        other = self.check({}, status=502)
        self.assertIn("502", other["message"])

    def test_the_only_source_reports_current_and_ahead(self):
        for version, state in (("0.46.1", "ahead"), ("0.48.2", "current")):
            with self.subTest(published=version):
                self.assertEqual(self.check([release(version)], version="0.48.2")["state"], state)
        self.assertEqual(self.check([release("0.49.0")], version="0.48.2")["state"], "available",
                         "源上真有更新的版本时照样可以升")

    def test_standalone_tray_checks_releases_without_git(self):
        execute = Mock(side_effect=AssertionError("Git must not run"))
        with patch("peach.distribution.standalone", return_value=True), patch.object(
                release_updates, "check", return_value=dict(state="available", latest_version="1.0.0", message="有新版本可下载。")) as check:
            result = VersionManager(execute=execute).check()
        check.assert_called_once()
        self.assertEqual(result.state, "available")
        self.assertIn("1.0.0", result.message)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
