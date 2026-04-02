import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from ocir_image_update import (
    build_image_repository,
    CleanupSettings,
    GitHubConfig,
    GitHubContentsClient,
    load_github_app_private_key,
    load_cleanup_settings,
    load_github_config,
    load_update_target,
    parse_ocir_push_event,
    RepositoryImage,
    replace_image_tag_in_text,
    resolve_config_value,
    select_images_to_delete,
    strip_ocir_namespace,
    summarize_event_payload,
    summarize_repository_images,
)


class ParseEventTests(unittest.TestCase):
    def test_parses_ocir_push_event(self):
        event = parse_ocir_push_event(
            {
                "eventType": "com.oraclecloud.artifacts.uploaddockerimage",
                "eventTime": "2026-03-09T00:00:00Z",
                "data": {
                    "compartmentId": "ocid1.compartment.oc1..example",
                    "resourceName": "team/api:1.2.3",
                    "additionalDetails": {
                        "path": "namespace/team/api",
                        "digest": "sha256:deadbeef",
                    },
                },
            }
        )

        self.assertEqual(event.compartment_id, "ocid1.compartment.oc1..example")
        self.assertEqual(event.repository_path, "namespace/team/api")
        self.assertEqual(event.resource_name, "team/api:1.2.3")
        self.assertEqual(event.tag, "1.2.3")
        self.assertEqual(event.digest, "sha256:deadbeef")

    def test_summarizes_event_payload_for_logging(self):
        summary = summarize_event_payload(
            {
                "eventType": "com.oraclecloud.artifacts.uploaddockerimage",
                "eventTime": "2026-03-09T00:00:00Z",
                "eventID": "event-123",
                "source": "OCIRegistry",
                "data": {
                    "resourceName": "team/api:1.2.3",
                    "additionalDetails": {"path": "namespace/team/api"},
                },
            }
        )

        self.assertEqual(summary["event_id"], "event-123")
        self.assertEqual(summary["event_time"], "2026-03-09T00:00:00Z")
        self.assertEqual(summary["event_type"], "com.oraclecloud.artifacts.uploaddockerimage")
        self.assertEqual(summary["repository_path"], "namespace/team/api")
        self.assertEqual(summary["resource_name"], "team/api:1.2.3")
        self.assertEqual(summary["source"], "OCIRegistry")


