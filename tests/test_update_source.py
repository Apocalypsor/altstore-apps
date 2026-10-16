import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_source as source


APP = {
    "name": "Example", "repository": "owner/app", "bundleIdentifier": "org.example.app",
    "asset_pattern": "*.ipa", "include_prereleases": True, "developerName": "Developer",
    "subtitle": "Example", "localizedDescription": "Example app", "iconURL": "https://example.org/icon.png",
    "tintColor": "3478F6",
}


def release(tag, index, *, prerelease=False, draft=False):
    return {
        "id": index, "tag_name": tag, "draft": draft, "prerelease": prerelease,
        "published_at": f"2026-01-{index:02d}T00:00:00Z", "updated_at": "2026-12-31T00:00:00Z",
        "html_url": f"https://github.com/owner/app/releases/tag/{tag}", "body": "Changes",
        "assets": [{"id": index, "name": "App.ipa", "state": "uploaded", "size": 100,
                    "updated_at": "2026-01-01T00:00:00Z",
                    "browser_download_url": f"https://github.com/owner/app/releases/download/{tag}/App.ipa"}],
    }


def metadata(version="1.0", build="1"):
    return {
        "CFBundleIdentifier": "org.example.app", "CFBundleShortVersionString": version,
        "CFBundleVersion": build, "MinimumOSVersion": "16.4", "size": 100, "sha256": "a" * 64,
        "appPermissions": {"entitlements": [], "privacy": {"NSPhotoLibraryAddUsageDescription": "Save photos"}},
    }


class VersionTests(unittest.TestCase):
    def test_numeric_not_lexical(self):
        self.assertGreater(source.version_key("2.7.10"), source.version_key("2.7.9"))
        self.assertEqual(source.version_key("1.0"), source.version_key("1.0.0"))
        self.assertGreater(source.version_key("100"), source.version_key("99"))

    def test_apple_prerelease_builds(self):
        builds = ["12d1", "12a1", "12b9", "12fc2", "12", "13b1"]
        self.assertEqual(sorted(builds, key=source.version_key), builds)

    def test_unrecognized_versions_fail_instead_of_guessing(self):
        for value in ("v1.2.3", "nightly", "", "1.0-beta"):
            with self.subTest(value=value), self.assertRaises(source.SourceError):
                source.version_key(value)

    def test_actual_ipa_version_beats_tag_and_shared_build_number(self):
        releases = [release("v2.7.9", 5), release("v2.7.10", 3), release("3.0.0-beta", 2, prerelease=True),
                    release("v2.8.1", 4)]
        data = {5: metadata("2.7.9", "99"), 3: metadata("2.7.10", "100"),
                2: metadata("3.0.0", "158"), 4: metadata("2.8.1", "158")}
        result = source.build_app(APP, [(r, r["assets"][0]) for r in releases], lambda a: data[a["id"]])
        self.assertEqual([v["version"] for v in result["versions"]], ["3.0.0", "2.8.1", "2.7.10", "2.7.9"])
        self.assertEqual(result["versions"][0]["buildVersion"], "158")
        self.assertIn("预发布", result["versions"][0]["localizedDescription"])

    def test_same_marketing_version_builds_and_duplicate_release(self):
        releases = [release("one", 1), release("two", 2), release("reupload", 3)]
        data = {1: metadata("1.0", "9"), 2: metadata("1.0", "10"), 3: metadata("1.0", "10")}
        result = source.build_app(APP, [(r, r["assets"][0]) for r in releases], lambda a: data[a["id"]])
        self.assertEqual([v["buildVersion"] for v in result["versions"]], ["10", "9"])
        self.assertIn("reupload", result["versions"][0]["downloadURL"])

    def test_wrong_bundle_identifier_stops_publication(self):
        data = metadata()
        data["CFBundleIdentifier"] = "wrong.app"
        r = release("v1", 1)
        with self.assertRaisesRegex(source.SourceError, "bundle ID"):
            source.build_app(APP, [(r, r["assets"][0])], lambda a: data)


class ReleaseTests(unittest.TestCase):
    def test_prereleases_included_drafts_and_missing_assets_excluded(self):
        releases = [release("beta", 3, prerelease=True), release("stable", 2),
                    release("draft", 4, draft=True), release("no-ipa", 5)]
        releases[-1]["assets"] = []
        self.assertEqual([r["tag_name"] for r, _ in source.select_releases(releases, APP, 10)], ["beta", "stable"])
        stable_only = dict(APP, include_prereleases=False)
        self.assertEqual([r["tag_name"] for r, _ in source.select_releases(releases, stable_only, 10)], ["stable"])

    def test_history_uses_publication_time_not_api_or_edit_order(self):
        releases = [release("old-edited", 1), release("middle", 2), release("new", 3)]
        self.assertEqual([r["tag_name"] for r, _ in source.select_releases(releases, APP, 2)], ["new", "middle"])

    def test_ambiguous_asset_and_empty_feed_fail(self):
        r = release("v1", 1)
        r["assets"].append(dict(r["assets"][0], name="Other.ipa"))
        with self.assertRaisesRegex(source.SourceError, "ambiguous"):
            source.select_releases([r], APP, 10)
        with self.assertRaisesRegex(source.SourceError, "No eligible"):
            source.select_releases([], APP, 10)

    @patch.object(source, "request")
    def test_pagination(self, request):
        request.side_effect = [io.BytesIO(json.dumps([{}] * 100).encode()), io.BytesIO(b'[{}]')]
        self.assertEqual(len(source.list_releases("owner/app", "token")), 101)
        self.assertIn("page=2", request.call_args.args[0])


