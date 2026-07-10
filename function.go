package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"regexp"
	"strings"
)

const (
	ocirPushEventType                 = "com.oraclecloud.artifacts.uploaddockerimage"
	defaultGitHubOwner                = "wafflestudio"
	defaultGitHubRepo                 = "waffle-world-oci"
	defaultGitHubBranch               = "main"
	defaultGitHubAppID                = 2842871
	defaultGitHubCommitMessage        = "build: update {repository_path} to {tag}"
	defaultManifestScanRoot           = "argocd"
	defaultOCIRRegistry               = "yny.ocir.io"
	defaultOCIRNamespace              = "ax1dvc8vmenm"
	defaultOCIRCleanupRetainCount     = 3
	defaultGitHubPrivateKeySecretOCID = "ocid1.vaultsecret.oc1.ap-chuncheon-1.amaaaaaat2m5lbqa2sn77mucconq5hgglwa7gflf6fx5rbt5lh3jbnrqavtq"
)

type fields = map[string]any

type OCIEvent struct {
	EventID    string       `json:"eventID"`
	EventIDAlt string       `json:"eventId"`
	ID         string       `json:"id"`
	EventTime  string       `json:"eventTime"`
	EventType  string       `json:"eventType"`
	Source     string       `json:"source"`
	Data       OCIEventData `json:"data"`
}

type OCIEventData struct {
	CompartmentID     string                   `json:"compartmentId"`
	ResourceName      string                   `json:"resourceName"`
	AdditionalDetails OCIEventAdditionalDetail `json:"additionalDetails"`
}

type OCIEventAdditionalDetail struct {
	Path   string `json:"path"`
	Digest string `json:"digest"`
}

type ImagePushEvent struct {
	CompartmentID  string
	RepositoryPath string
	ResourceName   string
	Tag            string
	Digest         string
}

type UpdateTarget struct {
	OCIRRepository  string
	ImageRepository string
	ManifestRoot    string
}

type UpdatedFile struct {
	Path      string `json:"path"`
	CommitSHA string `json:"commit_sha"`
}

type ProcessResult struct {
	Status          string         `json:"status"`
	Branch          string         `json:"branch"`
	ManifestRoot    string         `json:"manifest_root"`
	RepositoryPath  string         `json:"repository_path"`
	OCIRRepository  string         `json:"ocir_repository"`
	ImageRepository string         `json:"image_repository"`
	Tag             string         `json:"tag"`
	UpdatedFiles    []UpdatedFile  `json:"updated_files"`
	Reason          string         `json:"reason,omitempty"`
	ImageCleanup    *CleanupResult `json:"image_cleanup,omitempty"`
}

type unsupportedEventError struct {
	EventType string
}

func (e unsupportedEventError) Error() string {
	return fmt.Sprintf("Unsupported OCI event type: %s", e.EventType)
}

