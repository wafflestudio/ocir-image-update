package main

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/bradleyfalzon/ghinstallation/v2"
	"github.com/google/go-github/v75/github"
)

const githubTimeout = 10 * time.Second

type GitHubClient struct {
	api                   *github.Client
	owner, repo, branch   string
	commitMessageTemplate string
}

func newGitHubClient(ctx context.Context) (*GitHubClient, error) {
	owner := envOrDefault("GITHUB_OWNER", defaultGitHubOwner)
	repo := envOrDefault("GITHUB_REPO", defaultGitHubRepo)
	apiURL := strings.TrimRight(envOrDefault("GITHUB_API_URL", "https://api.github.com"), "/")
	key, err := loadGitHubAppPrivateKey(ctx)
	if err != nil {
		return nil, err
	}

	appsTransport, err := ghinstallation.NewAppsTransport(http.DefaultTransport, defaultGitHubAppID, []byte(key))
	if err != nil {
		return nil, fmt.Errorf("invalid GitHub App private key: %w", err)
	}
	appsTransport.BaseURL = apiURL
	appsClient, err := githubAPIClient(&http.Client{Transport: appsTransport, Timeout: githubTimeout}, apiURL)
	if err != nil {
		return nil, err
	}
	installation, _, err := appsClient.Apps.FindRepositoryInstallation(ctx, owner, repo)
	if err != nil {
		return nil, fmt.Errorf("resolve GitHub App installation for %s/%s: %w", owner, repo, err)
	}

	transport := ghinstallation.NewFromAppsTransport(appsTransport, installation.GetID())
	transport.BaseURL = apiURL
	transport.Client = &http.Client{Transport: http.DefaultTransport, Timeout: githubTimeout}
	transport.InstallationTokenOptions = &github.InstallationTokenOptions{
		Repositories: []string{repo},
		Permissions:  &github.InstallationPermissions{Contents: github.Ptr("write")},
	}
	if _, err := transport.Token(ctx); err != nil {
		return nil, fmt.Errorf("create GitHub App installation token: %w", err)
	}
	api, err := githubAPIClient(&http.Client{Transport: transport, Timeout: githubTimeout}, apiURL)
	if err != nil {
		return nil, err
	}

	client := &GitHubClient{
		api: api, owner: owner, repo: repo,
		branch:                envOrDefault("GITHUB_BRANCH", defaultGitHubBranch),
		commitMessageTemplate: envOrDefault("GITHUB_COMMIT_MESSAGE", defaultGitHubCommitMessage),
	}
	return client, nil
}

func githubAPIClient(httpClient *http.Client, apiURL string) (*github.Client, error) {
	client := github.NewClient(httpClient)
	client.UserAgent = "ocir-image-update-function"
	if apiURL == "https://api.github.com" {
		return client, nil
	}
	return client.WithEnterpriseURLs(apiURL+"/", apiURL+"/")
}

func loadGitHubAppPrivateKey(ctx context.Context) (string, error) {
	key, err := resolveConfigValue(ctx, "GITHUB_APP_PRIVATE_KEY")
	if err != nil {
		return "", err
	}
	if key != "" {
		return strings.TrimSpace(strings.ReplaceAll(key, `\n`, "\n")), nil
	}
	if encoded := os.Getenv("GITHUB_APP_PRIVATE_KEY_BASE64"); encoded != "" {
		decoded, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			return "", fmt.Errorf("GITHUB_APP_PRIVATE_KEY_BASE64 is not valid base64: %w", err)
		}
		return strings.TrimSpace(string(decoded)), nil
	}
	return "", errors.New("missing GitHub App private key")
}

func (c *GitHubClient) findCandidateYAMLFiles(ctx context.Context, root, image string) ([]string, error) {
	query := fmt.Sprintf(`"%s:" repo:%s/%s path:%s`, image, c.owner, c.repo, root)
	paths, available, err := c.searchCode(ctx, query)
	if err != nil {
		return nil, err
	}
	if available && len(paths) > 0 {
		return paths, nil
	}
	return c.listYAMLFiles(ctx, root)
}

func (c *GitHubClient) searchCode(ctx context.Context, query string) ([]string, bool, error) {
	search := func() (*github.CodeSearchResult, error) {
		result, _, err := c.api.Search.Code(ctx, query, &github.SearchOptions{ListOptions: github.ListOptions{PerPage: 100}})
		return result, err
	}
	result, err := search()
	status := githubErrorStatus(err)
	if status >= 500 {
		time.Sleep(200 * time.Millisecond)
		result, err = search()
		status = githubErrorStatus(err)
	}
	if status == 403 || status == 422 || status == 429 || status >= 500 {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("search GitHub manifests: %w", err)
	}
	paths := make([]string, 0, len(result.CodeResults))
	for _, item := range result.CodeResults {
		if isYAML(item.GetPath()) {
			paths = append(paths, item.GetPath())
		}
	}
	sort.Strings(paths)
	return paths, true, nil
}

func (c *GitHubClient) listYAMLFiles(ctx context.Context, root string) ([]string, error) {
	tree, _, err := c.api.Git.GetTree(ctx, c.owner, c.repo, c.branch, true)
	if err != nil {
		return nil, fmt.Errorf("list GitHub repository tree: %w", err)
	}
	if tree.GetTruncated() {
		return nil, errors.New("GitHub repository tree was truncated")
	}
	prefix := root + "/"
	paths := make([]string, 0)
	for _, item := range tree.Entries {
		path := item.GetPath()
		if item.GetType() == "blob" && (path == root || strings.HasPrefix(path, prefix)) && isYAML(path) {
			paths = append(paths, path)
		}
	}
	sort.Strings(paths)
	return paths, nil
}

func (c *GitHubClient) getFile(ctx context.Context, path string) (string, string, error) {
	file, _, _, err := c.api.Repositories.GetContents(ctx, c.owner, c.repo, path, &github.RepositoryContentGetOptions{Ref: c.branch})
	if err != nil {
		return "", "", fmt.Errorf("fetch %s: %w", path, err)
	}
	if file == nil {
		return "", "", fmt.Errorf("%s is not a file", path)
	}
	content, err := file.GetContent()
	if err != nil {
		return "", "", fmt.Errorf("decode %s: %w", path, err)
	}
	return content, file.GetSHA(), nil
}

func (c *GitHubClient) updateFile(ctx context.Context, path, sha, content, message string) (string, error) {
	result, _, err := c.api.Repositories.UpdateFile(ctx, c.owner, c.repo, path, &github.RepositoryContentFileOptions{
		Message: github.Ptr(message), Content: []byte(content),
		SHA: github.Ptr(sha), Branch: github.Ptr(c.branch),
	})
	if err != nil {
		return "", fmt.Errorf("update %s: %w", path, err)
	}
	return result.GetSHA(), nil
}

func githubErrorStatus(err error) int {
	var standard *github.ErrorResponse
	if errors.As(err, &standard) && standard.Response != nil {
		return standard.Response.StatusCode
	}
	var rate *github.RateLimitError
	if errors.As(err, &rate) && rate.Response != nil {
		return rate.Response.StatusCode
	}
	var abuse *github.AbuseRateLimitError
	if errors.As(err, &abuse) && abuse.Response != nil {
		return abuse.Response.StatusCode
	}
	return 0
}

func isYAML(path string) bool {
	return strings.HasSuffix(path, ".yaml") || strings.HasSuffix(path, ".yml")
}
