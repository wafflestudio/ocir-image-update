package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/oracle/oci-go-sdk/v65/artifacts"
	"github.com/oracle/oci-go-sdk/v65/common"
	"github.com/oracle/oci-go-sdk/v65/common/auth"
	"github.com/oracle/oci-go-sdk/v65/secrets"
)

type RepositoryImage struct {
	ImageID, Digest string
	DisplayNames    []string
	TimeCreated     *time.Time
	Versions        []string
}

type DeletedImage struct {
	Digest       string   `json:"digest,omitempty"`
	DisplayNames []string `json:"display_names"`
	ID           string   `json:"id"`
	TimeCreated  *string  `json:"time_created"`
	Versions     []string `json:"versions"`
}

type CleanupResult struct {
	DeletedImages *[]DeletedImage `json:"deleted_images,omitempty"`
	DeletedCount  *int            `json:"deleted_count,omitempty"`
	RetainCount   int             `json:"retain_count"`
	RepositoryID  string          `json:"repository_id,omitempty"`
	Status        string          `json:"status"`
	Reason        string          `json:"reason,omitempty"`
}

var secretCache sync.Map

func cleanupRetainCount() (int, error) {
	raw := strings.TrimSpace(envOrDefault("OCIR_CLEANUP_RETAIN_COUNT", strconv.Itoa(defaultOCIRCleanupRetainCount)))
	if raw == "" {
		return 0, nil
	}
	count, err := strconv.Atoi(raw)
	if err != nil || count < 0 {
		return 0, fmt.Errorf("OCIR_CLEANUP_RETAIN_COUNT must be zero or a positive integer")
	}
	return count, nil
}

func cleanupPushedRepositoryImages(ctx context.Context, event ImagePushEvent, repositoryName string) (*CleanupResult, error) {
	retain, err := cleanupRetainCount()
	if err != nil {
		return nil, err
	}
	finish := func(result *CleanupResult, imageCount int) *CleanupResult {
		values := fields{"ocir_repository": repositoryName, "retain_count": retain, "unique_image_count": imageCount}
		if result == nil {
			values["status"] = "disabled"
		} else {
			values["status"], values["reason"] = result.Status, result.Reason
			if result.DeletedCount != nil {
				values["deleted_count"] = *result.DeletedCount
			}
		}
		emitLog("ocir.cleanup_complete", values)
		return result
	}
	if retain == 0 {
		return finish(nil, 0), nil
	}
	skipped := func(reason string) *CleanupResult {
		return finish(&CleanupResult{Status: "skipped", Reason: reason, RetainCount: retain}, 0)
	}
	if event.CompartmentID == "" {
		return skipped("OCI event payload does not contain data.compartmentId"), nil
	}
	if event.Digest == "" {
		return skipped("OCI event payload does not contain data.additionalDetails.digest"), nil
	}

	client, err := newArtifactsClient()
	if err != nil {
		return nil, err
	}
	images, repositoryID, err := listRepositoryImages(ctx, client, event.CompartmentID, repositoryName, event.RepositoryPath)
	if err != nil {
		return nil, err
	}
	noop := func() *CleanupResult {
		empty := []DeletedImage{}
		return finish(&CleanupResult{DeletedImages: &empty, RetainCount: retain, RepositoryID: repositoryID, Status: "noop"}, len(images))
	}
	if len(images) == 0 {
		return noop(), nil
	}

	toDelete := selectImagesToDelete(images, retain, map[string]struct{}{event.Digest: {}})
	if len(toDelete) == 0 {
		return noop(), nil
	}

	deleted := make([]DeletedImage, 0, len(toDelete))
	for _, image := range toDelete {
		_, err := client.DeleteContainerImage(ctx, artifacts.DeleteContainerImageRequest{ImageId: common.String(image.ImageID)})
		if err != nil {
			return nil, fmt.Errorf("delete OCI image %s: %w", image.ImageID, err)
		}
		deleted = append(deleted, repositoryImageSummary(image))
	}
	count := len(deleted)
	return finish(&CleanupResult{
		DeletedImages: &deleted, DeletedCount: &count, RetainCount: retain,
		RepositoryID: repositoryID, Status: "deleted",
	}, len(images)), nil
}

func newArtifactsClient() (artifacts.ArtifactsClient, error) {
	provider, err := auth.ResourcePrincipalConfigurationProvider()
	if err != nil {
		return artifacts.ArtifactsClient{}, fmt.Errorf("create OCI resource principal: %w", err)
	}
	client, err := artifacts.NewArtifactsClientWithConfigurationProvider(provider)
	if err != nil {
		return artifacts.ArtifactsClient{}, fmt.Errorf("create OCI Artifacts client: %w", err)
	}
	return client, nil
}

