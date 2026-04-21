import github
from github import Github, InputGitTreeElement, GithubException


def build_commit_message(intent: dict) -> str:
    resource_type = intent.get("resource_type", "resource")
    resource_name = intent.get("resource_name", "unknown")
    return f"feat: generate {resource_type} {resource_name} via TF Tool"


def push_to_github(files: dict[str, str], repo_name: str, token: str, commit_message: str, branch: str = "") -> str:
    g = Github(token)
    try:
        repo = g.get_repo(repo_name)
    except GithubException as e:
        if e.status == 404:
            raise RuntimeError(f"Repository '{repo_name}' not found. Check the repo name and your GitHub token permissions.") from e
        raise

    target_branch = branch or repo.default_branch
    try:
        branch_ref = repo.get_branch(target_branch)
    except GithubException as e:
        if e.status == 404:
            try:
                default = repo.get_branch(repo.default_branch)
                repo.create_git_ref(f"refs/heads/{target_branch}", default.commit.sha)
                branch_ref = repo.get_branch(target_branch)
            except GithubException:
                raise RuntimeError(
                    f"Branch '{target_branch}' not found and could not be created in '{repo_name}'."
                ) from e
        elif e.status == 409:
            raise RuntimeError(
                f"Repository '{repo_name}' appears to be empty — push an initial commit first."
            ) from e
        else:
            raise

    latest_commit_sha = branch_ref.commit.sha
    base_tree = repo.get_git_commit(latest_commit_sha).tree

    tree_elements = [
        InputGitTreeElement(path=path, mode="100644", type="blob", content=content)
        for path, content in files.items()
    ]

    new_tree = repo.create_git_tree(tree_elements, base_tree)
    parent_commit = repo.get_git_commit(latest_commit_sha)
    new_commit = repo.create_git_commit(commit_message, new_tree, [parent_commit])
    repo.get_git_ref(f"heads/{target_branch}").edit(new_commit.sha)

    return new_commit.html_url