class DirectoryScanTests(unittest.TestCase):
    def test_strips_ocir_namespace(self):
        self.assertEqual(strip_ocir_namespace("namespace/dev/api", "namespace"), "dev/api")
        self.assertEqual(strip_ocir_namespace("dev/api", "namespace"), "dev/api")

    def test_builds_image_repository(self):
        self.assertEqual(
            build_image_repository("yny.ocir.io", "namespace/dev/api", "namespace"),
            "yny.ocir.io/namespace/dev/api",
        )
        self.assertEqual(
            build_image_repository("yny.ocir.io", "dev/api", "namespace"),
            "yny.ocir.io/namespace/dev/api",
        )

    def test_loads_update_target_from_built_in_defaults(self):
        event = parse_ocir_push_event(
            {
                "eventType": "com.oraclecloud.artifacts.uploaddockerimage",
                "data": {
                    "compartmentId": "ocid1.compartment.oc1..example",
                    "resourceName": "dev/api:2026-03-09",
                    "additionalDetails": {"path": "ax1dvc8vmenm/dev/api"},
                },
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            target = load_update_target(event)

        self.assertEqual(target.ocir_repository, "dev/api")
        self.assertEqual(target.image_repository, "yny.ocir.io/ax1dvc8vmenm/dev/api")
        self.assertEqual(target.manifest_root, "argocd")

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

    def test_finds_candidate_yaml_files_via_code_search(self):
        client = GitHubContentsClient(
            GitHubConfig(
                token="token",
                owner="wafflestudio",
                repo="waffle-world-oci",
                api_url="https://api.github.com",
                branch="main",
                commit_message_template="msg",
                timeout_seconds=10,
            )
        )

        with patch.object(
            client,
            "_search_code_paths",
            return_value=["argocd/snutt-prod/snutt-ev.yaml"],
        ) as mocked_search:
            candidates = client.find_candidate_yaml_files("argocd", "yny.ocir.io/ax1dvc8vmenm/snutt-prod/snutt-ev")

        self.assertEqual(candidates, ["argocd/snutt-prod/snutt-ev.yaml"])
        mocked_search.assert_called_once()

    def test_finds_candidate_yaml_files_via_git_tree_fallback(self):
        client = GitHubContentsClient(
            GitHubConfig(
                token="token",
                owner="wafflestudio",
                repo="waffle-world-oci",
                api_url="https://api.github.com",
                branch="main",
                commit_message_template="msg",
                timeout_seconds=10,
            )
        )

        with patch.object(client, "_search_code_paths", return_value=None):
            with patch.object(
                client,
                "list_yaml_files_recursive",
                return_value=["argocd/snutt-prod/snutt-ev.yaml", "argocd/snutt-dev/snutt-ev.yaml"],
            ) as mocked_list:
                candidates = client.find_candidate_yaml_files(
                    "argocd", "yny.ocir.io/ax1dvc8vmenm/snutt-prod/snutt-ev"
                )

        self.assertEqual(
            candidates,
            ["argocd/snutt-prod/snutt-ev.yaml", "argocd/snutt-dev/snutt-ev.yaml"],
        )
        mocked_list.assert_called_once_with("argocd")

    def test_falls_back_to_git_tree_when_code_search_returns_empty_results(self):
        client = GitHubContentsClient(
            GitHubConfig(
                token="token",
                owner="wafflestudio",
                repo="waffle-world-oci",
                api_url="https://api.github.com",
                branch="main",
                commit_message_template="msg",
                timeout_seconds=10,
            )
        )

        with patch.object(client, "_search_code_paths", return_value=[]):
            with patch.object(
                client,
                "list_yaml_files_recursive",
                return_value=["argocd/snutt-dev/snutt-ev-batch.yaml"],
            ) as mocked_list:
                candidates = client.find_candidate_yaml_files(
                    "argocd", "yny.ocir.io/ax1dvc8vmenm/snutt-dev/snutt-ev-batch"
                )

        self.assertEqual(candidates, ["argocd/snutt-dev/snutt-ev-batch.yaml"])
        mocked_list.assert_called_once_with("argocd")

    def test_code_search_retries_once_after_github_server_error(self):
        client = GitHubContentsClient(
            GitHubConfig(
                token="token",
                owner="wafflestudio",
                repo="waffle-world-oci",
                api_url="https://api.github.com",
                branch="main",
                commit_message_template="msg",
                timeout_seconds=10,
            )
        )

        query = '"yny.ocir.io/ax1dvc8vmenm/snutt-dev/snutt-ev:" repo:wafflestudio/waffle-world-oci path:argocd'
        with patch.object(
            client.session,
            "get",
            side_effect=[
                SimpleNamespace(
                    status_code=500,
                    json=lambda: {"message": "internal server error"},
                    text="internal server error",
                ),
                SimpleNamespace(
                    status_code=200,
                    json=lambda: {"items": [{"path": "argocd/snutt-dev/snutt-ev.yaml"}]},
                    text="",
                    raise_for_status=lambda: None,
                ),
            ],
        ):
            with patch("ocir_image_update.time.sleep") as mocked_sleep:
                self.assertEqual(
                    client._search_code_paths(query),
                    ["argocd/snutt-dev/snutt-ev.yaml"],
                )

        mocked_sleep.assert_called_once()

    def test_code_search_returns_none_after_retrying_github_server_error(self):
        client = GitHubContentsClient(
            GitHubConfig(
                token="token",
                owner="wafflestudio",
                repo="waffle-world-oci",
                api_url="https://api.github.com",
                branch="main",
                commit_message_template="msg",
                timeout_seconds=10,
            )
        )

        query = '"yny.ocir.io/ax1dvc8vmenm/snutt-dev/snutt-ev:" repo:wafflestudio/waffle-world-oci path:argocd'
        with patch.object(
            client.session,
            "get",
            side_effect=[
                SimpleNamespace(
                    status_code=500,
                    json=lambda: {"message": "internal server error"},
                    text="internal server error",
                ),
                SimpleNamespace(
                    status_code=503,
                    json=lambda: {"message": "service unavailable"},
                    text="service unavailable",
                ),
            ],
        ):
            with patch("ocir_image_update.time.sleep") as mocked_sleep:
                self.assertIsNone(client._search_code_paths(query))

        mocked_sleep.assert_called_once()

class SecretResolutionTests(unittest.TestCase):
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
        with patch.dict("os.environ", {}, clear=True):
            with patch("ocir_image_update.generate_github_app_installation_token", return_value="installation-token"):
                config = load_github_config()
                self.assertEqual(config.owner, "wafflestudio")
                self.assertEqual(config.repo, "waffle-world-oci")
                self.assertEqual(config.branch, "main")
                self.assertEqual(config.token, "installation-token")

    def test_resolve_config_value_prefers_plain_env(self):
        with patch.dict(
            "os.environ",
            {"SAMPLE_VALUE": "direct-value", "SAMPLE_VALUE_SECRET_OCID": "ocid1.secret.oc1..example"},
            clear=True,
        ):
            with patch("ocir_image_update.fetch_vault_secret") as mocked_fetch:
                self.assertEqual(resolve_config_value("SAMPLE_VALUE"), "direct-value")
                mocked_fetch.assert_not_called()

    def test_resolve_config_value_reads_vault_when_plain_env_missing(self):
        with patch.dict(
            "os.environ",
            {"SAMPLE_VALUE_SECRET_OCID": "ocid1.secret.oc1..example"},
            clear=True,
        ):
            with patch("ocir_image_update.fetch_vault_secret", return_value="vault-value") as mocked_fetch:
                self.assertEqual(resolve_config_value("SAMPLE_VALUE"), "vault-value")
                mocked_fetch.assert_called_once_with("ocid1.secret.oc1..example")

    def test_load_github_config_uses_hardcoded_app_id(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("ocir_image_update.load_github_app_private_key", return_value="pem"):
                with patch("ocir_image_update.build_github_app_jwt", return_value="jwt") as mocked_jwt:
                    with patch("ocir_image_update.requests.get") as mocked_get:
                        with patch("ocir_image_update.requests.post") as mocked_post:
                            mocked_get.return_value = SimpleNamespace(
                                json=lambda: {"id": 123},
                                raise_for_status=lambda: None,
                                status_code=200,
                            )
                            mocked_post.return_value = SimpleNamespace(
                                json=lambda: {"token": "installation-token"},
                                raise_for_status=lambda: None,
                                status_code=201,
                            )
                            config = load_github_config()
        self.assertEqual(config.token, "installation-token")
        mocked_jwt.assert_called_once_with("2842871", "pem")


class CleanupTests(unittest.TestCase):
    def test_cleanup_settings_default_to_three(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_cleanup_settings(), CleanupSettings(retain_count=3))

    def test_cleanup_settings_zero_disables_cleanup(self):
        with patch.dict("os.environ", {"OCIR_CLEANUP_RETAIN_COUNT": "0"}, clear=True):
            self.assertIsNone(load_cleanup_settings())

    def test_summarizes_repository_images_by_unique_image_id(self):
        images = summarize_repository_images(
            [
                SimpleNamespace(
                    id="image-1",
                    digest="sha256:new",
                    display_name="repo:latest",
                    time_created=datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc),
                    version="latest",
                ),
                SimpleNamespace(
                    id="image-1",
                    digest="sha256:new",
                    display_name="repo:abcd1234",
                    time_created=datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc),
                    version="abcd1234",
                ),
                SimpleNamespace(
                    id="image-2",
                    digest="sha256:old",
                    display_name="repo:old",
                    time_created=datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc),
                    version="old",
                ),
            ]
        )

        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].image_id, "image-1")
        self.assertEqual(images[0].versions, ("abcd1234", "latest"))
        self.assertEqual(images[0].display_names, ("repo:abcd1234", "repo:latest"))

    def test_selects_all_but_latest_three_unique_images_for_deletion(self):
        images = [
            RepositoryImage(
                image_id=f"image-{index}",
                digest=f"sha256:{index}",
                display_names=(f"repo:{index}",),
                time_created=datetime(2026, 3, 9 - index, 4, 0, tzinfo=timezone.utc),
                versions=(str(index),),
            )
            for index in range(5)
        ]

        to_delete = select_images_to_delete(images, retain_count=3)

        self.assertEqual([image.image_id for image in to_delete], ["image-3", "image-4"])

    def test_protects_current_digest_even_if_older_than_retain_count(self):
        images = [
            RepositoryImage(
                image_id="image-0",
                digest="sha256:0",
                display_names=("repo:0",),
                time_created=datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc),
                versions=("0",),
            ),
            RepositoryImage(
                image_id="image-1",
                digest="sha256:1",
                display_names=("repo:1",),
                time_created=datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc),
                versions=("1",),
            ),
            RepositoryImage(
                image_id="image-2",
                digest="sha256:protected",
                display_names=("repo:2",),
                time_created=datetime(2026, 3, 7, 4, 0, tzinfo=timezone.utc),
                versions=("2",),
            ),
        ]

        to_delete = select_images_to_delete(images, retain_count=2, protected_digests={"sha256:protected"})

        self.assertEqual(to_delete, [])


if __name__ == "__main__":
    unittest.main()
