package main

import (
	"testing"
	"time"
)

func TestParseOCIRPushEvent(t *testing.T) {
	event, err := parseOCIRPushEvent(OCIEvent{
		EventType: ocirPushEventType,
		Data: OCIEventData{
			CompartmentID: "ocid1.compartment.oc1..example",
			ResourceName:  "team/api:1.2.3",
			AdditionalDetails: OCIEventAdditionalDetail{
				Path:   "namespace/team/api",
				Digest: "sha256:deadbeef",
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if event.Tag != "1.2.3" || event.RepositoryPath != "namespace/team/api" || event.Digest != "sha256:deadbeef" {
		t.Fatalf("unexpected parsed event: %#v", event)
	}
}

func TestReplaceImageTagInText(t *testing.T) {
	original := "containers:\n  - image: yny.ocir.io/namespace/dev/api:old\n  - image: yny.ocir.io/namespace/dev/worker:stay\n"
	updated, replacements := replaceImageTagInText(original, "yny.ocir.io/namespace/dev/api", "20260309-1")
	if replacements != 1 {
		t.Fatalf("expected one replacement, got %d", replacements)
	}
	expected := "containers:\n  - image: yny.ocir.io/namespace/dev/api:20260309-1\n  - image: yny.ocir.io/namespace/dev/worker:stay\n"
	if updated != expected {
		t.Fatalf("unexpected content:\n%s", updated)
	}
}

func TestSelectImagesToDeleteKeepsNewestAndProtected(t *testing.T) {
	base := time.Date(2026, 3, 9, 4, 0, 0, 0, time.UTC)
	images := make([]RepositoryImage, 0, 4)
	for index := 0; index < 4; index++ {
		created := base.Add(-time.Duration(index) * 24 * time.Hour)
		images = append(images, RepositoryImage{
			ImageID:     string(rune('0' + index)),
			Digest:      "sha256:" + string(rune('0'+index)),
			TimeCreated: &created,
		})
	}
	deleted := selectImagesToDelete(images, 2, map[string]struct{}{"sha256:3": {}})
	if len(deleted) != 1 || deleted[0].ImageID != "2" {
		t.Fatalf("unexpected deletion set: %#v", deleted)
	}
}