func processEvent(ctx context.Context, payload OCIEvent) (ProcessResult, error) {
	event, err := parseOCIRPushEvent(payload)
	if err != nil {
		return ProcessResult{}, err
	}
	emitLog("event.accepted", fields{
		"digest":          event.Digest,
		"repository_path": event.RepositoryPath,
		"resource_name":   event.ResourceName,
		"tag":             event.Tag,
	})

	client, err := newGitHubClient(ctx)
	if err != nil {
		return ProcessResult{}, err
	}
	target := loadUpdateTarget(event)
	emitLog("manifest.target_resolved", map[string]any{
		"branch":           client.branch,
		"image_repository": target.ImageRepository,
		"manifest_root":    target.ManifestRoot,
		"ocir_repository":  target.OCIRRepository,
	})

	filePaths, err := client.findCandidateYAMLFiles(ctx, target.ManifestRoot, target.ImageRepository)
	if err != nil {
		return ProcessResult{}, err
	}
	emitLog("manifest.files_listed", map[string]any{
		"branch":        client.branch,
		"file_count":    len(filePaths),
		"manifest_root": target.ManifestRoot,
	})

	updatedFiles := make([]UpdatedFile, 0)
	for _, filePath := range filePaths {
		message := formatCommitMessage(client.commitMessageTemplate, event, target, filePath)
		updated, err := updateManifestFile(ctx, client, filePath, target.ImageRepository, event.Tag, message)
		if err != nil {
			return ProcessResult{}, err
		}
		if updated != nil {
			updatedFiles = append(updatedFiles, *updated)
		}
	}

	status := "noop"
	reason := ""
	if len(updatedFiles) > 0 {
		status = "updated"
	} else if len(filePaths) == 0 {
		status = "ignored"
		reason = "No matching manifests found"
	}

	result := ProcessResult{
		Status:          status,
		Branch:          client.branch,
		ManifestRoot:    target.ManifestRoot,
		RepositoryPath:  event.RepositoryPath,
		OCIRRepository:  target.OCIRRepository,
		ImageRepository: target.ImageRepository,
		Tag:             event.Tag,
		UpdatedFiles:    updatedFiles,
		Reason:          reason,
	}

	cleanupResult, err := cleanupPushedRepositoryImages(ctx, event, target.OCIRRepository)
	if err != nil {
		return ProcessResult{}, err
	}
	result.ImageCleanup = cleanupResult

	emitLog("manifest.update_complete", map[string]any{
		"branch":             result.Branch,
		"manifest_root":      result.ManifestRoot,
		"repository_path":    result.RepositoryPath,
		"status":             result.Status,
		"tag":                result.Tag,
		"updated_file_count": len(result.UpdatedFiles),
	})
	return result, nil
}

func updateManifestFile(ctx context.Context, client *GitHubClient, filePath, imageRepository, tag, commitMessage string) (*UpdatedFile, error) {
	currentContent, currentSHA, err := client.getFile(ctx, filePath)
	if err != nil {
		return nil, err
	}
	updatedContent, replacements := replaceImageTagInText(currentContent, imageRepository, tag)
	if replacements == 0 {
		emitLog("manifest.file_skipped", map[string]any{
			"image_repository": imageRepository,
			"path":             filePath,
			"reason":           "image_reference_not_found",
		})
		return nil, nil
	}

	commitSHA, err := client.updateFile(ctx, filePath, currentSHA, updatedContent, commitMessage)
	if err != nil && githubErrorStatus(err) == 409 {
		emitLog("manifest.file_conflict_retry", map[string]any{"path": filePath})
		currentContent, currentSHA, err = client.getFile(ctx, filePath)
		if err != nil {
			return nil, err
		}
		updatedContent, replacements = replaceImageTagInText(currentContent, imageRepository, tag)
		if replacements == 0 {
			emitLog("manifest.file_skipped", map[string]any{
				"image_repository": imageRepository,
				"path":             filePath,
				"reason":           "image_reference_not_found_after_retry",
			})
			return nil, nil
		}
		commitSHA, err = client.updateFile(ctx, filePath, currentSHA, updatedContent, commitMessage)
	}
	if err != nil {
		return nil, err
	}

	emitLog("manifest.file_updated", map[string]any{
		"commit_sha":   commitSHA,
		"path":         filePath,
		"replacements": replacements,
	})
	return &UpdatedFile{Path: filePath, CommitSHA: commitSHA}, nil
}

func parseOCIRPushEvent(payload OCIEvent) (ImagePushEvent, error) {
	if payload.EventType != ocirPushEventType {
		return ImagePushEvent{}, unsupportedEventError{EventType: payload.EventType}
	}
	if payload.Data.AdditionalDetails.Path == "" {
		return ImagePushEvent{}, errors.New("The event payload does not contain data.additionalDetails.path")
	}
	if payload.Data.ResourceName == "" {
		return ImagePushEvent{}, errors.New("The event payload does not contain data.resourceName")
	}
	tag, err := parseTagFromResourceName(payload.Data.ResourceName)
	if err != nil {
		return ImagePushEvent{}, err
	}
	return ImagePushEvent{
		CompartmentID:  payload.Data.CompartmentID,
		RepositoryPath: payload.Data.AdditionalDetails.Path,
		ResourceName:   payload.Data.ResourceName,
		Tag:            tag,
		Digest:         payload.Data.AdditionalDetails.Digest,
	}, nil
}

