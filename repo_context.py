import base64

from github import Github, GithubException

import roles

MAX_FILES = 20
MAX_TOTAL_CHARS = 60_000


class RepoAccessDenied(RuntimeError):
    """The signed-in user's role may not import from the requested repo."""


def fetch_terraform_files_as(email: str, github_token: str, repo_name: str, tf_path: str = "terraform") -> dict[str, str]:
    """`fetch_terraform_files` on behalf of a signed-in user.

    The fetch runs with the server's GitHub token, which can read repos the
    user has no business seeing. So the user's role is checked first
    (`roles.can_read_repo`): editors and admins may import from any repo,
    contributors only from repos owned by their mapped GitHub user, viewers
    from none. Raises RepoAccessDenied before any GitHub call otherwise.
    """
    repo_name = (repo_name or "").strip()
    if not roles.can_read_repo(email, repo_name):
        raise RepoAccessDenied(
            f"Role '{roles.get_role(email)}' cannot import from '{repo_name}'. "
            "Contributors can import only from repos owned by them; ask an editor or admin."
        )
    return fetch_terraform_files(github_token, repo_name, tf_path)


def fetch_terraform_files(github_token: str, repo_name: str, tf_path: str = "terraform") -> dict[str, str]:
    """
    Fetch all .tf files from tf_path in the repo.
    Returns {relative_path: file_content}.
    Raises RuntimeError on access/not-found errors.
    """
    g = Github(github_token)
    try:
        repo = g.get_repo(repo_name)
    except GithubException as e:
        if e.status == 404:
            raise RuntimeError(f"Repository '{repo_name}' not found or not accessible.")
        raise RuntimeError(f"GitHub error: {e.data.get('message', str(e))}")

    path = tf_path.strip("/")

    try:
        contents = repo.get_contents(path or ".")
    except GithubException as e:
        if e.status == 404:
            raise RuntimeError(f"Path '{tf_path or '/'}' not found in '{repo_name}'.")
        raise RuntimeError(f"GitHub error fetching path: {e.data.get('message', str(e))}")

    items = contents if isinstance(contents, list) else [contents]

    files: dict[str, str] = {}
    total_chars = 0

    for item in items:
        if len(files) >= MAX_FILES:
            break
        if item.type != "file" or not item.name.endswith(".tf"):
            continue
        try:
            content = base64.b64decode(item.content).decode("utf-8")
        except Exception:
            continue
        if total_chars + len(content) > MAX_TOTAL_CHARS:
            continue
        files[item.path] = content
        total_chars += len(content)

    return files


def format_repo_context_for_prompt(files: dict[str, str]) -> str:
    if not files:
        return ""

    lines = [
        "## Existing Terraform repository context",
        "",
        "The following .tf files already exist in the connected repository.",
        "When generating new HCL, you MUST follow these rules:",
        "- Do NOT re-declare the terraform{} block, provider blocks, or any variable that already appears below",
        "- Match the naming conventions and style of the existing resources (snake_case patterns, prefixes, etc.)",
        "- If a variable is already declared (e.g. okta_api_token, aws_region), reference it — do not redeclare it",
        "- Use the same Okta provider version constraint that is already pinned in the existing files",
        "- The generated HCL will sit alongside these files in the same directory — avoid resource name collisions",
        "- If a resource of the same type and name already exists, choose a distinct name and note the addition",
        "",
    ]

    for path, content in files.items():
        lines.append(f"### {path}")
        lines.append("```hcl")
        lines.append(content)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
