import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from ocir_image_update import (
    CleanupSettings,
    RepositoryImage,
    load_cleanup_settings,
    parse_ocir_push_event,
    process_event,
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


class ProcessEventTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "eventType": "com.oraclecloud.artifacts.uploaddockerimage",
            "data": {
                "compartmentId": "compartment",
                "resourceName": "dev/api:new",
                "additionalDetails": {
                    "path": "ax1dvc8vmenm/dev/api",
                    "digest": "sha256:new",
                },
            },
        }

    def test_push_deletes_old_images_without_github_configuration(self):
        client = SimpleNamespace(delete_container_image=unittest.mock.Mock())
        images = [RepositoryImage(f"image-{i}", f"sha256:{i}", (), None, ()) for i in range(5)]
        with patch.dict("os.environ", {}, clear=True), patch(
            "ocir_image_update.create_artifacts_client", return_value=client
        ), patch(
            "ocir_image_update.find_container_repository", return_value=SimpleNamespace(id="repo")
        ) as find_repo, patch("ocir_image_update.list_repository_images", return_value=images):
            result = process_event(self.payload)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["image_cleanup"]["deleted_count"], 2)
        self.assertEqual(client.delete_container_image.call_args_list,
                         [unittest.mock.call("image-3"), unittest.mock.call("image-4")])
        find_repo.assert_called_once_with(client, "compartment", "ax1dvc8vmenm/dev/api", "dev/api")

    def test_disabled_cleanup_does_not_access_oci(self):
        with patch.dict("os.environ", {"OCIR_CLEANUP_RETAIN_COUNT": "0"}, clear=True), patch(
            "ocir_image_update.create_artifacts_client"
        ) as create_client:
            self.assertEqual(process_event(self.payload)["status"], "disabled")
        create_client.assert_not_called()

    def test_missing_digest_or_compartment_skips_deletion(self):
        for field in ("digest", "compartmentId"):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                source = payload["data"]["additionalDetails"] if field == "digest" else payload["data"]
                del source[field]
                with patch.dict("os.environ", {}, clear=True), patch(
                    "ocir_image_update.create_artifacts_client"
                ) as create_client:
                    self.assertEqual(process_event(payload)["status"], "skipped")
                create_client.assert_not_called()


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