func parseTagFromResourceName(resourceName string) (string, error) {
	if strings.Contains(resourceName, "@") {
		return "", fmt.Errorf("Expected tag-based resource name but received digest reference: %s", resourceName)
	}
	lastColon := strings.LastIndex(resourceName, ":")
	if lastColon < 0 {
		return "", fmt.Errorf("Could not parse image tag from resource name: %s", resourceName)
	}
	return resourceName[lastColon+1:], nil
}

func loadUpdateTarget(event ImagePushEvent) UpdateTarget {
	manifestRoot := strings.Trim(envOrDefault("MANIFEST_SCAN_ROOT", defaultManifestScanRoot), "/")
	registry := strings.TrimRight(envOrDefault("OCIR_REGISTRY", defaultOCIRRegistry), "/")
	namespace := strings.Trim(envOrDefault("OCIR_NAMESPACE", defaultOCIRNamespace), "/")
	return UpdateTarget{
		OCIRRepository:  stripOCIRNamespace(event.RepositoryPath, namespace),
		ImageRepository: buildImageRepository(registry, event.RepositoryPath, namespace),
		ManifestRoot:    manifestRoot,
	}
}

func stripOCIRNamespace(repositoryPath, namespace string) string {
	normalized := strings.Trim(repositoryPath, "/")
	if namespace == "" {
		return normalized
	}
	return strings.TrimPrefix(normalized, namespace+"/")
}

func buildImageRepository(registry, repositoryPath, namespace string) string {
	normalized := strings.Trim(repositoryPath, "/")
	if namespace != "" && strings.HasPrefix(normalized, namespace+"/") {
		return registry + "/" + normalized
	}
	if namespace != "" {
		return registry + "/" + namespace + "/" + normalized
	}
	return registry + "/" + normalized
}

func replaceImageTagInText(content, imageRepository, newTag string) (string, int) {
	pattern := regexp.MustCompile(`(?m)(^\s*(?:-\s*)?image:\s*["']?` + regexp.QuoteMeta(imageRepository) + `:)([^"'\s#]+)(["']?\s*(?:#.*)?$)`)
	matches := pattern.FindAllStringSubmatchIndex(content, -1)
	if len(matches) == 0 {
		return content, 0
	}

	var result strings.Builder
	result.Grow(len(content) + len(matches)*len(newTag))
	last := 0
	for _, match := range matches {
		result.WriteString(content[last:match[0]])
		result.WriteString(content[match[2]:match[3]])
		result.WriteString(newTag)
		result.WriteString(content[match[6]:match[7]])
		last = match[1]
	}
	result.WriteString(content[last:])
	return result.String(), len(matches)
}

func formatCommitMessage(template string, event ImagePushEvent, target UpdateTarget, manifestPath string) string {
	return strings.NewReplacer(
		"{repository_path}", target.OCIRRepository,
		"{tag}", event.Tag,
		"{digest}", event.Digest,
		"{image_repository}", target.ImageRepository,
		"{manifest_path}", manifestPath,
	).Replace(template)
}

func summarizeEventPayload(payload OCIEvent) fields {
	eventID := payload.EventID
	if eventID == "" {
		eventID = payload.EventIDAlt
	}
	if eventID == "" {
		eventID = payload.ID
	}
	return fields{
		"event_id":        eventID,
		"event_time":      payload.EventTime,
		"event_type":      payload.EventType,
		"repository_path": payload.Data.AdditionalDetails.Path,
		"resource_name":   payload.Data.ResourceName,
		"source":          payload.Source,
	}
}

func emitLog(event string, values fields) {
	record := fields{"event": event}
	for key, value := range values {
		if value == nil {
			continue
		}
		if text, ok := value.(string); ok && text == "" {
			continue
		}
		record[key] = value
	}
	encoded, err := json.Marshal(record)
	if err != nil {
		log.Printf(`{"event":"log.encode_failed","reason":%q}`, err.Error())
		return
	}
	log.Print(string(encoded))
}

func merged(groups ...fields) fields {
	result := fields{}
	for _, group := range groups {
		for key, value := range group {
			result[key] = value
		}
	}
	return result
}

func envOrDefault(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}
