import unittest
from unittest.mock import patch

from ocir_image_update import (
    build_image_repository,
    derive_manifest_directory,
    load_github_app_private_key,
    load_github_config,
    load_update_target,
    parse_ocir_push_event,
    replace_image_tag_in_text,
    resolve_config_value,
    strip_ocir_namespace,
)


class ParseEventTests(unittest.TestCase):
    def test_parses_ocir_push_event(self):
        event = parse_ocir_push_event(
            {
                "eventType": "com.oraclecloud.artifacts.uploaddockerimage",
                "eventTime": "2026-03-09T00:00:00Z",
                "data": {
                    "resourceName": "team/api:1.2.3",
                    "additionalDetails": {
                        "path": "namespace/team/api",
                        "digest": "sha256:deadbeef",
                    },
                },
            }
        )

        self.assertEqual(event.repository_path, "namespace/team/api")
        self.assertEqual(event.resource_name, "team/api:1.2.3")
        self.assertEqual(event.tag, "1.2.3")
        self.assertEqual(event.digest, "sha256:deadbeef")


class DirectoryScanTests(unittest.TestCase):
    def test_strips_ocir_namespace(self):
        self.assertEqual(strip_ocir_namespace("namespace/dev/api", "namespace"), "dev/api")
        self.assertEqual(strip_ocir_namespace("dev/api", "namespace"), "dev/api")

    def test_derives_manifest_directory(self):
        self.assertEqual(derive_manifest_directory("dev/api", "argocd"), "argocd/dev")
        self.assertEqual(derive_manifest_directory("api", "argocd"), "argocd")

    def test_builds_image_repository(self):
        self.assertEqual(
            build_image_repository("yny.ocir.io", "namespace/dev/api", "namespace"),
            "yny.ocir.io/namespace/dev/api",
        )
        self.assertEqual(
            build_image_repository("yny.ocir.io", "dev/api", "namespace"),
            "yny.ocir.io/namespace/dev/api",
        )

    def test_loads_update_target(self):
        event = parse_ocir_push_event(
            {
                "eventType": "com.oraclecloud.artifacts.uploaddockerimage",
                "data": {
                    "resourceName": "dev/api:2026-03-09",
                    "additionalDetails": {"path": "namespace/dev/api"},
                },
            }
        )

        with patch.dict(
            "os.environ",
            {
                "MANIFEST_SCAN_ROOT": "argocd",
                "OCIR_REGISTRY": "yny.ocir.io",
                "OCIR_NAMESPACE": "namespace",
            },
            clear=True,
        ):
            target = load_update_target(event)

        self.assertEqual(target.ocir_repository, "dev/api")
        self.assertEqual(target.image_repository, "yny.ocir.io/namespace/dev/api")
        self.assertEqual(target.manifest_directory, "argocd/dev")

    def test_replaces_matching_image_lines(self):
        original = """\
containers:
  - image: yny.ocir.io/namespace/dev/api:old
  - image: yny.ocir.io/namespace/dev/worker:stay
"""

        updated, replacements = replace_image_tag_in_text(
            content=original,
            image_repository="yny.ocir.io/namespace/dev/api",
            new_tag="20260309-1",
        )

        self.assertEqual(replacements, 1)
        self.assertIn("image: yny.ocir.io/namespace/dev/api:20260309-1", updated)
        self.assertIn("image: yny.ocir.io/namespace/dev/worker:stay", updated)

    def test_replaces_quoted_image_lines(self):
        original = 'image: "yny.ocir.io/namespace/dev/api:old"\n'

        updated, replacements = replace_image_tag_in_text(
            content=original,
            image_repository="yny.ocir.io/namespace/dev/api",
            new_tag="latest",
        )

        self.assertEqual(replacements, 1)
        self.assertEqual(updated, 'image: "yny.ocir.io/namespace/dev/api:latest"\n')


class SecretResolutionTests(unittest.TestCase):
    def test_prefers_plain_env_over_vault(self):
        with patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "direct-token",
                "GITHUB_TOKEN_SECRET_OCID": "ocid1.secret.oc1..example",
            },
            clear=True,
        ):
            with patch("ocir_image_update.fetch_vault_secret") as mocked_fetch:
                self.assertEqual(resolve_config_value("GITHUB_TOKEN"), "direct-token")
                mocked_fetch.assert_not_called()

    def test_reads_vault_secret_when_plain_env_missing(self):
        with patch.dict(
            "os.environ",
            {"GITHUB_TOKEN_SECRET_OCID": "ocid1.secret.oc1..example"},
            clear=True,
        ):
            with patch("ocir_image_update.fetch_vault_secret", return_value="vault-token") as mocked_fetch:
                self.assertEqual(resolve_config_value("GITHUB_TOKEN"), "vault-token")
                mocked_fetch.assert_called_once_with("ocid1.secret.oc1..example")

    def test_private_key_secret_is_normalized_for_pem(self):
        with patch.dict(
            "os.environ",
            {"GITHUB_APP_PRIVATE_KEY_SECRET_OCID": "ocid1.secret.oc1..pem"},
            clear=True,
        ):
            with patch(
                "ocir_image_update.fetch_vault_secret",
                return_value="-----BEGIN KEY-----\\nabc\\n-----END KEY-----\\n",
            ):
                self.assertEqual(
                    load_github_app_private_key(),
                    "-----BEGIN KEY-----\nabc\n-----END KEY-----",
                )

    def test_private_key_uses_default_secret_ocid(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "ocir_image_update.fetch_vault_secret",
                return_value="-----BEGIN KEY-----\\nabc\\n-----END KEY-----\\n",
            ) as mocked_fetch:
                self.assertEqual(
                    load_github_app_private_key(),
                    "-----BEGIN KEY-----\nabc\n-----END KEY-----",
                )
                mocked_fetch.assert_called_once_with(
                    "ocid1.vaultsecret.oc1.ap-chuncheon-1.amaaaaaat2m5lbqa2sn77mucconq5hgglwa7gflf6fx5rbt5lh3jbnrqavtq"
                )

    def test_github_repo_defaults_to_waffle_world_oci(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "direct-token"}, clear=True):
            config = load_github_config()
            self.assertEqual(config.owner, "wafflestudio")
            self.assertEqual(config.repo, "waffle-world-oci")
            self.assertEqual(config.branch, "main")


if __name__ == "__main__":
    unittest.main()