class IpaTests(unittest.TestCase):
    @patch.object(source, "signed_entitlements", return_value=["com.apple.security.application-groups"])
    def test_main_binary_plist_and_extension_permissions(self, entitlements):
        main = {key: value for key, value in metadata("3.0.0", "158").items() if key.startswith("CF") or key == "MinimumOSVersion"}
        main.update(CFBundleExecutable="App", NSPhotoLibraryAddUsageDescription="Save photos")
        extension = dict(main, CFBundleShortVersionString="9.9", CFBundleExecutable="Extension",
                         NSCameraUsageDescription="Camera")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.ipa"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Payload/App.app/Info.plist", plistlib.dumps(main, fmt=plistlib.FMT_BINARY))
                archive.writestr("Payload/App.app/App", b"main binary")
                archive.writestr("Payload/App.app/PlugIns/Extension.appex/Info.plist", plistlib.dumps(extension))
                archive.writestr("Payload/App.app/PlugIns/Extension.appex/Extension", b"extension binary")
            result = source.inspect_ipa(path)
        self.assertEqual(result["CFBundleShortVersionString"], "3.0.0")
        self.assertEqual(result["CFBundleVersion"], "158")
        self.assertIn("NSCameraUsageDescription", result["appPermissions"]["privacy"])
        self.assertEqual(entitlements.call_count, 2)

    def test_multiple_main_apps_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.ipa"
            with zipfile.ZipFile(path, "w") as archive:
                for app in ("A", "B"):
                    archive.writestr(f"Payload/{app}.app/Info.plist", plistlib.dumps({}))
            with self.assertRaisesRegex(source.SourceError, "exactly one"):
                source.inspect_ipa(path)

    @patch.object(source.subprocess, "run")
    def test_unsigned_and_signed_entitlements(self, run):
        with tempfile.TemporaryDirectory() as directory:
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"code object is not signed at all")
            self.assertEqual(source.signed_entitlements(b"binary", Path(directory)), [])
            run.return_value = subprocess.CompletedProcess([], 0, plistlib.dumps({"example.entitlement": True}), b"")
            self.assertEqual(source.signed_entitlements(b"binary", Path(directory)), ["example.entitlement"])
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"invalid binary")
            with self.assertRaises(source.SourceError):
                source.signed_entitlements(b"binary", Path(directory))

    @patch.object(source, "inspect_ipa", return_value=metadata())
    @patch.object(source, "request")
    def test_cache_reuse_and_reuploaded_asset_invalidation(self, request, inspect):
        request.side_effect = lambda *a, **k: io.BytesIO(b"ipa")
        asset = dict(release("v1", 1)["assets"][0], size=3)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            source.asset_metadata(asset, cache)
            source.asset_metadata(asset, cache)
            self.assertEqual(request.call_count, 1)
            source.asset_metadata(dict(asset, id=2), cache)
            source.asset_metadata(dict(asset, updated_at="2026-02-01T00:00:00Z"), cache)
            self.assertEqual(request.call_count, 3)

    @patch.object(source, "request")
    def test_size_and_digest_mismatches_rejected(self, request):
        request.side_effect = lambda *a, **k: io.BytesIO(b"ipa")
        asset = release("v1", 1)["assets"][0]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(source.SourceError, "size mismatch"):
                source.asset_metadata(asset, Path(directory))
            with self.assertRaisesRegex(source.SourceError, "digest mismatch"):
                source.asset_metadata(dict(asset, size=3, digest="sha256:incorrect"), Path(directory))
            self.assertEqual(list(Path(directory).glob("*.json")), [])

    @patch.object(source, "generate_source", side_effect=source.SourceError("upstream failed"))
    def test_failed_update_preserves_previous_output(self, generate):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.json"
            config = Path(directory) / "apps.json"
            output.write_text("previous source")
            config.write_text("{}")
            with patch.object(sys, "argv", ["update_source", "--config", str(config), "--output", str(output)]):
                with self.assertRaises(source.SourceError):
                    source.main()
            self.assertEqual(output.read_text(), "previous source")


if __name__ == "__main__":
    unittest.main()