func listRepositoryImages(ctx context.Context, client artifacts.ArtifactsClient, compartmentID string, names ...string) ([]RepositoryImage, string, error) {
	seen := map[string]bool{}
	for _, name := range names {
		name = strings.Trim(name, "/")
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		var raw []artifacts.ContainerImageSummary
		var page *string
		for {
			response, err := client.ListContainerImages(ctx, artifacts.ListContainerImagesRequest{
				CompartmentId: common.String(compartmentID), RepositoryName: common.String(name),
				Limit: common.Int(1000), Page: page,
			})
			if err != nil {
				return nil, "", fmt.Errorf("list OCI images: %w", err)
			}
			raw = append(raw, response.Items...)
			page = response.OpcNextPage
			if str(page) == "" {
				break
			}
		}
		if len(raw) > 0 {
			return summarizeRepositoryImages(raw), str(raw[0].RepositoryId), nil
		}
	}
	return []RepositoryImage{}, "", nil
}

func summarizeRepositoryImages(raw []artifacts.ContainerImageSummary) []RepositoryImage {
	type group struct {
		RepositoryImage
		names, versions map[string]struct{}
	}
	groups, order := map[string]*group{}, []string{}
	for _, item := range raw {
		id := str(item.Id)
		if id == "" {
			continue
		}
		entry := groups[id]
		if entry == nil {
			entry = &group{
				RepositoryImage: RepositoryImage{ImageID: id, Digest: str(item.Digest)},
				names:           map[string]struct{}{}, versions: map[string]struct{}{},
			}
			groups[id], order = entry, append(order, id)
		}
		if value := str(item.DisplayName); value != "" {
			entry.names[value] = struct{}{}
		}
		if value := str(item.Version); value != "" {
			entry.versions[value] = struct{}{}
		}
		if item.TimeCreated != nil && (entry.TimeCreated == nil || item.TimeCreated.After(*entry.TimeCreated)) {
			created := item.TimeCreated.Time
			entry.TimeCreated = &created
		}
	}
	images := make([]RepositoryImage, 0, len(order))
	for _, id := range order {
		entry := groups[id]
		entry.DisplayNames, entry.Versions = sortedKeys(entry.names), sortedKeys(entry.versions)
		images = append(images, entry.RepositoryImage)
	}
	sort.SliceStable(images, func(i, j int) bool {
		if images[i].TimeCreated == nil {
			return false
		}
		return images[j].TimeCreated == nil || images[i].TimeCreated.After(*images[j].TimeCreated)
	})
	return images
}

func selectImagesToDelete(images []RepositoryImage, retain int, protected map[string]struct{}) []RepositoryImage {
	keep := map[string]bool{}
	for i := 0; i < retain && i < len(images); i++ {
		keep[images[i].ImageID] = true
	}
	for _, image := range images {
		_, isProtected := protected[image.Digest]
		keep[image.ImageID] = keep[image.ImageID] || isProtected
	}
	var result []RepositoryImage
	for _, image := range images {
		if !keep[image.ImageID] {
			result = append(result, image)
		}
	}
	return result
}

func repositoryImageSummary(image RepositoryImage) DeletedImage {
	var created *string
	if image.TimeCreated != nil {
		value := image.TimeCreated.Format("2006-01-02T15:04:05.999999999-07:00")
		created = &value
	}
	return DeletedImage{
		Digest: image.Digest, DisplayNames: image.DisplayNames, ID: image.ImageID,
		TimeCreated: created, Versions: image.Versions,
	}
}

func resolveConfigValue(ctx context.Context, name string) (string, error) {
	if value := os.Getenv(name); value != "" {
		return value, nil
	}
	ocid := os.Getenv(name + "_SECRET_OCID")
	if ocid == "" && name == "GITHUB_APP_PRIVATE_KEY" {
		ocid = defaultGitHubPrivateKeySecretOCID
	}
	if ocid = strings.TrimSpace(ocid); ocid == "" {
		return "", nil
	}
	return fetchVaultSecret(ctx, ocid)
}

func fetchVaultSecret(ctx context.Context, ocid string) (string, error) {
	if value, ok := secretCache.Load(ocid); ok {
		return value.(string), nil
	}
	provider, err := auth.ResourcePrincipalConfigurationProvider()
	if err != nil {
		return "", fmt.Errorf("create OCI resource principal: %w", err)
	}
	client, err := secrets.NewSecretsClientWithConfigurationProvider(provider)
	if err != nil {
		return "", fmt.Errorf("create OCI Secrets client: %w", err)
	}
	response, err := client.GetSecretBundle(ctx, secrets.GetSecretBundleRequest{SecretId: common.String(ocid)})
	if err != nil {
		return "", fmt.Errorf("read Vault secret %s: %w", ocid, err)
	}
	var encoded string
	switch content := response.SecretBundleContent.(type) {
	case secrets.Base64SecretBundleContentDetails:
		encoded = str(content.Content)
	case *secrets.Base64SecretBundleContentDetails:
		encoded = str(content.Content)
	default:
		return "", fmt.Errorf("Vault secret %s is not base64 text", ocid)
	}
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", fmt.Errorf("decode Vault secret %s: %w", ocid, err)
	}
	value := strings.TrimSpace(string(decoded))
	secretCache.Store(ocid, value)
	return value, nil
}

func sortedKeys(values map[string]struct{}) []string {
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func str(value *string) string {
	if value != nil {
		return *value
	}
	return ""
}
